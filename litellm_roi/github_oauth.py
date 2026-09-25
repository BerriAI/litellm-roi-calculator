"""GitHub OAuth repository connection; dashboard access remains independent."""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from .connectors import SourceError, payload, request
from .github_app import API_URL, HEADERS, save_private

OAUTH_ENV = {"client_id": "GITHUB_OAUTH_CLIENT_ID", "client_secret": "GITHUB_OAUTH_CLIENT_SECRET"}


class GitHubOAuth:
    def __init__(self, root: Path, transport=None):
        self.path = root / "github-oauth.json"
        self.transport = transport
        self.pending = {}
        self.lock = asyncio.Lock()

    def load(self):
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        for field, variable in OAUTH_ENV.items():
            if value := os.environ.get(variable, "").strip():
                data[field] = value
        return data

    def save(self, data):
        stored = dict(data)
        for field, variable in OAUTH_ENV.items():
            if os.environ.get(variable, "").strip():
                stored.pop(field, None)
        save_private(self.path, stored)

    def status(self):
        data = self.load()
        configured = bool(data.get("client_id") and data.get("client_secret"))
        connected = configured and bool(data.get("access_token")) and data.get("token_client_id") == data["client_id"]
        return {"configured": configured, "connected": connected,
            "account": data.get("account", "") if connected else ""}

    def client(self, token="", *, oauth=False):
        headers = {"Accept": "application/json"} if oauth else HEADERS.copy()
        if token:
            headers["Authorization"] = "Bearer " + token
        return httpx.AsyncClient(base_url="https://github.com/" if oauth else API_URL + "/",
            headers=headers, timeout=30, transport=self.transport, follow_redirects=False)

    def with_tokens(self, data, result):
        if not isinstance(result.get("access_token"), str) or not result["access_token"]:
            raise SourceError("GitHub did not authorize the connection. Connect GitHub again.")
        updated = {**data, "access_token": result["access_token"], "token_client_id": data["client_id"],
            "refresh_token": result.get("refresh_token", ""), "scope": result.get("scope", "")}
        try:
            updated["expires_at"] = time.time() + int(result["expires_in"]) if result.get("expires_in") else 0
            updated["refresh_expires_at"] = time.time() + int(result["refresh_token_expires_in"]) if result.get("refresh_token_expires_in") else 0
        except (TypeError, ValueError):
            raise SourceError("GitHub returned an invalid token expiry. Connect GitHub again.") from None
        return updated

    async def token(self):
        async with self.lock:
            data = self.load()
            if not self.status()["connected"]:
                raise SourceError("Connect GitHub to read your repositories.")
            if not data.get("expires_at") or data["expires_at"] > time.time() + 60:
                return data["access_token"]
            if not data.get("refresh_token") or (data.get("refresh_expires_at") and data["refresh_expires_at"] <= time.time()):
                raise SourceError("Your GitHub connection expired. Connect GitHub again.")
            async with self.client(oauth=True) as client:
                result = payload(await request(client, "POST", "login/oauth/access_token", json={
                    "client_id": data["client_id"], "client_secret": data["client_secret"],
                    "grant_type": "refresh_token", "refresh_token": data["refresh_token"]}))
            updated = self.with_tokens(data, result)
            self.save(updated)
            return updated["access_token"]

    def install(self, app: FastAPI, config, origin):
        # SessionMiddleware is shared with the legacy GitHub App connector.
        @app.get("/api/github/oauth")
        async def status(req: Request):
            return {**self.status(), "callback_url": origin(req) + "/github/oauth/callback"}

        @app.post("/api/github/oauth/setup")
        async def setup(req: Request):
            if self.status()["configured"]:
                raise SourceError("GitHub OAuth is already configured. The host can change its configuration on the server.")
            body = await req.json()
            data = self.load()
            for field, variable in OAUTH_ENV.items():
                value = str(body.get(field, "")).strip()
                if os.environ.get(variable, "").strip():
                    continue
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", value):
                    raise SourceError("Enter the client ID and client secret from your GitHub OAuth app.")
                data[field] = value
            self.save(data)
            return self.status()

        @app.post("/api/github/oauth/connect")
        async def connect(req: Request):
            if not self.status()["configured"]:
                raise SourceError("Configure GitHub OAuth once for this calculator before connecting.")
            body = await req.json()
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            self.pending = {key: value for key, value in self.pending.items() if value["deadline"] > time.time()}
            self.pending[state] = {"deadline": time.time() + 1800, "verifier": verifier,
                "client_id": self.load()["client_id"], "return_to": "settings" if body.get("return_to") == "settings" else "setup"}
            req.session["oauth_state"] = state
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            query = urlencode({"client_id": self.load()["client_id"], "redirect_uri": origin(req) + "/github/oauth/callback",
                "scope": "repo offline_access", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
            return {"url": "https://github.com/login/oauth/authorize?" + query}

        @app.get("/github/oauth/callback")
        async def callback(req: Request):
            state = req.query_params.get("state", "")
            cookie_state = req.session.pop("oauth_state", "")
            flow = self.pending.pop(state, {}) if state and secrets.compare_digest(state, cookie_state) else {}
            error = ""
            try:
                if not flow or flow["deadline"] <= time.time():
                    raise SourceError("GitHub connection expired.")
                data = self.load()
                code = req.query_params.get("code", "")
                if data.get("client_id") != flow["client_id"] or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", code):
                    raise SourceError("GitHub connection was not completed.")
                async with self.client(oauth=True) as client:
                    result = payload(await request(client, "POST", "login/oauth/access_token", json={
                        "client_id": data["client_id"], "client_secret": data["client_secret"], "code": code,
                        "redirect_uri": origin(req) + "/github/oauth/callback", "code_verifier": flow["verifier"]}))
                updated = self.with_tokens(data, result)
                async with self.client(updated["access_token"]) as client:
                    user = payload(await request(client, "GET", "user"))
                if not isinstance(user.get("login"), str) or not user["login"]:
                    raise SourceError("GitHub did not return your account.")
                updated["account"] = user["login"]
                # Validate settings first, so a server-managed Enterprise URL
                # cannot leave a partially switched connection behind.
                async with self.lock:
                    config.save({"github_connection": "oauth", "github_api_url": API_URL})
                    self.save(updated)
            except (SourceError, ValueError):
                error = "authorization"
            route = "/?" + urlencode({"page": flow.get("return_to", "setup"),
                "github": "error" if error else "connected", **({"reason": error} if error else {})})
            return RedirectResponse(route, status_code=303)
