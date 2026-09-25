import asyncio
import csv
import io
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .analytics import summarize
from .config import DEFAULT_PROMPT, ENV_FIELDS, ConfigStore, Settings
from .connectors import Gateway, GitHub, SourceError, request
from .storage import Store
from .sync import SyncManager, demo_report, utcnow

ASSETS = Path(__file__).parent / "static"


def create_app(data_dir: Path | None = None) -> FastAPI:
    root = data_dir or Path(os.environ.get("ROI_DATA_DIR", "~/.litellm-roi")).expanduser()
    config, store = ConfigStore(root), Store(root)
    manager = SyncManager(store)

    async def scheduler():
        while True:
            await asyncio.sleep(5)
            settings = config.load()
            if manager.next_update and utcnow() >= manager.next_update and not manager.state["running"]:
                manager.start(settings)

    @asynccontextmanager
    async def lifespan(app):
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

    @app.middleware("http")
    async def local_only(req: Request, call_next):
        host = (req.url.hostname or "").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse({"detail": "This dashboard accepts localhost connections only."}, status_code=403)
        origin = req.headers.get("origin")
        if req.url.path.startswith("/api") and origin and origin != str(req.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
        if req.method in {"POST", "PUT", "PATCH"} and "application/json" not in req.headers.get("content-type", ""):
            return JSONResponse({"detail": "Use application/json."}, status_code=415)
        response = await call_next(req)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(SourceError)
    async def source_error(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/state")
    async def state(mode: str = "live"):
        settings = config.load()
        report = demo_report() if mode == "demo" else store.latest()
        return {"settings": settings.public(), "status": manager.status(), "default_prompt": DEFAULT_PROMPT,
            "environment_fields": [field for field, env in ENV_FIELDS.items() if env in os.environ] + (["repos"] if "GITHUB_REPOS" in os.environ else []),
            "report": summarize(report, settings.identity_map if mode != "demo" else {}) if report else None}

    @app.put("/api/settings")
    async def settings(update: dict = Body(...)):
        if manager.state["running"]:
            raise HTTPException(409, "Wait for the current sync or cancel it before changing settings.")
        if set(update) - set(Settings.model_fields):
            raise HTTPException(400, "Unknown settings field.")
        try:
            saved = config.save(update)
        except ValidationError as exc:
            raise HTTPException(422, "; ".join(e["msg"] for e in exc.errors(include_input=False))) from None
        manager.schedule(saved)
        return saved.public()

    @app.post("/api/connections/test")
    async def test_connections():
        settings = config.load()
        if not settings.gateway_url or not settings.admin_key:
            raise SourceError("Save a gateway URL and admin key first.")
        gateway, github = Gateway(settings), GitHub(settings)
        try:
            await gateway.users()
            for repo in settings.repos:
                await request(github.client, "GET", f"repos/{repo}")
            models = await gateway.models()
            return {"ok": True, "models": models, "repos": len(settings.repos)}
        finally:
            await asyncio.gather(gateway.close(), github.close())

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
        raw = demo_report() if mode == "demo" else store.latest()
        if not raw:
            raise HTTPException(404, "Sync a report before exporting.")
        report = summarize(raw, config.load().identity_map if mode != "demo" else {})
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["email", "github_logins", "gateway_spend_usd", "estimated_hours", "merged_prs", "pending_estimates", "in_matched_cohort", "cost_per_estimated_hour", "start_utc", "end_utc"])
        for person in report["people"]:
            cells = [person["email"], ";".join(person["logins"]), person["spend"], person["hours"], person["prs"], person["pending_prs"], person["eligible"], person["cost_per_hour"], report["start"], report["end"]]
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in cells])
        return Response(stream.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="litellm-roi.csv"'})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    return app
