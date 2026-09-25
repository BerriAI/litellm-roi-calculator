import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from litellm_roi import sync as sync_module
from litellm_roi.app import create_app
from litellm_roi.config import ConfigStore, Settings
from litellm_roi.connectors import Gateway, GitHub
from litellm_roi.estimator import Estimator
from litellm_roi.storage import Store
from litellm_roi.sync import SyncManager, window


def test_settings_persist_schedule_without_returning_secrets(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        assert client.get("/api/state").json()["settings"]["backfill_days"] == 7
        response = client.put("/api/settings", json={"gateway_url": "https://gateway.example.com", "admin_key": "never-return-this",
            "github_token": "nor-this-token", "repos": ["org/repo"], "estimator_model": "test", "backfill_days": 90, "update_interval_minutes": 360})
        assert response.status_code == 200
        assert response.json()["backfill_days"] == 90
        assert response.json()["update_interval_minutes"] == 360
        assert "never-return-this" not in response.text
        assert "nor-this-token" not in client.get("/api/state").text
        assert client.get("/api/state").json()["status"]["next_update"] is None
        client.put("/api/settings", json={"admin_key": "", "update_interval_minutes": 0})
        assert client.get("/api/state").json()["settings"]["has_admin_key"]
        assert client.get("/api/state").json()["status"]["next_update"] is None
    assert ConfigStore(tmp_path).load().backfill_days == 90
    assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600


def test_local_origin_host_guards_and_read_only_demo(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        page = client.get("/")
        assert page.status_code == 200
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page.text)
        assert assets
        for asset in assets:
            assert client.get(asset).status_code == 200
        assert client.get("/assets/litellm-logo.jpg").status_code == 200
        assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
        assert client.put("/api/settings", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/state", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/sync", content="x", headers={"Content-Type": "text/plain"}).status_code == 415
        assert client.post("/api/sync", json={}).status_code == 400
        demo = client.get("/api/state?mode=demo").json()["report"]
        assert demo["mode"] == "demo" and demo["metrics"]["cost_per_hour"] > 0
        assert client.get("/api/state").json()["report"] is None
        assert "gateway_spend_usd" in client.get("/api/export?mode=demo").text
        assert client.get("/api/export").status_code == 404


def test_invalid_schedule_and_unknown_fields_rejected(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        for change in [{"backfill_days": 0}, {"update_interval_minutes": 1}, {"gateway_url": None}]:
            assert client.put("/api/settings", json=change).status_code == 422
        assert client.put("/api/settings", json={"temperature": 1}).status_code == 400


def test_url_change_does_not_forward_old_credentials(tmp_path, settings):
    store = ConfigStore(tmp_path)
    store.save(settings.model_dump())
    changed = store.save({"gateway_url": "https://new.example.com", "github_api_url": "https://git.example.com/api/v3"})
    assert changed.admin_key == changed.estimator_key == changed.github_token == ""


def test_backfill_is_inclusive_utc_and_schedule_manual(settings, monkeypatch, tmp_path):
    now = datetime(2026, 9, 25, 23, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(sync_module, "utcnow", lambda: now)
    settings.backfill_days = 90
    start, end = window(settings)
    assert end.isoformat() == "2026-09-25"
    assert (end - start).days == 89
    manager = SyncManager(Store(tmp_path))
    manager.schedule(settings)
    assert manager.next_update is None
    settings.update_interval_minutes = 360
    manager.schedule(settings)
    assert manager.next_update is None  # Saving setup never starts the first backfill.
    manager.store.save_report({"mode": "live", "synced_at": now.isoformat()})
    manager.schedule(settings)
    assert manager.next_update == now + timedelta(hours=6)


async def test_full_sync_caches_estimates_and_preserves_previous_report_on_source_failure(settings, tmp_path, monkeypatch):
    settings.github_token = ""
    settings.backfill_days = 30
    store = Store(tmp_path)
    calls = {"model": 0, "fail": False}
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(sync_module, "utcnow", lambda: now)

    def handler(req):
        path = req.url.path
        if req.url.host == "gateway.example.com":
            if calls["fail"]:
                return httpx.Response(401, text="private-token")
            if path == "/user/list":
                return httpx.Response(200, json={"users": [{"user_id": "u1", "user_email": "alice@example.com"}], "total_pages": 1})
            if path == "/user/daily/activity":
                return httpx.Response(200, json={"results": [{"date": "2026-09-12", "metrics": {"spend": 12}, "breakdown": {"entities": {"u1": {"metrics": {"spend": 12}}}}}], "metadata": {"has_more": False}})
            if path == "/v1/chat/completions":
                calls["model"] += 1
                assert json.loads(req.content)["temperature"] == 0
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"hours":4,"reasoning":"Implementation and regression testing."}'}}]})
        if path == "/repos/org/repo/pulls":
            return httpx.Response(200, json=[{"number": 42, "merged_at": "2026-09-12T12:00:00Z", "updated_at": "2026-09-12T12:00:00Z"}])
        if path.endswith("/files"):
            return httpx.Response(200, json=[{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 1, "patch": "@@\n-a\n+b"}])
        if path.endswith("/commits"):
            return httpx.Response(200, json=[{"author": {"login": "alice"}, "commit": {"author": {"email": "alice@example.com"}}}])
        if path.startswith("/users/"):
            return httpx.Response(200, json={"email": None})
        return httpx.Response(200, json={"title": "Fix", "user": {"login": "alice"}, "head": {"sha": "abc"},
            "html_url": "https://github.com/org/repo/pull/42", "merged_at": "2026-09-12T12:00:00Z", "changed_files": 1})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(sync_module, "Gateway", lambda cfg: Gateway(cfg, transport))
    monkeypatch.setattr(sync_module, "GitHub", lambda cfg: GitHub(cfg, transport))
    monkeypatch.setattr(sync_module, "Estimator", lambda cfg, db: Estimator(cfg, db, transport))
    manager = SyncManager(store)
    manager.start(settings)
    await manager.task
    first = store.latest()
    assert manager.state["error"] is None and first["pulls"][0]["estimate"]["hours"] == 4
    assert manager.state["phase"] == "complete"
    assert manager.state["done"] == manager.state["estimated"] == manager.state["total"] == 1
    assert manager.state["needs_attention"] == 0
    manager.start(settings)
    await manager.task
    assert calls["model"] == 1
    assert store.latest()["pulls"][0]["estimate"]["cached"]
    previous_id = store.latest()["id"]
    calls["fail"] = True
    manager.start(settings)
    await manager.task
    assert store.latest()["id"] == previous_id
    assert "private-token" not in manager.state["error"]


async def test_cancel_preserves_snapshot_and_disallows_overlapping_sync(settings, tmp_path, monkeypatch):
    entered = asyncio.Event()

    async def slow_spend(*_):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Gateway, "spend", slow_spend)
    manager = SyncManager(Store(tmp_path))
    manager.start(settings)
    await entered.wait()
    from litellm_roi.connectors import SourceError
    with pytest.raises(SourceError, match="already running"):
        manager.start(settings)
    await manager.cancel()
    assert manager.state["running"] is False
    assert manager.state["stage"] == "Sync cancelled"
    assert manager.state["phase"] == "cancelled"
    assert manager.next_update is None
    assert manager.store.latest() is None


def test_environment_override_and_schedule_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_GATEWAY_URL", "https://env.example.com")
    cfg = ConfigStore(tmp_path)
    assert cfg.save({"gateway_url": "https://other.example.com"}).gateway_url == "https://env.example.com"
    with pytest.raises(ValidationError):
        Settings(update_interval_minutes=-1)


async def test_scheduler_catches_up_on_startup_and_can_switch_to_manual(settings, tmp_path, monkeypatch):
    settings.update_interval_minutes = 5
    ConfigStore(tmp_path).save(settings.model_dump())
    store = Store(tmp_path)
    previous = store.save_report({"mode": "live", "synced_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()})
    requests = []

    def handler(req):
        requests.append(req.url.path)
        if req.url.path == "/user/list":
            return httpx.Response(200, json={"users": [], "total_pages": 1})
        if req.url.path == "/user/daily/activity":
            return httpx.Response(200, json={"results": [], "metadata": {"has_more": False}})
        if req.url.path == "/repos/org/repo/pulls":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected request: {req.url.path}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(sync_module, "Gateway", lambda cfg: Gateway(cfg, transport))
    monkeypatch.setattr(sync_module, "GitHub", lambda cfg: GitHub(cfg, transport))
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(10):
            while store.latest()["id"] == previous["id"]:
                await asyncio.sleep(0.1)
        await app.state.manager.task
        assert requests == ["/user/list", "/user/daily/activity", "/repos/org/repo/pulls"]
        assert store.latest()["pulls"] == []
        assert app.state.manager.next_update > datetime.now(timezone.utc)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.put("/api/settings", json={"update_interval_minutes": 0})
        assert response.status_code == 200
        assert app.state.manager.next_update is None


@pytest.mark.parametrize(("api_url", "repo_url"), [
    ("https://api.github.com", "https://github.com/company/repo.git"),
    ("https://github.company.example/api/v3", "https://github.company.example/company/repo/"),
    ("https://api.company.ghe.com", "https://company.ghe.com/company/repo"),
])
def test_repository_urls_follow_configured_github_host(api_url, repo_url):
    assert Settings(github_api_url=api_url, repos=[repo_url, "company/repo"]).repos == ["company/repo"]
    with pytest.raises(ValidationError, match="configured GitHub server"):
        Settings(github_api_url=api_url, repos=["https://unrelated.example/company/repo"])


def test_scoped_connection_checks_and_repository_discovery(tmp_path, monkeypatch):
    from litellm_roi import app as app_module
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/user/list":
            return httpx.Response(200, json={"users": [], "total_pages": 1})
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "estimator"}]})
        if req.url.path == "/api/v3/orgs/company/repos":
            return httpx.Response(200, json=[{"full_name": "company/internal", "visibility": "internal"}])
        if req.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        if req.url.path == "/api/v3/repos/company/internal":
            return httpx.Response(200, json={"full_name": "company/internal"})
        raise AssertionError(req.url)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(app_module, "Gateway", lambda cfg: Gateway(cfg, transport))
    monkeypatch.setattr(app_module, "GitHub", lambda cfg: GitHub(cfg, transport))
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        assert client.get("/api/github/repos").status_code == 400
        client.put("/api/settings", json={"github_api_url": "https://github.company.example/api/v3", "github_token": "test-only", "repos": ["company/internal"]})
        assert client.post("/api/connections/test?scope=github", json={}).status_code == 200
        assert calls == ["/api/v3/repos/company/internal", "/api/v3/repos/company/internal/pulls"]
        assert client.get("/api/github/repos?org=company").json()["repos"][0]["visibility"] == "internal"
        assert client.get("/api/github/repos?org=../outside").status_code == 422
        assert client.get("/api/github/repos?page=0").status_code == 422
        calls.clear()
        client.put("/api/settings", json={"gateway_url": "https://gateway.example.com", "admin_key": "test-only", "repos": []})
        response = client.post("/api/connections/test?scope=gateway", json={})
        assert response.status_code == 200 and response.json()["models"] == ["estimator"]
        assert calls == ["/user/list", "/v1/models"]
        assert client.post("/api/connections/test?scope=github", json={}).status_code == 400
        assert client.post("/api/connections/test?scope=anything", json={}).status_code == 422


