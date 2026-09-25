"""GitHub App repository connection. This does not authenticate dashboard visitors."""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .connectors import SourceError, payload, request

API_URL = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
APP_ENV = {"id": "GITHUB_APP_ID", "slug": "GITHUB_APP_SLUG", "client_id": "GITHUB_APP_CLIENT_ID",
    "client_secret": "GITHUB_APP_CLIENT_SECRET", "pem": "GITHUB_APP_PRIVATE_KEY"}


def save_private(path: Path, data: dict):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".github-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class GitHubApp:
    def __init__(self, root: Path, transport=None):
        self.path = root / "github-app.json"
        self.transport = transport
        self.tokens = {}
        self.repo_installations = {}
        self.lock = asyncio.Lock()
        self.pending = {}
        secret_path = root / "github-session.json"
        if not secret_path.exists():
            save_private(secret_path, {"secret": secrets.token_urlsafe(48)})
        self.session_secret = json.loads(secret_path.read_text())["secret"]

    def load(self):
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        for field, variable in APP_ENV.items():
            if value := os.environ.get(variable, "").strip():
                data[field] = value.replace("\\n", "\n") if field == "pem" else value
        return data

    @property
    def ready(self):
        return all(self.load().get(field) for field in APP_ENV)

    def status(self):
        data = self.load()
        return {"configured": self.ready, "connected": bool(data.get("installations")),
            "accounts": [item["account"] for item in data.get("installations", [])]}

    def client(self, token="", *, oauth=False):
        headers = {"Accept": "application/json"} if oauth else HEADERS.copy()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(base_url=("https://github.com" if oauth else API_URL) + "/",
            headers=headers, timeout=30, transport=self.transport, follow_redirects=False)

    def app_token(self):
        data = self.load()
        try:
            return jwt.encode({"iat": int(time.time()) - 60, "exp": int(time.time()) + 540,
                "iss": str(data["id"])}, data["pem"], algorithm="RS256")
        except (KeyError, ValueError, jwt.PyJWTError):
            raise SourceError("Check the GitHub App ID and private key in your hosting configuration.") from None

    async def installation_token(self, installation_id: int):
        async with self.lock:
            cached = self.tokens.get(installation_id)
            if cached and cached[1] > time.time() + 60:
                return cached[0]
            async with self.client(self.app_token()) as client:
                data = payload(await request(client, "POST", f"app/installations/{installation_id}/access_tokens"))
            self.tokens[installation_id] = (data["token"], datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp())
            return data["token"]

    async def token_for_repo(self, repo: str):
        key = repo.casefold()
        if key not in self.repo_installations:
            async with self.client(self.app_token()) as client:
                data = payload(await request(client, "GET", f"repos/{repo}/installation"))
            self.repo_installations[key] = data["id"]
        installation_id = self.repo_installations[key]
        if installation_id not in {item["id"] for item in self.load().get("installations", [])}:
            raise SourceError("Connect the GitHub account that owns this repository first.")
        return await self.installation_token(installation_id)

    async def repositories(self, installation_id: int, page: int):
        installs = self.load().get("installations", [])
        if installation_id not in {item["id"] for item in installs}:
            raise SourceError("Choose a connected GitHub account.")
        token = await self.installation_token(installation_id)
        async with self.client(token) as client:
            response = await request(client, "GET", "installation/repositories", params={"per_page": 100, "page": page})
        data = payload(response)
        return {"repos": [{"name": r["full_name"], "visibility": r.get("visibility") or ("private" if r["private"] else "public"),
            "archived": bool(r.get("archived"))} for r in data["repositories"]], "has_more": 'rel="next"' in response.headers.get("link", "")}

    def begin(self, req: Request, purpose: str):
        state = secrets.token_urlsafe(32)
        self.pending = {key: value for key, value in self.pending.items() if value > time.time()}
        self.pending[state] = time.time() + 1800
        req.session["flow"] = {"state": state, "purpose": purpose}
        return state

    def consume(self, req: Request, purpose: str):
        flow = req.session.pop("flow", {})
        state = req.query_params.get("state", "")
        deadline = self.pending.pop(state, 0)
        if not state or not secrets.compare_digest(state, flow.get("state", "")) or flow.get("purpose") != purpose or deadline <= time.time():
            raise SourceError("GitHub connection expired. Click Connect GitHub to try again.")

    def install_redirect(self, req: Request):
        slug = self.load().get("slug", "")
        if not re.fullmatch(r"[A-Za-z0-9-]+", slug):
            raise SourceError("Check the GitHub App slug in your hosting configuration.")
        state = self.begin(req, "install")
        return f"https://github.com/apps/{slug}/installations/new?{urlencode({'state': state})}"

    async def has_installations(self):
        # Approval may happen in an owner's browser. Discovery alone never
        # connects an account: we still verify access using the user's token.
        async with self.client(self.app_token()) as client:
            for page in range(1, 101):
                response = await request(client, "GET", "app/installations", params={"page": page, "per_page": 100})
                if any(not item.get("suspended_at") for item in payload(response)):
                    return True
                if 'rel="next"' not in response.headers.get("link", ""):
                    return False
        raise SourceError("GitHub returned too many installations. Try managing access on GitHub.")

    def oauth_redirect(self, req: Request, base: str, installation_id=None):
        req.session.pop("installation", None)
        if installation_id is not None:
            req.session["installation"] = installation_id
        state = self.begin(req, "oauth")
        verifier = secrets.token_urlsafe(48)
        req.session["verifier"] = verifier
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = urlencode({"client_id": self.load()["client_id"], "redirect_uri": base + "/github/callback",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        return "https://github.com/login/oauth/authorize?" + query

    def install(self, app: FastAPI, config, origin, *, secure: bool):
        app.add_middleware(SessionMiddleware, secret_key=self.session_secret,
            session_cookie="__Host-roi_github" if secure else "roi_github",
            max_age=1800, same_site="lax", https_only=secure)

        def destination(req, error="", *, pending=False):
            route = "/?" + urlencode({"page": req.session.pop("return_to", "setup"),
                "github": "pending" if pending else "error" if error else "connected", **({"reason": error} if error else {})})
            return RedirectResponse(route, status_code=303)

        @app.get("/api/github/app")
        async def status():
            return {**self.status(), "installations": self.load().get("installations", [])}

        @app.post("/api/github/connect")
        async def connect(req: Request):
            body = await req.json()
            req.session["return_to"] = "settings" if body.get("return_to") == "settings" else "setup"
            if self.ready:
                if body.get("mode") != "manage":
                    if await self.has_installations():
                        return {"url": self.oauth_redirect(req, origin(req))}
                    if body.get("mode") == "check":
                        return {"pending": True}
                return {"url": self.install_redirect(req)}
            base = origin(req)
            state = self.begin(req, "manifest")
            return {"action": f"https://github.com/settings/apps/new?{urlencode({'state': state})}", "manifest": {
                "name": "LiteLLM ROI " + secrets.token_hex(4), "url": base,
                "description": "Compare LiteLLM spend with estimated engineering hours from selected pull requests.",
                "hook_attributes": {"url": base + "/github/events", "active": False},
                "redirect_url": base + "/github/app/callback", "callback_urls": [base + "/github/callback"],
                "setup_url": base + "/github/installed", "setup_on_update": True,
                "request_oauth_on_install": False, "public": True,
                "default_permissions": {"contents": "read", "pull_requests": "read", "metadata": "read"},
                "default_events": []}}

        @app.get("/github/app/callback")
        async def manifest_callback(req: Request):
            try:
                self.consume(req, "manifest")
                code = req.query_params.get("code", "")
                if self.ready or not re.fullmatch(r"[A-Za-z0-9_\-]{1,512}", code):
                    raise SourceError("The GitHub App is already configured or the registration expired.")
                async with self.client() as client:
                    data = payload(await request(client, "POST", f"app-manifests/{code}/conversions"))
                # App credentials stay on the server; never send them to the frontend.
                saved = {field: data[field] for field in APP_ENV}
                save_private(self.path, saved)
                return RedirectResponse(self.install_redirect(req), status_code=303)
            except SourceError:
                return destination(req, "registration")

        @app.get("/github/installed")
        async def installed(req: Request):
            try:
                self.consume(req, "install")
                installation = req.query_params.get("installation_id", "")
                if not installation.isdigit():
                    return destination(req, pending=True)
                return RedirectResponse(self.oauth_redirect(req, origin(req), int(installation)), status_code=303)
            except SourceError:
                return destination(req, "expired")

        @app.get("/github/callback")
        async def callback(req: Request):
            try:
                self.consume(req, "oauth")
                code = req.query_params.get("code", "")
                verifier = req.session.pop("verifier", "")
                installation_id = req.session.pop("installation", None)
                if not code or not verifier:
                    raise SourceError("GitHub connection was not completed.")
                data = self.load()
                async with self.client(oauth=True) as client:
                    result = payload(await request(client, "POST", "login/oauth/access_token", json={
                        "client_id": data["client_id"], "client_secret": data["client_secret"], "code": code,
                        "redirect_uri": origin(req) + "/github/callback", "code_verifier": verifier}))
                if not result.get("access_token"):
                    raise SourceError("GitHub did not authorize this connection.")
                found = []
                # Check membership using the user token, never trust a callback's installation ID.
                async with self.client(result["access_token"]) as client:
                    for page in range(1, 101):
                        response = await request(client, "GET", "user/installations", params={"page": page, "per_page": 100})
                        for item in payload(response)["installations"]:
                            if ((installation_id is None or item["id"] == installation_id)
                                and str(item["app_id"]) == str(data["id"]) and not item.get("suspended_at")):
                                found.append({"id": item["id"], "account": item["account"]["login"]})
                        if (installation_id is not None and found) or 'rel="next"' not in response.headers.get("link", ""):
                            break
                    else:
                        raise SourceError("GitHub returned too many accounts. Connect one account at a time.")
                if not found:
                    if installation_id is None:
                        return RedirectResponse(self.install_redirect(req), status_code=303)
                    raise SourceError("This GitHub installation is not available to your account.")
                data = self.load()
                installations = {item["id"]: item for item in data.get("installations", [])}
                installations.update({item["id"]: item for item in found})
                data["installations"] = list(installations.values())
                save_private(self.path, data)
                self.tokens.clear()
                self.repo_installations.clear()
                config.save({"github_connection": "app", "github_api_url": API_URL})
                return destination(req)
            except SourceError:
                return destination(req, "authorization")
            finally:
                req.session.pop("verifier", None)
                req.session.pop("installation", None)
