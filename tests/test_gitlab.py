import json
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from litellm_roi import sync as sync_module
from litellm_roi.app import create_app
from litellm_roi.config import ConfigStore, CredentialDestinationError, Settings
from litellm_roi.connectors import Gateway, SourceError
from litellm_roi.estimator import Estimator
from litellm_roi.git_provider import git_provider_from_settings
from litellm_roi.gitlab import GitLab
from litellm_roi.storage import Store
from litellm_roi.sync import SyncManager


@pytest.fixture
def gitlab(settings):
    config = settings.model_copy(deep=True)
    config.git_provider = "gitlab"
    config.gitlab_url = "https://gitlab.acme.com"
    config.gitlab_pat = "test-gitlab-pat"
    return config


def token_header(req):
    assert req.headers["PRIVATE-TOKEN"] == "test-gitlab-pat"


def graph_response(merge_request, cursor=None):
    return {"data": {"project": {"mergeRequest": merge_request}}}


async def test_gitlab_pulls_filter_merged_window_and_normalize_shape(gitlab):
    served_pages = []

    def handler(req):
        token_header(req)
        served_pages.append(req.url.params["page"])
        assert req.url.raw_path.decode().split("?")[0] == "/api/v4/projects/group%3Fteam%2Fwidget/merge_requests"
        assert req.url.params["state"] == "merged"
        assert req.url.params["order_by"] == "updated_at"
        page = {
            "1": [
                {"iid": 10, "title": "Fix api", "description": "Details", "merged_at": "2026-09-15T12:00:00Z",
                    "updated_at": "2026-09-16T08:00:00Z", "sha": "abc", "web_url": "https://gitlab.acme.com/group/team/widget/-/merge_requests/10",
                    "author": {"username": "alice"}},
                {"iid": 11, "title": "Too early", "merged_at": None, "author": {"username": "bob"},
                    "updated_at": "2026-09-15T00:00:00Z", "sha": "def"},
                {"iid": 12, "title": "Closed unmerged", "state": "closed", "author": {"username": "bob"},
                    "merged_at": None, "updated_at": "2026-09-14T00:00:00Z", "sha": "123"},
            ],
            "2": [{"iid": 13, "title": "Old activity", "merged_at": None, "updated_at": "2026-08-30T00:00:00Z",
                "sha": "456", "author": {"username": "alice"}}],
            "3": [{"iid": 99, "title": "Never requested", "merged_at": "2026-09-20T00:00:00Z",
                "updated_at": "2026-09-20T00:00:00Z", "sha": "999", "author": {"username": "alice"}}],
        }[req.url.params["page"]]
        return httpx.Response(200, headers={} if req.url.params["page"] == "2" else {"x-next-page": "2"}, json=page)

    provider = GitLab(gitlab, httpx.MockTransport(handler))
    pulls = await provider.pulls("group?team/widget", date(2026, 9, 1), date(2026, 9, 30))
    await provider.close()
    assert [p["number"] for p in pulls] == [10]
    # Paging stops when updates fall before the window; page 3 would have matched but is never fetched.
    assert served_pages == ["1", "2"]
    assert pulls[0] == {"number": 10, "title": "Fix api", "body": "Details",
        "url": "https://gitlab.acme.com/group/team/widget/-/merge_requests/10",
        "login": "alice", "merged_at": "2026-09-15T12:00:00Z",
        "updated_at": "2026-09-16T08:00:00Z", "head_sha": "abc"}


def test_gitlab_legacy_match_uses_the_configured_server_host(gitlab):
    from litellm_roi.pull_cache import legacy_match
    previous = {"estimator_model": gitlab.estimator_model, "estimator_prompt": gitlab.estimator_prompt,
        "synced_at": datetime.fromisoformat("2026-09-20T12:00:00Z")}
    cached = {"url": "https://gitlab.acme.com/team/widget/-/merge_requests/10", "head_sha": "abc",
        "title": "Tidy", "login": "alice", "updated_at": "2026-09-15T10:00:00Z",
        "estimate": {"status": "estimated", "evidence_source": "pr_metadata", "effort_basis": "without_ai"}}
    pr = {"head_sha": "abc", "title": "Tidy", "login": "alice", "updated_at": "2026-09-15T10:00:00Z"}
    assert legacy_match(gitlab, previous, cached, pr)
    assert not legacy_match(gitlab, previous, {**cached, "url": "https://other.example.com/team/widget/-/merge_requests/10"}, pr)


