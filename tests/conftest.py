import pytest

from litellm_roi.config import ENV_FIELDS, Settings
from litellm_roi.github_app import APP_ENV


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    for key in [*ENV_FIELDS.values(), *APP_ENV.values(), "GITHUB_REPOS", "ROI_DATA_DIR", "ROI_PUBLIC_URL", "RENDER_EXTERNAL_URL", "PORT"]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings():
    return Settings(gateway_url="https://gateway.example.com", admin_key="test-admin-secret",
        estimator_key="test-inference-secret", github_token="test-github-secret", repos=["org/repo"],
        estimator_model="test-estimator", update_interval_minutes=0)


@pytest.fixture
def pr():
    return {"repo": "org/repo", "number": 42, "title": "Fix timezone conversion", "body": "Preserve UTC.",
        "head_sha": "abcdef", "url": "https://github.com/org/repo/pull/42", "login": "alice",
        "emails": ["alice@example.com"], "profile_email": "alice@example.com", "merged_at": "2026-09-12T12:00:00Z",
        "additions": 1, "deletions": 1, "incomplete_metadata": False, "changed_files": 1, "commit_count": 1,
        "commits": [{"sha": "abcdef", "message": "Fix timezone conversion\n\nPreserve UTC.", "additions": 1, "deletions": 1, "changed_files": 1}],
        "files": [{"filename": "time.py", "status": "modified", "additions": 1, "deletions": 1, "patch": "@@ -1 +1 @@\n-local()\n+utc()"}]}
