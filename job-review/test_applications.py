"""Application state tests use fictional jobs and temporary databases only."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from application_flow import APPLICATION_SCHEMA, ApplicationMixin
from store import ReviewError, Store, now
from task_store import TASK_SCHEMA, TaskMixin
from test_store import catalog


class ApplicationStore(ApplicationMixin, TaskMixin, Store):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.executescript(TASK_SCHEMA + APPLICATION_SCHEMA)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.store = ApplicationStore(self.path)
        self.store.import_catalog(catalog(), "fictional-test")

    def tearDown(self):
        self.temp.cleanup()

    def authorize(self, key="a:1", request_id="apply-1"):
        with self.store.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='approve',revision=revision+1 WHERE key=?", (key,))
            return self.store._create_application(db, self.store._row(db, "jobs", "key", key), request_id)

    def process(self, process_id):
        return next(p for p in self.store.processes() if p["id"] == process_id)

    def start(self):
        process_id = self.authorize()
        task = self.store.lease_task("test-worker", kinds=["apply"])
        self.store.start_application(process_id, task["lease_token"])
        return process_id, task

    def ask(self, process_id, task, questions=None):
        if questions is None:
            questions = [{"id": "q1", "text": "What is your answer?", "type": "text", "required": True, "scope": "application"}]
        return self.store.ask_questions(process_id, task["lease_token"], "form-v1", questions)

    def answer(self, process_id, answers=None, request_id="answer-1"):
        if answers is None:
            answers = [{"question_id": "q1", "question_revision": 1, "value": "Confirmed answer"}]
        return {"request_id": request_id, "process_id": process_id,
                "process_revision": self.process(process_id)["revision"], "form_version": "form-v1", "answers": answers}

    def receipt(self):
        return {"url": "https://jobs.example/confirmation/123", "evidence": "Application received. Reference 123.",
                "observed_at": now(), "reference": "123"}

    def conflict(self, callback):
        with self.assertRaises(ReviewError) as caught:
            callback()
        self.assertEqual(caught.exception.status, 409)

    def test_legacy_approval_and_queue_do_not_create_processes(self):
        self.store.edit_job({"key": "a:1", "revision": 1, "decision": "approve", "note": ""})
        self.store.queue_jobs({"request_id": "legacy", "jobs": [{"key": "a:1", "revision": 2}]})
        self.assertEqual(self.store.processes(), [])
        self.assertEqual(self.store.tasks(), [])
        self.conflict(lambda: self.authorize())

    def test_apply_snapshot_and_legacy_projection_survive_restart(self):
        pid = self.authorize()
        process = self.process(pid)
        self.assertEqual(process["snapshot"]["title"], "Engineer 1")
        self.assertEqual(process["snapshot"]["job_revision"], 2)
        self.assertEqual(process["submission_status"], "not_started")
        self.assertEqual(process["recruiting_status"], "awaiting_response")
        self.assertEqual(process["task_status"], "queued")
        self.assertEqual(self.store.state()["queue"][0]["id"], pid)
        self.assertEqual(ApplicationStore(self.path).processes(), self.store.processes())
        self.conflict(lambda: self.authorize(request_id="apply-again"))

    def test_preparation_requires_live_fenced_lease_and_current_job(self):
        pid = self.authorize()
        task = self.store.lease_task("worker", ["apply"])
        self.conflict(lambda: self.store.start_application(pid, "wrong-token"))
        changed = catalog()
        changed["companies"][0]["jobs"][0]["title"] = "Changed job"
        self.store.import_catalog(changed, "new-observation")
        self.conflict(lambda: self.store.start_application(pid, task["lease_token"]))
        self.assertEqual(self.process(pid)["submission_status"], "not_started")

    def test_questions_release_resource_and_answers_resume_same_task(self):
        pid, task = self.start()
        self.ask(pid, task)
        process = self.process(pid)
        self.assertEqual(process["task_status"], "waiting_for_user")
        self.assertEqual(self.store.state()["queue"][0]["status"], "needs_input")
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_resource_locks").fetchone()[0], 0)
        payload = self.answer(pid)
        receipt = self.store.answer_questions(payload)
        self.assertEqual(receipt["task_ids"], [task["id"]])
        self.assertEqual(self.process(pid)["task_status"], "queued")
        self.assertEqual(self.store.questions()[0]["answer"], "Confirmed answer")
        self.assertEqual(self.store.questions()[0]["revision"], 2)
        self.assertEqual(ApplicationStore(self.path).answer_questions(payload), receipt)
        self.assertEqual(self.store.state()["queue"][0]["status"], "queued")
        different = copy.deepcopy(payload)
        different["answers"][0]["value"] = "Changed answer"
        self.conflict(lambda: self.store.answer_questions(different))

    def test_partial_answers_wait_and_invalid_batch_is_atomic(self):
        pid, task = self.start()
        self.ask(pid, task, [
            {"id": "q1", "text": "Choose one", "type": "single_choice", "required": True,
             "choices": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]},
            {"id": "q2", "text": "Explain", "type": "text", "required": True},
        ])
        invalid = self.answer(pid, [{"question_id": "q1", "question_revision": 1, "value": "yes"},
                                    {"question_id": "q2", "question_revision": 1, "value": ""}])
        with self.assertRaises(ReviewError):
            self.store.answer_questions(invalid)
        self.assertTrue(all(q["answer"] is None for q in self.store.questions()))
        self.store.answer_questions(self.answer(pid, [{"question_id": "q1", "question_revision": 1, "value": "no"}]))
        self.assertEqual(self.process(pid)["task_status"], "waiting_for_user")
        self.store.answer_questions(self.answer(pid, [{"question_id": "q2", "question_revision": 1, "value": "Explanation"}], "answer-2"))
        self.assertEqual(self.process(pid)["task_status"], "queued")

    def test_answer_conflicts_on_stale_process_form_or_question(self):
        pid, task = self.start()
        self.ask(pid, task)
        payload = self.answer(pid)
        for field, value in (("process_revision", 1), ("form_version", "old-form")):
            invalid = {**payload, field: value}
            self.conflict(lambda: self.store.answer_questions(invalid))
        invalid = copy.deepcopy(payload)
        invalid["answers"][0]["question_revision"] = 0
        self.conflict(lambda: self.store.answer_questions(invalid))
        self.assertIsNone(self.store.questions()[0]["answer"])

    def test_new_question_round_preserves_old_answers_without_reuse(self):
        pid, task = self.start()
        self.ask(pid, task)
        self.store.answer_questions(self.answer(pid))
        resumed = self.store.lease_task("worker", ["apply"])
        self.store.start_application(pid, resumed["lease_token"])
        self.store.ask_questions(pid, resumed["lease_token"], "form-v2", [
            {"id": "q2", "text": "Changed question", "type": "text", "required": True}],
            preserve_answer_ids=["q1"])
        questions = self.store.questions()
        self.assertEqual([q["id"] for q in questions], ["q1", "q2"])
        self.assertEqual(questions[0]["answer"], "Confirmed answer")
        self.assertEqual(questions[0]["form_version"], "form-v2")
        self.assertIsNone(questions[1]["answer"])
        with self.store.transaction() as db:
            old = db.execute("SELECT * FROM wf_questions WHERE id='q1'").fetchone()
            self.assertEqual(json.loads(old["answer"]), "Confirmed answer")
            self.assertEqual(old["superseded"], 0)
            self.assertEqual(old["revision"], 2)

    def test_multi_choice_validation_does_not_infer_answers(self):
        pid, task = self.start()
        self.ask(pid, task, [{"id": "q1", "text": "Select all", "type": "multi_choice", "required": True,
                             "choices": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}])
        for value in ([], ["a", "a"], ["other"], "a"):
            with self.assertRaises(ReviewError):
                self.store.answer_questions(self.answer(pid, [{"question_id": "q1", "question_revision": 1, "value": value}]))
        self.store.answer_questions(self.answer(pid, [{"question_id": "q1", "question_revision": 1, "value": ["a", "b"]}]))
        self.assertEqual(self.store.questions()[0]["answer"], ["a", "b"])

    def test_submission_checkpoint_persists_before_confirmation(self):
        pid, task = self.start()
        self.conflict(lambda: self.store.confirm_submission(pid, task["lease_token"], self.receipt()))
        checkpoint = self.store.before_submit(pid, task["lease_token"], "form-v1")
        persisted = ApplicationStore(self.path)
        self.assertEqual(persisted.processes()[0]["submission_status"], "submission_unknown")
        self.assertTrue(persisted.tasks()[0]["checkpoint"]["submission_started"])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT id FROM wf_submission_attempts").fetchone()[0], checkpoint["attempt_id"])
        self.conflict(lambda: self.store.before_submit(pid, task["lease_token"], "form-v1"))
        submitted = self.store.confirm_submission(pid, task["lease_token"], self.receipt())
        self.assertEqual(submitted["submission_status"], "submitted")
        self.assertEqual(submitted["recruiting_status"], "awaiting_response")
        self.assertEqual(submitted["task_status"], "succeeded")
        self.assertEqual(self.store.state()["queue"][0]["status"], "applied")
        self.conflict(lambda: self.authorize(request_id="duplicate-apply"))

    def test_submission_receipt_requires_evidence_url_and_timestamp(self):
        pid, task = self.start()
        self.store.before_submit(pid, task["lease_token"], "form-v1")
        for field, value in (("evidence", ""), ("url", "http://jobs.example/ok"), ("observed_at", "yesterday"),
                             ("observed_at", "2020-01-01T00:00:00"), ("observed_at", "2999-01-01T00:00:00+00:00")):
            with self.assertRaises(ReviewError):
                self.store.confirm_submission(pid, task["lease_token"], {**self.receipt(), field: value})
        self.assertEqual(self.process(pid)["submission_status"], "submission_unknown")

    def test_failure_after_submit_is_never_retryable_or_cancellable(self):
        pid, task = self.start()
        self.store.before_submit(pid, task["lease_token"], "form-v1")
        failed = self.store.fail_task(task["id"], task["lease_token"], "Browser disconnected", retryable=True)
        self.assertEqual(failed["status"], "waiting_for_user")
        self.store.reconcile_processes()
        self.assertEqual(self.process(pid)["submission_status"], "submission_unknown")
        self.assertEqual(self.process(pid)["block_reason"], "submission_unknown")
        self.assertIsNone(self.store.lease_task("other-worker", ["apply"]))
        with self.store.transaction(True) as db:
            self.conflict(lambda: self.store._resume_task(db, task["id"]))
            self.conflict(lambda: self.store._cancel_process(db, self.store._task(db, task["id"])))
        self.conflict(lambda: self.authorize(request_id="retry-new-apply"))

    def test_expired_worker_requires_stop_ack_and_cannot_confirm(self):
        pid, task = self.start()
        self.store.before_submit(pid, task["lease_token"], "form-v1")
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (task["id"],))
        self.store.recover_expired_tasks()
        self.store.reconcile_processes()
        self.assertTrue(self.store.tasks()[0]["resource_quarantined"])
        self.conflict(lambda: self.store.confirm_submission(pid, task["lease_token"], self.receipt()))
        self.conflict(lambda: self.store.resolve_submission(pid, self.process(pid)["revision"], self.receipt()))
        self.store.acknowledge_worker_stopped(task["id"])
        self.assertIsNone(self.store.lease_task("new-worker", ["apply"]))
        self.assertEqual(self.process(pid)["submission_status"], "submission_unknown")
        result = self.store.resolve_submission(pid, self.process(pid)["revision"], self.receipt())
        self.assertEqual(result["submission_status"], "submitted")
        self.assertEqual(result["task_status"], "succeeded")
        self.assertEqual(result["events"][-1]["actor"], "operator")

    def test_submission_resolution_rejects_running_worker_and_old_receipt(self):
        pid, task = self.start()
        old_receipt = self.receipt()
        self.store.before_submit(pid, task["lease_token"], "form-v1")
        self.conflict(lambda: self.store.resolve_submission(pid, self.process(pid)["revision"], self.receipt()))
        self.conflict(lambda: self.store.confirm_submission(pid, task["lease_token"], old_receipt))
        self.store.fail_task(task["id"], task["lease_token"], "Lost confirmation")
        self.conflict(lambda: self.store.resolve_submission(pid, 1, self.receipt()))
        result = self.store.resolve_submission(pid, self.process(pid)["revision"], self.receipt())
        self.assertEqual(result["submission_status"], "submitted")

    def test_known_closed_job_blocks_start_and_submit(self):
        pid, task = self.start()
        with self.store.transaction(True) as db:
            db.execute("CREATE TABLE wf_observations (job_key TEXT PRIMARY KEY, availability TEXT)")
            db.execute("INSERT INTO wf_observations VALUES ('a:1','closed')")
        self.conflict(lambda: self.store.start_application(pid, task["lease_token"]))
        self.conflict(lambda: self.store.before_submit(pid, task["lease_token"], "form-v1"))
        self.store.reconcile_processes()
        process = self.process(pid)
        self.assertEqual(process["block_reason"], "authorization_changed")
        revision = process["revision"]
        self.assertEqual(self.store.reconcile_processes(), [])
        self.assertEqual(self.process(pid)["revision"], revision)

    def test_reconcile_cancels_stale_queued_work_without_starting_it(self):
        pid = self.authorize()
        changed = catalog()
        changed["companies"][0]["jobs"][0]["salary"] = "$999"
        self.store.import_catalog(changed, "updated")
        self.assertEqual(self.store.reconcile_processes(), [pid])
        self.assertEqual(self.process(pid)["task_status"], "cancelled")
        self.assertEqual(self.process(pid)["block_reason"], "task_cancelled")
        self.assertIsNone(self.store.lease_task("worker", ["apply"]))
        self.assertEqual(self.store.reconcile_processes(), [])

    def test_retry_wait_projects_to_legacy_queued_and_can_be_cancelled(self):
        pid, task = self.start()
        self.store.fail_task(task["id"], task["lease_token"], "Temporary page failure")
        self.store.reconcile_processes()
        process = self.process(pid)
        self.assertEqual(process["task_status"], "retry_wait")
        self.assertEqual(process["block_reason"], "task_retry_wait")
        self.assertEqual(self.store.state()["queue"][0]["status"], "queued")
        self.assertEqual(self.store.reconcile_processes(), [])
        self.assertEqual(self.process(pid)["revision"], process["revision"])
        self.store.cancel({"id": pid})
        self.store.reconcile_processes()
        self.assertEqual(self.process(pid)["task_status"], "cancelled")

    def test_reconcile_blocks_changed_running_work_without_releasing_browser(self):
        pid, task = self.start()
        changed = catalog()
        changed["companies"][0]["jobs"][0]["title"] = "Changed title"
        self.store.import_catalog(changed, "updated")
        self.store.reconcile_processes()
        self.assertEqual(self.process(pid)["block_reason"], "authorization_changed")
        self.assertEqual(self.process(pid)["task_status"], "running")
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_resource_locks").fetchone()[0], 1)
        self.conflict(lambda: self.store.before_submit(pid, task["lease_token"], "form-v1"))

    def test_recruiting_tracker_requires_submission_and_separate_evidence(self):
        pid, task = self.start()
        self.conflict(lambda: self.store.track_response(pid, self.process(pid)["revision"], "responded", "Recruiter email"))
        self.store.before_submit(pid, task["lease_token"], "form-v1")
        process = self.store.confirm_submission(pid, task["lease_token"], self.receipt())
        with self.assertRaises(ReviewError):
            self.store.track_response(pid, process["revision"], "responded", "")
        responded = self.store.track_response(pid, process["revision"], "responded", "Recruiter email asks for availability.")
        self.assertEqual(responded["recruiting_status"], "responded")
        self.assertEqual(responded["submission_status"], "submitted")
        self.conflict(lambda: self.store.track_response(pid, responded["revision"], "awaiting_response", "Invalid regression"))
        rejected = self.store.track_response(pid, responded["revision"], "rejected", "Employer sent a rejection.")
        self.conflict(lambda: self.store.track_response(pid, rejected["revision"], "interviewing", "Invalid reopening"))


if __name__ == "__main__":
    unittest.main()
