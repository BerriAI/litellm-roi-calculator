import asyncio
import math
from datetime import date

import httpx

from .config import Settings, email


class SourceError(Exception):
    """A user-safe connection error; upstream response bodies never reach the UI."""


async def request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
    for attempt in range(3):
        try:
            response = await client.request(method, path, **kwargs)
        except httpx.RequestError:
            raise SourceError("Could not reach the service. Check its URL and your network connection.") from None
        if response.status_code in (429, 502, 503, 504) and method == "GET" and attempt < 2:
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        if response.status_code >= 400:
            labels = {
                401: "Authentication failed. Check the configured key or token.",
                403: "Access denied or rate limit reached. Check permissions and the service's rate limits.",
                404: "Endpoint or repository not found. Check the URL, repository access, and LiteLLM version.",
                429: "Rate limit reached. Wait before syncing again.",
                400: "The service rejected the request. Check model support for temperature 0 and JSON output.",
            }
            if "X-GitHub-Api-Version" in client.headers:
                labels.update({
                    403: "GitHub denied access or reached a rate limit. Check token permissions, organization approval, and SSO authorization.",
                    404: "GitHub repository or organization not found. Check its name, token access, and Enterprise API URL. Private repositories require an authorized token.",
                })
            raise SourceError(labels.get(response.status_code, "The service returned an error.") + f" (HTTP {response.status_code})")
        return response
    raise SourceError("The service is temporarily unavailable.")


