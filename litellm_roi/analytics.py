from collections import defaultdict

from .config import email


def match_identity(pr: dict, observed_emails: set[str], mappings: dict[str, str]) -> tuple[str, str]:
    if mapped := mappings.get(pr["login"].casefold()):
        return mapped, "manual"
    candidates = {email(e) for e in pr.get("emails", [])} - {""}
    matched = candidates & observed_emails
    if len(matched) == 1:
        address = next(iter(matched))
        return address, "profile email" if address == pr.get("profile_email") else "commit email"
    if len(matched) > 1:
        return "", "ambiguous emails"
    return "", "email unavailable" if not candidates else "no gateway match"


def summarize(report: dict, mappings: dict[str, str]) -> dict:
    observed = {email(row["email"]) for row in report["spend"] if email(row["email"])}
    people = {}
    trend = defaultdict(lambda: {"spend": 0.0, "hours": 0.0, "prs": 0})

    def person(key: str, address: str = ""):
        if key not in people:
            people[key] = {"id": key, "email": address, "logins": [], "spend": None, "hours": 0.0,
                "prs": 0, "estimated_prs": 0, "pending_prs": 0, "match_methods": [], "eligible": False}
        return people[key]

    for row in report["spend"]:
        address = email(row["email"])
        item = person(address or "gateway:" + row["user_id"], address)
        item["spend"] = (item["spend"] or 0) + row["spend"]

    pulls = []
    for raw in report["pulls"]:
        address, method = match_identity(raw, observed, mappings)
        item = person(address or "github:" + raw["login"].casefold(), address)
        if raw["login"] not in item["logins"]:
            item["logins"].append(raw["login"])
        if method not in item["match_methods"]:
            item["match_methods"].append(method)
        item["prs"] += 1
        estimated = raw["estimate"]["status"] == "estimated"
        item["estimated_prs"] += int(estimated)
        item["pending_prs"] += int(not estimated)
        if estimated:
            item["hours"] += raw["estimate"]["hours"]
        pulls.append({**raw, "email": address, "match_method": method, "matched": address in observed})

    for item in people.values():
        item["eligible"] = item["spend"] is not None and item["estimated_prs"] > 0 and item["pending_prs"] == 0
        item["cost_per_hour"] = item["spend"] / item["hours"] if item["eligible"] and item["hours"] > 0 else None
    eligible_emails = {p["email"] for p in people.values() if p["eligible"]}
    for row in report["spend"]:
        trend[row["date"]]
        if email(row["email"]) in eligible_emails:
            trend[row["date"]]["spend"] += row["spend"]
    for pr in pulls:
        day = trend[pr["merged_at"][:10]]
        if pr["email"] in eligible_emails and pr["estimate"]["status"] == "estimated":
            day["hours"] += pr["estimate"]["hours"]
            day["prs"] += 1
    cohort = [p for p in people.values() if p["eligible"]]
    spend = sum(p["spend"] for p in cohort)
    hours = sum(p["hours"] for p in cohort)
    total_spend = sum(r["spend"] for r in report["spend"])
    return {
        "id": report.get("id"), "mode": report["mode"], "start": report["start"], "end": report["end"],
        "synced_at": report["synced_at"], "repos": report["repos"], "estimator_model": report["estimator_model"],
        "estimator_prompt": report.get("estimator_prompt", ""), "warnings": report.get("warnings", []),
        "effort_basis": report.get("effort_basis"),
        "metrics": {"matched_spend": spend, "output_hours": hours, "total_spend": total_spend,
            "total_output_hours": sum(p["hours"] for p in people.values()),
            "excluded_spend": max(0, total_spend - spend), "cost_per_hour": spend / hours if hours else None,
            "hours_per_dollar": hours / spend if spend else None, "merged_prs": len(pulls),
            "estimated_prs": sum(p["estimated_prs"] for p in people.values()),
            "matched_prs": sum(p["matched"] for p in pulls), "cohort_people": len(cohort),
            "people_with_prs": sum(p["prs"] > 0 for p in people.values()),
            "pending_prs": sum(p["pending_prs"] for p in people.values())},
        "people": sorted(people.values(), key=lambda p: (-p["hours"], p["id"])),
        "pulls": sorted(pulls, key=lambda p: p["merged_at"], reverse=True),
        "trend": [{"date": day, **values} for day, values in sorted(trend.items())],
    }
