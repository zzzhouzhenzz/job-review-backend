#!/usr/bin/env python3
"""Serve fictional jobs and a seeded question round for manual simulator checks.

Uses a temporary database, loopback only, and the disabled backend runtime.
No production database, website, account, or employer is accessed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "job-review"))
from server import ReviewServer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--instructions", action="store_true", help="Seed saved task guidance for the instruction editor check.")
    args = parser.parse_args()
    demo = json.loads((ROOT / "Sources/ReviewCore/Resources/demo-state.json").read_text())
    catalog = {"companies": [
        {"id": company["id"], "name": company["name"], **company["data"],
         "jobs": [{"job_id": job["job_id"], **job["data"]}
                  for job in demo["jobs"] if job["company_id"] == company["id"]]}
        for company in demo["companies"]]}
    with tempfile.TemporaryDirectory(prefix="jobswipe-simulator-") as temp:
        server = ReviewServer(("127.0.0.1", args.port), Path(temp) / "fictional.sqlite3")
        try:
            server.store.import_catalog(catalog, "Fictional simulator verification")
            # Seed a simulated ranker result before cards are published. No model is called.
            with server.store.transaction(True) as db:
                server.store._request_discovery(db, "canopy")
            ranking = server.store.lease_task("fictional-ranker", kinds=["rank"])
            context = server.store.discovery_context(ranking["id"], ranking["lease_token"])
            candidates = list(reversed(context["candidates"]))[:3]
            server.store.accept_ranking(ranking["id"], ranking["lease_token"], {"cards": [{
                "company_id": "canopy",
                "summary": "Fictional ranking: these three roles exercise the card layout. Their order comes from this sample ranker result.",
                "jobs": [{"key": job["key"], "revision": job["revision"],
                    "reason": f"Sample selection reason for {job['data']['title']}. No real applicant fit is claimed.",
                    "brief": {"work": "Build reliable sample services and tools.",
                              "fit": "Fictional backend experience matches this sample role. Fictional infrastructure experience supports the sample work.",
                              "gap": "Sample domain experience is unconfirmed. Sample on-call expectations need verification."}}
                    for job in candidates]}]})
            card = next(card for card in server.store.feed()["cards"] if card["company"]["id"] == "northstar")
            ack = server.store.receive_decision({"request_id": "simulator-seed-apply", "action": "apply",
                "card_id": card["id"], "card_revision": card["revision"],
                "selected": [card["jobs"][0]["key"]]})
            process_id = ack["process_ids"][0]
            task = server.store.lease_task("fictional-seed", kinds=["apply"])
            server.store.start_application(process_id, task["lease_token"])
            server.store.ask_questions(process_id, task["lease_token"], "fictional-form-v1", [
                {"id": "fixture-text", "text": "What interests you about this fictional role?", "type": "text",
                 "required": True, "scope": "application", "reason": "Simulator text-input check."},
                {"id": "fixture-single", "text": "Which fictional office would you choose?", "type": "single_choice",
                 "required": True, "scope": "application", "choices": [
                     {"id": "north", "label": "North office"}, {"id": "south", "label": "South office"}]},
                {"id": "fixture-multi", "text": "Which sample areas interest you?", "type": "multi_choice",
                 "required": False, "scope": "application", "choices": [
                     {"id": "infra", "label": "Infrastructure"}, {"id": "tools", "label": "Developer tools"}]}
            ])
            if args.instructions:
                target = server.store.unblock_instructions(task["id"])["target"]
                server.store.save_unblock_instruction({"request_id": "fictional-instruction-history",
                    **{key: target[key] for key in ("task_id", "task_updated_at", "process_id", "process_revision")},
                    "text": "Use only the fictional sample application. Keep the required questions for me to answer."})
            print(f"Fictional simulator server: http://127.0.0.1:{server.server_port}", flush=True)
            print("One seeded application needs answers. The other company cards remain available.", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
