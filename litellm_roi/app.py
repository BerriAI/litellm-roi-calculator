import asyncio
import csv
import io
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .analytics import summarize
from .config import DEFAULT_PROMPT, ConfigStore, CredentialDestinationError, Settings, environment_fields
from .connectors import Gateway, GitHub, SourceError, request
from .github_app import GitHubApp
from .github_oauth import GitHubOAuth
from .storage import Store
from .sync import IDLE_STATE, SyncManager, demo_report, utcnow

ASSETS = Path(__file__).parent / "static"


def create_app(data_dir: Path | None = None, *, demo_only: bool = False) -> FastAPI:
    public_url = "" if demo_only else (os.environ.get("ROI_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if public_url:
        parsed = urlsplit(public_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("ROI_PUBLIC_URL must be an HTTPS origin without a path, query, or credentials.")

    def origin(req):
        return public_url or str(req.base_url).rstrip("/")

    def make_github(settings):
        if settings.github_connection == "oauth":
            # OAuth credentials are never sent to a user-editable API URL.
            return GitHub(settings.model_copy(update={"github_api_url": "https://api.github.com", "github_token": ""}),
                user_token_provider=github_oauth.token)
        return GitHub(settings, token_provider=github_app.token_for_repo) if settings.github_connection == "app" else GitHub(settings)

    # A demo process never opens a real workspace, even if configured via environment.
    config = store = manager = github_app = github_oauth = None
    if not demo_only:
        root = data_dir or Path(os.environ.get("ROI_DATA_DIR", "~/.litellm-roi")).expanduser()
        config, store = ConfigStore(root), Store(root)
        github_app = GitHubApp(root)
        github_oauth = GitHubOAuth(root)
        manager = SyncManager(store, make_github)

    async def scheduler():
        while True:
            await asyncio.sleep(5)
            settings = config.load()
            if manager.next_update and utcnow() >= manager.next_update and not manager.state["running"]:
                manager.start(settings)

    @asynccontextmanager
    async def lifespan(app):
        if demo_only:
            yield
            return
        settings = config.load()
        manager.schedule(settings)
        previous = store.latest()
        if previous and manager.next_update:
            from datetime import datetime, timedelta
            manager.next_update = datetime.fromisoformat(previous["synced_at"]) + timedelta(minutes=settings.update_interval_minutes)
        job = asyncio.create_task(scheduler())
        yield
        job.cancel()
        await manager.cancel()
        try:
            await job
        except asyncio.CancelledError:
            pass

    app = FastAPI(title="LiteLLM ROI Calculator", lifespan=lifespan)
    app.state.manager, app.state.store, app.state.config = manager, store, config
    app.state.github_app = github_app
    app.state.github_oauth = github_oauth

    @app.middleware("http")
    async def local_only(req: Request, call_next):
        host = (req.url.hostname or "").lower()
        allowed = {urlsplit(public_url).hostname} if public_url else {"127.0.0.1", "localhost", "::1"}
        if host not in allowed and not (req.url.path == "/health" and req.method == "GET"):
            return JSONResponse({"detail": "This host is not allowed."}, status_code=403)
        request_origin = req.headers.get("origin")
        mutating = req.method not in {"GET", "HEAD", "OPTIONS"}
        if req.url.path.startswith("/api") and ((request_origin and request_origin != origin(req)) or (public_url and mutating and request_origin != public_url)):
            return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
        if req.method in {"POST", "PUT", "PATCH"} and "application/json" not in req.headers.get("content-type", ""):
            return JSONResponse({"detail": "Use application/json."}, status_code=415)
        if demo_only and req.url.path.startswith("/api") and req.url.path not in {"/api/state", "/api/export"}:
            return JSONResponse({"detail": "Demo mode cannot connect to services or change settings. Restart without --demo to connect your data."}, status_code=403)
        response = await call_next(req)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://github.com"
        return response

    @app.exception_handler(SourceError)
    async def source_error(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/state")
    async def state(mode: str = "live"):
        if demo_only or mode == "demo":
            return {"demo_only": demo_only, "settings": Settings().public(),
                "status": {**IDLE_STATE, "next_update": None}, "default_prompt": DEFAULT_PROMPT,
                "environment_fields": [], "report": summarize(demo_report(), {})}
        settings = config.load()
        report = store.latest()
        return {"demo_only": False, "settings": settings.public(), "status": manager.status(), "default_prompt": DEFAULT_PROMPT,
            "environment_fields": environment_fields(),
            "report": summarize(report, settings.identity_map) if report else None}

    @app.put("/api/settings")
    async def settings(update: dict = Body(...)):
        if manager.state["running"]:
            raise HTTPException(409, "Wait for the current sync or cancel it before changing settings.")
        if set(update) - set(Settings.model_fields):
            raise HTTPException(400, "Unknown settings field.")
        try:
            saved = config.save(update)
        except CredentialDestinationError as exc:
            raise HTTPException(400, str(exc)) from None
        except ValidationError as exc:
            raise HTTPException(422, "; ".join(e["msg"] for e in exc.errors(include_input=False))) from None
        manager.schedule(saved)
        return saved.public()

    @app.post("/api/connections/test")
    async def test_connections(scope: Literal["all", "gateway", "github"] = "all"):
        settings = config.load()
        models, repo_count = [], 0
        if scope in ("all", "gateway"):
            if not settings.gateway_url or not settings.admin_key:
                raise SourceError("Save a gateway URL and admin key first.")
            gateway = Gateway(settings)
            try:
                await gateway.users()
                models = await gateway.models()
            finally:
                await gateway.close()
        if scope in ("all", "github"):
            if not settings.repos:
                raise SourceError("Add at least one repository.")
            github = make_github(settings)
            try:
                for repo in settings.repos:
                    await request(github.client, "GET", f"repos/{repo}")
                    await request(github.client, "GET", f"repos/{repo}/pulls", params={"per_page": 1, "state": "closed"})
                repo_count = len(settings.repos)
            finally:
                await github.close()
        return {"ok": True, "models": models, "repos": repo_count}

    @app.get("/api/models")
    async def models():
        settings = config.load()
        if not settings.gateway_url or not settings.admin_key:
            raise SourceError("Save your gateway connection first.")
        gateway = Gateway(settings)
        try:
            return {"models": await gateway.models()}
        finally:
            await gateway.close()

    @app.get("/api/github/repos")
    async def repositories(org: str = Query(default="", pattern=r"^[A-Za-z0-9-]*$", max_length=100), page: int = Query(default=1, ge=1, le=10000), installation: int = Query(default=0, ge=0)):
        settings = config.load()
        if installation:
            return await github_app.repositories(installation, page)
        if not org and not settings.github_token and settings.github_connection != "oauth":
            raise SourceError("Add a GitHub token to browse your repositories, or enter an organization to browse public repositories.")
        github = make_github(settings)
        try:
            return await github.repositories(org, page)
        finally:
            await github.close()

    @app.post("/api/sync")
    async def sync():
        manager.start(config.load())
        return manager.status()

    @app.delete("/api/sync")
    async def cancel():
        await manager.cancel()
        return manager.status()

    @app.get("/api/export")
    async def export(mode: str = "live"):
        sample = demo_only or mode == "demo"
        raw = demo_report() if sample else store.latest()
        if not raw:
            raise HTTPException(404, "Sync a report before exporting.")
        report = summarize(raw, {} if sample else config.load().identity_map)
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["email", "github_logins", "gateway_spend_usd", "estimated_hours", "merged_prs", "pending_estimates", "in_matched_cohort", "cost_per_estimated_hour", "start_utc", "end_utc", "effort_basis"])
        for person in report["people"]:
            cells = [person["email"], ";".join(person["logins"]), person["spend"], person["hours"], person["prs"], person["pending_prs"], person["eligible"], person["cost_per_hour"], report["start"], report["end"], report["effort_basis"] or "unspecified"]
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in cells])
        return Response(stream.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="litellm-roi.csv"'})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    if not demo_only:
        github_app.install(app, config, origin, secure=bool(public_url))
        github_oauth.install(app, config, origin)
    return app