async def test_gitlab_evidence_maps_identities_files_and_commit_metadata(gitlab):
    queries = []

    def handler(req):
        token_header(req)
        path = req.url.path
        if path == "/api/graphql":
            queries.append(json.loads(req.content)["query"])
            if "diffStatsSummary" in queries[-1]:
                return httpx.Response(200, json=graph_response({
                    "diffStatsSummary": {"additions": 40, "deletions": 6, "fileCount": 2},
                    "diffStats": [{"path": "src/new.py", "additions": 40, "deletions": 0},
                        {"path": "src/old.py", "additions": 0, "deletions": 6}]}))
            return httpx.Response(200, json=graph_response({
                "commitCount": 2,
                "commits": {"pageInfo": {"hasNextPage": False},
                    "nodes": [{"sha": "abc", "message": "Fix api", "authorEmail": "alice@example.com",
                        "authorName": "Alice", "author": {"username": "alice"}},
                        {"sha": "def", "message": "Review", "authorEmail": "boss@example.com",
                        "authorName": "Someone Else", "author": {"username": "boss"}}]}}))
        assert path == "/api/v4/projects/team/widget/merge_requests/10"
        return httpx.Response(200, json={"iid": 10, "title": "Fix api", "description": "Details",
            "web_url": "https://gitlab.acme.com/team/widget/-/merge_requests/10", "merged_at": "2026-09-15T12:00:00Z",
            "sha": "abc", "author": {"username": "alice", "public_email": "alice@example.com"}})

    provider = GitLab(gitlab, httpx.MockTransport(handler))
    evidence = await provider.evidence("team/widget", {"number": 10})
    await provider.close()
    assert len(queries) == 2 and "diffStatsSummary" in queries[0] and "commit" in queries[1]
    assert evidence["login"] == "alice"
    assert evidence["profile_email"] == "alice@example.com"
    assert evidence["emails"] == ["alice@example.com"]
    assert "boss@example.com" not in evidence["emails"]
    assert evidence["head_sha"] == "abc"
    assert evidence["changed_files"] == 2
    assert evidence["commit_count"] == 2
    assert evidence["additions"] == 40 and evidence["deletions"] == 6
    assert evidence["files"] == [{"filename": "src/new.py", "additions": 40, "deletions": 0},
        {"filename": "src/old.py", "additions": 0, "deletions": 6}]
    assert evidence["incomplete_metadata"] is False


async def test_gitlab_evidence_pages_commits_and_flags_mismatched_counts(gitlab):
    def handler(req):
        token_header(req)
        path = req.url.path
        if path == "/api/graphql":
            body = json.loads(req.content)
            if "diffStatsSummary" in body["query"]:
                return httpx.Response(200, json=graph_response({
                    "diffStatsSummary": {"additions": 40, "deletions": 0, "fileCount": 1},
                    "diffStats": [{"path": "src/new.py", "additions": 40, "deletions": 0}]}))
            if not body["variables"].get("cursor"):
                return httpx.Response(200, json=graph_response({
                    "commitCount": gitlab_repos.get("count", 3),
                    "commits": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [
                        {"sha": "abc", "message": "Fix api", "authorEmail": "alice@example.com",
                        "authorName": "Alice", "author": {"username": "alice"}},
                        {"sha": "def", "message": "More", "authorEmail": "alice@example.com",
                        "authorName": "Alice", "author": {"username": "alice"}}]}}))
            return httpx.Response(200, json=graph_response({
                "commits": {"pageInfo": {"hasNextPage": False}, "nodes": [
                    {"sha": "789", "message": "Tail", "authorEmail": "alice@example.com",
                    "authorName": "Alice", "author": {"username": "alice"}}]}}))
        return httpx.Response(200, json={"iid": 10, "title": "Fix api", "merged_at": "2026-09-15T12:00:00Z",
            "sha": "abc", "author": {"username": "alice", "public_email": ""}})

    gitlab_repos = {"count": 3}
    provider = GitLab(gitlab, httpx.MockTransport(handler))
    evidence = await provider.evidence("team/widget", {"number": 10})
    await provider.close()
    assert [c["sha"] for c in evidence["commits"]] == ["abc", "def", "789"]
    assert evidence["commit_count"] == 3
    assert evidence["incomplete_metadata"] is False

    gitlab_repos = {"count": 5}
    provider = GitLab(gitlab, httpx.MockTransport(handler))
    evidence = await provider.evidence("team/widget", {"number": 10})
    await provider.close()
    # The commit count reported by GitLab disagrees with the returned pages.
    assert evidence["commit_count"] == 5
    assert evidence["incomplete_metadata"] is True


