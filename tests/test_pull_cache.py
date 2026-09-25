import asyncio
import copy
import os
from datetime import datetime, timedelta, timezone

import pytest

from litellm_roi import sync as sync_module
from litellm_roi.config import ConfigStore
from litellm_roi.connectors import Gateway, GitHub, SourceError
from litellm_roi.estimator import Estimator
from litellm_roi.pull_cache import legacy_report
from litellm_roi.storage import Store
from litellm_roi.sync import SyncManager


def listing(pr):
    return {**pr, "head": {"sha": pr["head_sha"]}, "user": {"login": pr["login"]}, "updated_at": pr["merged_at"]}


def result():
    return {"status": "estimated", "hours": 4, "reasoning": "Implementation and verification.",
        "evidence_source": "pr_metadata", "effort_basis": "without_ai", "cached": False}


@pytest.fixture
def sources(monkeypatch, pr):
    data = {"pulls": [copy.deepcopy(pr)], "details": [], "model": [], "spend": 0}

    async def spend(*_):
        data["spend"] += 1
        return [{"date": "2026-09-12", "email": "alice@example.com", "spend": data["spend"]}], {}

    async def pulls(*_):
        return [listing(item) for item in data["pulls"]]

    async def evidence(_self, _repo, item):
        data["details"].append(item["number"])
        return copy.deepcopy(next(p for p in data["pulls"] if p["number"] == item["number"]))

    async def estimate(_self, item):
        data["model"].append(item["number"])
        return result()

    monkeypatch.setattr(Gateway, "spend", spend)
    monkeypatch.setattr(GitHub, "pulls", pulls)
    monkeypatch.setattr(GitHub, "evidence", evidence)
    monkeypatch.setattr(Estimator, "estimate", estimate)
    return data


async def run(manager, settings):
    manager.start(settings)
    await manager.task
    assert manager.state["error"] is None


async def test_incremental_sync_fetches_only_new_prs_and_refreshes_spend(settings, pr, tmp_path, sources):
    manager = SyncManager(Store(tmp_path))
    await run(manager, settings)
    sources["pulls"].append({**pr, "number": 43})
    await run(manager, settings)
    assert sources["details"] == sources["model"] == [42, 43]
    assert sources["spend"] == 2
    assert manager.state["reused"] == 1
    assert [p["number"] for p in manager.store.latest()["pulls"]] == [42, 43]
    assert manager.store.latest()["spend"][0]["spend"] == 2
    # Removing a PR from the rolling window never keeps stale output in the report.
    sources["pulls"] = sources["pulls"][1:]
    manager.store.clear_reports()
    manager = SyncManager(Store(tmp_path))
    await run(manager, settings)
    assert sources["details"] == [42, 43]
    assert manager.state["reused"] == 1
    assert len(manager.store.latest()["pulls"]) == 1
    assert not {"body", "files", "commits"} & manager.store.latest()["pulls"][0].keys()


@pytest.mark.parametrize("change", ["body", "title", "head_sha", "login", "estimator_model", "estimator_prompt", "gateway_url", "github_api_url"])
async def test_changed_evidence_or_estimator_invalidates_reuse(settings, tmp_path, sources, change):
    manager = SyncManager(Store(tmp_path))
    await run(manager, settings)
    if change in sources["pulls"][0]:
        sources["pulls"][0][change] += "-changed"
    else:
        setattr(settings, change, getattr(settings, change) + "-changed")
    await run(manager, settings)
    assert sources["details"] == sources["model"] == [42, 42]
    assert manager.state["reused"] == 0


async def test_comment_activity_does_not_invalidate_success(settings, tmp_path, sources, monkeypatch):
    manager = SyncManager(Store(tmp_path))
    await run(manager, settings)

    async def pulls(*_):
        return [{**listing(sources["pulls"][0]), "updated_at": "2030-01-01T00:00:00Z"}]

    monkeypatch.setattr(GitHub, "pulls", pulls)
    await run(manager, settings)
    assert sources["details"] == [42]
    assert manager.state["reused"] == 1


async def test_failed_estimates_are_retried(settings, pr, tmp_path, sources, monkeypatch):
    manager = SyncManager(Store(tmp_path))
    sources["pulls"] += [{**pr, "number": 43}, {**pr, "number": 44}]
    first = True

    async def estimate(_self, item):
        if first and item["number"] == 43:
            raise SourceError("Model unavailable")
        if first and item["number"] == 44:
            return {"status": "needs_review", "hours": None}
        return result()

    monkeypatch.setattr(Estimator, "estimate", estimate)
    await run(manager, settings)
    first = False
    await run(manager, settings)
    assert sources["details"] == [42, 43, 44, 43, 44]
    assert manager.state["reused"] == 1
    assert manager.state["needs_attention"] == 0


async def test_completed_pr_survives_cancel_before_report(settings, pr, tmp_path, sources, monkeypatch):
    sources["pulls"].append({**pr, "number": 43})
    entered = asyncio.Event()

    async def slow_estimate(_self, item):
        if item["number"] == 43:
            entered.set()
            await asyncio.Event().wait()
        return result()

    monkeypatch.setattr(Estimator, "estimate", slow_estimate)
    manager = SyncManager(Store(tmp_path))
    manager.start(settings)
    await asyncio.wait_for(entered.wait(), 5)
    await manager.cancel()
    assert manager.store.latest() is None

    async def estimate(*_):
        return result()

    monkeypatch.setattr(Estimator, "estimate", estimate)
    manager = SyncManager(Store(tmp_path))
    await run(manager, settings)
    assert sources["details"] == [42, 43, 43]
    assert manager.state["reused"] == 1


async def test_existing_report_is_adopted_without_reprocessing(settings, tmp_path, pr, sources):
    ConfigStore(tmp_path).save(settings.model_dump())
    manager = SyncManager(Store(tmp_path))
    old = {k: v for k, v in pr.items() if k not in {"body", "files", "commits"}}
    old["estimate"] = result()
    manager.store.save_report({"mode": "live", "synced_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        "estimator_model": settings.estimator_model, "estimator_prompt": settings.estimator_prompt, "pulls": [old]})
    await run(manager, settings)
    assert sources["details"] == sources["model"] == []
    assert manager.state["reused"] == 1


def test_legacy_migration_rejects_uncertain_source_settings(settings, tmp_path, monkeypatch):
    ConfigStore(tmp_path).save(settings.model_dump())
    store = Store(tmp_path)
    now = datetime.now(timezone.utc)
    store.save_report({"mode": "live", "synced_at": now.isoformat(),
        "estimator_model": settings.estimator_model, "estimator_prompt": settings.estimator_prompt, "pulls": []})
    os.utime(tmp_path / "config.json", (now.timestamp() + 5, now.timestamp() + 5))
    assert legacy_report(store, settings) is None
    os.utime(tmp_path / "config.json", (now.timestamp() - 5, now.timestamp() - 5))
    monkeypatch.setenv("LITELLM_GATEWAY_URL", settings.gateway_url)
    assert legacy_report(store, settings) is None


def test_time_remaining_does_not_count_instantly_reused_prs_as_processing_speed(tmp_path, monkeypatch):
    manager = SyncManager(Store(tmp_path))
    manager.started_at = manager.estimates_started_at = 100
    manager.fast_reused = 990
    manager.state.update(running=True, total=1000, done=991, reused=990)
    monkeypatch.setattr(sync_module, "monotonic", lambda: 130)
    assert manager.status()["remaining_seconds"] is None
    manager.state["done"] = 993
    assert manager.status()["remaining_seconds"] == 70
