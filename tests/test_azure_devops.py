import base64
import json
from datetime import date

import httpx
import pytest
from pydantic import ValidationError

from litellm_roi import sync as sync_module
from litellm_roi.azure_devops import AzureDevOps
from litellm_roi.config import ConfigStore, CredentialDestinationError, Settings
from litellm_roi.connectors import Gateway, SourceError
from litellm_roi.estimator import Estimator
from litellm_roi.git_provider import git_provider_from_settings
from litellm_roi.storage import Store
from litellm_roi.sync import SyncManager


@pytest.fixture
def azure(settings):
    config = settings.model_copy(deep=True)
    config.git_provider = "azure_devops"
    config.azure_devops_org = "acme"
    config.azure_devops_pat = "test-azure-pat"
    return config


def authorized(req):
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b":test-azure-pat").decode()
    assert req.url.params["api-version"] == "7.1"


async def test_azure_pulls_filter_completed_window_and_normalize_shape_with_buffer(azure):
    seen_min_time = []

    def handler(req):
        authorized(req)
        assert req.url.path == "/acme/Contoso/_apis/git/repositories/widget/pullrequests"
        assert req.url.params["searchCriteria.status"] == "completed"
        seen_min_time.append(req.url.params["searchCriteria.minTime"])
        skip = int(req.url.params.get("$skip", "0"))
        if skip:
            return httpx.Response(200, json={"value": []})
        return httpx.Response(200, json={"value": [
            {"pullRequestId": 10, "title": "Fix api", "description": "Details", "closedDate": "2026-09-15T12:00:00.000Z",
                "createdBy": {"displayName": "Alice Silva"}, "lastMergeSourceCommit": {"commitId": "abc"}},
            {"pullRequestId": 11, "title": "Too early", "closedDate": "2026-08-31T23:59:59.999Z",
                "createdBy": {"displayName": "Bob"}},
            {"pullRequestId": 12, "title": "Not completed", "closedDate": None, "createdBy": {"displayName": "Bob"}},
        ]})

    provider = AzureDevOps(azure, httpx.MockTransport(handler))
    pulls = await provider.pulls("Contoso/widget", date(2026, 9, 1), date(2026, 9, 30))
    await provider.close()
    assert seen_min_time == ["2026-08-02T00:00:00Z"]
    assert [p["number"] for p in pulls] == [10]
    assert pulls[0] == {"number": 10, "title": "Fix api", "body": "Details",
        "url": "https://dev.azure.com/acme/Contoso/_git/widget/pullrequest/10",
        "login": "Alice Silva", "merged_at": "2026-09-15T12:00:00.000Z",
        "updated_at": "2026-09-15T12:00:00.000Z", "head_sha": "abc"}


async def test_azure_evidence_maps_identities_files_and_commit_metadata(azure):
    change_calls = []

    def handler(req):
        authorized(req)
        path = req.url.path
        if path.endswith("/iterations/7/changes"):
            change_calls.append(req.url.params.get("continuationToken"))
            entries = [{"changeType": "add", "item": {"path": "/src/new.py"}},
                {"changeType": "edit", "item": {"path": "/src/old.py"}},
                {"changeType": "delete", "item": {"path": "/src/gone.py"}},
                {"changeType": "rename", "item": {"path": "/src/moved.py"}}]
            body = {"changeEntries": entries}
            if req.url.params.get("continuationToken") is None:
                body["continuationToken"] = "next-page"
            return httpx.Response(200, json=body)
        if path.endswith("/iterations"):
            return httpx.Response(200, json={"value": [{"id": 3}, {"id": 7}]})
        if path.endswith("/commits"):
            return httpx.Response(200, json={"value": [
                {"commitId": "abc", "comment": "Fix api", "author": {"name": "Alice Silva", "email": "alice@example.com"}},
                {"commitId": "def", "comment": "Review", "author": {"name": "Someone Else", "email": "boss@example.com"}},
            ]})
        return httpx.Response(200, json={"pullRequestId": 10, "title": "Fix api", "description": "Details",
            "closedDate": "2026-09-15T12:00:00.000Z",
            "createdBy": {"displayName": "Alice Silva", "uniqueName": "alice@example.com"},
            "lastMergeSourceCommit": {"commitId": "abc"}})

    provider = AzureDevOps(azure, httpx.MockTransport(handler))
    evidence = await provider.evidence("Contoso/widget", {"number": 10})
    await provider.close()
    assert change_calls == [None, "next-page"]
    assert evidence["login"] == "Alice Silva"
    assert evidence["profile_email"] == "alice@example.com"
    assert evidence["emails"] == ["alice@example.com"]
    assert "boss@example.com" not in evidence["emails"]
    assert evidence["head_sha"] == "abc"
    assert evidence["changed_files"] == 8
    assert evidence["commit_count"] == 2
    assert {f["status"] for f in evidence["files"]} == {"added", "modified", "removed", "renamed"}
    assert all("additions" not in f and "deletions" not in f for f in evidence["files"])
    assert evidence["incomplete_metadata"] is False


