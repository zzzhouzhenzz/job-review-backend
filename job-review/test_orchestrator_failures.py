"""Local contention recovery and redacted diagnostics; fictional data only."""
import errno
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from applicant_evidence import _read
from codex_transport import CodexWorkerStopped
from orchestrator import Orchestrator
from store import ReviewError
from test_readiness import ReadyRuntime
from test_store import catalog
from workflow import WorkflowStore


class OrchestratorFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WorkflowStore(self.root / "review.sqlite3", low_watermark=0)
        self.store.grant_project_consent({"request_id": "fixture-consent", "policy_version": 1, "accepted": True})
        self.store.import_catalog(catalog(), "fictional")
        card = self.store.feed()["cards"][0]
        receipt = self.store.receive_decision({"request_id": "apply", "action": "apply",
            "card_id": card["id"], "card_revision": card["revision"],
            "selected": [card["jobs"][0]["key"]]})
        self.task_id, self.process_id = receipt["task_ids"][0], receipt["process_ids"][0]

    def runner(self, runtime):
        runner = Orchestrator(self.store, runtime)
        self.addCleanup(runner.readiness.close)
        runner.readiness.start()
        self.assertTrue(runner.readiness.wait(2))
        return runner

    def task(self):
        return next(task for task in self.store.tasks() if task["id"] == self.task_id)

    def test_prelease_profile_contention_has_no_model_calls_or_warning_flood(self):
        runtime = ReadyRuntime()
        runner = self.runner(runtime)
        before = self.task()
        def synchronize(*, strict=False):
            if strict:
                raise ReviewError('Private profile text', reason='profile_busy')
            return {'status': 'pending', 'reason': 'profile_busy'}
        with patch.object(self.store, 'sync_profile_answers', side_effect=synchronize), \
                self.assertLogs('orchestrator', level='DEBUG') as logs:
            for _ in range(3):
                self.assertEqual({'worker_status': 'profile_busy', 'task_id': None},
                                 runner.run_once(kinds=['apply']))
        self.assertEqual(before, self.task())
        self.assertEqual(0, runtime.probe_calls)
        self.assertTrue(all(record.levelname == 'DEBUG' for record in logs.records))
        self.assertNotIn('Private profile text', '\n'.join(logs.output))

    def test_profile_lock_contention_requeues_without_losing_answers_or_upload_proof(self):
        previous = self.store.lease_task("fictional-previous-worker", kinds=["apply"])
        self.store.start_application(self.process_id, previous["lease_token"])
        checkpoint = {"question_bindings": {"q1": "start_date"},
                      "resume_upload_evidence": {"filename": "fictional-resume.pdf", "verified": True}}
        self.store.checkpoint_task(self.task_id, previous["lease_token"], checkpoint)
        process = self.store.ask_questions(self.process_id, previous["lease_token"], "fictional-form", [
            {"id": "q1", "text": "Start date?", "type": "text", "choices": [],
             "required": True, "scope": "application", "reason": "Fictional missing fact"}])
        self.store.answer_questions({"request_id": "answer", "process_id": self.process_id,
            "process_revision": process["revision"], "form_version": process["form_version"],
            "answers": [{"question_id": "q1", "question_revision": 1, "value": "Next month"}]})
        questions, before = self.store.questions(), self.store.processes()[0]
        profile = self.root / "profile.md"
        profile.write_text("Fictional profile.")

        class ContendedProfile(ReadyRuntime):
            application_profile = profile

            def apply(self, store, task):
                lock = profile.with_name(f".{profile.name}.job-review.lock")
                descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    store.sync_profile_answers(strict=True)
                finally:
                    os.close(descriptor)

        runtime = ContendedProfile()
        runner = self.runner(runtime)
        with self.assertLogs("orchestrator", level="WARNING") as logs:
            result = runner.run_once(kinds=["apply"])
        task, process = self.task(), self.store.processes()[0]
        self.assertEqual({"worker_status": "profile_busy", "task_id": self.task_id}, result)
        self.assertEqual(1, runtime.probe_calls)
        self.assertEqual("queued", task["status"])
        self.assertEqual(0, task["failure_count"])
        self.assertEqual(checkpoint, task["checkpoint"])
        self.assertEqual(questions, self.store.questions())
        self.assertEqual(before["snapshot"], process["snapshot"])
        self.assertEqual(before["decision_id"], process["decision_id"])
        self.assertEqual("queued", self.store.state()["queue"][0]["status"])
        self.assertIn('"reason": "profile_busy"', "\n".join(logs.output))
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_resource_locks").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0])

    def test_profile_busy_after_submission_checkpoint_never_requeues(self):
        class SubmissionStarted(ReadyRuntime):
            def apply(self, store, task):
                store.before_submit(task["payload"]["process_id"], task["lease_token"], "fictional-form")
                raise ReviewError("Private details must not appear", reason="profile_busy")

        runner = self.runner(SubmissionStarted())
        with self.assertLogs("orchestrator", level="ERROR"), self.assertRaises(ReviewError):
            runner.run_once(kinds=["apply"])
        task = self.task()
        self.assertEqual("waiting_for_user", task["status"])
        self.assertTrue(task["checkpoint"]["submission_started"])
        self.assertEqual("submission_unknown", self.store.processes()[0]["submission_status"])

    def test_profile_busy_during_preflight_is_not_treated_as_completed_browser_work(self):
        class IncompletePreflight(ReadyRuntime):
            def prepare_browser(self, task, lease):
                raise ReviewError("Private preflight detail", reason="profile_busy")

        runner = self.runner(IncompletePreflight())
        with self.assertLogs("orchestrator", level="ERROR"), self.assertRaises(ReviewError):
            runner.run_once(kinds=["apply"])
        self.assertEqual("failed", self.task()["status"])
        self.assertEqual(1, self.task()["failure_count"])
        self.assertEqual("not_started", self.store.processes()[0]["submission_status"])

    def test_profile_busy_cannot_release_quarantined_browser(self):
        class Quarantined(ReadyRuntime):
            def apply(self, store, task):
                store.quarantine_task(task["id"], task["lease_token"])
                raise ReviewError("Private quarantine detail", reason="profile_busy")

        runner = self.runner(Quarantined())
        with self.assertLogs("orchestrator", level="ERROR"), self.assertRaises(ReviewError):
            runner.run_once(kinds=["apply"])
        self.assertEqual("waiting_for_user", self.task()["status"])
        self.assertTrue(self.task()["resource_quarantined"])
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM wf_resource_locks WHERE quarantined=1").fetchone()[0])

    def test_profile_busy_cannot_release_another_workers_lease(self):
        class NewOwner(ReadyRuntime):
            def apply(self, store, task):
                with store.transaction(True) as db:
                    db.execute("UPDATE wf_tasks SET lease_owner=?,lease_token=? WHERE id=?",
                               ("another-worker", "another-token", task["id"]))
                    db.execute("UPDATE wf_resource_locks SET lease_token=? WHERE task_id=?",
                               ("another-token", task["id"]))
                raise ReviewError("Private lease detail", reason="profile_busy")

        runner = self.runner(NewOwner())
        with self.assertLogs("orchestrator", level="ERROR"), self.assertRaises(ReviewError):
            runner.run_once(kinds=["apply"])
        self.assertEqual("running", self.task()["status"])
        self.assertEqual("another-worker", self.task()["lease_owner"])
        self.assertEqual(0, self.task()["failure_count"])
        with self.store.transaction() as db:
            self.assertEqual("another-token", db.execute("SELECT lease_token FROM wf_resource_locks").fetchone()[0])

    def test_source_read_failure_remains_failed_and_diagnostics_exclude_private_data(self):
        private = "PRIVATE_SOURCE_TEXT_AND_TOKEN"

        class UnreadableSource(ReadyRuntime):
            def apply(self, store, task):
                denied = PermissionError(errno.EPERM, private, "/private/" + private)
                with patch("applicant_evidence.Path.open", side_effect=denied):
                    try:
                        _read(Path("/private/" + private))
                    except ReviewError as error:
                        self.failure = error
                        error.reason = private
                        raise

        runtime = UnreadableSource()
        runner = self.runner(runtime)
        with self.assertLogs("orchestrator", level="ERROR") as logs, self.assertRaises(ReviewError) as caught:
            runner.run_once(kinds=["apply"])
        self.assertIs(runtime.failure, caught.exception)
        self.assertIsInstance(caught.exception.__cause__, PermissionError)
        self.assertEqual("failed", self.task()["status"])
        self.assertEqual(1, self.task()["failure_count"])
        output = "\n".join(logs.output)
        self.assertNotIn(private, output)
        self.assertNotIn("/private/", output)
        self.assertNotIn(str(self.root), output)
        details = json.loads(logs.records[-1].getMessage().split("Worker dispatch failed: ", 1)[1])
        self.assertEqual("ReviewError", details["type"])
        self.assertEqual("unclassified", details["reason"])
        self.assertEqual("PermissionError", details["cause_type"])
        self.assertEqual(errno.EPERM, details["cause_errno"])
        self.assertTrue(any(frame["file"] == "applicant_evidence.py" and frame["function"] == "_read"
                            and type(frame["line"]) is int for frame in details["traceback"]))

    def test_posting_review_failure_has_a_safe_diagnostic_reason(self):
        from orchestrator import _log_failure
        error = ReviewError("PRIVATE_REVIEW_OUTPUT", reason="posting_review_invalid")
        with self.assertLogs("orchestrator", level="ERROR") as logs:
            _log_failure(error)
        self.assertNotIn("PRIVATE_REVIEW_OUTPUT", "\n".join(logs.output))
        details = json.loads(logs.records[0].getMessage().split("Worker dispatch failed: ", 1)[1])
        self.assertEqual("posting_review_invalid", details["reason"])

    def test_repeated_safe_application_stops_preserve_checkpoint_and_failure_budget(self):
        original = self.store.processes()[0]
        checkpoint = {"resume_upload_evidence": {"filename": "fictional.pdf"},
                      "question_bindings": {"q1": "start_date"}}
        previous = self.store.lease_task("fixture", kinds=["apply"])
        self.store.checkpoint_task(self.task_id, previous["lease_token"], checkpoint)
        with self.store.transaction(True) as db:
            self.store._release_task(db, self.task_id, "queued")

        class SafeStopped(ReadyRuntime):
            def apply(self, store, task):
                raise CodexWorkerStopped("Fictional local review stopped.")

        for attempt in range(5):
            runner = self.runner(SafeStopped())
            result = runner.run_once(kinds=["apply"])
            runner.readiness.close()
            self.assertEqual({"worker_status": "stopped", "task_id": self.task_id}, result)
            task, process = self.task(), self.store.processes()[0]
            self.assertEqual("queued", task["status"])
            self.assertEqual(0, task["failure_count"])
            self.assertEqual(attempt + 2, task["attempts"])
            self.assertEqual(checkpoint, task["checkpoint"])
            self.assertEqual(original["snapshot"], process["snapshot"])
            self.assertEqual(original["decision_id"], process["decision_id"])
            self.assertEqual("queued", self.store.state()["queue"][0]["status"])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_resource_locks").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0])

    def test_safe_stop_before_browser_spawn_requeues_without_starting_application(self):
        class NotSpawned(ReadyRuntime):
            def prepare_browser(self, task, lease):
                raise CodexWorkerStopped("Fictional call stopped before startup.")

        result = self.runner(NotSpawned()).run_once(kinds=["apply"])
        self.assertEqual("stopped", result["worker_status"])
        self.assertEqual("queued", self.task()["status"])
        self.assertEqual(0, self.task()["failure_count"])
        self.assertEqual("not_started", self.store.processes()[0]["submission_status"])

    def test_repeated_safe_rank_stops_keep_same_task_without_failure_increment(self):
        with self.store.transaction(True) as db:
            task_id = self.store._enqueue_task(db, "rank", {"candidates": []}, "fictional-rank")
        class LocalStopped(ReadyRuntime):
            def rank(self, context, lease):
                raise CodexWorkerStopped("Fictional rank call stopped.")

        for _ in range(5):
            runner = self.runner(LocalStopped())
            self.assertEqual({"worker_status": "stopped", "task_id": task_id}, runner.run_once(kinds=["rank"]))
            runner.readiness.close()
        task = next(task for task in self.store.tasks() if task["id"] == task_id)
        self.assertEqual("queued", task["status"])
        self.assertEqual(5, task["attempts"])
        self.assertEqual(0, task["failure_count"])

    def test_safe_stop_after_submission_checkpoint_parks_unknown_without_replay(self):
        class SubmissionStarted(ReadyRuntime):
            def apply(self, store, task):
                store.before_submit(task["payload"]["process_id"], task["lease_token"], "fictional-form")
                raise CodexWorkerStopped("Fictional submit call not spawned.")

        runner = self.runner(SubmissionStarted())
        self.assertEqual("submission_unknown", runner.run_once(kinds=["apply"])["worker_status"])
        task = self.task()
        self.assertEqual("waiting_for_user", task["status"])
        self.assertTrue(task["checkpoint"]["submission_started"])
        self.assertEqual(0, task["failure_count"])
        self.assertEqual("needs_input", self.store.state()["queue"][0]["status"])
        self.assertEqual("submission_unknown", self.store.processes()[0]["submission_status"])
        self.assertEqual("idle", runner.run_once(kinds=["apply"])["worker_status"])

    def assert_safe_stop_cannot_change_fenced_task(self, fence):
        class FencedStopped(ReadyRuntime):
            def apply(self, store, task):
                fence(store, task)
                with store.transaction() as db:
                    self.before_task = dict(store._task(db, task["id"]))
                    self.before_locks = [dict(row) for row in db.execute("SELECT * FROM wf_resource_locks")]
                raise CodexWorkerStopped("Fictional call stopped.")

        runtime = FencedStopped()
        runner = self.runner(runtime)
        with self.assertLogs("orchestrator", level="ERROR"), self.assertRaises(ReviewError):
            runner.run_once(kinds=["apply"])
        with self.store.transaction() as db:
            self.assertEqual(runtime.before_task, dict(self.store._task(db, self.task_id)))
            self.assertEqual(runtime.before_locks, [dict(row) for row in db.execute("SELECT * FROM wf_resource_locks")])

    def test_safe_stop_does_not_release_quarantine(self):
        self.assert_safe_stop_cannot_change_fenced_task(
            lambda store, task: store.quarantine_task(task["id"], task["lease_token"]))

    def test_safe_stop_does_not_change_expired_lease(self):
        def expire(store, task):
            with store.transaction(True) as db:
                db.execute("UPDATE wf_tasks SET lease_until='2000-01-01' WHERE id=?", (task["id"],))
        self.assert_safe_stop_cannot_change_fenced_task(expire)

    def test_safe_stop_does_not_change_another_workers_lease(self):
        def new_owner(store, task):
            with store.transaction(True) as db:
                db.execute("UPDATE wf_tasks SET lease_owner='other',lease_token='other-token' WHERE id=?", (task["id"],))
                db.execute("UPDATE wf_resource_locks SET lease_token='other-token' WHERE task_id=?", (task["id"],))
        self.assert_safe_stop_cannot_change_fenced_task(new_owner)

    def test_safe_stop_leaves_released_question_round_unchanged(self):
        def park(store, task):
            store.ask_questions(task["payload"]["process_id"], task["lease_token"], "fictional-form", [
                {"id": "q1", "text": "Fictional question?", "type": "text", "choices": [],
                 "required": True, "scope": "application", "reason": "Fictional missing fact"}])
        self.assert_safe_stop_cannot_change_fenced_task(park)
        self.assertEqual("waiting_for_user", self.task()["status"])
        self.assertEqual("questions_pending", self.task()["error"])
        self.assertEqual(1, len(self.store.questions()))

    def test_question_review_safe_stop_occurs_before_question_round_releases_lease(self):
        from codex_runtime import CodexRuntime
        from test_readiness import healthy_browser
        profile, resume, index = (self.root / name for name in ("profile.md", "resume.pdf", "index.json"))
        profile.write_text("Fictional candidate.")
        resume.write_text("Fictional placeholder; never uploaded.")
        index.write_text("[]")
        snapshot = self.store.processes()[0]["snapshot"]
        prepared = {"outcome": "questions", "job_id": snapshot["job_id"], "job_url": snapshot["url"],
            "observed_job": {key: snapshot.get(key) for key in
                             ("title", "url", "location", "arrangement", "salary", "level", "gaps")},
            "posting_unchanged": True, "posting_evidence": "Fictional exact posting verified.",
            "form_url": "https://example.com/form", "tab_id": "fictional-form",
            "form_fields": [{"name": "notice", "value": ""}], "answer_bindings": [], "blocker": "",
            "questions": [{"text": "Notice period?", "field_name": "notice", "type": "text",
                           "choices": [], "required": True, "reason": "Fictional missing fact."}]}
        case = self
        class LocalReviewStops:
            calls = []
            def run(self, role, prompt, schema, lease, *, browser=False):
                self.calls.append((role, browser))
                if role == "linkedin_session":
                    return healthy_browser()
                if role == "application_prepare":
                    return prepared
                case.assertEqual("application_question_review", role)
                case.assertFalse(browser)
                case.assertEqual("running", case.task()["status"])
                case.assertEqual([], case.store.questions())
                raise CodexWorkerStopped("Fictional local question review stopped.")

        transport = LocalReviewStops()
        runtime = CodexRuntime(transport, evidence_files=[profile], application_profile=profile,
                               resume=resume, application_index=index, supported_kinds=["apply"])
        result = self.runner(runtime).run_once(kinds=["apply"])
        self.assertEqual("stopped", result["worker_status"])
        self.assertEqual([("linkedin_session", True), ("application_prepare", True),
                          ("application_question_review", False)], transport.calls)
        self.assertEqual("queued", self.task()["status"])
        self.assertEqual(0, self.task()["failure_count"])
        self.assertEqual([], self.store.questions())


if __name__ == "__main__":
    unittest.main()