async def test_gitlab_rejects_unexpected_shapes_limit_and_broken_cursor(gitlab):
    mode = {"value": "loop"}

    def handler(req):
        token_header(req)
        path = req.url.raw_path.decode().split("?")[0]
        if path.endswith("/merge_requests/10"):
            return httpx.Response(200, json={"iid": 10, "title": "Fix api", "author": {"username": "alice"}})
        if path.endswith("/merge_requests"):
            if mode["value"] == "bad-page":
                return httpx.Response(200, json={"unexpected": "object"})
            return httpx.Response(200, headers={"x-next-page": "2"}, json=[])
        if path == "/api/graphql":
            body = json.loads(req.content)
            if "diffStatsSummary" in body["query"]:
                return httpx.Response(200, json=graph_response({
                    "diffStatsSummary": {"additions": 0, "deletions": 0, "fileCount": 0}, "diffStats": []}))
            if mode["value"] == "no-endcursor":
                return httpx.Response(200, json=graph_response({"commits": {"pageInfo": {"hasNextPage": True}}}))
            return httpx.Response(200, json=[{"path_with_namespace": 3}])
        return httpx.Response(200, json=[{"missing": "path"}])

    provider = GitLab(gitlab, httpx.MockTransport(handler))
    with pytest.raises(SourceError):  # pages() hits its pagination limit
        [page async for page in provider.pages("api/v4/projects/t/w/merge_requests", limit=1)]
    mode["value"] = "bad-page"
    with pytest.raises(SourceError):  # pages() rejects a non-list response
        [page async for page in provider.pages("api/v4/projects/t/w/merge_requests")]
    mode["value"] = "no-endcursor"
    with pytest.raises(SourceError):  # commit pagination continues without an end cursor
        await provider.evidence("t/w", {"number": 10})
    mode["value"] = "bad-list"
    with pytest.raises(SourceError):  # repository list with malformed entries
        await provider.repositories()
    await provider.close()


async def test_gitlab_errors_never_leak_the_pat(gitlab):
    provider = GitLab(gitlab, httpx.MockTransport(lambda _: httpx.Response(401, text="test-gitlab-pat")))
    with pytest.raises(SourceError) as exc:
        await provider.repositories()
    await provider.close()
    assert "test-gitlab-pat" not in str(exc.value)


async def test_gitlab_graphql_errors_become_source_errors(gitlab):
    def handler(req):
        token_header(req)
        if req.url.path == "/api/graphql":
            return httpx.Response(200, json={"errors": [{"message": "not allowed"}]})
        return httpx.Response(200, json={"iid": 10, "title": "Fix api", "author": {"username": "alice"}})

    provider = GitLab(gitlab, httpx.MockTransport(handler))
    with pytest.raises(SourceError):
        await provider.evidence("team/widget", {"number": 10})
    await provider.close()


