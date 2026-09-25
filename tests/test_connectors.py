import json
from datetime import date

import httpx
import pytest

from litellm_roi.connectors import Gateway, GitHub, SourceError, complete_patch


async def test_gateway_pagination_daily_totals_and_metadata_email(settings):
    calls = []

    def handler(req):
        calls.append((req.url.path, dict(req.url.params)))
        assert req.headers["authorization"] == "Bearer test-admin-secret"
        page = int(req.url.params["page"])
        if req.url.path == "/user/list":
            return httpx.Response(200, json={"users": [{"user_id": f"u{page}", "user_email": f"person{page}@example.com"}], "total_pages": 2})
        assert req.url.params["start_date"] == "2026-09-01"
        assert req.url.params["end_date"] == "2026-09-30"
        assert req.url.params["timezone"] == "0"
        return httpx.Response(200, json={"results": [{"date": f"2026-09-0{page}", "metrics": {"spend": 10},
            "breakdown": {"entities": {f"u{page}": {"metrics": {"spend": 7, "api_requests": 2}}}}}],
            "metadata": {"has_more": page < 2, "total_pages": 2}})

    gateway = Gateway(settings, httpx.MockTransport(handler))
    records, users = await gateway.spend(date(2026, 9, 1), date(2026, 9, 30))
    await gateway.close()
    assert len(calls) == 4
    assert len(users) == 2
    assert sum(row["spend"] for row in records) == 20
    assert sum(row["spend"] for row in records if row["email"]) == 14


async def test_gateway_failure_does_not_leak_response_secrets(settings):
    gateway = Gateway(settings, httpx.MockTransport(lambda _: httpx.Response(401, text="test-admin-secret")))
    with pytest.raises(SourceError) as exc:
        await gateway.users()
    await gateway.close()
    assert "test-admin-secret" not in str(exc.value)


async def test_git_pr_window_filters_unmerged_and_paginates(settings):
    calls = []

    def handler(req):
        page = int(req.url.params["page"])
        calls.append(page)
        if page == 1:
            return httpx.Response(200, headers={"link": '<https://api.github.com/next>; rel="next"'}, json=[
                {"number": 1, "merged_at": "2026-09-30T23:59:59Z", "updated_at": "2026-10-01T00:00:00Z"},
                {"number": 2, "merged_at": None, "updated_at": "2026-09-15T00:00:00Z"}])
        return httpx.Response(200, json=[
            {"number": 3, "merged_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z"},
            {"number": 4, "merged_at": "2026-08-31T23:59:59Z", "updated_at": "2026-08-31T23:59:59Z"}])

    github = GitHub(settings, httpx.MockTransport(handler))
    prs = await github.pulls("org/repo", date(2026, 9, 1), date(2026, 9, 30))
    await github.close()
    assert [p["number"] for p in prs] == [1, 3]
    assert calls == [1, 2]


async def test_email_evidence_only_uses_pr_author_and_detects_missing_file(settings):
    settings.github_token = ""
    def handler(req):
        path = req.url.path
        if path.endswith("/files"):
            return httpx.Response(200, json=[{"filename": "a.py", "patch": "@@ -1 +1 @@\n-a\n+b", "additions": 1, "deletions": 1}])
        if path.endswith("/commits"):
            return httpx.Response(200, json=[
                {"author": {"login": "alice"}, "commit": {"author": {"email": "alice@example.com"}}},
                {"author": {"login": "bob"}, "commit": {"author": {"email": "boss@example.com"}}},
                {"author": None, "commit": {"author": {"email": "unlinked@example.com"}}}])
        if path.startswith("/users/"):
            return httpx.Response(200, json={"email": None})
        return httpx.Response(200, json={"number": 42, "user": {"login": "alice"}, "title": "Fix",
            "html_url": "https://github.com/org/repo/pull/42", "head": {"sha": "abc"}, "merged_at": "2026-09-12T00:00:00Z", "changed_files": 2})

    github = GitHub(settings, httpx.MockTransport(handler))
    evidence = await github.evidence("org/repo", {"number": 42})
    await github.close()
    assert evidence["emails"] == ["alice@example.com"]
    assert evidence["incomplete_metadata"] is True
    assert "patch" not in evidence["files"][0]


