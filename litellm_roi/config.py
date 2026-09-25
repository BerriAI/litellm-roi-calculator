import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationInfo, field_validator

DEFAULT_PROMPT = (
    "Estimate how many hours it would take an engineer to complete the work in this pull request without AI assistance. "
    "Explain your estimate briefly."
)
SECRET_FIELDS = ("admin_key", "estimator_key", "github_token")


class CredentialDestinationError(ValueError):
    """Changing a destination must never redirect an environment credential."""


def email(value: str | None) -> str:
    value = (value or "").strip().casefold()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) or value.endswith("noreply.github.com"):
        return ""
    return value


class Settings(BaseModel):
    gateway_url: str = ""
    admin_key: str = ""
    estimator_key: str = ""
    github_token: str = ""
    github_connection: Literal["app", "token"] = "token"
    github_api_url: str = "https://api.github.com"
    repos: list[str] = Field(default_factory=list)
    estimator_model: str = ""
    estimator_prompt: str = DEFAULT_PROMPT
    identity_map: dict[str, str] = Field(default_factory=dict)
    backfill_days: int = Field(default=7, ge=1, le=3650)
    update_interval_minutes: int = Field(default=60, ge=0, le=43200)

    @field_validator("update_interval_minutes")
    @classmethod
    def valid_interval(cls, value: int) -> int:
        if 0 < value < 5:
            raise ValueError("Choose manual updates (0), or an interval of at least 5 minutes.")
        return value

    @field_validator("gateway_url", "github_api_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Use a complete http:// or https:// URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Put credentials in the key field; URLs cannot contain credentials, query, or fragment.")
        return value

    @field_validator("repos")
    @classmethod
    def valid_repos(cls, values: list[str], info: ValidationInfo) -> list[str]:
        repos = []
        seen = set()
        for value in values:
            value = value.strip()
            if "://" in value:
                parsed = urlsplit(value)
                api_host = urlsplit(info.data.get("github_api_url", "https://api.github.com")).hostname or ""
                web_host = "github.com" if api_host == "api.github.com" else api_host.removeprefix("api.")
                if parsed.scheme not in ("http", "https") or parsed.hostname != web_host or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError("Repository URLs must belong to your configured GitHub server.")
                value = parsed.path.strip("/")
            value = value.rstrip("/").removesuffix(".git")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
                raise ValueError("Repositories must be owner/repo or a GitHub repository URL.")
            if value.casefold() not in seen:
                repos.append(value)
                seen.add(value.casefold())
        return repos

    @field_validator("identity_map")
    @classmethod
    def valid_identities(cls, values: dict[str, str]) -> dict[str, str]:
        result = {}
        for login, address in values.items():
            if not re.fullmatch(r"[A-Za-z0-9_\[\]-]+", login.strip()) or not email(address):
                raise ValueError("Each identity needs a GitHub username and a valid gateway email.")
            result[login.strip().casefold()] = email(address)
        return result

    @field_validator("estimator_prompt")
    @classmethod
    def valid_prompt(cls, value: str) -> str:
        if not value.strip() or len(value) > 20000:
            raise ValueError("The estimator prompt must contain between 1 and 20,000 characters.")
        return value.strip()

    def public(self) -> dict:
        values = self.model_dump(exclude=set(SECRET_FIELDS))
        for name in SECRET_FIELDS:
            values[f"has_{name}"] = bool(getattr(self, name))
        values["temperature"] = 0
        values["ready"] = bool(self.gateway_url and self.admin_key and self.repos and self.estimator_model)
        return values


ENV_FIELDS = {
    "gateway_url": "LITELLM_GATEWAY_URL",
    "admin_key": "LITELLM_ADMIN_KEY",
    "estimator_key": "LITELLM_ESTIMATOR_KEY",
    "estimator_model": "LITELLM_ESTIMATOR_MODEL",
    "github_token": "GITHUB_TOKEN",
    "github_api_url": "GITHUB_API_URL",
}


def environment_overrides() -> dict:
    values = {field: value for field, variable in ENV_FIELDS.items()
        if (value := os.environ.get(variable, "").strip())}
    repos = [repo.strip() for repo in os.environ.get("GITHUB_REPOS", "").split(",") if repo.strip()]
    if repos:
        values["repos"] = repos
    return values


def environment_fields() -> list[str]:
    fields = set(environment_overrides())
    if fields & {"admin_key", "estimator_key"}:
        fields.add("gateway_url")
    if "github_token" in fields:
        fields.add("github_api_url")
    return sorted(fields)


class ConfigStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "config.json"

    def load(self) -> Settings:
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        if data.get("estimator_prompt") == DEFAULT_PROMPT.replace(" without AI assistance", ""):
            data["estimator_prompt"] = DEFAULT_PROMPT
        data.update(environment_overrides())
        return Settings(**data)

    def save(self, update: dict) -> Settings:
        data = self.load().model_dump()
        candidate = Settings(**{**data, **update})
        managed = environment_overrides()
        if managed.keys() & {"admin_key", "estimator_key"} and candidate.gateway_url != data["gateway_url"]:
            raise CredentialDestinationError("The gateway URL is locked while a gateway key is supplied by the server environment. Change the URL on the server.")
        if "github_token" in managed and candidate.github_api_url != data["github_api_url"]:
            raise CredentialDestinationError("The GitHub API URL is locked while a GitHub token is supplied by the server environment. Change the URL on the server.")
        update = dict(update)
        for name in SECRET_FIELDS:
            if update.get(name) == "":
                update.pop(name)
        if update.get("gateway_url", data["gateway_url"]).rstrip("/") != data["gateway_url"]:
            data["admin_key"] = ""
            data["estimator_key"] = ""
        if update.get("github_api_url", data["github_api_url"]).rstrip("/") != data["github_api_url"]:
            data["github_token"] = ""
        data.update(update)
        settings = Settings(**data)
        stored = settings.model_dump()
        for name in SECRET_FIELDS:
            if name in managed:
                stored[name] = ""
        fd, temp = tempfile.mkstemp(dir=self.root, prefix=".config-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(stored, stream, indent=2)
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return self.load()
