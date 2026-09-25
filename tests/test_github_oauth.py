import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from litellm_roi.app import create_app
from litellm_roi.config import ConfigStore, Settings
from litellm_roi.connectors import GitHub, SourceError
from litellm_roi.github_oauth import GitHubOAuth

CLIENT = {"client_id": "oauth-client", "client_secret": "secret-client-fixture"}
TOKENS = {"access_token": "secret-access-fixture", "refresh_token": "secret-refresh-fixture",
    "expires_in": 28800, "refresh_token_expires_in": 15552000, "scope": "repo"}


def begin(client):
    result = client.post("/api/github/oauth/connect", json={"return_to": "settings"}).json()
    query = parse_qs(urlsplit(result["url"]).query)
    assert result["url"].startswith("https://github.com/login/oauth/authorize?")
    assert query["scope"] == ["repo offline_access"]
    assert query["code_challenge_method"] == ["S256"]
    assert "secret" not in result["url"]
    return query["state"][0]


def connected_data(connection):
    return {**connection.with_tokens(CLIENT, TOKENS), "account": "engineer"}


def test_oauth_connect_and_picker_use_account_without_installation(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    connection = app.state.github_oauth
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/login/oauth/access_token":
            body = json.loads(req.content)
            assert body["client_secret"] == CLIENT["client_secret"]
            assert len(body["code_verifier"]) >= 43
            assert body["redirect_uri"] == "http://localhost/github/oauth/callback"
            return httpx.Response(200, json=TOKENS)
        assert req.headers["authorization"] == "Bearer secret-access-fixture"
        if req.url.path == "/user":
            return httpx.Response(200, json={"login": "engineer"})
        assert req.url.path == "/user/repos"
        assert req.url.params["affiliation"] == "owner,collaborator,organization_member"
        assert req.url.params["page"] == "2"
        return httpx.Response(200, json=[{"full_name": "company/internal", "visibility": "internal", "private": True}],
            headers={"Link": '<next>; rel="next"'})

    transport = httpx.MockTransport(handler)
    connection.transport = transport
    monkeypatch.setattr("litellm_roi.app.GitHub", lambda settings, **kwargs: GitHub(settings, transport, **kwargs))
    with TestClient(app, base_url="http://localhost") as client:
        assert client.post("/api/github/oauth/setup", json=CLIENT).status_code == 200
        state = begin(client)
        response = client.get("/github/oauth/callback", params={"state": state, "code": "valid-code"}, follow_redirects=False)
        assert response.headers["location"] == "/?page=settings&github=connected"
        assert ConfigStore(tmp_path).load().github_connection == "oauth"
        status = client.get("/api/github/oauth").json()
        assert status == {"configured": True, "connected": True, "account": "engineer",
            "callback_url": "http://localhost/github/oauth/callback"}
        repos = client.get("/api/github/repos?page=2").json()
        assert repos == {"repos": [{"name": "company/internal", "visibility": "internal", "archived": False}], "has_more": True}
        assert connection.path.stat().st_mode & 0o777 == 0o600
        for path in ("/api/state", "/api/github/oauth", "/api/github/app"):
            text = client.get(path).text
            assert all(secret not in text for secret in (CLIENT["client_secret"], TOKENS["access_token"], TOKENS["refresh_token"]))
        assert "secret" not in client.cookies.get("roi_github", "")
        assert client.get("/github-oauth.json").status_code == 404
        before = len(calls)
        client.get("/github/oauth/callback", params={"state": state, "code": "valid-code"})
        assert len(calls) == before
        assert all("installation" not in path for path in calls)


@pytest.mark.parametrize("attack", ["wrong_state", "expired", "no_cookie", "denied", "client_changed"])
def test_untrusted_oauth_callback_cannot_exchange_code(tmp_path, attack):
    app = create_app(tmp_path)
    connection = app.state.github_oauth
    connection.save(CLIENT)
    connection.transport = httpx.MockTransport(lambda req: pytest.fail("Untrusted callback reached GitHub"))
    with TestClient(app, base_url="http://localhost") as client:
        state = begin(client)
        code = "code"
        if attack == "wrong_state":
            state = "forged"
        elif attack == "expired":
            connection.pending[state]["deadline"] = time.time() - 1
        elif attack == "no_cookie":
            client.cookies.clear()
        elif attack == "denied":
            code = ""
        else:
            connection.save({**CLIENT, "client_id": "changed-client"})
        result = client.get("/github/oauth/callback", params={"state": state, "code": code}, follow_redirects=False)
        assert "github=error" in result.headers["location"]
        assert not connection.status()["connected"]


async def test_expiring_tokens_refresh_once_for_parallel_workers(tmp_path):
    connection = GitHubOAuth(tmp_path)
    connection.save({**connected_data(connection), "expires_at": time.time() - 1})
    calls = []

    async def handler(req):
        calls.append(req.url.path)
        assert str(req.url) == "https://github.com/login/oauth/access_token"
        body = json.loads(req.content)
        assert body == {**CLIENT, "grant_type": "refresh_token", "refresh_token": TOKENS["refresh_token"]}
        await asyncio.sleep(0)
        return httpx.Response(200, json={**TOKENS, "access_token": "new-access", "refresh_token": "new-refresh"})

    connection.transport = httpx.MockTransport(handler)
    assert await asyncio.gather(*(connection.token() for _ in range(3))) == ["new-access"] * 3
    assert calls == ["/login/oauth/access_token"]
    assert connection.load()["refresh_token"] == "new-refresh"
    assert connection.load()["account"] == "engineer"


@pytest.mark.parametrize("changes", [{"expires_at": 0}, {"client_id": "new-client"},
    {"expires_at": 1, "refresh_expires_at": 1}, {"expires_at": 1, "refresh_token": ""}])
async def test_token_lifetime_and_client_binding(tmp_path, changes):
    connection = GitHubOAuth(tmp_path, httpx.MockTransport(lambda req: pytest.fail("Unexpected token refresh")))
    connection.save({**connected_data(connection), **changes})
    if changes == {"expires_at": 0}:
        assert await connection.token() == TOKENS["access_token"]
    else:
        with pytest.raises(SourceError):
            await connection.token()


def test_setup_is_once_only_and_environment_secrets_are_not_copied(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", CLIENT["client_id"])
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", CLIENT["client_secret"])
    app = create_app(tmp_path)
    connection = app.state.github_oauth
    connection.save(connected_data(connection))
    assert CLIENT["client_secret"] not in connection.path.read_text()
    with TestClient(app, base_url="http://localhost") as client:
        before = connection.path.read_bytes()
        assert client.post("/api/github/oauth/setup", json={"client_id": "attacker", "client_secret": "attacker"}).status_code == 400
        assert connection.path.read_bytes() == before


def test_public_settings_cannot_redirect_oauth_tokens(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    connection = app.state.github_oauth
    connection.save(connected_data(connection))
    ConfigStore(tmp_path).save({"github_connection": "oauth"})
    calls = []

    def handler(req):
        calls.append(str(req.url))
        assert req.url.scheme == "https" and req.url.host == "api.github.com"
        assert req.headers["authorization"] == "Bearer secret-access-fixture"
        return httpx.Response(200, json=[])

    monkeypatch.setattr("litellm_roi.app.GitHub", lambda settings, **kwargs: GitHub(settings, httpx.MockTransport(handler), **kwargs))
    with TestClient(app, base_url="http://localhost") as client:
        assert client.put("/api/settings", json={"github_api_url": "https://attacker.example"}).status_code == 200
        assert client.get("/api/github/repos").status_code == 200
        assert len(calls) == 1


@pytest.mark.parametrize("url", ["https://attacker.example/repos/company/private", "http://api.github.com/user/repos", "https://api.github.com:8443/user/repos"])
async def test_oauth_credentials_never_reach_other_destinations(url):
    async def token():
        pytest.fail("Untrusted destination requested the token")
    github = GitHub(Settings(), httpx.MockTransport(lambda req: pytest.fail("Untrusted request was sent")), user_token_provider=token)
    try:
        with pytest.raises(SourceError):
            await github.client.get(url)
    finally:
        await github.close()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_oauth_credentials_are_not_forwarded_on_redirects(tmp_path, status):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(status, headers={"Location": "https://attacker.example/capture"})

    connection = GitHubOAuth(tmp_path, httpx.MockTransport(handler))
    for client in (connection.client(TOKENS["access_token"]), connection.client(oauth=True)):
        async with client:
            assert (await client.post("endpoint", json=CLIENT)).status_code == status
    assert len(calls) == 2


def test_demo_cannot_set_up_oauth(tmp_path):
    with TestClient(create_app(tmp_path, demo_only=True), base_url="http://localhost") as client:
        assert client.post("/api/github/oauth/setup", json=CLIENT).status_code == 403
        assert client.get("/github/oauth/callback").status_code == 404
    assert not (tmp_path / "github-oauth.json").exists()