async def test_azure_errors_never_leak_the_pat(azure):
    provider = AzureDevOps(azure, httpx.MockTransport(lambda _: httpx.Response(401, text="test-azure-pat")))
    with pytest.raises(SourceError) as exc:
        await provider.repositories()
    await provider.close()
    assert "test-azure-pat" not in str(exc.value)


async def test_azure_repositories_page_and_project_filter(azure):
    calls = []

    def handler(req):
        authorized(req)
        calls.append(req.url.path)
        headers = {}
        if req.url.params.get("$skip") != "100":
            headers["x-ms-continuationtoken"] = "more"
        return httpx.Response(200, headers=headers, json={"value": [
            {"name": "widget", "project": {"name": "Contoso"}, "isDisabled": False}]})

    provider = AzureDevOps(azure, httpx.MockTransport(handler))
    org_wide = await provider.repositories(page=1)
    project = await provider.repositories("Contoso", page=2)
    await provider.close()
    assert org_wide == {"repos": [{"name": "Contoso/widget", "visibility": "private", "archived": False}], "has_more": True}
    assert project["has_more"] is False
    assert calls == ["/acme/_apis/git/repositories", "/acme/Contoso/_apis/git/repositories"]


async def test_azure_probe_checks_repo_and_completed_pulls(azure):
    calls = []

    def handler(req):
        authorized(req)
        calls.append((req.url.path, dict(req.url.params)))
        return httpx.Response(200, json={"value": []})

    provider = AzureDevOps(azure, httpx.MockTransport(handler))
    await provider.probe("Contoso/widget")
    await provider.close()
    assert calls[0][0] == "/acme/Contoso/_apis/git/repositories/widget"
    assert calls[1][0] == "/acme/Contoso/_apis/git/repositories/widget/pullrequests"
    assert calls[1][1]["searchCriteria.status"] == "completed"


def test_factory_selects_provider_from_settings(azure, settings):
    assert isinstance(git_provider_from_settings(azure), AzureDevOps)
    from litellm_roi.connectors import GitHub
    assert isinstance(git_provider_from_settings(settings), GitHub)


def test_settings_cache_source_for_azure():
    settings = Settings(git_provider="azure_devops", azure_devops_org="acme", azure_devops_pat="x")
    assert settings.cache_source == "https://dev.azure.com/acme"


def test_azure_settings_validate_org_urls_and_repo_names():
    with pytest.raises(ValidationError):
        Settings(git_provider="azure_devops", azure_devops_org="bad org")
    settings = Settings(git_provider="azure_devops", azure_devops_org="acme",
        repos=["https://dev.azure.com/acme/My Project/_git/widget", "My Project/widget.git"])
    assert settings.repos == ["My Project/widget"]
    with pytest.raises(ValidationError, match="Azure DevOps repository URLs"):
        Settings(git_provider="azure_devops", azure_devops_org="acme", repos=["https://dev.azure.com/acme/no-git-here"])