def payload(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        raise SourceError("The service returned an unexpected response instead of JSON.") from None


def finite_number(value) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise SourceError("The gateway returned an invalid spend value.") from None
    if not math.isfinite(result) or result < 0:
        raise SourceError("The gateway returned a negative or non-finite spend value.")
    return result


def complete_patch(file: dict) -> bool:
    patch = file.get("patch")
    if patch is None:
        return not file.get("additions") and not file.get("deletions") and file.get("status") == "renamed"
    lines = patch.splitlines()
    return (sum(line.startswith("+") for line in lines) == file.get("additions", 0)
        and sum(line.startswith("-") for line in lines) == file.get("deletions", 0))


class Gateway:
    def __init__(self, settings: Settings, transport=None):
        base = settings.gateway_url.rstrip("/").removesuffix("/v1")
        self.client = httpx.AsyncClient(base_url=base + "/", headers={"Authorization": f"Bearer {settings.admin_key}"}, timeout=45, transport=transport, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def models(self) -> list[str]:
        data = payload(await request(self.client, "GET", "v1/models"))
        return sorted({r["id"] for r in data.get("data", []) if isinstance(r.get("id"), str)})

    async def users(self) -> dict[str, str]:
        result = {}
        for page in range(1, 10001):
            data = payload(await request(self.client, "GET", "user/list", params={"page": page, "page_size": 100}))
            if not isinstance(data, dict) or not isinstance(data.get("users"), list):
                raise SourceError("Unsupported /user/list response. Use a LiteLLM gateway with user management enabled.")
            for user in data["users"]:
                result[str(user["user_id"])] = email(user.get("user_email")) or email(user.get("user_id"))
            if page >= data.get("total_pages", page + (len(data["users"]) == 100)):
                return result
        raise SourceError("The gateway user list exceeded the pagination limit; no partial report was saved.")

    async def spend(self, start: date, end: date) -> tuple[list[dict], dict[str, str]]:
        users = await self.users()
        records = {}
        for page in range(1, 10001):
            data = payload(await request(self.client, "GET", "user/daily/activity", params={
                "start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": 0,
                "page": page, "page_size": 1000,
            }))
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise SourceError("Unsupported daily activity response. Upgrade LiteLLM to a version with /user/daily/activity.")
            for day in data["results"]:
                day_date = str(day["date"])[:10]
                if not start.isoformat() <= day_date <= end.isoformat():
                    continue
                entities = day.get("breakdown", {}).get("entities", {})
                day_total = finite_number(day.get("metrics", {}).get("spend", 0))
                accounted = 0.0
                for uid, item in entities.items():
                    cost = finite_number(item.get("metrics", {}).get("spend", 0))
                    accounted += cost
                    address = users.get(uid) or email(item.get("metadata", {}).get("user_email")) or email(uid)
                    # LiteLLM paginates raw spend rows, then aggregates each page.
                    # A user's day can span pages; each amount is only a partial total.
                    record = records.setdefault((day_date, uid), {"date": day_date, "user_id": uid,
                        "email": "", "spend": 0.0, "requests": 0})
                    record["email"] = record["email"] or address
                    record["spend"] += cost
                    record["requests"] += item.get("metrics", {}).get("api_requests", 0)
                if accounted > day_total + 0.0001:
                    raise SourceError("Daily spend does not reconcile with the user breakdown; no partial report was saved.")
                remainder = max(0, day_total - accounted)
                if remainder:
                    record = records.setdefault((day_date, "__unassigned__"), {"date": day_date,
                        "user_id": "__unassigned__", "email": "", "spend": 0.0, "requests": 0})
                    record["spend"] += remainder
            metadata = data.get("metadata", {})
            if not metadata.get("has_more", page < metadata.get("total_pages", page)):
                return list(records.values()), users
        raise SourceError("Gateway activity exceeded the pagination limit; no partial report was saved.")


class GitHub:
    def __init__(self, settings: Settings, transport=None, token_provider=None, user_token_provider=None):
        self.token_provider = token_provider
        self.user_token_provider = user_token_provider
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if settings.github_token and token_provider is None and user_token_provider is None:
            headers["Authorization"] = f"Bearer {settings.github_token}"

        async def authorize(req):
            if token_provider is not None or user_token_provider is not None:
                if req.url.scheme != "https" or req.url.host != "api.github.com" or req.url.port not in (None, 443):
                    raise SourceError("GitHub connections use https://api.github.com. Use Advanced for Enterprise Server.")
                if user_token_provider is not None:
                    req.headers["Authorization"] = "Bearer " + await user_token_provider()
                    return
                parts = req.url.path.strip("/").split("/")
                if len(parts) >= 3 and parts[0] == "repos":
                    req.headers["Authorization"] = "Bearer " + await token_provider("/".join(parts[1:3]))

        self.client = httpx.AsyncClient(base_url=settings.github_api_url + "/", headers=headers, timeout=45,
            transport=transport, event_hooks={"request": [authorize]}, follow_redirects=False)
        self.profiles = {}

    async def close(self):
        await self.client.aclose()

    async def repositories(self, organization: str = "", page: int = 1) -> dict:
        path = f"orgs/{organization}/repos" if organization else "user/repos"
        params = {"per_page": 100, "page": page, "sort": "updated", "direction": "desc"}
        if not organization:
            params["affiliation"] = "owner,collaborator,organization_member"
        response = await request(self.client, "GET", path, params=params)
        data = payload(response)
        if not isinstance(data, list) or any(not isinstance(repo, dict) or not isinstance(repo.get("full_name"), str) for repo in data):
            raise SourceError("GitHub returned an unexpected repository list.")
        return {"repos": [{"name": repo["full_name"], "visibility": repo.get("visibility") or ("private" if repo.get("private") else "public"),
            "archived": bool(repo.get("archived"))} for repo in data], "has_more": 'rel="next"' in response.headers.get("link", "")}

    async def pages(self, path: str, params: dict | None = None, limit=10000):
        for page in range(1, limit + 1):
            response = await request(self.client, "GET", path, params={**(params or {}), "per_page": 100, "page": page})
            data = payload(response)
            if not isinstance(data, list):
                raise SourceError("GitHub returned an unexpected pagination response.")
            yield data
            if 'rel="next"' not in response.headers.get("link", ""):
                return
        raise SourceError("GitHub's pagination limit was reached; narrow the date range.")

    async def pulls(self, repo: str, start: date, end: date) -> list[dict]:
        pulls = []
        async for page in self.pages(f"repos/{repo}/pulls", {"state": "closed", "sort": "updated", "direction": "desc"}):
            for pr in page:
                merged = pr.get("merged_at")
                if merged and start.isoformat() <= merged[:10] <= end.isoformat():
                    pulls.append(pr)
            if page and page[-1]["updated_at"][:10] < start.isoformat():
                break
        return pulls

    async def evidence(self, repo: str, pr: dict) -> dict:
        number = pr["number"]
        detail = payload(await request(self.client, "GET", f"repos/{repo}/pulls/{number}"))
        login = (detail.get("user") or {}).get("login", "deleted-user")
        files = []
        async for page in self.pages(f"repos/{repo}/pulls/{number}/files", limit=30):
            files.extend({k: item.get(k) for k in ("filename", "status", "additions", "deletions")} for item in page)
        emails = set()
        if login not in self.profiles:
            response = await self.client.get(f"users/{login}")
            self.profiles[login] = email(payload(response).get("email")) if response.status_code == 200 else ""
        if self.profiles[login]:
            emails.add(self.profiles[login])
        commits, authors, commit_count = await self.commit_metadata(repo, number, detail)
        for author in authors:
            if author["login"].casefold() == login.casefold() and (address := email(author["email"])):
                emails.add(address)
        missing = len(files) != detail.get("changed_files", len(files)) or len(commits) != commit_count
        return {
            "repo": repo, "number": number, "title": detail["title"], "body": detail.get("body") or "",
            "url": detail["html_url"], "login": login, "emails": sorted(emails),
            "profile_email": self.profiles[login], "merged_at": detail["merged_at"],
            "head_sha": detail["head"]["sha"], "additions": detail.get("additions", 0),
            "deletions": detail.get("deletions", 0), "changed_files": detail.get("changed_files", len(files)),
            "files": files, "commits": commits, "commit_count": commit_count, "incomplete_metadata": missing,
        }

    async def commit_metadata(self, repo: str, number: int, detail: dict) -> tuple[list, list, int]:
        authorization = self.client.headers.get("Authorization", "")
        if self.user_token_provider:
            authorization = "Bearer " + await self.user_token_provider()
        elif self.token_provider:
            authorization = "Bearer " + await self.token_provider(repo)
        commits, authors = [], []
        if not authorization:
            # Public repositories also work without authenticated GraphQL. This
            # endpoint includes messages, but not individual commit line counts.
            async for page in self.pages(f"repos/{repo}/pulls/{number}/commits", limit=3):
                for item in page:
                    commit = item.get("commit", {})
                    commits.append({"sha": item.get("sha", ""), "message": commit.get("message", "")})
                    authors.append({"login": (item.get("author") or {}).get("login", ""),
                        "email": (commit.get("author") or {}).get("email", "")})
            return commits, authors, detail.get("commits", len(commits))
        base = str(self.client.base_url).rstrip("/")
        endpoint = base.removesuffix("/api/v3") + "/api/graphql" if base.endswith("/api/v3") else base + "/graphql"
        query = """query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
          repository(owner:$owner, name:$name) { pullRequest(number:$number) {
            commits(first:100, after:$cursor) {
              totalCount pageInfo { hasNextPage endCursor }
              nodes { commit { oid message additions deletions changedFilesIfAvailable
                author { email user { login } } } }
            }
          } }
        }"""
        owner, name = repo.split("/")
        cursor = None
        for _ in range(100):
            data = payload(await request(self.client, "POST", endpoint,
                headers={"Authorization": authorization}, json={"query": query,
                    "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor}}))
            if data.get("errors"):
                raise SourceError("GitHub could not read commit metadata. Check repository permissions and API compatibility.")
            try:
                connection = data["data"]["repository"]["pullRequest"]["commits"]
                for node in connection["nodes"]:
                    commit = node["commit"]
                    commits.append({"sha": commit["oid"], "message": commit["message"],
                        "additions": commit["additions"], "deletions": commit["deletions"],
                        "changed_files": commit["changedFilesIfAvailable"]})
                    author = commit.get("author") or {}
                    authors.append({"login": (author.get("user") or {}).get("login", ""),
                        "email": author.get("email", "")})
                if not connection["pageInfo"]["hasNextPage"]:
                    return commits, authors, connection["totalCount"]
                next_cursor = connection["pageInfo"]["endCursor"]
                if not next_cursor or next_cursor == cursor:
                    raise ValueError("pagination")
                cursor = next_cursor
            except (KeyError, TypeError, ValueError):
                raise SourceError("GitHub returned incomplete commit metadata. No partial report was saved.") from None
        raise SourceError("GitHub commit metadata exceeded the pagination limit.")
