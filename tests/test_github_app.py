import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from litellm_roi.app import create_app
from litellm_roi.config import ConfigStore, Settings
from litellm_roi.connectors import GitHub, SourceError
from litellm_roi.github_app import GitHubApp, save_private


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


@pytest.fixture
def app_credentials(private_key):
    return {"id": 123, "slug": "test-roi-app", "pem": private_key, "client_id": "test-client", "client_secret": "never-expose-client-secret"}


def state_from(url):
    return parse_qs(urlsplit(url).query)["state"][0]


def begin_install(client, *, manifest=False):
    result = client.post("/api/github/connect", json={"return_to": "setup", "mode": "manage"}).json()
    if manifest:
        response = client.get("/github/app/callback", params={"code": "test-code", "state": state_from(result["action"])}, follow_redirects=False)
        assert response.status_code == 303
        url = response.headers["location"]
    else:
        url = result["url"]
    return state_from(url)


def begin_oauth(client, state, installation=7):
    response = client.get("/github/installed", params={"installation_id": installation, "state": state}, follow_redirects=False)
    assert response.status_code == 303
    url = response.headers["location"]
    assert "code_challenge_method=S256" in url
    return state_from(url)


def test_connect_manifest_and_verified_installation(tmp_path, app_credentials):
    app = create_app(tmp_path)
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/app-manifests/test-code/conversions":
            return httpx.Response(201, json=app_credentials)
        if req.url.path == "/login/oauth/access_token":
            body = json.loads(req.content)
            assert len(body["code_verifier"]) >= 43
            assert body["redirect_uri"] == "http://localhost/github/callback"
            return httpx.Response(200, json={"access_token": "never-expose-user-token"})
        if req.url.path == "/user/installations":
            assert req.headers["authorization"] == "Bearer never-expose-user-token"
            return httpx.Response(200, json={"installations": [{"id": 7, "app_id": 123, "account": {"login": "company"}}]})
        raise AssertionError(req.url)

    app.state.github_app.transport = httpx.MockTransport(handler)
    with TestClient(app, base_url="http://localhost") as client:
        draft = client.post("/api/github/connect", json={}).json()
        assert draft["manifest"]["default_permissions"] == {"contents": "read", "pull_requests": "read", "metadata": "read"}
        assert draft["manifest"]["hook_attributes"]["active"] is False
        state = begin_install(client, manifest=True)
        assert (tmp_path / "github-app.json").stat().st_mode & 0o777 == 0o600
        oauth_state = begin_oauth(client, state)
        response = client.get("/github/callback", params={"state": oauth_state, "code": "authorization-code"}, follow_redirects=False)
        assert response.headers["location"] == "/?page=setup&github=connected"
        public = client.get("/api/github/app")
        assert public.json()["installations"] == [{"id": 7, "account": "company"}]
        assert "never-expose" not in public.text
        assert "pem" not in public.text
        assert "never-expose" not in client.cookies.get("roi_github", "")
        assert ConfigStore(tmp_path).load().github_connection == "app"
        assert app.state.github_app.tokens == {}
        assert "access_token" not in (tmp_path / "github-app.json").read_text()
        # The consumed state cannot be replayed, even with the same cookie.
        count = len(calls)
        client.get("/github/callback", params={"state": oauth_state, "code": "authorization-code"}, follow_redirects=False)
        assert len(calls) == count


@pytest.mark.parametrize("attack", ["wrong_state", "expired", "no_cookie"])
def test_registration_checks_state_before_exchanging_code(tmp_path, attack):
    app = create_app(tmp_path)
    app.state.github_app.transport = httpx.MockTransport(lambda req: pytest.fail("Untrusted callback reached GitHub"))
    with TestClient(app, base_url="http://localhost") as client:
        draft = client.post("/api/github/connect", json={}).json()
        state = state_from(draft["action"])
        if attack == "wrong_state":
            state = "forged"
        elif attack == "expired":
            app.state.github_app.pending[state] = time.time() - 1
        else:
            client.cookies.clear()
        response = client.get("/github/app/callback", params={"state": state, "code": "test-code"}, follow_redirects=False)
        assert "github=error" in response.headers["location"]
        assert not app.state.github_app.ready


@pytest.mark.parametrize("installation", [
    {"id": 8, "app_id": 123, "account": {"login": "other"}},
    {"id": 7, "app_id": 999, "account": {"login": "other"}},
    {"id": 7, "app_id": 123, "account": {"login": "other"}, "suspended_at": "2026-01-01"},
])
def test_forged_or_suspended_installation_is_never_connected(tmp_path, app_credentials, installation):
    app = create_app(tmp_path)
    save_private(app.state.github_app.path, app_credentials)

    def handler(req):
        if req.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "user-token"})
        return httpx.Response(200, json={"installations": [installation]})

    app.state.github_app.transport = httpx.MockTransport(handler)
    with TestClient(app, base_url="http://localhost") as client:
        state = begin_oauth(client, begin_install(client))
        response = client.get("/github/callback", params={"state": state, "code": "code"}, follow_redirects=False)
        assert "github=error" in response.headers["location"]
        assert not app.state.github_app.status()["connected"]
        assert ConfigStore(tmp_path).load().github_connection == "token"


