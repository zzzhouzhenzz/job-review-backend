"""Readiness gates use fictional probe evidence and temporary local databases."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import Orchestrator, UnconfiguredRuntime
from readiness import AGENT_CAPABILITIES, CHECK_IDS, ReadinessMonitor
from test_store import catalog
from workflow import WorkflowStore


def healthy_probe(probe_id):
    """Fictional successful adapter response, shared by backend unit tests."""
    return {
        "probe_id": probe_id,
        "chrome": {"attached": True, "session_id": "fictional-chrome-session"},
        "linkedin": {"authenticated": True, "session_id": "fictional-chrome-session"},
        "posting": {
            "session_id": "fictional-chrome-session", "job_id": "1234567890",
            "url": "https://www.linkedin.com/jobs/view/1234567890/",
            "title": "Fictional Infrastructure Engineer", "company": "Fictional Company",
            "description": "Build fictional infrastructure and operate reliable test services.",
            "full_description": True,
        },
        "agents": {role: {"ready": True, "probe_id": probe_id,
                          "session_id": "fictional-" + role + "-session",
                          "capabilities": sorted(capabilities)}
                   for role, capabilities in AGENT_CAPABILITIES.items()},
    }


class ReadyRuntime:
    enabled = True

    def __init__(self):
        self.probe_calls = 0
        self.explore_calls = 0
        self.rank_calls = 0

    def preflight(self, probe_id, lease):
        self.probe_calls += 1
        return healthy_probe(probe_id)

    def explore(self, context, lease):
        self.explore_calls += 1
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
        self.path = Path(self.tmp.name) / "readiness.sqlite3"
        self.store = WorkflowStore(self.path)
        self.monitors = []
        self.release_events = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for monitor in self.monitors:
            monitor.wait(2)
            monitor.close()
        self.tmp.cleanup()

    def monitor(self, runtime=None, **options):
        monitor = ReadinessMonitor(self.store, runtime or ReadyRuntime(), **options)
        self.monitors.append(monitor)
        return monitor

    def orchestrator(self, runtime=None, **options):
        value = Orchestrator(self.store, runtime, readiness_options=options)
        self.monitors.append(value.readiness)
        return value

    def start(self, monitor):
        monitor.start()
        result = monitor.wait(2)
        self.assertNotEqual("checking", result["status"])
        return result

    def checks(self, report):
        return {entry["id"]: entry["status"] for entry in report["checks"]}

    def browser_lock(self):
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM wf_resource_locks WHERE resource='browser'").fetchone()
            return None if row is None else dict(row)

    def blocked_runtime(self):
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)

        class BlockingRuntime(ReadyRuntime):
            def preflight(self, probe_id, lease):
                self.probe_calls += 1
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("Test did not release the fictional probe.")
                return healthy_probe(probe_id)

        return BlockingRuntime(), entered, release

    def test_unconfigured_runtime_checks_storage_without_claiming_work(self):
        monitor = self.monitor(UnconfiguredRuntime())
        report = self.start(monitor)
        self.assertEqual("not_configured", report["status"])
        self.assertFalse(report["ready"])
        self.assertEqual("passed", self.checks(report)["storage"])
        self.assertEqual({"not_configured"}, set(self.checks(report).values()) - {"passed"})
        with self.store.transaction() as db:
            self.assertEqual([], db.execute("SELECT * FROM wf_settings").fetchall())
        self.assertEqual([], self.store.tasks())
        self.assertIsNone(self.browser_lock())

    def test_complete_probe_is_ready_and_public_snapshot_contains_no_raw_evidence(self):
        runtime = ReadyRuntime()
        monitor = self.monitor(runtime)
        report = self.start(monitor)
        self.assertEqual("ready", report["status"])
        self.assertTrue(report["ready"])
        self.assertEqual(set(CHECK_IDS), set(self.checks(report)))
        self.assertEqual({"passed"}, set(self.checks(report).values()))
        self.assertIsNotNone(report["checked_at"])
        self.assertIsNotNone(report["expires_at"])
        self.assertEqual(1, runtime.probe_calls)
        self.assertIsNone(self.browser_lock())
        public = json.dumps(report)
        for private in ("fictional-chrome-session", "fictional-explorer-session", "1234567890",
                        "Fictional Infrastructure Engineer", "reliable test services"):
            self.assertNotIn(private, public)
        report["checks"][0]["status"] = "changed by client"
        self.assertEqual("passed", monitor.snapshot()["checks"][0]["status"])

    def test_invalid_evidence_never_makes_workers_ready(self):
        cases = [
            ("missing result", lambda result: None, "chrome_attachment"),
            ("wrong nonce", lambda result: {**result, "probe_id": "previous-probe"}, "chrome_attachment"),
            ("missing nonce", lambda result: {key: value for key, value in result.items() if key != "probe_id"}, "chrome_attachment"),
            ("detached Chrome", ("chrome", "attached", False), "chrome_attachment"),
            ("missing Chrome session", ("chrome", "session_id", ""), "chrome_attachment"),
            ("signed out", ("linkedin", "authenticated", False), "linkedin_auth"),
            ("wrong auth session", ("linkedin", "session_id", "another-session"), "linkedin_auth"),
            ("wrong read session", ("posting", "session_id", "another-session"), "linkedin_job_read"),
            ("missing body", ("posting", "description", "  "), "linkedin_job_read"),
            ("preview only", ("posting", "full_description", False), "linkedin_job_read"),
            ("login URL", ("posting", "url", "https://www.linkedin.com/login"), "linkedin_job_read"),
            ("lookalike host", ("posting", "url", "https://www.linkedin.com.example.com/jobs/view/1234567890"), "linkedin_job_read"),
            ("credential URL", ("posting", "url", "https://name:password@www.linkedin.com/jobs/view/1234567890"), "linkedin_job_read"),
            ("wrong job ID", ("posting", "job_id", "9999"), "linkedin_job_read"),
        ]
        for label, mutation, failed_check in cases:
            with self.subTest(label=label):
                class InvalidRuntime(ReadyRuntime):
                    def preflight(self, probe_id, lease):
                        result = healthy_probe(probe_id)
                        if callable(mutation):
                            return mutation(result)
                        section, key, value = mutation
                        result[section][key] = value
                        return result

                report = self.start(self.monitor(InvalidRuntime()))
                self.assertFalse(report["ready"])
                self.assertEqual("blocked", report["status"])
                self.assertEqual("failed", self.checks(report)[failed_check])
                self.assertIsNone(self.browser_lock())

    def test_each_agent_must_echo_nonce_have_capabilities_and_use_dedicated_session(self):
        cases = [
            ("missing role", lambda agents: agents.pop("ranker"), "ranker_agent"),
            ("not ready", lambda agents: agents["explorer"].update(ready=False), "explorer_agent"),
            ("missing nonce", lambda agents: agents["application"].pop("probe_id"), "application_agent"),
            ("stale nonce", lambda agents: agents["ranker"].update(probe_id="old"), "ranker_agent"),
            ("missing session", lambda agents: agents["explorer"].update(session_id=" "), "explorer_agent"),
            ("missing flashcard capability", lambda agents: agents["ranker"].update(capabilities=["rank"]), "ranker_agent"),
            ("missing receipt capability", lambda agents: agents["application"].update(capabilities=["apply", "questions"]), "application_agent"),
            ("wrong capabilities type", lambda agents: agents["explorer"].update(capabilities="explore"), "explorer_agent"),
            ("shared sessions", lambda agents: agents["ranker"].update(session_id=agents["explorer"]["session_id"]), "ranker_agent"),
        ]
        for label, mutation, failed_check in cases:
            with self.subTest(label=label):
                class InvalidRuntime(ReadyRuntime):
                    def preflight(self, probe_id, lease):
                        result = healthy_probe(probe_id)
                        mutation(result["agents"])
                        return result

                report = self.start(self.monitor(InvalidRuntime()))
                self.assertFalse(report["ready"])
                self.assertEqual("failed", self.checks(report)[failed_check])

    def test_enabled_runtime_without_preflight_is_blocked(self):
        class MissingProbeRuntime:
            enabled = True

        with self.assertLogs("readiness", level="ERROR"):
            report = self.start(self.monitor(MissingProbeRuntime()))
        self.assertFalse(report["ready"])
        self.assertEqual("blocked", report["status"])
        self.assertEqual("passed", self.checks(report)["storage"])
        self.assertEqual("failed", self.checks(report)["explorer_agent"])
        self.assertIsNone(self.browser_lock())

    def test_adapter_exception_is_logged_but_redacted_from_public_report(self):
        class FailingRuntime(ReadyRuntime):
            def preflight(self, probe_id, lease):
                raise RuntimeError("private-cookie=fictional-secret")

        with self.assertLogs("readiness", level="ERROR") as logged:
            report = self.start(self.monitor(FailingRuntime()))
        self.assertEqual("blocked", report["status"])
        self.assertNotIn("fictional-secret", json.dumps(report))
        self.assertIn("fictional-secret", "\n".join(logged.output))
        self.assertIsNone(self.browser_lock())

    def test_timeout_does_not_overlap_or_accept_a_late_success(self):
        runtime, entered, release = self.blocked_runtime()
        clock = MutableClock()
        monitor = self.monitor(runtime, timeout=30, retry_interval=1, clock=clock)
        monitor.start()
        self.assertTrue(entered.wait(2))
        original_lock = self.browser_lock()
        monitor._timed_out(monitor._probe_id)
        report = monitor.snapshot()
        self.assertEqual("timed_out", report["status"])
        self.assertFalse(report["ready"])
        self.assertEqual(1, self.browser_lock()["quarantined"])
        self.assertEqual(original_lock["task_id"], self.browser_lock()["task_id"])
        clock.value += 2
        monitor.poll()
        self.assertEqual(1, runtime.probe_calls)
        self.assertEqual(original_lock["task_id"], self.browser_lock()["task_id"])
        release.set()
        report = monitor.wait(2)
        self.assertEqual("timed_out", report["status"])
        self.assertFalse(report["ready"])
        self.assertIsNone(self.browser_lock())
        monitor.poll()
        self.assertTrue(monitor.wait(2)["ready"])
        self.assertEqual(2, runtime.probe_calls)

    def test_expired_readiness_requires_a_new_successful_probe(self):
        runtime, clock = ReadyRuntime(), MutableClock()
        monitor = self.monitor(runtime, ttl=10, clock=clock)
        first = self.start(monitor)
        self.assertTrue(first["ready"])
        clock.value += 10
        expired = monitor.snapshot()
        self.assertEqual("stale", expired["status"])
        self.assertFalse(expired["ready"])
        self.assertEqual({"stale"}, set(self.checks(expired).values()))
        self.assertTrue(self.start(monitor)["ready"])
        self.assertEqual(2, runtime.probe_calls)

    def test_new_monitor_never_reuses_a_previous_boot_success(self):
        first = self.start(self.monitor())
        fresh = self.monitor()
        initial = fresh.snapshot()
        self.assertNotEqual(first["boot_id"], initial["boot_id"])
        self.assertEqual("checking", initial["status"])
        self.assertFalse(initial["ready"])
        self.assertIsNone(initial["checked_at"])
        self.assertIsNone(initial["expires_at"])
        self.assertTrue(self.start(fresh)["ready"])

    def test_browser_in_use_blocks_probe_without_calling_adapter(self):
        with self.store.transaction(write=True) as db:
            task_id = self.store._enqueue_task(db, "explore", {}, "existing-worker", resource="browser")
        worker = self.store.lease_task("already-running")
        runtime = ReadyRuntime()
        report = self.start(self.monitor(runtime))
        self.assertEqual("blocked", report["status"])
        self.assertFalse(report["ready"])
        self.assertEqual(0, runtime.probe_calls)
        self.assertEqual(1, len(self.store.tasks()))
        self.assertEqual(task_id, self.browser_lock()["task_id"])
        self.assertEqual(worker["lease_token"], self.browser_lock()["lease_token"])

    def test_storage_failure_prevents_runtime_probe(self):
        runtime = ReadyRuntime()
        monitor = self.monitor(runtime)
        with patch.object(monitor, "_check_storage", side_effect=OSError("fictional storage error")):
            with self.assertLogs("readiness", level="ERROR"):
                report = self.start(monitor)
        self.assertEqual("blocked", report["status"])
        self.assertFalse(report["ready"])
        self.assertEqual("failed", self.checks(report)["storage"])
        self.assertEqual("blocked", self.checks(report)["chrome_attachment"])
        self.assertEqual(0, runtime.probe_calls)
        self.assertEqual([], self.store.tasks())

    def test_dispatch_waits_for_fresh_probe_then_runs_queued_work(self):
        runtime, entered, release = self.blocked_runtime()
        orchestrator = self.orchestrator(runtime)
        result = orchestrator.run_once()
        self.assertEqual("checking", result["worker_status"])
        self.assertIsNone(result["task_id"])
        self.assertTrue(entered.wait(2))
        work = [task for task in self.store.tasks() if task["kind"] == "explore"]
        self.assertEqual(1, len(work))
        self.assertEqual("queued", work[0]["status"])
        self.assertEqual(0, work[0]["attempts"])
        release.set()
        self.assertTrue(orchestrator.readiness.wait(2)["ready"])
        result = orchestrator.run_once()
        self.assertEqual("completed_dispatch", result["worker_status"])
        self.assertEqual(work[0]["id"], result["task_id"])
        self.assertEqual(1, runtime.explore_calls)

    def test_health_expiring_during_task_lease_requeues_before_runtime_call(self):
        runtime, clock = ReadyRuntime(), MutableClock()
        orchestrator = self.orchestrator(runtime, ttl=10, clock=clock)
        self.assertTrue(self.start(orchestrator.readiness)["ready"])
        lease_task = self.store.lease_task

        def lease_then_expire(*args, **kwargs):
            task = lease_task(*args, **kwargs)
            clock.value += 10
            return task

        with patch.object(self.store, "lease_task", side_effect=lease_then_expire):
            result = orchestrator.run_once()
        self.assertEqual("stale", result["worker_status"])
        self.assertIsNone(result["task_id"])
        self.assertEqual(0, runtime.explore_calls)
        work = next(task for task in self.store.tasks() if task["kind"] == "explore")
        self.assertEqual("queued", work["status"])
        self.assertEqual(0, work["failure_count"])
        self.assertIsNone(work["lease_token"])
        self.assertIsNone(self.browser_lock())

    def test_runtime_failure_invalidates_previously_ready_probe(self):
        class FailingRuntime(ReadyRuntime):
            def explore(self, context, lease):
                raise OSError("Fictional worker failure")

        orchestrator = self.orchestrator(FailingRuntime())
        self.assertTrue(self.start(orchestrator.readiness)["ready"])
        with self.assertRaises(OSError):
            orchestrator.run_once()
        self.assertFalse(orchestrator.readiness.snapshot()["ready"])
        self.assertEqual("blocked", orchestrator.readiness.snapshot()["status"])
        self.assertEqual("retry_wait", next(task for task in self.store.tasks() if task["kind"] == "explore")["status"])

    def test_health_expiring_during_context_read_prevents_adapter_call(self):
        runtime, clock = ReadyRuntime(), MutableClock()
        orchestrator = self.orchestrator(runtime, ttl=10, clock=clock)
        self.assertTrue(self.start(orchestrator.readiness)["ready"])
        context_read = self.store.discovery_context

        def read_then_expire(*args):
            context = context_read(*args)
            clock.value += 10
            return context

        with patch.object(self.store, "discovery_context", side_effect=read_then_expire):
            result = orchestrator.run_once()
        self.assertEqual("stale", result["worker_status"])
        self.assertEqual(0, runtime.explore_calls)
        self.assertIsNone(self.browser_lock())

    def test_expired_health_during_application_setup_requeues_without_applying(self):
        class ApplicationRuntime(ReadyRuntime):
            def apply(self, store, task):
                raise AssertionError("The application adapter must not run with expired health")

        self.store.import_catalog(catalog(), "Fictional application readiness test")
        card = self.store.feed()["cards"][0]
        self.store.receive_decision({"request_id": "health-apply", "action": "apply",
                                    "card_id": card["id"], "card_revision": card["revision"],
                                    "selected": [card["jobs"][0]["key"]]})
        clock = MutableClock()
        orchestrator = self.orchestrator(ApplicationRuntime(), ttl=10, clock=clock)
        self.assertTrue(self.start(orchestrator.readiness)["ready"])
        start_application = self.store.start_application

        def prepare_then_expire(*args):
            process = start_application(*args)
            clock.value += 10
            return process

        with patch.object(self.store, "start_application", side_effect=prepare_then_expire):
            result = orchestrator.run_once()
        self.assertEqual("stale", result["worker_status"])
        process = self.store.processes()[0]
        self.assertEqual("queued", process["task_status"])
        self.assertNotEqual("submitted", process["submission_status"])
        with self.store.transaction() as db:
            self.assertEqual("queued", db.execute("SELECT status FROM queue WHERE id=?", (process["id"],)).fetchone()[0])
        self.assertIsNone(self.browser_lock())

    def test_invalidation_discards_an_already_running_probe(self):
        runtime, entered, release = self.blocked_runtime()
        clock = MutableClock()
        monitor = self.monitor(runtime, clock=clock)
        monitor.start()
        self.assertTrue(entered.wait(2))
        monitor.invalidate()
        release.set()
        report = monitor.wait(2)
        self.assertFalse(report["ready"])
        self.assertEqual("blocked", report["status"])
        self.assertIsNone(self.browser_lock())
        monitor.poll()
        self.assertEqual(1, runtime.probe_calls)
        clock.value += 30
        self.assertTrue(self.start(monitor)["ready"])

    def test_timer_quarantines_without_needing_an_http_poll(self):
        runtime, entered, release = self.blocked_runtime()
        monitor = self.monitor(runtime, timeout=0.2)
        quarantined = threading.Event()
        original = self.store.timeout_preflight

        def timeout(*args):
            result = original(*args)
            quarantined.set()
            return result

        with patch.object(self.store, "timeout_preflight", side_effect=timeout):
            monitor.start()
            self.assertTrue(entered.wait(2))
            self.assertTrue(quarantined.wait(2))
            self.assertFalse(monitor.snapshot()["ready"])
            self.assertEqual(1, self.browser_lock()["quarantined"])
            release.set()
            self.assertEqual("timed_out", monitor.wait(2)["status"])

    def test_lost_browser_lease_cannot_produce_ready_result(self):
        runtime, entered, release = self.blocked_runtime()
        monitor = self.monitor(runtime)
        monitor.start()
        self.assertTrue(entered.wait(2))
        with self.store.transaction(write=True) as db:
            db.execute("UPDATE wf_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE status='running'")
        self.store.recover_expired_tasks()
        with self.assertLogs("readiness", level="ERROR"):
            release.set()
            report = monitor.wait(2)
        self.assertFalse(report["ready"])
        self.assertEqual("blocked", report["status"])
        self.assertIsNone(self.browser_lock())


if __name__ == "__main__":
    unittest.main()