async def test_initial_backfill_progress_and_incomplete_estimates(settings, tmp_path, monkeypatch, pr):
    from litellm_roi.connectors import SourceError
    manager = SyncManager(Store(tmp_path))
    settings.update_interval_minutes = 5
    seen = []

    async def spend(*_):
        seen.append(manager.state["phase"])
        return [], {}

    async def pulls(*_):
        seen.append(manager.state["phase"])
        return [{"number": n} for n in (1, 2, 3)]

    async def evidence(_self, _repo, item):
        seen.append(manager.state["phase"])
        return {**pr, "number": item["number"]}

    async def estimate(_self, item):
        if item["number"] == 2:
            raise SourceError("Model unavailable.")
        if item["number"] == 3:
            return {"status": "needs_review", "hours": None}
        return {"status": "estimated", "hours": 4}

    monkeypatch.setattr(Gateway, "spend", spend)
    monkeypatch.setattr(GitHub, "pulls", pulls)
    monkeypatch.setattr(GitHub, "evidence", evidence)
    monkeypatch.setattr(Estimator, "estimate", estimate)
    manager.start(settings)
    await manager.task
    assert seen == ["spend", "repositories", "estimates", "estimates", "estimates"]
    assert manager.state["phase"] == "complete"
    assert manager.state["done"] == manager.state["total"] == 3
    assert manager.state["estimated"] == 1
    assert manager.state["needs_attention"] == 2
    assert manager.next_update is not None
    assert manager.store.latest() is not None


