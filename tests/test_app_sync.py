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
        response = client.put("/api/settings", json={"gateway_url": "https://gateway.example.com", "admin_key": "never-return-this",
            "github_token": "nor-this-token", "repos": ["org/repo"], "estimator_model": "test", "backfill_days": 90, "update_interval_minutes": 360})
        assert response.status_code == 200
        assert response.json()["backfill_days"] == 90
        assert response.json()["update_interval_minutes"] == 360
        assert "never-return-this" not in response.text
        assert "nor-this-token" not in client.get("/api/state").text
        assert client.get("/api/state").json()["status"]["next_update"] is not None
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
    assert manager.next_update == now + timedelta(hours=6)


async def test_full_sync_caches_estimates_and_preserves_previous_report_on_source_failure(settings, tmp_path, monkeypatch):
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