def test_reconnect_discovers_approved_accounts_without_reinstallation(tmp_path, app_credentials):
    app = create_app(tmp_path)
    save_private(app.state.github_app.path, app_credentials)
    account = {"id": 7, "app_id": 123, "account": {"login": "company"}}

    def handler(req):
        if req.url.path == "/app/installations":
            return httpx.Response(200, json=[account])
        if req.url.path == "/login/oauth/access_token":
            assert json.loads(req.content)["code_verifier"]
            return httpx.Response(200, json={"access_token": "temporary-user-token"})
        assert req.url.path == "/user/installations"
        assert req.headers["authorization"] == "Bearer temporary-user-token"
        if req.url.params["page"] == "1":
            return httpx.Response(200, json={"installations": [
                {**account, "id": 8, "app_id": 999}, {**account, "id": 9, "suspended_at": "2026-01-01"},
            ]}, headers={"Link": '<next>; rel="next"'})
        return httpx.Response(200, json={"installations": [account]})

    app.state.github_app.transport = httpx.MockTransport(handler)
    with TestClient(app, base_url="http://localhost") as client:
        result = client.post("/api/github/connect", json={"return_to": "settings"}).json()
        assert result["url"].startswith("https://github.com/login/oauth/authorize?")
        assert not app.state.github_app.status()["connected"]  # Discovery is not consent.
        response = client.get("/github/callback", params={"state": state_from(result["url"]), "code": "code"}, follow_redirects=False)
        assert response.headers["location"] == "/?page=settings&github=connected"
        assert app.state.github_app.load()["installations"] == [{"id": 7, "account": "company"}]
        assert "temporary-user-token" not in (tmp_path / "github-app.json").read_text()
        assert "temporary-user-token" not in client.cookies.get("roi_github", "")


def test_reconnect_cannot_claim_another_users_installation(tmp_path, app_credentials):
    app = create_app(tmp_path)
    save_private(app.state.github_app.path, app_credentials)

    def handler(req):
        if req.url.path == "/app/installations":
            return httpx.Response(200, json=[{"id": 7}])
        if req.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "user-token"})
        assert req.url.path == "/user/installations"
        return httpx.Response(200, json={"installations": []})

    app.state.github_app.transport = httpx.MockTransport(handler)
    with TestClient(app, base_url="http://localhost") as client:
        result = client.post("/api/github/connect", json={}).json()
        response = client.get("/github/callback", params={"state": state_from(result["url"]), "code": "code"}, follow_redirects=False)
        assert response.headers["location"].startswith("https://github.com/apps/test-roi-app/installations/new?")
        assert not app.state.github_app.status()["connected"]
        assert ConfigStore(tmp_path).load().github_connection == "token"


def test_approval_wait_can_be_checked_without_restarting_installation(tmp_path, app_credentials):
    app = create_app(tmp_path)
    save_private(app.state.github_app.path, app_credentials)

    def handler(req):
        assert req.url.path == "/app/installations"
        return httpx.Response(200, json=[])

    app.state.github_app.transport = httpx.MockTransport(handler)
    with TestClient(app, base_url="http://localhost") as client:
        state = begin_install(client)
        response = client.get("/github/installed", params={"state": state, "setup_action": "request"}, follow_redirects=False)
        assert response.headers["location"] == "/?page=setup&github=pending"
        assert client.post("/api/github/connect", json={"mode": "check"}).json() == {"pending": True}
        assert not app.state.github_app.status()["connected"]
        result = client.post("/api/github/connect", json={}).json()
        assert result["url"].startswith("https://github.com/apps/test-roi-app/installations/new?")


@pytest.mark.parametrize("attack", ["wrong_state", "expired", "no_cookie"])
def test_reconnect_checks_state_before_exchanging_code(tmp_path, app_credentials, attack):
    app = create_app(tmp_path)
    save_private(app.state.github_app.path, app_credentials)
    app.state.github_app.transport = httpx.MockTransport(lambda req: httpx.Response(200, json=[{"id": 7}]))
    with TestClient(app, base_url="http://localhost") as client:
        result = client.post("/api/github/connect", json={}).json()
        state = state_from(result["url"])
        if attack == "wrong_state":
            state = "forged"
        elif attack == "expired":
            app.state.github_app.pending[state] = time.time() - 1
        else:
            client.cookies.clear()
        app.state.github_app.transport = httpx.MockTransport(lambda req: pytest.fail("Untrusted callback reached GitHub"))
        response = client.get("/github/callback", params={"state": state, "code": "code"}, follow_redirects=False)
        assert "github=error" in response.headers["location"]
        assert not app.state.github_app.status()["connected"]


