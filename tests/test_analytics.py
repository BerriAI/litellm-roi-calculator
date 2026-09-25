from copy import deepcopy

import pytest

from litellm_roi.analytics import match_identity, summarize
from litellm_roi.config import email


def report(pr):
    return {"mode": "live", "start": "2026-09-01", "end": "2026-09-30", "synced_at": "2026-09-30T12:00:00Z",
        "repos": ["org/repo"], "estimator_model": "test-model",
        "spend": [{"date": "2026-09-12", "email": " Alice@Example.com ", "user_id": "u1", "spend": 12},
            {"date": "2026-09-12", "email": "bob@example.com", "user_id": "u2", "spend": 8},
            {"date": "2026-09-12", "email": "", "user_id": "shared", "spend": 5}],
        "pulls": [{**pr, "estimate": {"status": "estimated", "hours": 4, "reasoning": "Explanation"}}]}


def test_matched_cohort_uses_same_population_and_shows_excluded_spend(pr):
    result = summarize(report(pr), {})
    assert result["metrics"]["cost_per_hour"] == 3
    assert result["metrics"]["matched_spend"] == 12
    assert result["metrics"]["total_spend"] == 25
    assert result["metrics"]["excluded_spend"] == 13
    assert result["metrics"]["output_hours"] == 4
    assert result["trend"][0] == {"date": "2026-09-12", "spend": 12, "hours": 4, "prs": 1}


def test_pending_pr_excludes_entire_person_from_ratio(pr):
    data = report(pr)
    data["pulls"].append({**pr, "number": 43, "estimate": {"status": "error", "hours": None}})
    result = summarize(data, {})
    assert result["metrics"]["cost_per_hour"] is None
    assert result["metrics"]["matched_spend"] == 0
    assert result["metrics"]["total_output_hours"] == 4
    assert result["metrics"]["pending_prs"] == 1


def test_older_reports_are_not_relabelled_as_without_ai(pr):
    data = report(pr)
    assert summarize(data, {})["effort_basis"] is None
    data["effort_basis"] = "without_ai"
    assert summarize(data, {})["effort_basis"] == "without_ai"


def test_missing_spend_is_not_zero_and_manual_match_recomputes(pr):
    data = report(pr)
    data["pulls"][0]["emails"] = []
    before = summarize(data, {})
    contributor = next(p for p in before["people"] if p["prs"])
    assert contributor["spend"] is None
    assert before["metrics"]["output_hours"] == 0
    after = summarize(data, {"alice": "alice@example.com"})
    assert after["metrics"]["output_hours"] == 4
    assert after["metrics"]["cost_per_hour"] == 3


def test_two_github_logins_same_gateway_email_do_not_duplicate_cost(pr):
    data = report(pr)
    second = deepcopy(data["pulls"][0])
    second.update(number=43, login="alice-work")
    data["pulls"].append(second)
    result = summarize(data, {})
    assert result["metrics"]["matched_spend"] == 12
    assert result["metrics"]["output_hours"] == 8
    assert result["metrics"]["cohort_people"] == 1


def test_ambiguous_emails_require_override(pr):
    pr["emails"] = ["alice@example.com", "bob@example.com"]
    assert match_identity(pr, set(pr["emails"]), {}) == ("", "ambiguous emails")
    assert match_identity(pr, set(pr["emails"]), {"alice": "bob@example.com"}) == ("bob@example.com", "manual")


def test_zero_spend_and_zero_output_denominators(pr):
    data = report(pr)
    data["spend"][0]["spend"] = 0
    result = summarize(data, {})
    assert result["metrics"]["cost_per_hour"] == 0
    assert result["metrics"]["hours_per_dollar"] is None
    data["pulls"][0]["estimate"]["hours"] = 0
    assert summarize(data, {})["metrics"]["cost_per_hour"] is None


@pytest.mark.parametrize("value", ["123+alice@users.noreply.github.com", "alice", "a@b", "", None])
def test_unusable_email_not_guessed(value):
    assert email(value) == ""


def test_email_aliases_not_merged():
    assert email(" Alice+work@Example.com ") == "alice+work@example.com"