async def test_gitlab_graphql_missing_merge_request_is_a_source_error(gitlab):
    def handler(req):
        token_header(req)
        if req.url.path == "/api/graphql":
            return httpx.Response(200, json={"data": {"project": None}})
        return httpx.Response(200, json={"iid": 10, "title": "Fix api", "author": {"username": "alice"}})

    provider = GitLab(gitlab, httpx.MockTransport(handler))
    with pytest.raises(SourceError):
        await provider.evidence("team/widget", {"number": 10})
    await provider.close()


async def test_gitlab_diff_stats_fallback_sums_files_when_summary_missing(gitlab):
    def handler(req):
        token_header(req)
        if req.url.path == "/api/graphql":
            if b"diffStatsSummary" in req.content:
                return httpx.Response(200, json=graph_response({"diffStats": [
                    {"path": "src/new.py", "additions": 40, "deletions": 0}]}))
            return httpx.Response(200, json=graph_response({"commitCount": 1,
                "commits": {"pageInfo": {"hasNextPage": False},
                    "nodes": [{"sha": "abc", "message": "Fix api", "authorEmail": "alice@example.com",
                    "authorName": "Alice", "author": {"username": "alice"}}]}}))
        return httpx.Response(200, json={"iid": 10, "title": "Fix api", "merged_at": "2026-09-15T12:00:00Z",
            "sha": "abc", "author": {"username": "alice"}})

    provider = GitLab(gitlab, httpx.MockTransport(handler))
    evidence = await provider.evidence("team/widget", {"number": 10})
    await provider.close()
    assert evidence["additions"] == 40 and evidence["deletions"] == 0
    assert evidence["changed_files"] == 1
    assert evidence["incomplete_metadata"] is False


async def test_gitlab_null_totals_are_flagged_for_review(gitlab):
    def handler(req):
        token_header(req)
        if req.url.path == "/api/graphql":
            if b"diffStatsSummary" in req.content:
                return httpx.Response(200, json=graph_response({
                    "diffStatsSummary": {"additions": None, "deletions": 0, "fileCount": 1},
                    "diffStats": [{"path": "src/new.py", "additions": 0, "deletions": 0}]}))
            return httpx.Response(200, json=graph_response({"commitCount": 1,
                "commits": {"pageInfo": {"hasNextPage": False},
                    "nodes": [{"sha": "abc", "message": "Fix api", "authorEmail": "alice@example.com",
                    "authorName": "Alice", "author": {"username": "alice"}}]}}))
        return httpx.Response(200, json={"iid": 10, "title": "Too large", "merged_at": "2026-09-15T12:00:00Z",
            "sha": "abc", "author": {"username": "alice"}})

    provider = GitLab(gitlab, httpx.MockTransport(handler))
    evidence = await provider.evidence("team/widget", {"number": 10})
    await provider.close()
    assert evidence["incomplete_metadata"] is True


async def test_gitlab_repositories_page_and_group_filter(gitlab):
    calls = []

    def handler(req):
        token_header(req)
        calls.append((req.url.path, req.url.params.get("membership"), req.url.params.get("include_subgroups")))
        headers = {} if req.url.params["page"] == "2" else {"x-next-page": "2"}
        return httpx.Response(200, headers=headers, json=[
            {"path_with_namespace": "team/widget", "visibility": "private", "archived": False}])

    provider = GitLab(gitlab, httpx.MockTransport(handler))
    org_wide = await provider.repositories(page=1)
    group = await provider.repositories("team", page=2)
    await provider.close()
    assert org_wide == {"repos": [{"name": "team/widget", "visibility": "private", "archived": False}], "has_more": True}
    assert group["has_more"] is False
    assert calls == [("/api/v4/projects", "true", None),
        ("/api/v4/groups/team/projects", None, "true")]


async def test_gitlab_probe_checks_repo_and_merged_pulls(gitlab):
    calls = []

    def handler(req):
        token_header(req)
        calls.append((req.url.raw_path.decode().split("?")[0], dict(req.url.params)))
        return httpx.Response(200, json=[])

    provider = GitLab(gitlab, httpx.MockTransport(handler))
    await provider.probe("team/widget")
    await provider.close()
    assert calls[0][0] == "/api/v4/projects/team%2Fwidget"
    assert calls[1][0] == "/api/v4/projects/team%2Fwidget/merge_requests"
    assert calls[1][1]["state"] == "merged"


