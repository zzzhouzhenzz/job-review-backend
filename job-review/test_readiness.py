"""Task-scoped browser checks with fictional evidence and temporary databases."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import Orchestrator
from readiness import BrowserWorkUncertain, CHECK_IDS, ReadinessMonitor
from store import ReviewError
from test_store import catalog
from workflow import WorkflowStore


def healthy_browser():
    return {"chrome": {"attached": True, "session_id": "fictional-chrome"},
            "linkedin": {"authenticated": True, "session_id": "fictional-chrome",
                         "url": "https://www.linkedin.com/jobs/", "tab_id": "fictional-tab"}}


class ReadyRuntime:
    enabled = True

    def __init__(self):
        self.probe_calls = self.explore_calls = self.rank_calls = 0
        self.seen_browser = None

    def prepare_browser(self, task, lease):
        self.probe_calls += 1
        return healthy_browser()

    def explore(self, context, lease):
        self.explore_calls += 1
        self.seen_browser = context["browser"]
        return {"companies": [], "source": "Fictional test with no openings"}

    def rank(self, context, lease):
        self.rank_calls += 1
        return {"cards": []}


class MutableClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WorkflowStore(Path(self.tmp.name) / "readiness.sqlite3", low_watermark=0)
        self.store.grant_project_consent({"request_id": "fixture-consent", "policy_version": 1, "accepted": True})
        self.monitors = []

    def tearDown(self):
        for monitor in self.monitors:
            monitor.close()
        self.tmp.cleanup()

    def orchestrator(self, runtime=None, **options):
        value = Orchestrator(self.store, runtime, readiness_options=options)
        self.monitors.append(value.readiness)
        value.readiness.start()
        value.readiness.wait(2)
        return value

    def enqueue(self, kind="explore", name="one"):
        with self.store.transaction(True) as db:
            return self.store._enqueue_task(db, kind, {"after_explore": True}, name,
                                           resource="browser" if kind == "explore" else None)

    def task(self, task_id):
        return next(task for task in self.store.tasks() if task["id"] == task_id)

    def apply_task(self):
        self.store.import_catalog(catalog(), "Fictional task readiness test")
        card = self.store.feed()["cards"][0]
        return self.store.receive_decision({"request_id": "apply", "action": "apply", "card_id": card["id"],
            "card_revision": card["revision"], "selected": [card["jobs"][0]["key"]]})["task_ids"][0]

    def test_startup_checks_only_storage_and_never_creates_browser_probe(self):
        runtime = ReadyRuntime()
        monitor = self.orchestrator(runtime).readiness
        for _ in range(3):
            monitor.poll()
        report = monitor.snapshot()
        self.assertEqual("idle", report["status"])
        self.assertFalse(report["ready"])
        self.assertTrue(monitor.storage_ready)
        self.assertEqual({"storage", "chrome_attachment", "linkedin_auth"}, set(CHECK_IDS))
        self.assertEqual(["passed", "on_demand", "on_demand"], [row["status"] for row in report["checks"]])
        self.assertEqual(0, runtime.probe_calls)
        self.assertEqual([], self.store.tasks())

    def test_source_change_requeues_before_or_after_question_lease_release(self):
        from applicant_evidence import EvidenceChanged
        for park_first in (False, True):
            with self.subTest(park_first=park_first):
                path = Path(self.tmp.name) / ('source-change-' + str(park_first) + '.sqlite3')
                store = WorkflowStore(path, low_watermark=0)
                store.grant_project_consent({"request_id": "fixture-consent", "policy_version": 1, "accepted": True})
                store.import_catalog(catalog(), 'fictional')
                card = store.feed()['cards'][0]
                receipt = store.receive_decision({'request_id': 'apply', 'action': 'apply',
                    'card_id': card['id'], 'card_revision': card['revision'], 'selected': [card['jobs'][0]['key']]})
                class SourceChanges(ReadyRuntime):
                    def apply(self, current_store, task):
                        if park_first:
                            current_store.ask_questions(task['payload']['process_id'], task['lease_token'], 'form', [
                                {'id': 'q', 'text': 'Fictional question?', 'type': 'text', 'choices': [],
                                 'required': True, 'scope': 'application', 'reason': 'Fictional'}])
                        raise EvidenceChanged()
                runner = Orchestrator(store, SourceChanges())
                self.monitors.append(runner.readiness)
                runner.readiness.start()
                runner.readiness.wait(2)
                self.assertEqual('evidence_changed', runner.run_once(kinds=['apply'])['worker_status'])
                task = next(t for t in store.tasks() if t['id'] == receipt['task_ids'][0])
                self.assertEqual('queued', task['status'])
                self.assertEqual(0, task['failure_count'])
                self.assertEqual('queued', store.state()['queue'][0]['status'])
                with store.transaction() as db:
                    self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_disabled_runtime_reports_not_configured_and_does_not_lease(self):
        task_id = self.enqueue()
        runner = self.orchestrator()
        self.assertEqual("not_configured", runner.readiness.snapshot()["status"])
        self.assertEqual("not_configured", runner.run_once()["worker_status"])
        self.assertEqual(0, self.task(task_id)["attempts"])

    def test_rank_does_not_require_chrome_or_browser_readiness(self):
        class NoBrowser(ReadyRuntime):
            def prepare_browser(self, task, lease):
                raise AssertionError("Rank must not touch Chrome")
        runtime = NoBrowser()
        runner = self.orchestrator(runtime)
        runner.readiness.browser_failed()
        task_id = self.enqueue("rank")
        self.assertEqual(task_id, runner.run_once(kinds=["rank"])["task_id"])
        self.assertEqual("succeeded", self.task(task_id)["status"])
        self.assertEqual(1, runtime.rank_calls)
        self.assertFalse(runner.readiness.snapshot()["ready"])

    def test_browser_check_runs_under_task_lease_and_passes_exact_tab_to_explorer(self):
        store = self.store
        class LeasedRuntime(ReadyRuntime):
            def prepare_browser(self, task, lease):
                with store.transaction() as db:
                    row = store._leased_task(db, task["id"], lease.token)
                    assert row["resource"] == "browser"
                    assert db.execute("SELECT task_id FROM wf_resource_locks").fetchone()[0] == task["id"]
                return super().prepare_browser(task, lease)
        runtime = LeasedRuntime()
        runner = self.orchestrator(runtime)
        self.enqueue()
        runner.run_once()
        self.assertEqual(healthy_browser(), runtime.seen_browser)
        self.assertEqual(1, runtime.probe_calls)
        report = runner.readiness.snapshot()
        self.assertTrue(report["ready"])
        self.assertIsNotNone(report["checked_at"])
        self.assertIsNotNone(report["expires_at"])
        self.assertEqual(["passed"] * 3, [check["status"] for check in report["checks"]])

    def test_every_browser_task_rechecks_even_with_fresh_observed_health(self):
        runtime = ReadyRuntime()
        runner = self.orchestrator(runtime)
        self.enqueue(name="one")
        self.enqueue(name="two")
        runner.run_once()
        runner.run_once()
        self.assertEqual(2, runtime.probe_calls)
        self.assertEqual(2, runtime.explore_calls)

    def test_observation_expiry_does_not_start_browser_work(self):
        runtime, clock = ReadyRuntime(), MutableClock()
        runner = self.orchestrator(runtime, ttl=10, clock=clock)
        self.enqueue()
        runner.run_once()
        clock.value += 10
        report = runner.readiness.snapshot()
        self.assertEqual("idle", report["status"])
        self.assertFalse(report["ready"])
        self.assertIsNone(report["expires_at"])
        self.assertEqual(1, runtime.probe_calls)

    def test_invalid_browser_evidence_parks_without_exploring_or_holding_resource(self):
        changes = [("chrome", "attached", False), ("linkedin", "authenticated", False),
                   ("linkedin", "session_id", "different"), ("linkedin", "tab_id", ""),
                   ("linkedin", "url", "http://www.linkedin.com/jobs/"),
                   ("linkedin", "url", "https://example.com/jobs/"),
                   ("linkedin", "url", "https://secret@www.linkedin.com/jobs/")]
        for i, (section, key, value) in enumerate(changes):
            with self.subTest(field=key, value=value):
                class InvalidRuntime(ReadyRuntime):
                    def prepare_browser(self, task, lease):
                        result = healthy_browser()
                        result[section][key] = value
                        return result
                runtime = InvalidRuntime()
                runner = self.orchestrator(runtime)
                task_id = self.enqueue(name=str(i))
                self.assertEqual("blocked", runner.run_once()["worker_status"])
                task = self.task(task_id)
                self.assertEqual("waiting_for_user", task["status"])
                self.assertFalse(task["resource_quarantined"])
                self.assertEqual(0, task["failure_count"])
                self.assertEqual(0, runtime.explore_calls)

    def test_failed_browser_readiness_does_not_start_application_preparation(self):
        class SignedOut(ReadyRuntime):
            def prepare_browser(self, task, lease):
                result = healthy_browser()
                result["linkedin"]["authenticated"] = False
                return result
        task_id = self.apply_task()
        runner = self.orchestrator(SignedOut())
        self.assertEqual("blocked", runner.run_once(kinds=["apply"])["worker_status"])
        self.assertEqual("not_started", self.store.processes()[0]["submission_status"])
        self.assertEqual("waiting_for_user", self.task(task_id)["status"])

    def test_timeout_quarantines_browser_but_does_not_block_rank(self):
        class Uncertain(ReadyRuntime):
            def prepare_browser(self, task, lease):
                raise BrowserWorkUncertain("Fictional browser command may be active")
        runner = self.orchestrator(Uncertain())
        first, second = self.enqueue(name="first"), self.enqueue(name="second")
        with self.assertRaises(BrowserWorkUncertain):
            runner.run_once(kinds=["explore"])
        parked = self.task(first)
        self.assertEqual("waiting_for_user", parked["status"])
        self.assertTrue(parked["resource_quarantined"])
        self.assertEqual(1, parked["failure_count"])
        self.assertEqual("idle", runner.run_once(kinds=["explore"])["worker_status"])
        self.assertEqual(0, self.task(second)["attempts"])
        rank = self.enqueue("rank", "rank")
        runner.run_once(kinds=["rank"])
        self.assertEqual("succeeded", self.task(rank)["status"])
        with self.store.transaction(True) as db:
            with self.assertRaises(ReviewError):
                self.store._resume_task(db, first)
        self.store.acknowledge_worker_stopped(first)
        with self.store.transaction(True) as db:
            self.store._resume_task(db, first)
        self.assertEqual("queued", self.task(first)["status"])

    def test_timeout_after_submission_checkpoint_preserves_uncertainty(self):
        class UncertainApplication(ReadyRuntime):
            def apply(self, store, task):
                store.before_submit(task["payload"]["process_id"], task["lease_token"], "fictional-form")
                raise BrowserWorkUncertain("Fictional submit acknowledgement was lost")
        task_id = self.apply_task()
        runner = self.orchestrator(UncertainApplication())
        with self.assertRaises(BrowserWorkUncertain):
            runner.run_once(kinds=["apply"])
        task = self.task(task_id)
        self.assertTrue(task["resource_quarantined"])
        self.assertTrue(task["checkpoint"]["submission_started"])
        self.assertEqual("submission_unknown", self.store.processes()[0]["submission_status"])
        self.store.acknowledge_worker_stopped(task_id)
        with self.store.transaction(True) as db:
            with self.assertRaises(ReviewError):
                self.store._resume_task(db, task_id)

    def test_lost_lease_during_browser_check_prevents_dispatch_and_quarantines(self):
        store = self.store
        class LostLease(ReadyRuntime):
            def prepare_browser(self, task, lease):
                with store.transaction(True) as db:
                    db.execute("UPDATE wf_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (task["id"],))
                return healthy_browser()
        runtime = LostLease()
        runner = self.orchestrator(runtime)
        task_id = self.enqueue()
        with self.assertRaises(ReviewError):
            runner.run_once()
        self.assertEqual(0, runtime.explore_calls)
        self.assertTrue(self.task(task_id)["resource_quarantined"])

    def test_supported_kinds_and_lane_filter_do_not_claim_other_work(self):
        runtime = ReadyRuntime()
        runtime.supported_kinds = {"rank"}
        runner = self.orchestrator(runtime)
        explore, rank = self.enqueue(), self.enqueue("rank", "rank")
        self.assertEqual("not_configured", runner.run_once(kinds=["apply"])["worker_status"])
        self.assertEqual(rank, runner.run_once()["task_id"])
        self.assertEqual(0, self.task(explore)["attempts"])
        self.assertEqual(0, runtime.probe_calls)

    def test_quarantine_rejects_stale_token_without_changing_new_owner(self):
        task_id = self.enqueue()
        task = self.store.lease_task("worker")
        with self.assertRaises(ReviewError):
            self.store.quarantine_task(task_id, "wrong-token")
        self.assertEqual("running", self.task(task_id)["status"])
        self.assertFalse(self.task(task_id)["resource_quarantined"])
        self.store.quarantine_task(task_id, task["lease_token"])
        with self.assertRaises(ReviewError):
            self.store.quarantine_task(task_id, task["lease_token"])
        self.assertEqual(1, self.task(task_id)["failure_count"])

    def test_storage_failure_does_not_lease_work(self):
        runtime = ReadyRuntime()
        runner = Orchestrator(self.store, runtime)
        self.monitors.append(runner.readiness)
        task_id = self.enqueue()
        with patch.object(runner.readiness, "_check_storage", side_effect=OSError("fictional failure")):
            with self.assertLogs("readiness", level="ERROR"):
                runner.readiness.start()
                runner.readiness.wait(2)
        self.assertEqual("blocked", runner.run_once()["worker_status"])
        self.assertEqual(0, self.task(task_id)["attempts"])
        self.assertEqual(0, runtime.probe_calls)

    def test_private_runtime_error_is_not_copied_into_public_health_or_task(self):
        class PrivateFailure(ReadyRuntime):
            def prepare_browser(self, task, lease):
                raise OSError("private fictional page text")
        runner = self.orchestrator(PrivateFailure())
        task_id = self.enqueue()
        with self.assertRaises(OSError):
            runner.run_once()
        self.assertNotIn("private fictional page text", str(runner.readiness.snapshot()))
        self.assertNotIn("private fictional page text", self.task(task_id)["error"])
        self.assertEqual("failed", self.task(task_id)["status"])


if __name__ == "__main__":
    unittest.main()
