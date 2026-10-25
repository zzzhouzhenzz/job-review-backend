#!/usr/bin/env python3
"""Attach concise review text to the original, preserved research."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "output/research/job-review-20-2026-10-08/app/data.json"


def main():
    catalog = json.loads(SOURCE.read_text())
    briefs = json.loads((ROOT / "briefs.json").read_text())
    seen = set()
    for company in catalog["companies"]:
        for job in company["jobs"]:
            key = company["id"] + ":" + job["job_id"]
            job["brief"] = briefs[key]
            seen.add(key)
            if key == "verkada:4525665007":
                # The resume confirms cross-team work, but not specific mentoring examples.
                job["strengths"] = [
                    value.replace("Cross-team implementation and mentoring evidence support independent ownership.",
                                  "Cross-team implementation supports independent ownership.")
                    for value in job["strengths"]
                ]
    if seen != set(briefs):
        raise ValueError("Short descriptions must match the original job IDs exactly.")
    catalog["source_file"] = str(SOURCE)
    (ROOT / "initial-catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
    print(f"Prepared {len(seen)} jobs with concise descriptions.")


if __name__ == "__main__":
    main()
