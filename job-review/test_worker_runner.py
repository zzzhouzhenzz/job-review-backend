"""Worker lane concurrency and shutdown use only fictional local tasks."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from orchestrator import Orchestrator
from readiness import BrowserWorkUncertain
from test_readiness import ReadyRuntime
from test_store import catalog
from worker_runner import WorkerRunner, WorkerShutdownTimeout
from workflow import WorkflowStore


class WorkerRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkflowStore(Path(self.temp.name) / "lanes.sqlite3", low_watermark=0)
        self.store.grant_project_consent({"request_id": "fixture-consent", "policy_version": 1, "accepted": True})
        self.release = threading.Event()
        self.runner = None
        self.orchestrator = None

    def tearDown(self):
        self.release.set()
        if self.runner is not None:
            self.runner.stop(timeout=3)
        if self.orchestrator is not None:
            self.orchestrator.readiness.close()
        self.temp.cleanup()

    def start(self, runtime):
        self.orchestrator = Orchestrator(self.store, runtime)
        self.orchestrator.readiness.start()
        self.orchestrator.readiness.wait(2)
        self.runner = WorkerRunner(self.orchestrator, poll_interval=0.01)
        self.runner.start()
        return self.runner

    def enqueue(self, kind, name):
        with self.store.transaction(True) as db:
            return self.store._enqueue_task(db, kind, {"after_explore": True}, name,
                                           resource="browser" if kind == "explore" else None)

    def apply_task(self):
        self.store.import_catalog(catalog(), "Fictional worker lane test")
        card = self.store.feed()["cards"][0]
        return self.store.receive_decision({"request_id": "apply", "action": "apply", "card_id": card["id"],
            "card_revision": card["revision"], "selected": [card["jobs"][0]["key"]]})["task_ids"][0]

    def task(self, task_id):
        return next(task for task in self.store.tasks() if task["id"] == task_id)

    def wait_status(self, task_id, status):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.task(task_id)["status"] == status:
                return
            threading.Event().wait(0.01)
        self.fail(f"Task did not become {status}: {self.task(task_id)['status']}")

    def holding_runtime(self, *, uncertain_on_stop=False):
        entered, release = threading.Event(), self.release
        ranked = threading.Event()
        class HoldingRuntime(ReadyRuntime):
            def apply(self, store, task):
                entered.set()
                if not release.wait(5):
                    raise BrowserWorkUncertain("Fictional test release deadline")
                if uncertain_on_stop:
                    raise BrowserWorkUncertain("Fictional pending browser command")
                store.ask_questions(task["payload"]["process_id"], task["lease_token"], "fictional-form", [{
                    "id": "fictional-question", "text": "Fictional missing answer", "type": "text", "required": True}])

            def rank(self, context, lease):
                ranked.set()
                return super().rank(context, lease)
        return HoldingRuntime(), entered, ranked

    def test_rank_progresses_while_application_holds_chrome_but_explore_waits(self):
        apply = self.apply_task()
        runtime, entered, ranked = self.holding_runtime()
        runner = self.start(runtime)
        self.assertTrue(entered.wait(2))
        explore = self.enqueue("explore", "explore")
        rank = self.enqueue("rank", "rank")
        self.assertTrue(ranked.wait(2))
        self.wait_status(rank, "succeeded")
        self.assertEqual("running", self.task(apply)["status"])
        self.assertEqual(0, self.task(explore)["attempts"])
        self.assertEqual(0, runtime.explore_calls)
        self.assertEqual(1, runtime.probe_calls)
        self.release.set()
        self.wait_status(apply, "waiting_for_user")
        self.wait_status(explore, "succeeded")
        self.assertEqual(2, runtime.probe_calls)
        self.assertEqual(1, runtime.explore_calls)
        self.assertEqual({"discovery", "application"}, set(runner.snapshot()))

    def test_supported_kinds_leave_application_unclaimed(self):
        apply = self.apply_task()
        rank = self.enqueue("rank", "rank")
        runtime = ReadyRuntime()
        runtime.supported_kinds = {"rank"}
        runner = self.start(runtime)
        self.wait_status(rank, "succeeded")
        self.assertEqual(0, self.task(apply)["attempts"])
        self.assertEqual(0, runtime.probe_calls)
        self.assertEqual({"discovery"}, set(runner.snapshot()))

    def test_idle_runner_does_not_touch_chrome(self):
        runtime = ReadyRuntime()
        runner = self.start(runtime)
        threading.Event().wait(0.05)
        runner.check_running()
        self.assertEqual(0, runtime.probe_calls)
        self.assertEqual([], self.store.tasks())

    def test_bounded_stop_reports_live_thread_without_releasing_browser(self):
        apply = self.apply_task()
        runtime, entered, _ = self.holding_runtime()
        runner = self.start(runtime)
        self.assertTrue(entered.wait(2))
        with self.assertRaises(WorkerShutdownTimeout):
            runner.stop(timeout=0.01)
        self.assertEqual("running", self.task(apply)["status"])
        with self.store.transaction() as db:
            self.assertEqual(apply, db.execute("SELECT task_id FROM wf_resource_locks WHERE resource='browser'").fetchone()[0])
        self.release.set()
        runner.stop(timeout=2)
        self.assertTrue(all(not row["running"] for row in runner.snapshot().values()))
        self.assertEqual("waiting_for_user", self.task(apply)["status"])

    def test_shutdown_calls_runtime_close_once_and_quarantines_uncertain_browser(self):
        apply = self.apply_task()
        runtime, entered, _ = self.holding_runtime(uncertain_on_stop=True)
        closed = []
        def close():
            closed.append(True)
            self.release.set()
        runtime.close = close
        runner = self.start(runtime)
        self.assertTrue(entered.wait(2))
        with self.assertLogs("worker_runner", level="ERROR"):
            runner.stop(timeout=None)
        runner.request_stop()
        runner.stop()
        self.assertEqual(1, len(closed))
        self.assertTrue(self.task(apply)["resource_quarantined"])
        self.assertEqual("waiting_for_user", self.task(apply)["status"])

    def test_failed_rank_dispatch_does_not_kill_discovery_lane(self):
        class FailingRank(ReadyRuntime):
            supported_kinds = {"rank"}
            def rank(self, context, lease):
                raise OSError("Fictional private error")
        rank = self.enqueue("rank", "rank")
        with self.assertLogs("worker_runner", level="ERROR") as logs:
            runner = self.start(FailingRank())
            self.wait_status(rank, "retry_wait")
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not logs.output:
                threading.Event().wait(0.01)
        runner.check_running()
        self.assertTrue(runner.snapshot()["discovery"]["running"])
        self.assertNotIn("Fictional private error", str(logs.output))

    def test_shutdown_rechecks_runtime_background_lane_after_first_close_timeout(self):
        closed, waited = [], []
        release = self.release
        class BackgroundRuntime(ReadyRuntime):
            def close(self):
                closed.append(True)
                raise WorkerShutdownTimeout('Fictional background reviewer remains active')

            def wait_closed(self, timeout=5):
                waited.append(timeout)
                if not release.is_set():
                    raise WorkerShutdownTimeout('Fictional background reviewer remains active')

        runner = self.start(BackgroundRuntime())
        for _ in range(2):
            with self.assertRaises(WorkerShutdownTimeout):
                runner.stop(timeout=0.1)
        self.assertEqual(1, len(closed))
        self.assertEqual(2, len(waited))
        self.release.set()
        runner.stop(timeout=1)
        self.assertEqual(3, len(waited))

    def test_unbounded_shutdown_waits_through_initial_runtime_timeout(self):
        closed, waited = [], []
        class BackgroundRuntime(ReadyRuntime):
            def close(self):
                closed.append(True)
                raise WorkerShutdownTimeout('Fictional first bounded join timed out')

            def wait_closed(self, timeout=5):
                waited.append(timeout)

        runner = self.start(BackgroundRuntime())
        runner.stop(timeout=None)
        self.assertEqual([True], closed)
        self.assertEqual([None], waited)


if __name__ == "__main__":
    unittest.main()