def test_detects_truncated_patch_even_when_patch_present():
    assert not complete_patch({"patch": "@@\n+one", "additions": 300, "deletions": 0})
    assert complete_patch({"patch": "@@\n+one\n-old", "additions": 1, "deletions": 1})


@pytest.mark.parametrize("api_url,graphql_path", [("https://api.github.com", "/graphql"),
    ("https://github.company.com/api/v3", "/api/graphql")])
async def test_authenticated_commit_metadata_paginates_and_ignores_missing_binary_patch(settings, api_url, graphql_path):
    settings.github_api_url = api_url
    cursors = []

    def handler(req):
        path = req.url.path
        assert req.headers["authorization"] == "Bearer test-github-secret"
        if path == graphql_path:
            body = json.loads(req.content)
            assert body["variables"]["owner"] == "org"
            assert body["variables"]["number"] == 42
            cursor = body["variables"]["cursor"]
            cursors.append(cursor)
            login = "alice" if cursor is None else "bob"
            return httpx.Response(200, json={"data": {"repository": {"pullRequest": {"commits": {
                "totalCount": 2, "pageInfo": {"hasNextPage": cursor is None, "endCursor": "next"},
                "nodes": [{"commit": {"oid": login, "message": "Add diagram\n\nDescribe the flow.",
                    "additions": 1, "deletions": 0, "changedFilesIfAvailable": 1,
                    "author": {"email": f"{login}@example.com", "user": {"login": login}}}}],
            }}}}})
        if path.endswith("/files"):
            return httpx.Response(200, json=[{"filename": "diagram.png", "status": "added", "additions": 0, "deletions": 0}])
        if "/users/" in path:
            return httpx.Response(200, json={"email": None})
        return httpx.Response(200, json={"number": 42, "user": {"login": "alice"}, "title": "Add diagram",
            "html_url": api_url + "/org/repo/pull/42", "head": {"sha": "abc"}, "merged_at": "2026-09-12T00:00:00Z",
            "changed_files": 1, "commits": 2})

    github = GitHub(settings, httpx.MockTransport(handler))
    evidence = await github.evidence("org/repo", {"number": 42})
    await github.close()
    assert cursors == [None, "next"]
    assert evidence["emails"] == ["alice@example.com"]
    assert not evidence["incomplete_metadata"]
    assert evidence["commit_count"] == len(evidence["commits"]) == 2
    assert evidence["commits"][0]["message"] == "Add diagram\n\nDescribe the flow."
    assert evidence["commits"][0]["additions"] == 1


async def test_commit_metadata_uses_installation_token_and_rejects_partial_graphql_response(settings):
    repositories = []

    async def token_provider(repo):
        repositories.append(repo)
        return "test-installation-token"

    def handler(req):
        assert req.url.path == "/graphql"
        assert req.headers["authorization"] == "Bearer test-installation-token"
        return httpx.Response(200, json={"data": {}, "errors": [{"message": "test-installation-token private upstream details"}]})

    github = GitHub(settings, httpx.MockTransport(handler), token_provider=token_provider)
    with pytest.raises(SourceError) as exc:
        await github.commit_metadata("org/repo", 42, {})
    await github.close()
    assert repositories == ["org/repo"]
    assert "test-installation-token" not in str(exc.value)


async def test_gateway_missing_entity_breakdown_is_unassigned(settings):
    def handler(req):
        if req.url.path == "/user/list":
            return httpx.Response(200, json={"users": [], "total_pages": 1})
        return httpx.Response(200, json={"results": [{"date": "2026-09-01", "metrics": {"spend": 123}}], "metadata": {"has_more": False}})
    gateway = Gateway(settings, httpx.MockTransport(handler))
    records, _ = await gateway.spend(date(2026, 9, 1), date(2026, 9, 30))
    await gateway.close()
    assert records[0]["email"] == ""
    assert records[0]["spend"] == 123