async def test_background_tokens_refresh_and_repository_scope(tmp_path, app_credentials, private_key):
    connection = GitHubApp(tmp_path)
    save_private(connection.path, {**app_credentials, "installations": [{"id": 7, "account": "company"}]})
    issued = []

    def handler(req):
        claims = jwt.decode(req.headers["authorization"].removeprefix("Bearer "),
            serialization.load_pem_private_key(private_key.encode(), password=None).public_key(), algorithms=["RS256"])
        assert claims["iss"] == "123"
        if req.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 8 if "unconnected" in req.url.path else 7})
        issued.append(req.url.path)
        return httpx.Response(201, json={"token": f"installation-token-{len(issued)}",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})

    connection.transport = httpx.MockTransport(handler)
    assert await connection.token_for_repo("company/repo") == "installation-token-1"
    assert await connection.token_for_repo("company/second") == "installation-token-1"
    connection.tokens[7] = ("expired", time.time() - 1)
    assert await connection.token_for_repo("company/repo") == "installation-token-2"
    with pytest.raises(SourceError, match="owns this repository"):
        await connection.token_for_repo("unconnected/repo")
    assert len(issued) == 2


async def test_installation_repository_picker_uses_only_connected_accounts(tmp_path, app_credentials):
    connection = GitHubApp(tmp_path)
    save_private(connection.path, {**app_credentials, "installations": [{"id": 7, "account": "company"}]})
    connection.tokens[7] = ("installation-token", time.time() + 3600)

    def handler(req):
        assert req.url.path == "/installation/repositories"
        assert req.url.params["page"] == "2"
        assert req.headers["authorization"] == "Bearer installation-token"
        return httpx.Response(200, json={"repositories": [{"full_name": "company/internal", "private": True, "visibility": "internal"}]}, headers={"Link": '<next>; rel="next"'})

    connection.transport = httpx.MockTransport(handler)
    with pytest.raises(SourceError):
        await connection.repositories(8, 1)
    result = await connection.repositories(7, 2)
    assert result == {"repos": [{"name": "company/internal", "visibility": "internal", "archived": False}], "has_more": True}


async def test_app_repository_credentials_not_forwarded_to_other_hosts_or_profiles():
    calls = []

    async def token_for_repo(repo):
        calls.append(repo)
        return "scoped-" + repo

    def handler(req):
        if req.url.path.startswith("/repos/"):
            assert req.headers["authorization"] == "Bearer scoped-company/repo"
        else:
            assert "authorization" not in req.headers
        return httpx.Response(200, json=[])

    github = GitHub(Settings(github_token="old-manual-token"), httpx.MockTransport(handler), token_provider=token_for_repo)
    try:
        await github.client.get("repos/company/repo/pulls")
        await github.client.get("users/person")
        with pytest.raises(SourceError, match="Enterprise"):
            await github.client.get("https://untrusted.example/repos/company/repo/pulls")
    finally:
        await github.close()
    assert calls == ["company/repo"]


def test_hosted_origin_without_dashboard_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://roi.example.com")
    with TestClient(create_app(tmp_path), base_url="https://roi.example.com") as client:
        assert client.get("/api/state").status_code == 200  # Authentication belongs to the hosting layer.
        assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
        assert client.get("/health", headers={"Host": "render-health"}).status_code == 200
        for origin in [None, "https://evil.example", "http://roi.example.com"]:
            headers = {"Origin": origin} if origin else {}
            assert client.put("/api/settings", json={}, headers=headers).status_code == 403
        response = client.post("/api/github/connect", json={}, headers={"Origin": "https://roi.example.com"})
        assert response.status_code == 200
        assert response.json()["manifest"]["redirect_url"] == "https://roi.example.com/github/app/callback"
        cookie = response.headers["set-cookie"]
        assert "__Host-roi_github=" in cookie
        assert all(attribute in cookie.lower() for attribute in ("secure", "httponly", "samesite=lax"))


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com", "https://example.com/path", "https://example.com?x=y"])
def test_reject_invalid_public_origin(tmp_path, monkeypatch, url):
    monkeypatch.setenv("ROI_PUBLIC_URL", url)
    with pytest.raises(ValueError, match="HTTPS origin"):
        create_app(tmp_path)


def test_demo_does_not_register_github_connection_or_create_credentials(tmp_path):
    root = tmp_path / "demo"
    with TestClient(create_app(root, demo_only=True), base_url="http://localhost") as client:
        assert client.post("/api/github/connect", json={}).status_code == 403
        assert client.get("/github/callback?code=secret").status_code == 404
        assert not root.exists()
