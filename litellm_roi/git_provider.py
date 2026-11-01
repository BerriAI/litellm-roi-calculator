"""The git host contract used by sync, the app shell, and estimators.

Providers list merged pull requests in a normalized shape so caches and the
estimator stay host-agnostic:

    {"number", "title", "body", "url", "login", "merged_at", "updated_at", "head_sha"}

Line-level additions/deletions are optional evidence throughout; Azure DevOps
does not publish them through the REST API.
"""

from datetime import date
from typing import Protocol

from .config import Settings


class GitProvider(Protocol):
    async def repositories(self, organization: str = "", page: int = 1) -> dict: ...
    async def pulls(self, repo: str, start: date, end: date) -> list[dict]: ...
    async def evidence(self, repo: str, pr: dict) -> dict: ...
    async def probe(self, repo: str) -> None: ...
    async def close(self) -> None: ...


def git_provider_from_settings(settings: Settings, transport=None, token_provider=None) -> GitProvider:
    if settings.git_provider == "azure_devops":
        from .azure_devops import AzureDevOps
        return AzureDevOps(settings, transport=transport)
    if settings.git_provider == "gitlab":
        from .gitlab import GitLab
        return GitLab(settings, transport=transport)
    from .connectors import GitHub
    return GitHub(settings, transport=transport, token_provider=token_provider)
