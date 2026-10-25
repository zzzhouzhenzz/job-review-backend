"""End-to-end policy tests. All data and databases are fictional and temporary."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import Orchestrator
from test_readiness import ReadyRuntime
from store import ReviewError, now
from test_store import catalog
from workflow import WorkflowStore


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "workflow.sqlite3"
        self.store = WorkflowStore(self.path)
        self.store.grant_project_consent({"request_id": "fixture-consent", "policy_version": 1, "accepted": True})
        self.catalog = catalog()
        self.store.import_catalog(self.catalog, "fictional fixture")

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, cid="a"):
        return next(c for c in self.store.feed()["cards"] if c["company"]["id"] == cid)

    def decision(self, action="apply", cid="a", request_id="decision-1", **extra):
        card = self.card(cid)
        return {"request_id": request_id, "card_id": card["id"], "card_revision": card["revision"],
                "action": action, "selected": [card["jobs"][0]["key"]] if action in {"apply", "pass", "later"} else [], **extra}

    def test_apply_is_atomic_durable_and_exactly_idempotent(self):
        payload = self.decision()
        receipt = self.store.receive_decision(payload)
        self.assertEqual(1, len(receipt["process_ids"]))
        reopened = WorkflowStore(self.path)
        self.assertEqual(receipt, reopened.receive_decision(payload))
        self.assertEqual(1, len(reopened.processes()))
        self.assertEqual("queued", reopened.state()["queue"][0]["status"])
        changed = {**payload, "note": "different"}
        with self.assertRaises(ReviewError):
            reopened.receive_decision(changed)
        self.assertEqual(1, len(reopened.tasks()))
        with reopened.transaction() as db:
            self.assertEqual(payload, json.loads(db.execute("SELECT request_body FROM wf_commands WHERE id=?", (payload["request_id"],)).fetchone()[0]))

    def test_invalid_selection_saves_nothing(self):
        payload = self.decision(selected=["missing"])
        with self.assertRaises(ReviewError):
            self.store.receive_decision(payload)
        self.assertEqual([], self.store.tasks())
        self.assertEqual([], self.store.processes())
        self.assertEqual(2, len(self.store.feed()["cards"]))

    def test_old_approvals_and_queue_never_become_tasks(self):
        state = self.store.state()
        job = state["jobs"][0]
        self.store.edit_job({"key": job["key"], "revision": job["revision"], "decision": "approve"})
        job = next(j for j in self.store.state()["jobs"] if j["key"] == job["key"])
        self.store.queue_jobs({"request_id": "old", "jobs": [{"key": job["key"], "revision": job["revision"]}]})
        self.assertEqual([], self.store.processes())
        Orchestrator(self.store).run_once()
        self.assertFalse(any(t["kind"] == "apply" for t in self.store.tasks()))
        self.assertTrue(all(t["attempts"] == 0 for t in self.store.tasks()))

    def test_new_arrivals_keep_current_card_and_stale_revisions_reject(self):
        before = self.card()
        newer = copy.deepcopy(self.catalog)
        fourth = copy.deepcopy(newer["companies"][0]["jobs"][0])
        fourth.update(job_id="fourth", title="Engineer Four", url="https://example.com/fourth", rank=0)
        newer["companies"][0]["jobs"].append(fourth)
        self.store.import_catalog(newer, "new job")
        self.assertEqual(before, self.card())
        payload = self.decision()
        newer["companies"][0]["jobs"][0]["salary"] = "$different"
        self.store.import_catalog(newer, "material update")
        with self.assertRaises(ReviewError):
            self.store.receive_decision(payload)
        self.assertFalse(self.store.processes())

    def test_refill_is_single_durable_request_and_disabled_worker_does_not_claim(self):
        orchestrator = Orchestrator(self.store)
        for _ in range(4):
            self.assertEqual("not_configured", orchestrator.run_once()["worker_status"])
        reopened = WorkflowStore(self.path)
        Orchestrator(reopened).tick()
        self.assertEqual(1, len(reopened.tasks()))
        self.assertEqual(0, reopened.tasks()[0]["attempts"])

    def test_find_more_ranks_remaining_before_exploring_and_obeys_exclusions(self):
        newer = copy.deepcopy(self.catalog)
        for i in (4, 5):
            new = copy.deepcopy(newer["companies"][0]["jobs"][0])
            new.update(job_id=str(i), title=f"Engineer {i}", url=f"https://example.com/new-{i}", rank=i)
            newer["companies"][0]["jobs"].append(new)
        self.store.import_catalog(newer, "more known jobs")
        payload = self.decision("find_more", exclude_job_keys=["a:4"])
        receipt = self.store.receive_decision(payload)
        task = self.store.lease_task("ranker", kinds=["rank"])
        self.store.discovery_context(task["id"], task["lease_token"])
        self.assertEqual(receipt["task_ids"][0], task["id"])
        self.assertEqual(["a:5"], [j["key"] for j in task["payload"]["candidates"]])
        candidate = task["payload"]["candidates"][0]
        self.store.accept_ranking(task["id"], task["lease_token"], {"cards": [{"company_id": "a", "summary": "This role matches the confirmed experience.", "jobs": [
            {"key": candidate["key"], "revision": candidate["revision"], "reason": "Confirmed relevant experience.",
             "brief": {"work": "Build systems.", "fit": "Relevant engineering experience.", "gap": "Domain experience needs confirmation."}}]}]})
        self.assertEqual(["a:5"], [j["key"] for j in self.card()["jobs"]])

    def test_exploration_is_hidden_until_ranked_and_no_matches_is_valid(self):
        self.store.receive_decision(self.decision("find_more"))
        task = self.store.lease_task("explorer", kinds=["explore"])
        new = copy.deepcopy(self.catalog["companies"][0])
        new["jobs"] = [{"job_id": "new", "title": "New Engineer", "url": "https://example.com/new-role"}]
        next_task = self.store.accept_exploration(task["id"], task["lease_token"], {"companies": [new], "source": "https://example.com/careers"})
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT verified_at FROM wf_observations WHERE job_key='a:new'").fetchone()[0])
        self.assertFalse(any(c["company"]["id"] == "a" for c in self.store.feed()["cards"]))
        ranked = self.store.lease_task("ranker", kinds=["rank"])
        self.store.discovery_context(ranked["id"], ranked["lease_token"])
        self.assertEqual(next_task["next_task_id"], ranked["id"])
        result = self.store.accept_ranking(ranked["id"], ranked["lease_token"], {"cards": []})
        self.assertIsNone(result["next_task_id"])
        self.assertFalse(any(c["company"]["id"] == "a" for c in self.store.feed()["cards"]))

    def test_cancel_keeps_legacy_and_process_state_consistent_and_replays(self):
        receipt = self.store.receive_decision(self.decision())
        payload = {"request_id": "cancel-1", "task_id": receipt["task_ids"][0]}
        ack = self.store.cancel_task_command(payload)
        self.assertEqual(ack, self.store.cancel_task_command(payload))
        self.assertEqual("cancelled", self.store.state()["queue"][0]["status"])
        self.assertEqual("cancelled", self.store.processes()[0]["task_status"])

    def test_legacy_cancel_and_exclusion_cancel_v1_work(self):
        receipt = self.store.receive_decision(self.decision())
        self.store.cancel({"id": receipt["process_ids"][0]})
        self.assertEqual("cancelled", self.store.tasks()[0]["status"])
        receipt = self.store.receive_decision(self.decision(cid="b", request_id="b"))
        company = next(c for c in self.store.state()["companies"] if c["id"] == "b")
        self.store.edit_company({"id": "b", "revision": company["revision"], "excluded": True})
        self.assertEqual("cancelled", next(t for t in self.store.tasks() if t["id"] == receipt["task_ids"][0])["status"])

    def test_material_change_stops_authorized_work_before_dispatch(self):
        self.store.receive_decision(self.decision())
        updated = copy.deepcopy(self.catalog)
        updated["companies"][0]["jobs"][0]["title"] = "Changed scope"
        self.store.import_catalog(updated, "changed source")
        self.store.reconcile_processes()
        self.assertEqual("cancelled", self.store.tasks()[0]["status"])
        self.assertIsNone(self.store.lease_task("worker", kinds=["apply"]))

    def test_ranker_cannot_return_excluded_or_changed_candidates(self):
        self.store.receive_decision(self.decision("find_more"))
        task = self.store.lease_task("explorer", kinds=["explore"])
        result = self.store.accept_exploration(task["id"], task["lease_token"], {"companies": [], "source": "No eligible openings"})
        self.assertIsNone(result["next_task_id"])
        self.assertEqual("succeeded", self.store.tasks()[0]["status"])

    def test_orchestrator_resumes_questions_then_confirms_only_with_receipt(self):
        class FakeRuntime(ReadyRuntime):

            def apply(self, store, task):
                pid = task["payload"]["process_id"]
                if not store.questions():
                    store.ask_questions(pid, task["lease_token"], "form-1", [{
                        "id": "question-1", "text": "Your example answer?", "type": "text",
                        "required": True, "choices": [], "scope": "application"}])
                else:
                    store.before_submit(pid, task["lease_token"], "form-1")
                    store.confirm_submission(pid, task["lease_token"], {
                        "url": "https://example.com/receipt", "evidence": "Fictional test confirmation",
                        "observed_at": now(), "reference": "test-only"})

        self.store.receive_decision(self.decision())
        orchestrator = Orchestrator(self.store, FakeRuntime())
        self.addCleanup(orchestrator.readiness.close)
        orchestrator.readiness.start()
        orchestrator.readiness.wait(2)
        self.assertTrue(orchestrator.readiness.storage_ready)
        orchestrator.run_once()
        process = self.store.processes()[0]
        self.assertEqual("waiting_for_user", process["task_status"])
        self.store.answer_questions({"request_id": "answer", "process_id": process["id"],
            "process_revision": process["revision"], "form_version": "form-1", "answers": [
                {"question_id": "question-1", "question_revision": 1, "value": "Fictional answer"}]})
        orchestrator.run_once()
        self.assertEqual("submitted", self.store.processes()[0]["submission_status"])
        self.assertEqual("awaiting_response", self.store.processes()[0]["recruiting_status"])

    def test_orchestrator_does_not_replay_ambiguous_submission(self):
        class FakeRuntime(ReadyRuntime):

            def apply(self, store, task):
                store.before_submit(task["payload"]["process_id"], task["lease_token"], "form-1")
                raise OSError("Fictional loss of connection after submit")

        self.store.receive_decision(self.decision())
        orchestrator = Orchestrator(self.store, FakeRuntime())
        self.addCleanup(orchestrator.readiness.close)
        orchestrator.readiness.start()
        orchestrator.readiness.wait(2)
        self.assertTrue(orchestrator.readiness.storage_ready)
        with self.assertRaises(OSError):
            orchestrator.run_once()
        self.assertEqual("submission_unknown", self.store.processes()[0]["submission_status"])
        self.assertIsNone(self.store.lease_task("new-worker", kinds=["apply"]))


if __name__ == "__main__":
    unittest.main()
