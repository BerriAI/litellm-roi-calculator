import json

import httpx
import pytest
from fastapi.testclient import TestClient

from litellm_roi.app import create_app
from litellm_roi.config import ENV_FIELDS, ConfigStore
from litellm_roi.connectors import Gateway, GitHub
from litellm_roi.estimator import Estimator
from litellm_roi.github_app import GitHubApp, save_private
from litellm_roi.storage import Store


@pytest.mark.parametrize(("secret_field", "url_field"), [
    ("admin_key", "gateway_url"), ("estimator_key", "gateway_url"), ("github_token", "github_api_url"),
])
def test_public_settings_cannot_redirect_environment_credentials(tmp_path, settings, monkeypatch, secret_field, url_field):
    config = ConfigStore(tmp_path)
    config.save(settings.model_dump())
    secret = "environment-only-credential-fixture"
    monkeypatch.setenv(ENV_FIELDS[secret_field], secret)
    monkeypatch.setenv("ROI_PUBLIC_URL", "https://roi.example.com")
    with TestClient(create_app(tmp_path), base_url="https://roi.example.com") as client:
        state = client.get("/api/state")
        assert secret not in state.text
        assert url_field in state.json()["environment_fields"]
        before = config.path.read_bytes()
        # Even supplying a new key cannot rebind the environment's credential.
        result = client.put("/api/settings", headers={"Origin": "https://roi.example.com"},
            json={url_field: "https://other.example.com", secret_field: "visitor-key"})
        assert result.status_code == 400
        assert "locked" in result.text and secret not in result.text
        assert config.path.read_bytes() == before
        assert getattr(config.load(), url_field) == getattr(settings, url_field)

        # Public calculator controls remain editable without authentication.
        result = client.put("/api/settings", headers={"Origin": "https://roi.example.com"},
            json={"backfill_days": 14, "estimator_prompt": "Estimate the effort."})
        assert result.status_code == 200
        assert result.json()["backfill_days"] == 14
        assert secret not in result.text and secret not in config.path.read_text()
        assert json.loads(config.path.read_text())[secret_field] == ""
        assert getattr(config.load(), secret_field) == secret


def test_saved_credentials_not_available_through_public_api_or_files(tmp_path, settings, monkeypatch):
    config = ConfigStore(tmp_path)
    config.save(settings.model_dump())
    monkeypatch.setenv("ROI_PUBLIC_URL", "https://roi.example.com")
    save_private(tmp_path / "github-app.json", {"client_secret": "app-secret-fixture", "pem": "private-key-fixture"})
    secrets = [settings.admin_key, settings.estimator_key, settings.github_token, "app-secret-fixture", "private-key-fixture"]
    with TestClient(create_app(tmp_path), base_url="https://roi.example.com") as client:
        for path in ("/api/state", "/api/state?mode=demo", "/api/github/app", "/api/export?mode=demo"):
            response = client.get(path)
            assert response.status_code == 200
            assert all(secret not in response.text for secret in secrets)
        for path in ("/config.json", "/github-app.json", "/github-session.json",
                "/assets/config.json", "/assets/%2e%2e/config.json", "/assets/%2e%2e/github-app.json"):
            response = client.get(path)
            assert response.status_code == 404
            assert all(secret not in response.text for secret in secrets)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_credentials_are_never_forwarded_through_redirects(settings, tmp_path, status):
    calls = []

    def handler(req):
        assert req.url.host in {"gateway.example.com", "api.github.com", "github.com"}
        calls.append(req.url.host)
        return httpx.Response(status, headers={"Location": "https://other.example.com/capture"})

    transport = httpx.MockTransport(handler)
    gateway = Gateway(settings, transport)
    github = GitHub(settings, transport)
    estimator = Estimator(settings, Store(tmp_path), transport)
    connection = GitHubApp(tmp_path, transport)
    clients = [gateway.client, github.client, estimator.client,
        connection.client("installation-fixture"), connection.client(oauth=True)]
    try:
        for client in clients:
            response = await client.get("endpoint")
            assert response.status_code == status
        assert len(calls) == len(clients)
    finally:
        for client in clients:
            await client.aclose()
