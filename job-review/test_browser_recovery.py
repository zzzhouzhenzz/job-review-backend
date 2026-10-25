"""Browser recovery uses fictional runtimes and temporary databases only."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from orchestrator import Orchestrator
from readiness import BrowserNotReady, BrowserRecoverable, BrowserWorkUncertain, WorkerCapacityUnavailable
from store import ReviewError, now
from test_readiness import ReadyRuntime
from test_store import catalog
from workflow import WorkflowStore


class BrowserRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fictional.sqlite3"
        self.store = WorkflowStore(self.path, low_watermark=0)
        self.store.grant_project_consent({"request_id": "consent", "policy_version": 1, "accepted": True})
        self.store.import_catalog(catalog(), "fictional")
        card = self.store.feed()["cards"][0]
        receipt = self.store.receive_decision({"request_id": "apply", "action": "apply",
            "card_id": card["id"], "card_revision": card["revision"], "selected": [card["jobs"][0]["key"]]})
        self.task_id, self.pid = receipt["task_ids"][0], receipt["process_ids"][0]

    def runner(self, runtime):
        runner = Orchestrator(self.store, runtime)
        self.addCleanup(runner.readiness.close)
        runner.readiness.start()
        runner.readiness.wait(2)
        self.assertTrue(runner.readiness.storage_ready)
        return runner

    def task(self):
        return next(row for row in self.store.tasks() if row["id"] == self.task_id)

    def process(self):
        return next(row for row in self.store.processes() if row["id"] == self.pid)

    def due(self):
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET available_at='2000-01-01' WHERE id=?", (self.task_id,))

    def test_capacity_backoff_preserves_answers_upload_and_resumes_after_restart(self):
        first = self.store.lease_task('fixture', kinds=['apply'])
        self.store.start_application(self.pid, first['lease_token'])
        upload = {'resume_upload_evidence': {'filename': 'fictional.pdf', 'verified': True}}
        self.store.checkpoint_task(self.task_id, first['lease_token'], upload)
        process = self.store.ask_questions(self.pid, first['lease_token'], 'form', [
            {'id': 'q', 'text': 'Notice?', 'type': 'text', 'required': True, 'scope': 'application'}])
        self.store.answer_questions({'request_id': 'answer', 'process_id': self.pid,
            'process_revision': process['revision'], 'form_version': 'form', 'answers': [
                {'question_id': 'q', 'question_revision': 1, 'value': 'Two weeks'}]})
        answers, original = self.store.questions(), self.process()
        case = self
        class Capacity(ReadyRuntime):
            def prepare_browser(self, task, lease):
                self.probe_calls += 1
                raise WorkerCapacityUnavailable()
            def apply(self, store, task):
                case.fail('No browser work can start before readiness succeeds')
        runtime = Capacity()
        runner = self.runner(runtime)
        self.assertEqual('retry_wait', runner.run_once(kinds=['apply'])['worker_status'])
        prior = self.task()
        self.assertEqual(0, prior['failure_count'])
        self.assertEqual({**upload, 'worker_capacity_failures': 1}, prior['checkpoint'])
        self.assertEqual(answers, self.store.questions())
        self.assertEqual(original['decision_id'], self.process()['decision_id'])
        self.assertEqual(original['snapshot'], self.process()['snapshot'])
        self.assertEqual(prior['available_at'], runner.readiness.snapshot()['retry_at'])
        self.assertEqual('idle', runner.run_once(kinds=['apply'])['worker_status'])
        self.assertEqual(1, runtime.probe_calls)
        runner.readiness.close()
        self.store = WorkflowStore(self.path, low_watermark=0)
        self.assertEqual(prior, self.task())
        class Healthy(ReadyRuntime):
            def apply(self, store, task):
                case.assertEqual(1, self.probe_calls)
                case.assertEqual(prior['checkpoint'], task['checkpoint'])
                case.assertEqual(answers, store.questions())
                store.before_submit(case.pid, task['lease_token'], 'form')
                store.confirm_submission(case.pid, task['lease_token'], {
                    'url': 'https://example.com/receipt/fictional', 'evidence': 'Fictional accepted',
                    'observed_at': now(), 'reference': 'fictional'})
        resumed = self.runner(Healthy())
        self.assertEqual('idle', resumed.run_once(kinds=['apply'])['worker_status'])
        self.due()
        self.assertEqual('completed_dispatch', resumed.run_once(kinds=['apply'])['worker_status'])
        self.assertEqual('submitted', self.process()['submission_status'])
        self.assertEqual('idle', resumed.run_once(kinds=['apply'])['worker_status'])
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_capacity_has_three_durable_retries_and_cannot_be_reset_by_worker(self):
        class Capacity(ReadyRuntime):
            def prepare_browser(self, task, lease):
                raise WorkerCapacityUnavailable()
        runner = self.runner(Capacity())
        for count, delay in enumerate((30, 60, 120), start=1):
            self.assertEqual('retry_wait', runner.run_once(kinds=['apply'])['worker_status'])
            task = self.task()
            self.assertEqual(count, task['checkpoint']['worker_capacity_failures'])
            self.assertEqual(0, task['failure_count'])
            delta = datetime.fromisoformat(task['available_at']) - datetime.now(timezone.utc)
            self.assertGreater(delta.total_seconds(), delay - 2)
            self.due()
            lease = self.store.lease_task('fixture', kinds=['apply'])
            self.store.checkpoint_task(self.task_id, lease['lease_token'], {'progress': 'saved'})
            self.assertEqual(count, self.task()['checkpoint']['worker_capacity_failures'])
            with self.assertRaises(ReviewError):
                self.store.checkpoint_task(self.task_id, lease['lease_token'], {'worker_capacity_failures': 0})
            with self.store.transaction(True) as db:
                self.store._release_task(db, self.task_id, 'queued')
        self.assertEqual('failed', runner.run_once(kinds=['apply'])['worker_status'])
        self.assertEqual(4, self.task()['checkpoint']['worker_capacity_failures'])
        self.assertEqual('model_at_capacity', self.task()['error'])
        self.assertEqual('idle', runner.run_once(kinds=['apply'])['worker_status'])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_resource_locks').fetchone()[0])

    def test_capacity_never_replays_started_submission(self):
        case = self
        class Capacity(ReadyRuntime):
            def apply(self, store, task):
                store.before_submit(case.pid, task['lease_token'], 'form')
                raise WorkerCapacityUnavailable()
        runner = self.runner(Capacity())
        self.assertEqual('waiting_for_user', runner.run_once(kinds=['apply'])['worker_status'])
        self.assertEqual('submission_unknown', self.task()['error'])
        self.assertEqual('idle', runner.run_once(kinds=['apply'])['worker_status'])

    def test_capacity_recovery_requires_current_unquarantined_lease(self):
        task = self.store.lease_task('fixture', kinds=['apply'])
        with self.assertRaises(ReviewError):
            self.store.schedule_worker_capacity_recovery(self.task_id, 'wrong-owner')
        self.assertEqual(task, self.task())
        self.store.quarantine_task(self.task_id, task['lease_token'])
        quarantined = self.task()
        with self.assertRaises(ReviewError):
            self.store.schedule_worker_capacity_recovery(self.task_id, task['lease_token'])
        self.assertEqual(quarantined, self.task())

    def park(self, reason="application_action_required", *, checkpoint=None):
        task = self.store.lease_task("fixture", kinds=["apply"])
        self.store.start_application(self.pid, task["lease_token"])
        if checkpoint is not None:
            self.store.checkpoint_task(self.task_id, task["lease_token"], checkpoint)
        with self.store.transaction(True) as db:
            self.store._release_task(db, self.task_id, "waiting_for_user", error=reason)
            db.execute("UPDATE wf_processes SET block_reason=? WHERE id=?", (reason, self.pid))
            db.execute("UPDATE queue SET status='needs_input' WHERE id=?", (self.pid,))
        return self.task(), self.process()

    def resume(self, task=None, process=None, reason="browser_tab_unavailable"):
        task, process = task or self.task(), process or self.process()
        return self.store.resume_recoverable_task(self.task_id, task["updated_at"], reason=reason,
            evidence_reference="fictional:verified-current-prerequisite", process_revision=process["revision"])

    def test_ready_flags_do_not_override_explicit_human_blocker(self):
        class Conflicting(ReadyRuntime):
            def prepare_browser(self, task, lease):
                result = super().prepare_browser(task, lease)
                result.update(blocker_reason='tool_approval_required', blocker='Approval denied.')
                return result
        runner = self.runner(Conflicting())
        self.assertEqual('blocked', runner.run_once(kinds=['apply'])['worker_status'])
        self.assertEqual('waiting_for_user', self.task()['status'])
        self.assertEqual('tool_approval_required', self.task()['error'])

    def test_failed_preflight_backoff_survives_restart_and_rechecks_before_apply(self):
        case = self
        class Broken(ReadyRuntime):
            def prepare_browser(self, task, lease):
                self.probe_calls += 1
                raise BrowserRecoverable("browser_attachment_transient")
            def apply(self, store, task):
                case.fail("Preparation must not run before readiness passes")
        runtime = Broken()
        runner = self.runner(runtime)
        self.assertEqual("retry_wait", runner.run_once(kinds=["apply"])["worker_status"])
        prior = self.task()
        self.assertEqual(1, prior["failure_count"])
        self.assertGreater(prior["available_at"], now())
        self.assertEqual("retry_wait", runner.readiness.snapshot()["status"])
        self.assertEqual(prior["available_at"], runner.readiness.snapshot()["retry_at"])
        self.assertEqual("not_started", self.process()["submission_status"])
        self.assertEqual("idle", runner.run_once(kinds=["apply"])["worker_status"])
        self.assertEqual(1, runtime.probe_calls)
        runner.readiness.close()
        self.store = WorkflowStore(self.path, low_watermark=0)
        self.assertEqual(prior, self.task())
        class Healthy(ReadyRuntime):
            def apply(self, store, task):
                case.assertEqual(1, self.probe_calls)
                store.before_submit(case.pid, task["lease_token"], "fictional-form")
                store.confirm_submission(case.pid, task["lease_token"], {
                    "url": "https://example.com/receipt/fictional", "evidence": "Fictional application accepted",
                    "observed_at": now(), "reference": "fictional"})
        healthy = Healthy()
        resumed = self.runner(healthy)
        self.assertEqual("idle", resumed.run_once(kinds=["apply"])["worker_status"])
        self.due()
        self.assertEqual("completed_dispatch", resumed.run_once(kinds=["apply"])["worker_status"])
        self.assertEqual("submitted", self.process()["submission_status"])
        self.assertEqual(self.task_id, self.task()["id"])
        self.assertEqual(1, self.task()["failure_count"])
        self.assertEqual(2, self.task()["attempts"])
        self.assertIsNone(resumed.readiness.snapshot()["retry_at"])

    def test_native_writer_busy_uses_durable_budget_without_replaying_application(self):
        import sys
        from types import SimpleNamespace
        from codex_appserver import AppServerError, AppServerResult
        from codex_transport import CodexTransport
        from test_codex_appserver import SCHEMA, THREAD

        initial = self.store.lease_task("fixture", kinds=["apply"])
        checkpoint = {"resume_upload_evidence": {"filename": "fictional.pdf", "verified": True}}
        self.store.checkpoint_task(self.task_id, initial["lease_token"], checkpoint)
        lease = SimpleNamespace(task_id=self.task_id, token=initial["lease_token"], heartbeat=lambda: True)
        transport = CodexTransport(Path(sys.executable).resolve(), Path(self.temp.name) / "runs",
                                   cwd=Path(self.temp.name), browser_transport="app-server")
        self.addCleanup(transport.close)
        result = AppServerResult({"ok": True}, THREAD, True, True, (("cua_repl", "js"),), 100, 0)
        with patch("codex_appserver.run_appserver", return_value=result):
            transport.run("linkedin_session", "fixture", SCHEMA, lease, browser=True)
        binding = transport.run_directory / "_browser_sessions" / (self.task_id + ".readiness.json")
        previous = binding.read_bytes()
        with self.store.transaction(True) as db:
            self.store._release_task(db, self.task_id, "queued")
        original = self.process()
        case = self

        class Busy(ReadyRuntime):
            def prepare_browser(self, task, lease):
                transport.run("linkedin_session", "fixture", SCHEMA, lease, browser=True)
                return super().prepare_browser(task, lease)

            def apply(self, store, task):
                case.fail("Readiness ownership conflict must not dispatch an application")

        failure = AppServerError("App-server rejected a protocol request.", thread_id=THREAD,
            reason="protocol_rejected", protocol_method="thread/resume", protocol_code=-32600,
            protocol_error={"code": -32600, "message": f"thread {THREAD} already has an active writer"})
        runner = self.runner(Busy())
        with patch("codex_appserver.run_appserver", side_effect=failure) as native:
            for count, expected in enumerate(("retry_wait", "retry_wait", "waiting_for_user"), start=1):
                self.assertEqual(expected, runner.run_once(kinds=["apply"])["worker_status"])
                task = self.task()
                self.assertEqual(count, task["failure_count"])
                self.assertEqual(checkpoint, task["checkpoint"])
                self.assertEqual("browser_session_unavailable", task["error"])
                self.assertEqual(previous, binding.read_bytes())
                self.assertEqual("idle", runner.run_once(kinds=["apply"])["worker_status"])
                self.assertEqual(count, native.call_count)
                if count == 1:
                    runner.readiness.close()
                    self.store = WorkflowStore(self.path, low_watermark=0)
                    self.assertEqual(task, self.task())
                    runner = self.runner(Busy())
                self.due()
        self.assertEqual(original["decision_id"], self.process()["decision_id"])
        self.assertEqual(original["snapshot"], self.process()["snapshot"])
        self.assertEqual("not_started", self.process()["submission_status"])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_resource_locks").fetchone()[0])
            self.assertEqual("waiting_for_user", self.task()["status"])

    def test_prepare_failure_reuses_saved_answers_and_upload_checkpoint(self):
        first = self.store.lease_task("fixture", kinds=["apply"])
        self.store.start_application(self.pid, first["lease_token"])
        checkpoint = {"resume_upload_evidence": {"filename": "fictional.pdf", "verified": True},
                      "question_bindings": {"q": "notice"}}
        self.store.checkpoint_task(self.task_id, first["lease_token"], checkpoint)
        process = self.store.ask_questions(self.pid, first["lease_token"], "form", [
            {"id": "q", "text": "Notice?", "type": "text", "required": True, "scope": "application"}])
        self.store.answer_questions({"request_id": "answer", "process_id": self.pid,
            "process_revision": process["revision"], "form_version": "form", "answers": [
                {"question_id": "q", "question_revision": 1, "value": "Two weeks"}]})
        saved_answers, original = self.store.questions(), self.process()
        case = self
        class TabUnavailable(ReadyRuntime):
            def apply(self, store, task):
                case.assertEqual(saved_answers, store.questions())
                case.assertEqual(checkpoint, task["checkpoint"])
                raise BrowserRecoverable("browser_tab_unavailable")
        runner = self.runner(TabUnavailable())
        for attempt in range(2):
            self.assertEqual("retry_wait", runner.run_once(kinds=["apply"])["worker_status"])
            self.assertEqual(checkpoint, self.task()["checkpoint"])
            self.assertEqual(saved_answers, self.store.questions())
            self.assertEqual(original["snapshot"], self.process()["snapshot"])
            self.assertEqual(original["decision_id"], self.process()["decision_id"])
            delta = datetime.fromisoformat(self.task()["available_at"]) - datetime.now(timezone.utc)
            self.assertGreater(delta.total_seconds(), (5 if attempt == 0 else 10) - 1)
            self.due()
        self.assertEqual("waiting_for_user", runner.run_once(kinds=["apply"])["worker_status"])
        self.assertEqual(3, self.task()["failure_count"])
        self.assertEqual("idle", runner.run_once(kinds=["apply"])["worker_status"])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_resource_locks").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0])

    def test_temporary_page_retries_survive_restart_and_release_browser_at_limit(self):
        class TemporaryPage(ReadyRuntime):
            def apply(self, store, task):
                raise BrowserRecoverable('application_page_transient')
        runner = self.runner(TemporaryPage())
        self.assertEqual('retry_wait', runner.run_once(kinds=['apply'])['worker_status'])
        prior = self.task()
        runner.readiness.close()
        self.store = WorkflowStore(self.path, low_watermark=0)
        self.assertEqual(prior, self.task())
        runner = self.runner(TemporaryPage())
        self.assertEqual('idle', runner.run_once(kinds=['apply'])['worker_status'])
        self.due()
        self.assertEqual('retry_wait', runner.run_once(kinds=['apply'])['worker_status'])
        self.due()
        self.assertEqual('waiting_for_user', runner.run_once(kinds=['apply'])['worker_status'])
        self.assertEqual(3, self.task()['failure_count'])
        self.assertEqual('application_page_transient', self.task()['error'])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_resource_locks').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_human_login_and_tool_denial_are_parked_without_retry(self):
        for reason in ("login_required", "tool_approval_required", "challenge_required", "file_access_required"):
            with self.subTest(reason=reason):
                with self.store.transaction(True) as db:
                    self.store._release_task(db, self.task_id, "queued")
                class Human(ReadyRuntime):
                    def prepare_browser(self, task, lease):
                        self.probe_calls += 1
                        raise BrowserNotReady("private page content", reason=reason)
                runtime = Human()
                runner = self.runner(runtime)
                self.assertEqual("blocked", runner.run_once(kinds=["apply"])["worker_status"])
                self.assertEqual(reason, self.task()["error"])
                self.assertEqual(reason, self.process()["block_reason"])
                self.assertEqual("needs_input", self.store.state()["queue"][0]["status"])
                self.assertEqual("idle", runner.run_once(kinds=["apply"])["worker_status"])
                self.assertEqual(0, self.task()["failure_count"])
                self.assertEqual(1, runtime.probe_calls)
                self.assertNotIn("private page content", str(self.task()))
                with self.assertRaises(ReviewError):
                    self.resume()

    def test_recovery_never_replays_submission_or_quarantine(self):
        case = self
        class Uncertain(ReadyRuntime):
            def apply(self, store, task):
                store.before_submit(case.pid, task["lease_token"], "form")
                raise BrowserRecoverable("browser_session_unavailable")
        runner = self.runner(Uncertain())
        self.assertEqual("waiting_for_user", runner.run_once(kinds=["apply"])["worker_status"])
        self.assertEqual("submission_unknown", self.task()["error"])
        self.assertTrue(self.task()["checkpoint"]["submission_started"])
        self.assertEqual("idle", runner.run_once(kinds=["apply"])["worker_status"])
        with self.assertRaises(ReviewError):
            self.resume()

    def test_quarantined_and_expired_leases_cannot_use_recovery_exception(self):
        for mode in ("quarantine", "expired", "other_owner"):
            with self.subTest(mode=mode):
                path = Path(self.temp.name) / (mode + ".sqlite3")
                self.store = WorkflowStore(path, low_watermark=0)
                self.store.grant_project_consent({"request_id": "consent", "policy_version": 1, "accepted": True})
                with self.store.transaction(True) as db:
                    self.task_id = self.store._enqueue_task(db, "explore", {}, mode, resource="browser")
                store = self.store
                class Unsafe(ReadyRuntime):
                    def prepare_browser(self, task, lease):
                        if mode == "quarantine":
                            store.quarantine_task(task["id"], task["lease_token"])
                        else:
                            with store.transaction(True) as db:
                                if mode == "expired":
                                    db.execute("UPDATE wf_tasks SET lease_until='2000-01-01' WHERE id=?", (task["id"],))
                                else:
                                    db.execute("UPDATE wf_tasks SET lease_token='new-owner' WHERE id=?", (task["id"],))
                                    db.execute("UPDATE wf_resource_locks SET lease_token='new-owner' WHERE task_id=?", (task["id"],))
                        raise BrowserRecoverable("browser_tab_unavailable")
                runner = self.runner(Unsafe())
                with self.assertLogs("orchestrator", level="ERROR"), self.assertRaises(ReviewError):
                    runner.run_once(kinds=["explore"])
                task = self.task()
                self.assertNotEqual("retry_wait", task["status"])
                if mode == "other_owner":
                    self.assertEqual("new-owner", task["lease_token"])
                else:
                    self.assertTrue(task["resource_quarantined"])

    def test_operator_recovery_is_revision_checked_and_preserves_task_authority(self):
        checkpoint = {"resume_upload_evidence": {"filename": "fictional.pdf"}}
        before, process = self.park(checkpoint=checkpoint)
        with self.assertRaises(ReviewError):
            self.resume({**before, "updated_at": "stale"}, process)
        with self.assertRaises(ReviewError):
            self.resume(before, {**process, "revision": process["revision"] - 1})
        after = self.resume(before, process)
        self.assertEqual("queued", after["status"])
        for key in ("id", "checkpoint", "payload", "attempts", "failure_count"):
            self.assertEqual(before[key], after[key])
        current = self.process()
        self.assertEqual(process["snapshot"], current["snapshot"])
        self.assertEqual(process["decision_id"], current["decision_id"])
        self.assertEqual("recovery_requested", current["events"][-1]["type"])
        self.assertIsNone(current["block_reason"])
        self.assertEqual("operator", current["events"][-1]["actor"])
        with self.assertRaises(ReviewError):
            self.resume(before, process)

    def test_operator_profile_recovery_requires_posting_block_and_saved_authorization(self):
        self.park("posting_changed")
        self.assertEqual("queued", self.resume(reason="saved_profile_reconciled")["status"])
        self.park("posting_changed")
        with self.store.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='pass' WHERE key=?", (self.process()["job_key"],))
        with self.assertRaises(ReviewError):
            self.resume(reason="saved_profile_reconciled")

    def test_operator_cannot_override_missing_answers_exhaustion_or_unknown_submission(self):
        self.park()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET failure_count=max_attempts WHERE id=?", (self.task_id,))
        with self.assertRaises(ReviewError):
            self.resume()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET failure_count=0,checkpoint=? WHERE id=?",
                       (json.dumps({"submission_started": True}), self.task_id))
        with self.assertRaises(ReviewError):
            self.resume()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET checkpoint='{}' WHERE id=?", (self.task_id,))
            stamp = now()
            db.execute("""INSERT INTO wf_questions(id,process_id,form_version,text,type,choices,required,
                scope,reason,created_at,updated_at) VALUES ('q',?,'form','Missing?','text','[]',1,
                'application','Missing',?,?)""", (self.pid, stamp, stamp))
        with self.assertRaises(ReviewError):
            self.resume()

    def test_only_typed_explicit_reasons_allow_recovery(self):
        for reason in ("login_required", "tool_approval_required", "application_action_required", "uncertain", ""):
            with self.assertRaises(ValueError):
                BrowserRecoverable(reason)
        with self.assertRaises(ValueError):
            BrowserNotReady(reason="browser_tab_unavailable")

    def test_readiness_uses_structured_reason_and_never_guesses_from_free_text(self):
        for reason, status, error in (("browser_session_unavailable", "retry_wait", "browser_session_unavailable"),
                                     ("login_required", "waiting_for_user", "login_required"),
                                     (None, "waiting_for_user", "browser_action_required")):
            with self.subTest(reason=reason):
                with self.store.transaction(True) as db:
                    self.store._release_task(db, self.task_id, "queued")
                class Report(ReadyRuntime):
                    def prepare_browser(self, task, lease):
                        return {"chrome": {"attached": False, "session_id": ""},
                                "linkedin": {"authenticated": False}, "blocker_reason": reason,
                                "blocker": "The browser tab is unavailable; retry automatically."}
                runner = self.runner(Report())
                runner.run_once(kinds=["apply"])
                self.assertEqual(status, self.task()["status"])
                self.assertEqual(error, self.task()["error"])

    def test_recovering_exploration_releases_browser_and_does_not_delay_ranking(self):
        with self.store.transaction(True) as db:
            self.store._release_task(db, self.task_id, "cancelled")
            explore = self.store._enqueue_task(db, "explore", {}, "explore", resource="browser")
            rank = self.store._enqueue_task(db, "rank", {}, "rank")
        class Reattach(ReadyRuntime):
            def prepare_browser(self, task, lease):
                self.probe_calls += 1
                raise BrowserRecoverable("browser_attachment_transient")
        runtime = Reattach()
        runner = self.runner(runtime)
        self.assertEqual({"worker_status": "retry_wait", "task_id": explore}, runner.run_once(kinds=["explore"]))
        self.assertEqual({"worker_status": "completed_dispatch", "task_id": rank}, runner.run_once(kinds=["rank"]))
        self.assertEqual(1, runtime.probe_calls)
        self.assertEqual(1, runtime.rank_calls)
        pending = next(task for task in self.store.tasks() if task["id"] == explore)
        self.assertEqual("retry_wait", pending["status"])
        self.assertEqual(1, pending["attempts"])

    def test_profile_lock_before_dispatch_defers_without_leasing_or_browser_work(self):
        runtime = ReadyRuntime()
        runner = self.runner(runtime)
        before = self.task()
        def sync(*, strict=False):
            if strict:
                raise ReviewError("Private profile lock context", reason="profile_busy")
            return {"status": "current"}
        with patch.object(self.store, "sync_profile_answers", side_effect=sync), self.assertLogs("orchestrator", level="DEBUG") as logs:
            result = runner.run_once(kinds=["apply"])
        self.assertEqual({"worker_status": "profile_busy", "task_id": None}, result)
        self.assertEqual(before, self.task())
        self.assertEqual(0, runtime.probe_calls)
        self.assertNotIn("Private profile lock context", str(logs.output))


if __name__ == "__main__":
    unittest.main()
