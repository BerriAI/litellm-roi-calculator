"""Azure DevOps (Repos) git provider using the REST API with a Personal Access Token.

Caveat: searchCriteria.minTime filters by creation date, so queries ask for PRs
created up to WINDOW_BUFFER_DAYS before the sync window and filter completed PRs
locally by closedDate. A PR opened earlier than that and merged inside the
window may be missed.
Azure DevOps does not publish per-file line counts; file entries carry status
(change type) only, and the estimator receives file/commit counts instead.
"""

import base64
from datetime import date, timedelta

import httpx

from .config import Settings, email
from .connectors import SourceError, payload, request

API_VERSION = "7.1"
WINDOW_BUFFER_DAYS = 30
CHANGE_STATUSES = {"add": "added", "edit": "modified", "delete": "removed",
    "rename": "renamed", "sourceRename": "renamed", "targetRename": "renamed"}


class AzureDevOps:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        token = base64.b64encode(f":{settings.azure_devops_pat}".encode()).decode()
        self.client = httpx.AsyncClient(base_url=f"https://dev.azure.com/{settings.azure_devops_org}/",
            headers={"Authorization": f"Basic {token}"}, timeout=45, transport=transport, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    def _repo(self, repo: str) -> tuple[str, str]:
        project, _, name = repo.partition("/")
        if not project or not name:
            raise SourceError("Azure DevOps repositories must be project/repo.")
        return project, name

    def _web_url(self, project: str, name: str, number: int) -> str:
        return f"https://dev.azure.com/{self.settings.azure_devops_org}/{project}/_git/{name}/pullrequest/{number}"

    async def repositories(self, project: str = "", page: int = 1) -> dict:
        path = f"{project}/_apis/git/repositories" if project else "_apis/git/repositories"
        params = {"api-version": API_VERSION, "$top": 100, "$skip": (page - 1) * 100}
        response = await request(self.client, "GET", path, params=params)
        data = payload(response)
        if not isinstance(data, dict) or not isinstance(data.get("value"), list):
            raise SourceError("Azure DevOps returned an unexpected repository list.")
        repos = [{"name": f"{(item.get('project') or {}).get('name', '')}/{item.get('name', '')}",
            "visibility": "private", "archived": bool(item.get("isDisabled"))} for item in data["value"]]
        return {"repos": [repo for repo in repos if repo["name"].count("/") == 1 and "" not in repo["name"].split("/")],
            "has_more": bool(response.headers.get("x-ms-continuationtoken"))}

    async def _value_pages(self, path: str, params: dict, limit=10000, top=100):
        skip = 0
        for _ in range(limit):
            data = payload(await request(self.client, "GET", path, params={**params, "$top": top, "$skip": skip}))
            if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                raise SourceError("Azure DevOps returned an unexpected pagination response.")
            yield data["value"]
            if len(data["value"]) < top:
                return
            skip += top
        raise SourceError("Azure DevOps pagination limit was reached; narrow the date range.")

    async def pulls(self, repo: str, start: date, end: date) -> list[dict]:
        project, name = self._repo(repo)
        min_time = (start - timedelta(days=WINDOW_BUFFER_DAYS)).isoformat() + "T00:00:00Z"
        pulls = []
        async for page in self._value_pages(f"{project}/_apis/git/repositories/{name}/pullrequests",
                {"api-version": API_VERSION, "searchCriteria.status": "completed", "searchCriteria.minTime": min_time}):
            for pr in page:
                closed = pr.get("closedDate") or ""
                if closed and start.isoformat() <= closed[:10] <= end.isoformat():
                    pulls.append({"number": pr["pullRequestId"], "title": pr.get("title") or "",
                        "body": pr.get("description") or "", "url": self._web_url(project, name, pr["pullRequestId"]),
                        "login": (pr.get("createdBy") or {}).get("displayName", ""),
                        "merged_at": closed, "updated_at": closed,
                        "head_sha": (pr.get("lastMergeSourceCommit") or {}).get("commitId", "")})
        return pulls

    async def evidence(self, repo: str, pr: dict) -> dict:
        project, name = self._repo(repo)
        number = pr["number"]
        detail = payload(await request(self.client, "GET", f"{project}/_apis/git/repositories/{name}/pullrequests/{number}",
            params={"api-version": API_VERSION}))
        commits, authors = [], []
        async for page in self._value_pages(f"{project}/_apis/git/repositories/{name}/pullrequests/{number}/commits",
                {"api-version": API_VERSION}):
            for item in page:
                commits.append({"sha": item.get("commitId", ""), "message": item.get("comment", "")})
                author = item.get("author") or {}
                authors.append({"login": author.get("name", ""), "email": author.get("email", "")})
        files, missing = await self._changes(project, name, number)
        created_by = detail.get("createdBy") or {}
        login = created_by.get("displayName", "") or "unknown"
        profile_email = email(created_by.get("uniqueName"))
        emails = {profile_email} if profile_email else set()
        for author in authors:
            if author["login"].casefold() == login.casefold() and (address := email(author["email"])):
                emails.add(address)
        merged = detail.get("closedDate") or ""
        return {"repo": repo, "number": number, "title": detail.get("title", ""), "body": detail.get("description") or "",
            "url": self._web_url(project, name, number), "login": login, "emails": sorted(emails),
            "profile_email": profile_email, "merged_at": merged,
            "head_sha": (detail.get("lastMergeSourceCommit") or {}).get("commitId", ""),
            "changed_files": len(files), "files": files, "commits": commits, "commit_count": len(commits),
            "incomplete_metadata": missing}

    async def _changes(self, project: str, name: str, number: int) -> tuple[list, bool]:
        iterations = payload(await request(self.client, "GET",
            f"{project}/_apis/git/repositories/{name}/pullrequests/{number}/iterations", params={"api-version": API_VERSION}))
        values = iterations.get("value") if isinstance(iterations, dict) else None
        if not values:
            return [], False
        latest = max(item.get("id", 0) for item in values)
        files, token = [], None
        for _ in range(5):
            params = {"api-version": API_VERSION, "$top": 1000}
            if token:
                params["continuationToken"] = token
            data = payload(await request(self.client, "GET",
                f"{project}/_apis/git/repositories/{name}/pullrequests/{number}/iterations/{latest}/changes", params=params))
            if not isinstance(data, dict) or not isinstance(data.get("changeEntries"), list):
                raise SourceError("Azure DevOps returned an unexpected file changes response.")
            for change in data["changeEntries"]:
                path = ((change.get("item") or {}).get("path") or "").lstrip("/")
                if not isinstance(path, str) or not path:
                    continue
                files.append({"filename": path, "status": CHANGE_STATUSES.get(change.get("changeType"), "modified")})
            token = data.get("continuationToken")
            if not token:
                return files, False
        return files, True

    async def probe(self, repo: str) -> None:
        project, name = self._repo(repo)
        await request(self.client, "GET", f"{project}/_apis/git/repositories/{name}", params={"api-version": API_VERSION})
        await request(self.client, "GET", f"{project}/_apis/git/repositories/{name}/pullrequests",
            params={"api-version": API_VERSION, "searchCriteria.status": "completed", "$top": 1})
