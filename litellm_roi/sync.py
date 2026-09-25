import asyncio
from datetime import date, datetime, timedelta, timezone

from .config import Settings
from .connectors import Gateway, GitHub, SourceError
from .estimator import Estimator
from .storage import Store

IDLE_STATE = {"running": False, "phase": "idle", "stage": "idle", "done": 0, "total": 0,
    "estimated": 0, "needs_attention": 0, "error": None}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def window(settings: Settings) -> tuple[date, date]:
    end = utcnow().date()
    return end - timedelta(days=settings.backfill_days - 1), end


class SyncManager:
    def __init__(self, store: Store, github_factory=None):
        self.store = store
        self.github_factory = github_factory
        self.task: asyncio.Task | None = None
        self.state = IDLE_STATE.copy()
        self.next_update: datetime | None = None

    def schedule(self, settings: Settings):
        self.next_update = utcnow() + timedelta(minutes=settings.update_interval_minutes) if settings.update_interval_minutes and settings.public()["ready"] and self.store.latest() else None

    def status(self):
        return {**self.state, "next_update": self.next_update.isoformat() if self.next_update else None}

    def start(self, settings: Settings):
        if self.state["running"]:
            raise SourceError("A sync is already running.")
        if not settings.public()["ready"]:
            raise SourceError("Connect your gateway, choose repositories, and select an estimator model first.")
        self.state = {"running": True, "phase": "spend", "stage": "Reading gateway spend", "done": 0, "total": 0,
            "estimated": 0, "needs_attention": 0, "error": None}
        self.task = asyncio.create_task(self.run(settings))

    async def cancel(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            # Cancellation may arrive before run() has entered its try/finally.
            self.state.update(running=False, phase="cancelled", stage="Sync cancelled")

    async def run(self, settings: Settings):
        gateway = Gateway(settings)
        github = self.github_factory(settings) if self.github_factory and settings.github_connection == "app" else GitHub(settings)
        estimator = Estimator(settings, self.store)
        try:
            start, end = window(settings)
            spend, _ = await gateway.spend(start, end)
            queue = []
            self.state["phase"] = "repositories"
            for repo in settings.repos:
                self.state["stage"] = f"Reading {repo}"
                queue.extend((repo, pr) for pr in await github.pulls(repo, start, end))
            self.state.update(phase="estimates", stage="Estimating pull requests", total=len(queue))
            pulls = []
            for repo, pr in queue:
                self.state["stage"] = f"Estimating {repo} #{pr['number']}"
                evidence = await github.evidence(repo, pr)
                try:
                    estimate = await estimator.estimate(evidence)
                except SourceError as exc:
                    estimate = {"status": "error", "hours": None, "reasoning": str(exc)}
                evidence.pop("files")
                evidence.pop("body")
                pulls.append({**evidence, "estimate": estimate})
                self.state["done"] += 1
                self.state["estimated" if estimate["status"] == "estimated" else "needs_attention"] += 1
            self.store.save_report({"mode": "live", "start": start.isoformat(), "end": end.isoformat(),
                "synced_at": utcnow().isoformat(), "repos": settings.repos,
                "estimator_model": settings.estimator_model, "estimator_prompt": settings.estimator_prompt,
                "spend": spend, "pulls": pulls, "warnings": []})
            self.state.update(phase="complete", stage="Up to date")
        except asyncio.CancelledError:
            self.state.update(phase="cancelled", stage="Sync cancelled")
            raise
        except SourceError as exc:
            self.state.update(phase="error", stage="Sync failed", error=str(exc))
        except Exception:
            self.state.update(phase="error", stage="Sync failed", error="Unexpected source response. No partial report was saved. Check service compatibility and try again.")
        finally:
            self.state["running"] = False
            self.schedule(settings)
            await asyncio.gather(gateway.close(), github.close(), estimator.close())


def demo_report() -> dict:
    start, end = utcnow().date() - timedelta(days=29), utcnow().date()
    team = [("alex", "alex@example.com", 6.5, 18.2), ("jordan", "jordan@example.com", 4.0, 12.8),
        ("sam", "sam@example.com", 8.0, 23.4), ("riley", "riley@example.com", 3.5, 9.6)]
    titles = ["Add usage breakdown by model", "Fix streaming response cancellation", "Add SSO account linking",
        "Improve request retry handling", "Add cost tracking for cached tokens", "Simplify provider configuration",
        "Fix pagination in activity export", "Add deployment health checks", "Update API key permissions",
        "Improve dashboard loading states", "Add model response validation", "Fix timezone handling in reports"]
    pulls, spend = [], []
    for i, title in enumerate(titles):
        login, address, hours, cost = team[i % len(team)]
        day = (start + timedelta(days=2 + i * 2)).isoformat()
        pulls.append({"repo": "example/gateway", "number": 142 + i, "title": title,
            "url": "", "login": login, "emails": [address], "profile_email": address,
            "merged_at": day + "T14:20:00Z", "head_sha": f"demo-{i}", "additions": 47 + i * 23,
            "deletions": 12 + i * 4, "estimate": {"status": "estimated", "hours": hours + (i % 3),
                "reasoning": "Sample estimate for demonstration. A live run includes the model's reasoning grounded in the actual pull request diff.", "model": "your-estimator-model", "cached": False}})
        spend.append({"date": day, "user_id": login, "email": address, "spend": cost + i, "requests": 150 + i * 27})
    pulls.append({**pulls[0], "number": 156, "login": "casey", "emails": [], "profile_email": "",
        "title": "Add integration tests for billing", "estimate": {**pulls[0]["estimate"], "hours": 5.5}})
    spend.append({"date": end.isoformat(), "user_id": "shared-key", "email": "", "spend": 31.8, "requests": 210})
    return {"mode": "demo", "start": start.isoformat(), "end": end.isoformat(), "synced_at": utcnow().isoformat(),
        "repos": ["example/gateway"], "estimator_model": "your-estimator-model", "spend": spend, "pulls": pulls}
