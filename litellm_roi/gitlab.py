"""GitLab provider using the REST and GraphQL APIs with a personal access token.

Repository listing, the merged-merge-request window scan, and probes use REST;
merge request evidence uses GraphQL for diff statistics and commits.
Per-commit line statistics are omitted: for large merge requests GitLab may
omit or reject statistics, and fetching per-commit line stats would multiply
round trips, so evidence carries the merge request totals instead.
"""

from urllib.parse import quote

import httpx

from .config import Settings, email
from .connectors import SourceError, payload, request

STATS_QUERY = """query($fullPath: ID!, $iid: String!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      diffStatsSummary { additions deletions fileCount }
      diffStats { path additions deletions }
    }
  }
}"""

COMMITS_QUERY = """query($fullPath: ID!, $iid: String!, $cursor: String) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      commitCount
      commits(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { sha message authorEmail authorName author { username } }
      }
    }
  }
}"""


class GitLab:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=f"{settings.gitlab_url.rstrip('/')}/",
            headers={"PRIVATE-TOKEN": settings.gitlab_pat}, timeout=45, transport=transport, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    def _path(self, value: str) -> str:
        return quote(value.strip().strip("/"), safe="")

    async def repositories(self, organization: str = "", page: int = 1) -> dict:
        if organization:
            path, params = f"api/v4/groups/{self._path(organization)}/projects", {"include_subgroups": "true"}
        else:
            path, params = "api/v4/projects", {"membership": "true"}
        response = await request(self.client, "GET", path, params={
            **params, "order_by": "last_activity_at", "sort": "desc", "per_page": 100, "page": page})
        data = payload(response)
        if not isinstance(data, list) or any(not isinstance(repo, dict) or not isinstance(repo.get("path_with_namespace"), str) for repo in data):
            raise SourceError("GitLab returned an unexpected repository list.")
        return {"repos": [{"name": repo["path_with_namespace"], "visibility": repo.get("visibility") or "private",
            "archived": bool(repo.get("archived"))} for repo in data],
            "has_more": bool(response.headers.get("x-next-page", ""))}

    async def pages(self, path: str, params: dict | None = None, limit=10000):
        for page in range(1, limit + 1):
            response = await request(self.client, "GET", path, params={**(params or {}), "per_page": 100, "page": page})
            data = payload(response)
            if not isinstance(data, list):
                raise SourceError("GitLab returned an unexpected pagination response.")
            yield data
            if not response.headers.get("x-next-page", ""):
                return
        raise SourceError("GitLab's pagination limit was reached; narrow the date range.")

    async def pulls(self, repo: str, start, end) -> list[dict]:
        pulls = []
        async for page in self.pages(f"api/v4/projects/{self._path(repo)}/merge_requests",
                {"state": "merged", "order_by": "updated_at", "sort": "desc"}):
            for mr in page:
                merged = mr.get("merged_at") or ""
                if merged and start.isoformat() <= merged[:10] <= end.isoformat():
                    pulls.append({"number": mr["iid"], "title": mr.get("title") or "",
                        "body": mr.get("description") or "", "url": mr.get("web_url", ""),
                        "login": (mr.get("author") or {}).get("username", ""),
                        "merged_at": merged, "updated_at": mr.get("updated_at", ""),
                        "head_sha": mr.get("sha", "")})
            if page and page[-1].get("updated_at", "")[:10] < start.isoformat():
                break
        return pulls

    async def probe(self, repo: str) -> None:
        await request(self.client, "GET", f"api/v4/projects/{self._path(repo)}")
        await request(self.client, "GET", f"api/v4/projects/{self._path(repo)}/merge_requests",
            params={"state": "merged", "per_page": 1})

    async def evidence(self, repo: str, pr: dict) -> dict:
        number = pr["number"]
        detail = payload(await request(self.client, "GET",
            f"api/v4/projects/{self._path(repo)}/merge_requests/{number}"))
        author = detail.get("author") or {}
        login = author.get("username", "") or "deleted-user"
        profile_email = email(author.get("public_email"))
        summary, files, stats_missing = await self._diff_stats(repo, number)
        commits, authors, commit_count = await self._commit_metadata(repo, number)
        emails = {profile_email} if profile_email else set()
        for commit_author in authors:
            if commit_author["login"].casefold() == login.casefold() and (address := email(commit_author["email"])):
                emails.add(address)
        missing = stats_missing
        if summary["fileCount"] != len(files) or (commit_count is not None and commit_count != len(commits)):
            missing = True
        return {"repo": repo, "number": number, "title": detail.get("title", ""),
            "body": detail.get("description") or "", "url": detail.get("web_url", ""), "login": login,
            "emails": sorted(emails), "profile_email": profile_email,
            "merged_at": detail.get("merged_at") or pr.get("merged_at", ""),
            "head_sha": detail.get("sha", pr.get("head_sha", "")),
            "additions": summary["additions"], "deletions": summary["deletions"],
            "changed_files": summary["fileCount"], "files": files,
            "commits": commits, "commit_count": commit_count if commit_count is not None else len(commits),
            "incomplete_metadata": missing}

    async def _diff_stats(self, repo: str, number: int) -> tuple[dict, list, bool]:
        merge_request = await self._query(STATS_QUERY, {"fullPath": repo.strip().strip("/"), "iid": str(number)})
        summary, missing = merge_request.get("diffStatsSummary"), False
        if summary and (summary.get("additions") is None or summary.get("deletions") is None or summary.get("fileCount") is None):
            # GitLab declined the statistics; fall back to the returned files and mark the request incomplete.
            missing = True
            summary = None
        if not summary:
            summary = {"fileCount": 0}
        files = [{"filename": entry.get("path", ""), "additions": entry.get("additions") or 0,
            "deletions": entry.get("deletions") or 0} for entry in (merge_request.get("diffStats") or [])]
        if not summary.get("fileCount"):
            # Totals fall back to the returned files.
            summary = {"additions": sum(entry["additions"] for entry in files),
                "deletions": sum(entry["deletions"] for entry in files), "fileCount": len(files)}
        return {"additions": summary["additions"], "deletions": summary["deletions"],
            "fileCount": summary["fileCount"]}, files, missing

    async def _commit_metadata(self, repo: str, number: int) -> tuple[list, list, int | None]:
        commit_count = None
        cursor = ""
        commits, authors = [], []
        for _ in range(1000):
            merge_request = await self._query(COMMITS_QUERY,
                {"fullPath": repo.strip().strip("/"), "iid": str(number), "cursor": cursor or None})
            if commit_count is None and isinstance(merge_request.get("commitCount"), int):
                commit_count = merge_request["commitCount"]
            connection = merge_request.get("commits") or {}
            for node in connection.get("nodes") or []:
                commits.append({"sha": node.get("sha", ""), "message": node.get("message", "")})
                author = node.get("author") or {}
                authors.append({"login": author.get("username", ""), "email": node.get("authorEmail", "")})
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return commits, authors, commit_count
            next_cursor = page_info.get("endCursor") or ""
            if next_cursor in ("", cursor):
                raise SourceError("GitLab returned incomplete commit metadata. No partial report was saved.")
            cursor = next_cursor
        raise SourceError("GitLab commit metadata exceeded the pagination limit.")

    async def _query(self, query: str, variables: dict) -> dict:
        data = payload(await request(self.client, "POST", "api/graphql",
            json={"query": query, "variables": variables}))
        if data.get("errors"):
            raise SourceError("GitLab could not read merge request statistics. Check repository permissions and API compatibility.")
        merge_request = ((data.get("data") or {}).get("project") or {}).get("mergeRequest")
        if not merge_request:
            raise SourceError("GitLab could not read the merge request statistics. Check the repository path and the number.")
        return merge_request