def test_azure_pat_is_secret_locked_and_cleared_with_org_change(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "env-pat")
    monkeypatch.setenv("AZURE_DEVOPS_ORG", "acme")
    store = ConfigStore(tmp_path)
    saved = store.save({"git_provider": "azure_devops", "azure_devops_org": "acme", "azure_devops_pat": "ui-pat"})
    assert saved.azure_devops_pat == "env-pat"
    assert "env-pat" not in json.dumps(saved.public())
    with pytest.raises(CredentialDestinationError):
        store.save({"azure_devops_org": "other"})
    monkeypatch.delenv("AZURE_DEVOPS_PAT")
    monkeypatch.delenv("AZURE_DEVOPS_ORG")
    store = ConfigStore(tmp_path)
    store.save({"git_provider": "azure_devops", "azure_devops_org": "acme", "azure_devops_pat": "ui-pat"})
    assert ConfigStore(tmp_path).load().azure_devops_pat == "ui-pat"
    changed = store.save({"azure_devops_org": "other"})
    assert changed.azure_devops_pat == ""
    assert json.loads((tmp_path / "config.json").read_text())["azure_devops_pat"] == ""


def test_identity_map_accepts_display_names_with_spaces():
    settings = Settings(identity_map={"Alice Silva": "alice@example.com"})
    assert settings.identity_map == {"alice silva": "alice@example.com"}


async def test_full_azure_sync_produces_report_without_line_counts(azure, tmp_path, monkeypatch):
    azure.gateway_url = "https://gateway.example.com"
    azure.admin_key = "test-gateway"
    azure.repos = ["Contoso/widget"]
    azure.estimator_model = "test-model"

    def handler(req):
        if req.url.host == "gateway.example.com":
            if req.url.path == "/user/list":
                return httpx.Response(200, json={"users": [{"user_id": "u1", "user_email": "alice@example.com"}], "total_pages": 1})
            if req.url.path == "/user/daily/activity":
                return httpx.Response(200, json={"results": [{"date": date.today().isoformat(), "metrics": {"spend": 3},
                    "breakdown": {"entities": {"u1": {"metrics": {"spend": 3}}}}}], "metadata": {"has_more": False}})
            if req.url.path == "/v1/chat/completions":
                content = json.loads(req.content)["messages"][-1]["content"]
                assert '"additions"' not in content and "alice@example.com" not in content
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                    "message": {"content": '{"hours":2,"reasoning":"Small metadata-only change."}'}}]})
            raise AssertionError(req.url)
        assert req.headers["authorization"].startswith("Basic ")
        path = req.url.path
        if path.endswith("/iterations/9/changes"):
            return httpx.Response(200, json={"changeEntries": [{"changeType": "edit", "item": {"path": "/src/app.py"}}]})
        if path.endswith("/iterations"):
            return httpx.Response(200, json={"value": [{"id": 9}]})
        if path.endswith("/commits"):
            return httpx.Response(200, json={"value": [
                {"commitId": "abc", "comment": "Tidy", "author": {"name": "Alice Silva", "email": "alice@example.com"}}]})
        if path.endswith("/pullrequests/10"):
            return httpx.Response(200, json={"title": "Tidy", "description": "Cleanup", "closedDate": date.today().isoformat() + "T10:00:00Z",
                "createdBy": {"displayName": "Alice Silva", "uniqueName": "alice@example.com"},
                "lastMergeSourceCommit": {"commitId": "abc"}})
        if path.endswith("/pullrequests"):
            return httpx.Response(200, json={"value": [
                {"pullRequestId": 10, "title": "Tidy", "description": "Cleanup", "closedDate": date.today().isoformat() + "T10:00:00Z",
                    "createdBy": {"displayName": "Alice Silva"}, "lastMergeSourceCommit": {"commitId": "abc"}}]})
        raise AssertionError(req.url)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(sync_module, "Gateway", lambda cfg: Gateway(cfg, transport))
    monkeypatch.setattr(sync_module, "Estimator", lambda cfg, db: Estimator(cfg, db, transport))
    manager = SyncManager(Store(tmp_path), lambda cfg: git_provider_from_settings(cfg, transport=transport))
    manager.start(azure)
    await manager.task
    assert manager.state["error"] is None and manager.state["phase"] == "complete"
    report = Store(tmp_path).latest()
    assert [p["number"] for p in report["pulls"]] == [10]
    assert report["pulls"][0]["estimate"]["hours"] == 2
    assert report["pulls"][0]["login"] == "Alice Silva"
    # A second sync reuses the Azure evidence through the provider-agnostic cache.
    manager.start(azure)
    await manager.task
    assert manager.state["reused"] == 1