async def test_company_repositories_use_configured_host_token_and_pagination(settings):
    settings.github_api_url = "https://github.company.example/api/v3"
    calls = []

    def handler(req):
        calls.append(req)
        assert req.url.host == "github.company.example"
        assert req.headers["authorization"] == "Bearer test-github-secret"
        assert req.url.params["per_page"] == "100"
        if req.url.path == "/api/v3/orgs/company/repos":
            assert req.url.params["page"] == "2"
            return httpx.Response(200, headers={"link": '<https://untrusted.example/next>; rel="next"'}, json=[
                {"full_name": "company/internal", "visibility": "internal"},
                {"full_name": "company/private", "private": True, "archived": True},
            ])
        assert req.url.path == "/api/v3/user/repos"
        assert req.url.params["affiliation"] == "owner,collaborator,organization_member"
        return httpx.Response(200, json=[{"full_name": "company/public", "private": False}])

    github = GitHub(settings, httpx.MockTransport(handler))
    organization = await github.repositories("company", 2)
    personal = await github.repositories()
    await github.close()
    assert len(calls) == 2  # Never follows an arbitrary pagination URL with the token.
    assert organization == {"repos": [
        {"name": "company/internal", "visibility": "internal", "archived": False},
        {"name": "company/private", "visibility": "private", "archived": True},
    ], "has_more": True}
    assert personal["has_more"] is False
    assert personal["repos"][0]["visibility"] == "public"


async def test_github_access_errors_explain_company_authorization_without_leaking(settings):
    github = GitHub(settings, httpx.MockTransport(lambda _: httpx.Response(403, text="private-token")))
    with pytest.raises(SourceError, match="SSO authorization") as exc:
        await github.repositories("company")
    await github.close()
    assert "private-token" not in str(exc.value)


async def test_gateway_sums_user_and_unassigned_spend_across_pages(settings):
    def handler(req):
        if req.url.path == "/user/list":
            return httpx.Response(200, json={"users": [
                {"user_id": "u1", "user_email": "alice@example.com"},
                {"user_id": "u2", "user_email": "bob@example.com"},
            ], "total_pages": 1})
        page = int(req.url.params["page"])
        # The same date/user occurs on both pages because LiteLLM paginates
        # raw rows before aggregating by user and date within each page.
        rows = [{"date": "2026-09-25", "metrics": {"spend": 10 if page == 1 else 5},
            "breakdown": {"entities": {"u1": {"metrics": {
                "spend": 7 if page == 1 else 3, "api_requests": 3 if page == 1 else 2,
            }}}}}]
        if page == 2:
            rows.append({"date": "2026-09-24", "metrics": {"spend": 2}, "breakdown": {
                "entities": {"u2": {"metrics": {"spend": 2, "api_requests": 1}}}}})
        return httpx.Response(200, json={"results": rows, "metadata": {"has_more": page < 2}})

    gateway = Gateway(settings, httpx.MockTransport(handler))
    try:
        for _ in range(2):  # A new sync imports a fresh snapshot, not lifetime totals.
            records, _ = await gateway.spend(date(2026, 9, 24), date(2026, 9, 25))
            by_user = {row["user_id"]: row for row in records}
            assert by_user["u1"]["spend"] == 10
            assert by_user["u1"]["requests"] == 5
            assert by_user["u1"]["email"] == "alice@example.com"
            assert by_user["u2"]["spend"] == 2
            assert by_user["u2"]["date"] == "2026-09-24"
            assert by_user["__unassigned__"]["spend"] == 5
            assert sum(row["spend"] for row in records) == 17
    finally:
        await gateway.close()
