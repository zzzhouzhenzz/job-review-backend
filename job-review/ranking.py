"""Validate ranker-authored flashcard content. No model or UI renderer lives here."""
from store import ReviewError, text


def validate_ranked_cards(result, candidates, eligible):
    """Keep ranking, explanations, and short display text in one atomic result."""
    if not isinstance(result, dict) or not isinstance(result.get("cards"), list) or len(result["cards"]) > 50:
        raise ReviewError("Ranker result must contain up to 50 cards (an empty list is valid).")
    cards, companies, keys = [], set(), set()
    for item in result["cards"]:
        if not isinstance(item, dict):
            raise ReviewError("Each ranked card must be an object.")
        cid = text(item.get("company_id"), "Company ID", 200, required=True)
        if cid in companies:
            raise ReviewError("Return one ranked card per company.")
        companies.add(cid)
        summary = text(item.get("summary"), "Card summary", 1000, required=True)
        jobs = item.get("jobs")
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= 3:
            raise ReviewError("A ranked card must contain one to three jobs.")
        content_jobs, snapshots = [], []
        for job in jobs:
            if not isinstance(job, dict):
                raise ReviewError("Each ranked job must be an object.")
            key = text(job.get("key"), "Job key", 1000, required=True)
            if key in keys or key not in candidates or key not in eligible:
                raise ReviewError("Rank result contains a duplicate or excluded job.", 409)
            current = eligible[key]
            if current["company_id"] != cid:
                raise ReviewError("Each ranked job must belong to the card company.")
            if (type(job.get("revision")) is not int or job["revision"] != current["revision"]
                    or candidates[key]["revision"] != current["revision"]):
                raise ReviewError("A ranked job changed. Rank the current version.", 409, reason="stale_revision")
            brief = job.get("brief")
            if not isinstance(brief, dict):
                raise ReviewError("Each ranked job needs work, fit, and gap text.")
            brief = {field: text(brief.get(field), f"Brief {field}", 600, required=True)
                     for field in ("work", "fit", "gap")}
            content_jobs.append({"key": key, "revision": current["revision"],
                                 "reason": text(job.get("reason"), "Match reason", 5000, required=True),
                                 "brief": brief})
            snapshots.append(current)
            keys.add(key)
        cards.append({"company_id": cid, "jobs": snapshots,
                      "content": {"schema_version": 1, "source": "ranker", "summary": summary,
                                  "jobs": content_jobs}})
    return cards