async def test_cancel_immediately_after_start_allows_retry(settings, tmp_path):
    manager = SyncManager(Store(tmp_path))
    manager.start(settings)
    await manager.cancel()
    assert not manager.state["running"]
    assert manager.state["phase"] == "cancelled"
    assert manager.next_update is None
    manager.start(settings)
    await manager.cancel()


async def test_parallel_sync_bounds_work_and_preserves_order(settings, tmp_path, monkeypatch, pr):
    manager = SyncManager(Store(tmp_path))
    entered = [asyncio.Event() for _ in range(7)]
    release = [asyncio.Event() for _ in range(7)]
    active = set()
    peak = 0

    async def spend(*_):
        return [], {}

    async def pulls(*_):
        return [{"number": n} for n in range(7)]

    async def evidence(_self, _repo, item):
        nonlocal peak
        n = item["number"]
        active.add(n)
        peak = max(peak, len(active))
        entered[n].set()
        await release[n].wait()
        return {**pr, "number": n}

    async def estimate(_self, item):
        n = item["number"]
        active.remove(n)
        return {"status": "estimated" if n != 1 else "needs_review", "hours": 4 if n != 1 else None}

    monkeypatch.setattr(Gateway, "spend", spend)
    monkeypatch.setattr(GitHub, "pulls", pulls)
    monkeypatch.setattr(GitHub, "evidence", evidence)
    monkeypatch.setattr(Estimator, "estimate", estimate)
    manager.start(settings)
    try:
        async with asyncio.timeout(5):
            await asyncio.gather(*(event.wait() for event in entered[:3]))
            assert active == {0, 1, 2}
            assert manager.state["done"] == 0 and manager.state["total"] == 7
            release[2].set()
            await entered[3].wait()
            assert active == {0, 1, 3}
            assert manager.state["done"] == manager.state["estimated"] == 1
            for event in release:
                event.set()
            await manager.task
    finally:
        await manager.cancel()
    assert peak == 3 and not active
    assert manager.state["done"] == 7
    assert manager.state["estimated"] == 6 and manager.state["needs_attention"] == 1
    report = manager.store.latest()
    assert [item["number"] for item in report["pulls"]] == list(range(7))
    assert all(not ({"files", "body", "commits"} & item.keys()) for item in report["pulls"])


