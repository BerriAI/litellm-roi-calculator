"""Reuse successful PR snapshots before fetching file and commit details."""

import hashlib
import json
from datetime import datetime
from urllib.parse import urlsplit

from .config import Settings, environment_fields
from .storage import Store


def cache_key(settings: Settings, context: str, repo: str, pr: dict) -> str | None:
    head = (pr.get("head") or {}).get("sha")
    if not head or "title" not in pr or "body" not in pr or not (pr.get("user") or {}).get("login"):
        return None
    # updated_at includes comments/labels, which do not change the evidence.
    value = ["pull-v1", settings.github_api_url.rstrip("/"), context, repo.casefold(), pr["number"],
        head, pr["title"], pr["body"] or "", pr["user"]["login"].casefold()]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def legacy_report(store: Store, settings: Settings) -> dict | None:
    """Adopt unchanged PRs from pre-index reports, only with known settings provenance."""
    previous = store.latest()
    if not previous or previous.get("cache_context") or previous.get("estimator_model") != settings.estimator_model or previous.get("estimator_prompt") != settings.estimator_prompt:
        return None
    # Older reports lack a source fingerprint. A settings write after the report,
    # or environment-managed sources, means we cannot prove which source was used.
    if set(environment_fields()) & {"gateway_url", "estimator_model", "github_api_url", "repos"}:
        return None
    config_path = store.path.parent / "config.json"
    try:
        synced_at = datetime.fromisoformat(previous["synced_at"])
        if not config_path.exists() or config_path.stat().st_mtime > synced_at.timestamp():
            return None
        previous["synced_at"] = synced_at
    except (KeyError, ValueError):
        return None
    return previous


def legacy_match(settings: Settings, previous: dict, cached: dict, pr: dict) -> bool:
    estimate = cached.get("estimate", {})
    if estimate.get("status") != "estimated" or estimate.get("evidence_source") != "pr_metadata" or estimate.get("effort_basis") != "without_ai":
        return False
    api_host = urlsplit(settings.github_api_url).hostname or ""
    web_host = "github.com" if api_host == "api.github.com" else api_host.removeprefix("api.")
    if urlsplit(cached.get("url", "")).hostname != web_host:
        return False
    if cached.get("head_sha") != (pr.get("head") or {}).get("sha") or cached.get("title") != pr.get("title") or cached.get("login", "").casefold() != (pr.get("user") or {}).get("login", "").casefold():
        return False
    try:
        return datetime.fromisoformat(pr["updated_at"]) <= previous["synced_at"]
    except (KeyError, ValueError, TypeError):
        return False
