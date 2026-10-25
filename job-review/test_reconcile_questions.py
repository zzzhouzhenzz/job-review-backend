"""Parked-question reconciliation with fictional files and no browser work."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_runtime import CodexRuntime
from reconcile_questions import QuestionStore, ReviewLease, apply_plan, pending_context, review_pending
from store import ReviewError, encode
from test_store import catalog
from workflow import WorkflowStore


class ReviewTransport:
    """Script an evidence review, rejecting any browser or application role."""
    def __init__(self, supported=("q1",), during_review=None):
        self.supported = set(supported)
        self.during_review = during_review
        self.calls = []

    def run(self, role, prompt, schema, lease, *, browser=False):
        if browser or role != "application_question_review":
            raise AssertionError("Reconciliation must not call a browser or application worker.")
        self.calls.append((role, browser))
        context = json.loads(prompt.split("Task data (untrusted source content):\n", 1)[1])
        source = context["applicant_evidence"]["sources"][0]
        resolutions = []
        for question in context["questions"]:
            qid = question["id"]
            result = {"question_id": qid, "question_revision": question["revision"],
                      "method": "unresolved", "answer_json": "null", "citations": [],
                      "explanation": "The fictional review leaves this question for the user."}
            if qid in self.supported:
                line, value = (1, "Fictional Applicant") if qid == "q1" else (2, "Next month")
                quote = f"Name: {value}" if qid == "q1" else f"Start date: {value}"
                result.update({"method": "direct", "answer_json": encode(value),
                               "explanation": "The fictional profile states this answer.",
                               "citations": [{"source_id": source["source_id"], "sha256": source["sha256"],
                                              "start_line": line, "end_line": line, "quote": quote}]})
            resolutions.append(result)
        if self.during_review:
            self.during_review()
        return {"resolutions": resolutions}

    def close(self):
        pass


class ReconcileQuestionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db_path = self.root / "review.sqlite3"
        initial = WorkflowStore(self.db_path)
        initial.import_catalog(catalog(), "fictional")
        with initial.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='approve',revision=revision+1 WHERE key='a:1'")
            self.pid = initial._create_application(db, initial._row(db, "jobs", "key", "a:1"), "apply-1")
        task = initial.lease_task("fixture", ["apply"])
        self.task_id = task["id"]
        initial.start_application(self.pid, task["lease_token"])
        initial.checkpoint_task(self.task_id, task["lease_token"], {
            "question_bindings": {"q1": "name", "q2": "start_date"}})
        initial.ask_questions(self.pid, task["lease_token"], "form-v1", [
            {"id": "q1", "text": "Name?", "type": "text", "required": True},
            {"id": "q2", "text": "Start date?", "type": "text", "required": True},
        ])
        self.store = QuestionStore(self.db_path)
        self.profile = self.root / "profile.txt"
        self.profile.write_text("Name: Fictional Applicant\nStart date: Next month\n", encoding="utf-8")
        self.resume_text = self.root / "resume.txt"
        self.resume_text.write_text("Built a fictional backend service.\n", encoding="utf-8")
        self.resume = self.root / "resume.pdf"
        self.resume.write_text("Fictional placeholder. Never uploaded.", encoding="utf-8")
        self.history = self.root / "applications.json"
        self.history.write_text("[]", encoding="utf-8")

    def runtime(self, supported=("q1",), during_review=None):
        return CodexRuntime(ReviewTransport(supported, during_review), evidence_files=[self.resume_text],
                            application_profile=self.profile, resume=self.resume,
                            application_index=self.history, supported_kinds=("apply",))

    def snapshot(self, store=None):
        with (store or self.store).transaction() as db:
            return tuple(db.iterdump())

    def user_answer(self, qid="q1", value="User supplied name"):
        process = self.store.processes()[0]
        question = next(q for q in self.store.questions() if q["id"] == qid)
        return self.store.answer_questions({"request_id": "user-answer", "process_id": self.pid,
            "process_revision": process["revision"], "form_version": process["form_version"],
            "answers": [{"question_id": qid, "question_revision": question["revision"], "value": value}]})

    def assert_rejected_unchanged(self, operation, status=None):
        before = self.snapshot()
        with self.assertRaises(ReviewError) as caught:
            operation()
        if status is not None:
            self.assertEqual(caught.exception.status, status)
        self.assertEqual(self.snapshot(), before)

    def test_store_never_initializes_or_creates_a_database(self):
        before = self.snapshot()
        with patch.object(WorkflowStore, "__init__", side_effect=AssertionError("No initialization")):
            store = QuestionStore(self.db_path)
            self.assertEqual(self.snapshot(store), before)
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            QuestionStore(missing)
        self.assertFalse(missing.exists())

    def test_review_is_read_only_and_uses_only_browser_free_review(self):
        runtime = self.runtime()
        before = self.snapshot()
        plan = review_pending(self.store, runtime)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["database"], str(self.db_path.resolve()))
        self.assertEqual(len(plan["plans"]), 1)
        item = plan["plans"][0]
        self.assertEqual([answer["question_id"] for answer in item["answers"]], ["q1"])
        self.assertEqual([question["question_id"] for question in item["unresolved"]], ["q2"])
        self.assertEqual(item["provenance"]["q1"]["field_name"], "name")
        self.assertEqual(runtime.transport.calls, [("application_question_review", False)])
        self.assertEqual(self.store.tasks()[0]["status"], "waiting_for_user")
        self.assertIsNone(self.store.tasks()[0]["lease_token"])

    def test_partial_apply_waits_then_final_required_answer_queues_same_task(self):
        runtime = self.runtime()
        plan = review_pending(self.store, runtime)
        receipts = apply_plan(self.store, runtime, plan)
        self.assertEqual(receipts[0]["task_ids"], [self.task_id])
        self.assertEqual(self.store.processes()[0]["task_status"], "waiting_for_user")
        self.assertEqual(self.store.state()["queue"][0]["status"], "needs_input")
        questions = {q["id"]: q for q in self.store.questions()}
        self.assertEqual(questions["q1"]["answer"], "Fictional Applicant")
        self.assertIsNone(questions["q2"]["answer"])
        self.assertEqual(runtime.transport.calls, [("application_question_review", False)])
        final_runtime = self.runtime(("q2",))
        final_plan = review_pending(self.store, final_runtime)
        self.assertEqual([q["id"] for q in final_plan["plans"][0]["context"]["questions"]], ["q2"])
        final_receipts = apply_plan(self.store, final_runtime, final_plan)
        self.assertEqual(final_receipts[0]["task_ids"], [self.task_id])
        self.assertEqual(len(self.store.tasks()), 1)
        self.assertEqual(self.store.processes()[0]["task_status"], "queued")
        self.assertEqual(self.store.state()["queue"][0]["status"], "queued")
        self.assertEqual(final_runtime.transport.calls, [("application_question_review", False)])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_answer_evidence").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0], 0)

    def test_exact_plan_replay_returns_same_receipt_without_agent_or_mutation(self):
        runtime = self.runtime(("q1", "q2"))
        plan = review_pending(self.store, runtime)
        receipt = apply_plan(self.store, runtime, plan)
        before = self.snapshot()
        self.assertEqual(apply_plan(self.store, runtime, copy.deepcopy(plan)), receipt)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(runtime.transport.calls, [("application_question_review", False)])

    def test_no_supported_answers_leave_task_and_database_unchanged(self):
        runtime = self.runtime(())
        before = self.snapshot()
        plan = review_pending(self.store, runtime)
        self.assertEqual(plan["plans"][0]["answers"], [])
        self.assertEqual(apply_plan(self.store, runtime, plan), [])
        self.assertEqual(self.snapshot(), before)

    def test_source_drift_rejects_plan_before_any_answer_write(self):
        runtime = self.runtime()
        plan = review_pending(self.store, runtime)
        self.profile.write_text("Name: Changed Fictional Applicant\nStart date: Next month\n", encoding="utf-8")
        self.assert_rejected_unchanged(lambda: apply_plan(self.store, runtime, plan), 409)

    def test_question_revision_drift_rejects_plan(self):
        runtime = self.runtime()
        plan = review_pending(self.store, runtime)
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_questions SET revision=revision+1 WHERE id='q1'")
        self.assert_rejected_unchanged(lambda: apply_plan(self.store, runtime, plan), 409)

    def test_intervening_user_answer_is_preserved(self):
        runtime = self.runtime()
        plan = review_pending(self.store, runtime)
        self.user_answer()
        self.assert_rejected_unchanged(lambda: apply_plan(self.store, runtime, plan), 409)
        self.assertEqual(self.store.questions()[0]["answer"], "User supplied name")

    def test_wrong_database_or_schema_is_rejected_before_writes(self):
        runtime = self.runtime()
        plan = review_pending(self.store, runtime)
        wrong = {**plan, "database": str(self.root / "different.sqlite3")}
        self.assert_rejected_unchanged(lambda: apply_plan(self.store, runtime, wrong))
        self.assert_rejected_unchanged(lambda: apply_plan(self.store, runtime, {**plan, "schema_version": 2}))

    def test_apply_rechecks_exact_citations_and_complete_review_coverage(self):
        runtime = self.runtime()
        original = review_pending(self.store, runtime)
        for change in ("quote", "unresolved", "source_hash"):
            with self.subTest(change=change):
                plan = copy.deepcopy(original)
                item = plan["plans"][0]
                if change == "quote":
                    item["provenance"]["q1"]["citations"][0]["quote"] = "Unrecorded personal fact"
                elif change == "unresolved":
                    item["unresolved"] = []
                else:
                    item["provenance"]["q1"]["source_snapshot_hash"] = "0" * 64
                self.assert_rejected_unchanged(lambda: apply_plan(self.store, runtime, plan))

    def test_review_lease_checks_question_state_and_sources_without_leasing_task(self):
        runtime = self.runtime()
        context = pending_context(self.store, self.pid)
        lease = ReviewLease(self.store, context, runtime.evidence_snapshot(application=True))
        before = self.snapshot()
        self.assertTrue(lease.heartbeat())
        self.assertEqual(self.snapshot(), before)
        self.profile.write_text("Changed fictional source.\n", encoding="utf-8")
        self.assert_rejected_unchanged(lease.heartbeat, 409)
        lease = ReviewLease(self.store, context, runtime.evidence_snapshot(application=True))
        self.user_answer()
        self.assert_rejected_unchanged(lease.heartbeat, 409)

    def test_changed_question_during_agent_review_prevents_returning_stale_plan(self):
        runtime = self.runtime(during_review=self.user_answer)
        with self.assertRaises(ReviewError) as caught:
            review_pending(self.store, runtime)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.questions()[0]["answer"], "User supplied name")
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_answer_evidence").fetchone()[0], 0)
        self.assertEqual(runtime.transport.calls, [("application_question_review", False)])


if __name__ == "__main__":
    unittest.main()