@pytest.mark.parametrize("source_failure", [False, True])
async def test_parallel_workers_settle_before_close_on_cancel_or_failure(settings, tmp_path, monkeypatch, source_failure):
    from litellm_roi.connectors import SourceError
    manager = SyncManager(Store(tmp_path))
    previous = manager.store.save_report({"mode": "live", "synced_at": datetime.now(timezone.utc).isoformat(), "pulls": []})
    entered = asyncio.Event()
    fail = asyncio.Event()
    active = set()
    finished = set()
    closed = []

    async def spend(*_):
        return [], {}

    async def pulls(*_):
        return [{"number": n} for n in range(5)]

    async def evidence(_self, _repo, item):
        n = item["number"]
        active.add(n)
        if len(active) == 3:
            entered.set()
        try:
            if n == 0 and source_failure:
                await fail.wait()
                raise SourceError("GitHub unavailable.")
            await asyncio.Event().wait()
        finally:
            active.remove(n)
            finished.add(n)

    async def close(self):
        assert not active
        closed.append(type(self).__name__)
        await self.client.aclose()

    monkeypatch.setattr(Gateway, "spend", spend)
    monkeypatch.setattr(GitHub, "pulls", pulls)
    monkeypatch.setattr(GitHub, "evidence", evidence)
    for cls in (Gateway, GitHub, Estimator):
        monkeypatch.setattr(cls, "close", close)
    manager.start(settings)
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            if source_failure:
                fail.set()
                await manager.task
            else:
                await manager.cancel()
    finally:
        await manager.cancel()
    assert finished == {0, 1, 2}
    assert sorted(closed) == ["Estimator", "Gateway", "GitHub"]
    assert manager.state["phase"] == ("error" if source_failure else "cancelled")
    assert manager.state["running"] is False
    assert manager.store.latest()["id"] == previous["id"]


async def test_failed_initial_backfill_requires_explicit_retry(settings, tmp_path, monkeypatch):
    from litellm_roi.connectors import SourceError
    settings.update_interval_minutes = 5
    ConfigStore(tmp_path).save(settings.model_dump())
    app = create_app(tmp_path)

    async def failed_spend(*_):
        raise SourceError("Gateway unavailable.")

    monkeypatch.setattr(Gateway, "spend", failed_spend)
    async with app.router.lifespan_context(app):
        manager = app.state.manager
        assert manager.next_update is None
        manager.start(settings)
        await manager.task
        assert manager.state["phase"] == "error"
        assert manager.state["error"] == "Gateway unavailable."
        assert manager.store.latest() is None
        assert manager.next_update is None
