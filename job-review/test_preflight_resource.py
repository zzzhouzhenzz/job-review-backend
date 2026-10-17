"""Health probes share the durable browser guard with application workers."""
import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from store import ReviewError, Store
from task_store import TASK_SCHEMA, TaskMixin


class PreflightStore(TaskMixin, Store):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.executescript(TASK_SCHEMA)


class PreflightResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "preflight.sqlite3"
        self.store = PreflightStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def enqueue(self, key="worker", kind="explore", payload=None, resource="browser"):
        with self.store.transaction(write=True) as db:
            return self.store._enqueue_task(db, kind, payload or {}, key, resource=resource)

    def expire(self, task_id):
        with self.store.transaction(write=True) as db:
            db.execute("UPDATE wf_tasks SET lease_until='2000-01-01' WHERE id=?", (task_id,))

    def lock(self):
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM wf_resource_locks WHERE resource='browser'").fetchone()
            return None if row is None else dict(row)

    def test_probe_is_created_and_leased_atomically(self):
        probe = self.store.lease_preflight("server", "boot-1")
        self.assertEqual("reconcile", probe["kind"])
        self.assertEqual("running", probe["status"])
        self.assertEqual({"purpose": "preflight", "probe_id": "boot-1"}, probe["payload"])
        self.assertEqual(1, probe["attempts"])
        self.assertEqual(1, probe["max_attempts"])
        self.assertEqual(probe["id"], self.lock()["task_id"])
        self.assertEqual(probe["lease_token"], self.lock()["lease_token"])
        self.enqueue()
        self.assertIsNone(self.store.lease_task("worker"))

    def test_busy_worker_creates_no_dangling_probe(self):
        task_id = self.enqueue()
        worker = self.store.lease_task("worker")
        self.assertIsNone(self.store.lease_preflight("server", "boot-1"))
        self.assertEqual([task_id], [task["id"] for task in self.store.tasks()])
        self.assertEqual(worker["lease_token"], self.lock()["lease_token"])

    def test_expired_worker_is_quarantined_before_probe_can_start(self):
        task_id = self.enqueue()
        worker = self.store.lease_task("worker")
        self.expire(task_id)
        self.assertIsNone(self.store.lease_preflight("server", "boot-1"))
        self.assertEqual(1, len(self.store.tasks()))
        self.assertEqual("waiting_for_user", self.store.tasks()[0]["status"])
        self.assertEqual(1, self.lock()["quarantined"])
        self.assertEqual(worker["lease_token"], self.lock()["lease_token"])
        self.assertIsNone(self.store.lease_preflight("server", "boot-2"))
        self.assertEqual(1, len(self.store.tasks()))

    def test_competing_probes_leave_only_one_running_task(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            probes = list(pool.map(lambda key: self.store.lease_preflight("server", key), ["a", "b"]))
        self.assertEqual(1, sum(probe is not None for probe in probes))
        self.assertEqual(1, len(self.store.tasks()))
        self.assertEqual("running", self.store.tasks()[0]["status"])

    def test_timeout_keeps_original_token_in_quarantined_lock(self):
        probe = self.store.lease_preflight("server", "boot-1")
        for unused in range(2):
            task = self.store.timeout_preflight(probe["id"], probe["lease_token"])
            self.assertEqual("waiting_for_user", task["status"])
            self.assertIsNone(task["lease_token"])
            self.assertEqual("preflight_timeout", task["error"])
            self.assertTrue(task["resource_quarantined"])
            self.assertEqual(probe["lease_token"], self.lock()["lease_token"])
        self.enqueue()
        self.assertIsNone(self.store.lease_task("worker"))
        self.assertIsNone(self.store.lease_preflight("server", "boot-2"))

    def test_probe_return_after_timeout_releases_only_its_lock(self):
        probe = self.store.lease_preflight("server", "boot-1")
        self.store.timeout_preflight(probe["id"], probe["lease_token"])
        task = self.store.stop_preflight(probe["id"], probe["lease_token"], success=False,
                                         error="Probe returned after its deadline.")
        self.assertEqual("failed", task["status"])
        self.assertEqual("Probe returned after its deadline.", task["error"])
        self.assertIsNone(self.lock())
        self.assertIsNotNone(self.store.lease_preflight("server", "boot-2"))

    def test_return_after_generic_expiry_recovery_can_release_quarantine(self):
        probe = self.store.lease_preflight("server", "boot-1")
        self.expire(probe["id"])
        self.assertEqual(1, self.store.recover_expired_tasks())
        self.store = PreflightStore(self.path)
        self.assertEqual(1, self.lock()["quarantined"])
        task = self.store.stop_preflight(probe["id"], probe["lease_token"], success=False,
                                         error="Lease expired during probe.")
        self.assertEqual("failed", task["status"])
        self.assertIsNone(self.lock())

    def test_stopped_probe_cannot_be_replayed_or_release_a_later_owner(self):
        probe = self.store.lease_preflight("server", "boot-1")
        stopped = self.store.stop_preflight(probe["id"], probe["lease_token"], success=True)
        self.assertEqual("succeeded", stopped["status"])
        self.assertIsNone(self.store.lease_preflight("server", "boot-1"))
        task_id = self.enqueue()
        self.store.lease_task("worker")
        original_lock = self.lock()
        self.assertEqual(task_id, original_lock["task_id"])
        self.assertEqual(stopped, self.store.stop_preflight(probe["id"], probe["lease_token"], success=False))
        self.assertEqual(stopped, self.store.timeout_preflight(probe["id"], probe["lease_token"]))
        self.assertEqual(original_lock, self.lock())

    def test_operator_release_then_late_return_does_not_touch_a_new_probe(self):
        old = self.store.lease_preflight("server", "boot-1")
        self.store.timeout_preflight(old["id"], old["lease_token"])
        released = self.store.acknowledge_worker_stopped(old["id"])
        new = self.store.lease_preflight("server", "boot-2")
        original_lock = self.lock()
        self.assertEqual(released, self.store.stop_preflight(old["id"], old["lease_token"], success=True))
        self.assertEqual(released, self.store.timeout_preflight(old["id"], old["lease_token"]))
        self.assertEqual(original_lock, self.lock())
        self.assertEqual(new["id"], self.lock()["task_id"])

    def test_wrong_token_or_other_task_cannot_clear_probe(self):
        probe = self.store.lease_preflight("server", "boot-1")
        original_lock = self.lock()
        for operation in (self.store.timeout_preflight,
                          lambda task_id, token: self.store.stop_preflight(task_id, token, success=True)):
            with self.assertRaises(ReviewError) as caught:
                operation(probe["id"], "wrong-token")
            self.assertEqual(409, caught.exception.status)
            task_id = self.enqueue(key=str(id(operation)))
            with self.assertRaises(ReviewError):
                operation(task_id, probe["lease_token"])
            self.assertEqual(original_lock, self.lock())

    def test_generic_lease_skips_internal_probe_even_if_manually_requeued(self):
        internal = self.enqueue("internal", kind="reconcile", payload={"purpose": "preflight", "probe_id": "a"})
        regular = self.enqueue("regular", kind="reconcile", payload={"purpose": "other"})
        leased = self.store.lease_task("worker", kinds=["reconcile"])
        self.assertEqual(regular, leased["id"])
        self.store.finish_task(regular, leased["lease_token"], {})
        with self.store.transaction(write=True) as db:
            db.execute("UPDATE wf_tasks SET status='retry_wait' WHERE id=?", (internal,))
        self.assertIsNone(self.store.lease_task("worker"))
        self.assertIsNone(self.lock())

    def test_preflight_validation_does_not_create_task(self):
        for kwargs in ({"owner": "", "probe_id": "a"}, {"owner": "a", "probe_id": ""},
                       {"owner": "a", "probe_id": "b", "lease_seconds": True}):
            with self.assertRaises(ReviewError):
                self.store.lease_preflight(**kwargs)
        self.assertEqual([], self.store.tasks())


if __name__ == "__main__":
    unittest.main()