def test_factory_selects_provider_from_settings(gitlab, settings):
    assert isinstance(git_provider_from_settings(gitlab), GitLab)
    from litellm_roi.azure_devops import AzureDevOps
    assert isinstance(git_provider_from_settings(
        settings.model_copy(update={"git_provider": "azure_devops"})), AzureDevOps)


def test_settings_cache_source_for_gitlab():
    settings = Settings(git_provider="gitlab", gitlab_url="https://gitlab.acme.com", gitlab_pat="x")
    assert settings.cache_source == "https://gitlab.acme.com"


def test_gitlab_settings_validate_repos_subgroups_and_urls():
    settings = Settings(git_provider="gitlab", gitlab_url="https://gitlab.acme.com",
        repos=["https://gitlab.acme.com/group/subgroup/widget", "group/subgroup/widget.git"])
    assert settings.repos == ["group/subgroup/widget"]
    with pytest.raises(ValidationError):
        Settings(git_provider="gitlab", repos=["https://gitlab.example.com/group/widget"])
    with pytest.raises(ValidationError):
        Settings(git_provider="gitlab", repos=["widget"])
    with pytest.raises(ValidationError):
        Settings(git_provider="gitlab", repos=["https://gitlab.acme.com/group/widget?ref=main"])
    # Web UI paths are not project paths.
    with pytest.raises(ValidationError):
        Settings(git_provider="gitlab", repos=["https://gitlab.acme.com/group/widget/-/merge_requests/12"])
    with pytest.raises(ValidationError):
        Settings(git_provider="gitlab", gitlab_url="http://gitlab.acme.com")
    assert Settings(git_provider="gitlab", gitlab_url="http://localhost").gitlab_url == "http://localhost"


def test_gitlab_pat_is_secret_locked_and_cleared_with_url_change(tmp_path, monkeypatch):
    from litellm_roi.config import environment_fields
    monkeypatch.setenv("GITLAB_PAT", "env-pat")
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.acme.com")
    assert "gitlab_pat" in environment_fields() and "gitlab_url" in environment_fields()
    store = ConfigStore(tmp_path)
    saved = store.save({"git_provider": "gitlab", "gitlab_url": "https://gitlab.acme.com", "gitlab_pat": "ui-pat"})
    assert saved.gitlab_pat == "env-pat"
    assert "env-pat" not in json.dumps(saved.public())
    with pytest.raises(CredentialDestinationError):
        store.save({"gitlab_url": "https://other.example.com"})
    monkeypatch.delenv("GITLAB_PAT")
    monkeypatch.delenv("GITLAB_URL")
    store = ConfigStore(tmp_path)
    store.save({"git_provider": "gitlab", "gitlab_url": "https://gitlab.acme.com", "gitlab_pat": "ui-pat"})
    assert ConfigStore(tmp_path).load().gitlab_pat == "ui-pat"
    changed = store.save({"gitlab_url": "https://other.example.com"})
    assert changed.gitlab_pat == ""
    assert json.loads((tmp_path / "config.json").read_text())["gitlab_pat"] == ""


class _FakeProvider:
    async def repositories(self, organization, page):
        return {"repos": [{"name": f"{organization or 'team'}/widget", "visibility": "private", "archived": False}],
            "has_more": False}

    async def close(self):
        pass


def test_app_gitlab_repositories_gate_and_browse(tmp_path, monkeypatch):
    from litellm_roi import app as app_module

    app = create_app(tmp_path)
    app.state.config.save({"git_provider": "github", "github_token": "t"})
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/api/gitlab/repos")
        assert response.status_code == 400 and "Switch the git provider" in response.json()["detail"]
        app.state.config.save({"git_provider": "gitlab", "gitlab_url": "https://gitlab.acme.com"})
        response = client.get("/api/gitlab/repos")
        assert response.status_code == 400 and "personal access token" in response.json()["detail"]
        app.state.config.save({"gitlab_pat": "p"})
        monkeypatch.setattr(app_module, "git_provider_from_settings", lambda s, **kwargs: _FakeProvider())
        response = client.get("/api/gitlab/repos?group=team")
        assert response.json() == {"repos": [{"name": "team/widget", "visibility": "private", "archived": False}],
            "has_more": False}


