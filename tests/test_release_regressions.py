import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from litellm_roi import app as app_module
from litellm_roi import cli
from litellm_roi.app import create_app
from litellm_roi.config import ENV_FIELDS, ConfigStore, Settings
from litellm_roi.storage import Store


@pytest.mark.parametrize("empty", ["", " \t "])
def test_empty_environment_values_leave_saved_settings_editable(tmp_path, monkeypatch, settings, empty):
    ConfigStore(tmp_path).save(settings.model_dump())
    for variable in [*ENV_FIELDS.values(), "GITHUB_REPOS"]:
        monkeypatch.setenv(variable, empty)
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        state = client.get("/api/state").json()
        assert state["environment_fields"] == []
        assert state["settings"]["ready"]
        assert state["settings"]["github_api_url"] == "https://api.github.com"
        response = client.put("/api/settings", json={"admin_key": "new-fixture-key",
            "estimator_model": "new-model", "repos": ["other/repo"]})
        assert response.status_code == 200
        assert response.json()["has_admin_key"]
        assert response.json()["estimator_model"] == "new-model"
        assert response.json()["repos"] == ["other/repo"]
    assert ConfigStore(tmp_path).load().admin_key == "new-fixture-key"


def test_example_environment_does_not_lock_first_run_setup(tmp_path, monkeypatch):
    example = Path(__file__).parents[1] / ".env.example"
    for name, value in dotenv_values(example).items():
        if value is not None:
            monkeypatch.setenv(name, value)
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        state = client.get("/api/state").json()
        assert state["environment_fields"] == []
        assert not state["settings"]["ready"]
        result = client.put("/api/settings", json={"gateway_url": "https://fixture.invalid",
            "admin_key": "fixture-key", "estimator_model": "fixture-model", "repos": ["example/repo"]})
        assert result.status_code == 200 and result.json()["ready"]
        assert client.get("/api/state").json()["status"]["next_update"] is None


def test_nonempty_environment_values_still_override_and_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_GATEWAY_URL", " https://gateway.example.com ")
    monkeypatch.setenv("LITELLM_ADMIN_KEY", "fixture-key")
    monkeypatch.setenv("GITHUB_REPOS", " company/one, company/two ")
    monkeypatch.setenv("LITELLM_ESTIMATOR_MODEL", " ")
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        state = client.get("/api/state").json()
        assert set(state["environment_fields"]) == {"gateway_url", "admin_key", "repos"}
        assert state["settings"]["gateway_url"] == "https://gateway.example.com"
        assert state["settings"]["repos"] == ["company/one", "company/two"]
        assert state["settings"]["has_admin_key"]
        assert "fixture-key" not in client.get("/api/state").text


@pytest.mark.parametrize("configuration", ["overdue", "invalid"])
def test_cli_demo_never_opens_workspace_or_calls_connected_services(tmp_path, monkeypatch, settings, configuration):
    settings.update_interval_minutes = 5
    ConfigStore(tmp_path).save(settings.model_dump())
    Store(tmp_path).save_report({"mode": "live",
        "synced_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        "private_marker": "never-export-real-workspace"})
    if configuration == "invalid":
        (tmp_path / "config.json").write_text("invalid saved JSON")
        monkeypatch.setenv("LITELLM_GATEWAY_URL", "not a URL")
    else:
        monkeypatch.setenv("LITELLM_GATEWAY_URL", settings.gateway_url)
        monkeypatch.setenv("LITELLM_ADMIN_KEY", settings.admin_key)
    original = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    def forbidden(*_args, **_kwargs):
        pytest.fail("Demo must not construct a config store, report store, scheduler, or connector.")

    for name in ("ConfigStore", "Store", "SyncManager", "Gateway", "GitHub"):
        monkeypatch.setattr(app_module, name, forbidden)
    monkeypatch.setattr(cli, "load_dotenv", lambda: False)
    monkeypatch.setattr(sys, "argv", ["litellm-roi", "--demo", "--no-browser", "--data-dir", str(tmp_path)])
    started = []

    def fake_server(app, **_kwargs):
        started.append(True)
        with TestClient(app, base_url="http://localhost") as client:
            for path in ("/api/state", "/api/state?mode=live", "/api/state?mode=demo"):
                state = client.get(path).json()
                assert state["demo_only"] is True
                assert state["report"]["mode"] == "demo"
                assert state["settings"] == Settings().public()
                assert state["environment_fields"] == []
                assert state["status"]["running"] is False
                assert state["status"]["next_update"] is None
            for method, path in [("POST", "/api/sync"), ("DELETE", "/api/sync"),
                    ("PUT", "/api/settings"), ("POST", "/api/connections/test"),
                    ("GET", "/api/models"), ("GET", "/api/github/repos?org=company")]:
                response = client.request(method, path, json={})
                assert response.status_code == 403
                assert "Restart without --demo" in response.json()["detail"]
            for path in ("/api/export", "/api/export?mode=live", "/api/export?mode=demo"):
                response = client.get(path)
                assert response.status_code == 200
                assert "alex@example.com" in response.text
                assert "never-export-real-workspace" not in response.text
            assert client.get("/").status_code == 200
            assert client.get("/health").status_code == 200

    monkeypatch.setattr(cli.uvicorn, "run", fake_server)
    cli.main()
    assert started == [True]
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == original


def test_sample_view_does_not_expose_live_settings_or_sync_status(tmp_path, settings):
    ConfigStore(tmp_path).save(settings.model_dump())
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        state = client.get("/api/state?mode=demo").json()
        assert state["demo_only"] is False
        assert not state["settings"]["has_admin_key"]
        assert state["settings"]["repos"] == []
        assert state["status"]["next_update"] is None
        live = client.get("/api/state").json()
        assert live["settings"]["has_admin_key"]
        assert live["settings"]["repos"] == settings.repos
