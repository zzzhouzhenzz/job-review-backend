"""Durable queue tests use temporary databases and no external workers."""
import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from store import ReviewError, Store
from task_store import TASK_SCHEMA, TaskMixin


class TaskStore(TaskMixin, Store):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.executescript(TASK_SCHEMA)


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def enqueue(self, kind="explore", key="discovery", resource=None):
        with self.store.transaction(write=True) as db:
            return self.store._enqueue_task(db, kind, {"reason": "test"}, key, resource=resource)

    def expire(self, task_id):
        with self.store.transaction(write=True) as db:
            db.execute("UPDATE wf_tasks SET lease_until='2000-01-01' WHERE id=?", (task_id,))

    def make_available(self, task_id):
        with self.store.transaction(write=True) as db:
            db.execute("UPDATE wf_tasks SET available_at='2000-01-01' WHERE id=?", (task_id,))

    def test_active_deduplication_survives_restart(self):
        task_id = self.enqueue()
        self.store = TaskStore(self.path)
        self.assertEqual(task_id, self.enqueue())
        lease = self.store.lease_task("worker")
        self.assertEqual(task_id, self.enqueue())
        self.store.finish_task(task_id, lease["lease_token"], {"found": 0})
        self.assertNotEqual(task_id, self.enqueue())

    def test_competing_workers_cannot_claim_same_task(self):
        self.enqueue()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            leases = list(pool.map(self.store.lease_task, ["worker1", "worker2"]))
        self.assertEqual(1, sum(lease is not None for lease in leases))

    def test_resource_serializes_tasks_but_not_unrelated_work(self):
        first = self.enqueue(resource="browser")
        second = self.enqueue(key="second", resource="browser")
        ranking = self.enqueue("rank", key="rank")
        lease = self.store.lease_task("worker1")
        self.assertEqual(first, lease["id"])
        self.assertEqual(ranking, self.store.lease_task("worker2")["id"])
        self.assertIsNone(self.store.lease_task("worker3"))
        self.store.finish_task(first, lease["lease_token"], {})
        self.assertEqual(second, self.store.lease_task("worker3")["id"])

    def test_kind_filter_and_validation(self):
        self.enqueue()
        self.assertIsNone(self.store.lease_task("ranker", kinds=["rank"]))
        self.assertIsNone(self.store.lease_task("none", kinds=[]))
        with self.assertRaises(ReviewError):
            self.store.lease_task("worker", lease_seconds=True)
        with self.assertRaises(ReviewError):
            self.store.lease_task("worker", kinds="explore")

    def test_fencing_token_and_expiry_cover_every_worker_write(self):
        task_id = self.enqueue()
        lease = self.store.lease_task("worker")
        operations = [lambda token: self.store.heartbeat_task(task_id, token),
                      lambda token: self.store.checkpoint_task(task_id, token, {}),
                      lambda token: self.store.finish_task(task_id, token, {}),
                      lambda token: self.store.fail_task(task_id, token, "failed")]
        for operation in operations:
            with self.assertRaises(ReviewError) as caught:
                operation("wrong-token")
            self.assertEqual(409, caught.exception.status)
        self.expire(task_id)
        for operation in operations:
            with self.assertRaises(ReviewError):
                operation(lease["lease_token"])

    def test_retry_backoff_and_bounded_failures(self):
        task_id = self.enqueue()
        for index in range(3):
            lease = self.store.lease_task("worker")
            self.assertEqual(index + 1, lease["attempts"])
            result = self.store.fail_task(task_id, lease["lease_token"], "temporary failure")
            self.assertEqual(index + 1, result["failure_count"])
            self.assertEqual("retry_wait" if index < 2 else "failed", result["status"])
            self.assertIsNone(self.store.lease_task("worker"))
            self.make_available(task_id)
        self.assertIsNone(self.store.lease_task("worker"))

    def test_nonretryable_failure_releases_resource(self):
        task_id = self.enqueue(resource="browser")
        lease = self.store.lease_task("worker")
        self.store.fail_task(task_id, lease["lease_token"], "invalid source", retryable=False)
        next_id = self.enqueue(resource="browser")
        self.assertEqual(next_id, self.store.lease_task("worker")["id"])

    def test_resource_expiry_quarantines_until_explicit_stop_acknowledgement(self):
        task_id = self.enqueue(resource="browser")
        lease = self.store.lease_task("worker")
        self.expire(task_id)
        self.assertEqual(1, self.store.recover_expired_tasks())
        self.assertEqual(0, self.store.recover_expired_tasks())
        task = self.store.tasks()[0]
        self.assertEqual("waiting_for_user", task["status"])
        self.assertEqual("worker_lease_lost", task["error"])
        self.assertTrue(task["resource_quarantined"])
        other_id = self.enqueue(key="other", resource="browser")
        self.assertIsNone(self.store.lease_task("other"))
        with self.store.transaction(write=True) as db:
            with self.assertRaises(ReviewError):
                self.store._resume_task(db, task_id)
        self.store.acknowledge_worker_stopped(task_id)
        self.assertEqual(other_id, self.store.lease_task("other")["id"])
        with self.assertRaises(ReviewError):
            self.store.finish_task(task_id, lease["lease_token"], {})
        with self.store.transaction(write=True) as db:
            self.assertEqual("queued", self.store._resume_task(db, task_id)["status"])

    def test_non_resource_expiry_retries_with_new_fence_after_cooldown(self):
        task_id = self.enqueue("rank")
        old = self.store.lease_task("old")
        self.expire(task_id)
        self.assertIsNone(self.store.lease_task("new"))
        self.make_available(task_id)
        new = self.store.lease_task("new")
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        with self.assertRaises(ReviewError):
            self.store.finish_task(task_id, old["lease_token"], {})

    def test_submission_checkpoint_survives_failure_and_blocks_replay(self):
        task_id = self.enqueue("apply", resource="browser")
        lease = self.store.lease_task("worker")
        self.store.checkpoint_task(task_id, lease["lease_token"], {"submission_started": True})
        with self.assertRaises(ReviewError):
            self.store.checkpoint_task(task_id, lease["lease_token"], {})
        task = self.store.fail_task(task_id, lease["lease_token"], "connection lost")
        self.assertEqual("waiting_for_user", task["status"])
        self.assertIn("submission_unknown", task["error"])
        with self.store.transaction(write=True) as db:
            with self.assertRaises(ReviewError):
                self.store._resume_task(db, task_id)
        self.assertIsNone(self.store.lease_task("worker"))

    def test_submission_checkpoint_survives_browser_worker_loss(self):
        task_id = self.enqueue("apply", resource="browser")
        lease = self.store.lease_task("worker")
        self.store.checkpoint_task(task_id, lease["lease_token"], {"submission_started": True})
        self.expire(task_id)
        self.store.recover_expired_tasks()
        self.store = TaskStore(self.path)
        self.assertTrue(self.store.tasks()[0]["resource_quarantined"])
        stopped = self.store.acknowledge_worker_stopped(task_id)
        self.assertEqual("waiting_for_user", stopped["status"])
        with self.store.transaction(write=True) as db:
            with self.assertRaises(ReviewError):
                self.store._resume_task(db, task_id)

    def test_apply_cannot_finish_without_application_receipt_flow(self):
        task_id = self.enqueue("apply", resource="browser")
        lease = self.store.lease_task("worker")
        with self.assertRaises(ReviewError):
            self.store.finish_task(task_id, lease["lease_token"], {"claimed": "applied"})

    def test_clean_question_pauses_do_not_exhaust_failure_budget(self):
        task_id = self.enqueue("apply", resource="browser")
        for index in range(5):
            lease = self.store.lease_task("worker")
            self.assertEqual(index + 1, lease["attempts"])
            with self.store.transaction(write=True) as db:
                self.store._leased_task(db, task_id, lease["lease_token"])
                self.store._release_task(db, task_id, "waiting_for_user")
                self.store._resume_task(db, task_id)
        self.assertEqual(0, self.store.tasks()[0]["failure_count"])

    def test_cancel_only_before_work_and_json_validation(self):
        task_id = self.enqueue()
        with self.store.transaction(write=True) as db:
            self.assertEqual("cancelled", self.store._cancel_task(db, task_id)["status"])
        second_id = self.enqueue()
        lease = self.store.lease_task("worker")
        with self.store.transaction(write=True) as db:
            with self.assertRaises(ReviewError):
                self.store._cancel_task(db, second_id)
        with self.assertRaises(ReviewError):
            self.store.checkpoint_task(second_id, lease["lease_token"], {"invalid": float("nan")})


if __name__ == "__main__":
    unittest.main()