async def test_full_gitlab_sync_produces_report_and_reuses_cache(gitlab, tmp_path, monkeypatch):
    gitlab.gateway_url = "https://gateway.example.com"
    gitlab.admin_key = "test-gateway"
    gitlab.repos = ["team/widget"]
    gitlab.estimator_model = "test-model"
    # utcnow matches the sync window's clock; date.today() would break in timezones ahead of UTC.
    today = sync_module.utcnow().date()
    start = today - timedelta(days=gitlab.backfill_days - 1)
    assert start.isoformat() < today.isoformat()

    def handler(req):
        if req.url.host == "gateway.example.com":
            if req.url.path == "/user/list":
                return httpx.Response(200, json={"users": [{"user_id": "u1", "user_email": "alice@example.com"}], "total_pages": 1})
            if req.url.path == "/user/daily/activity":
                return httpx.Response(200, json={"results": [{"date": today.isoformat(), "metrics": {"spend": 3},
                    "breakdown": {"entities": {"u1": {"metrics": {"spend": 3}}}}}], "metadata": {"has_more": False}})
            if req.url.path == "/v1/chat/completions":
                content = json.loads(req.content)["messages"][-1]["content"]
                assert "alice@example.com" not in content
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                    "message": {"content": '{"hours":2,"reasoning":"Small change."}'}}]})
            raise AssertionError(req.url)
        token_header(req)
        path = req.url.path
        if path == "/api/graphql":
            body = json.loads(req.content)
            if "diffStatsSummary" in body["query"]:
                return httpx.Response(200, json=graph_response({
                    "diffStatsSummary": {"additions": 9, "deletions": 0, "fileCount": 1},
                    "diffStats": [{"path": "src/app.py", "additions": 9, "deletions": 0}]}))
            return httpx.Response(200, json=graph_response({"commitCount": 1,
                "commits": {"pageInfo": {"hasNextPage": False},
                    "nodes": [{"sha": "abc", "message": "Tidy", "authorEmail": "alice@example.com",
                    "authorName": "Alice", "author": {"username": "alice"}}]}}))
        if path == "/api/v4/projects/team/widget/merge_requests":
            assert dict(req.url.params)["state"] == "merged"
            return httpx.Response(200, json=[{"iid": 10, "title": "Tidy", "description": "Cleanup",
                "merged_at": today.isoformat() + "T10:00:00Z", "updated_at": today.isoformat() + "T11:00:00Z",
                "sha": "abc", "web_url": "https://gitlab.acme.com/team/widget/-/merge_requests/10",
                "author": {"username": "alice"}}])
        return httpx.Response(200, json={"iid": 10, "title": "Tidy", "description": "Cleanup",
            "web_url": "https://gitlab.acme.com/team/widget/-/merge_requests/10",
            "merged_at": today.isoformat() + "T10:00:00Z", "sha": "abc",
            "author": {"username": "alice", "public_email": "alice@example.com"}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(sync_module, "Gateway", lambda cfg: Gateway(cfg, transport))
    monkeypatch.setattr(sync_module, "Estimator", lambda cfg, db: Estimator(cfg, db, transport))
    manager = SyncManager(Store(tmp_path), lambda cfg: git_provider_from_settings(cfg, transport=transport))
    manager.start(gitlab)
    await manager.task
    assert manager.state["error"] is None and manager.state["phase"] == "complete"
    report = Store(tmp_path).latest()
    assert [p["number"] for p in report["pulls"]] == [10]
    assert report["pulls"][0]["estimate"]["hours"] == 2
    assert report["pulls"][0]["login"] == "alice"
    # A second sync reuses the cached evidence through the provider-agnostic cache.
    manager.start(gitlab)
    await manager.task
    assert manager.state["reused"] == 1