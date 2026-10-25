"""Accepted rankings export once; file failures never repeat model work."""
import errno
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from ranked_output import write_ranked_output
from store import ReviewError
from test_store import catalog
from workflow import WorkflowStore


class RankArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fixture.sqlite3"
        self.store = WorkflowStore(self.path)
        self.store.import_catalog(catalog(), "Fictional fixture")
        with self.store.transaction(True) as db:
            self.task_id = self.store._request_discovery(db, "a")
        self.task = self.store.lease_task("fixture-ranker", ["rank"])
        self.store.discovery_context(self.task_id, self.task["lease_token"])
        candidates = {job["key"]: job for job in self.task["payload"]["candidates"]}
        self.result = {"cards": [{"company_id": "a", "summary": "Three fictional matches.", "jobs": [
            {"key": key, "revision": candidates[key]["revision"], "reason": f"Evidence for {key}.",
             "brief": {"work": "Build backend services.", "fit": "Backend experience.", "gap": "Confirm domain experience."}}
            for key in ("a:3", "a:1", "a:2")]}]}

    def tearDown(self):
        self.tmp.cleanup()

    def accept(self, result=None):
        return self.store.accept_ranking(self.task_id, self.task["lease_token"],
                                         self.result if result is None else result)

    def outbox(self):
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM wf_rank_outputs WHERE task_id=?", (self.task_id,)).fetchone()
            return dict(row) if row else None

    def current_task(self):
        return next(task for task in self.store.tasks() if task["id"] == self.task_id)

    def test_success_exports_exact_validated_snapshots_and_private_atomic_file(self):
        receipt = self.accept()
        artifact = Path(receipt["artifact_path"])
        payload = json.loads(artifact.read_text())
        self.assertEqual("ready", receipt["artifact_status"])
        self.assertEqual("succeeded", self.current_task()["status"])
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual(self.task_id, payload["task_id"])
        self.assertEqual(receipt["card_ids"], payload["card_ids"])
        exported = payload["cards"][0]
        live = next(card for card in self.store.feed()["cards"] if card["company"]["id"] == "a")
        for field in ("id", "revision", "company", "jobs", "content"):
            self.assertEqual(live[field], exported[field])
        self.assertEqual(["a:3", "a:1", "a:2"], [job["key"] for job in exported["jobs"]])
        self.assertEqual(0o600, stat.S_IMODE(artifact.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(artifact.parent.stat().st_mode))
        self.assertEqual([artifact], list(artifact.parent.iterdir()))
        self.assertEqual("ready", self.outbox()["status"])

    def test_disk_failure_parks_export_without_publication_or_agent_retry(self):
        with patch("ranked_output.write_ranked_output", side_effect=OSError(errno.ENOSPC, "disk full")):
            receipt = self.accept()
        self.assertEqual("pending", receipt["artifact_status"])
        self.assertIn("errno=28", receipt["artifact_error"])
        self.assertEqual("waiting_for_user", self.current_task()["status"])
        self.assertEqual("ranked_output_pending", self.current_task()["error"])
        self.assertIsNone(self.store.lease_task("another-worker", ["rank"]))
        with self.store.transaction(True) as db:
            with self.assertRaisesRegex(ReviewError, "retry export"):
                self.store._resume_task(db, self.task_id)
        self.assertEqual([], [card for card in self.store.feed()["cards"] if card["id"] in receipt["card_ids"]])
        with self.store.transaction() as db:
            self.assertEqual("prepared", db.execute("SELECT status FROM wf_cards WHERE id=?", (receipt["card_ids"][0],)).fetchone()[0])
        self.store = WorkflowStore(self.path)
        repaired = self.store.repair_ranked_outputs()
        self.assertEqual(1, len(repaired))
        self.assertEqual("ready", repaired[0]["artifact_status"])
        self.assertTrue(Path(repaired[0]["artifact_path"]).is_file())
        self.assertEqual("succeeded", self.current_task()["status"])
        self.assertEqual(1, self.current_task()["attempts"])
        self.assertEqual(receipt["card_ids"], [card["id"] for card in self.store.feed()["cards"] if card["company"]["id"] == "a"])
        self.assertEqual([], self.store.repair_ranked_outputs())

    def test_crash_after_file_rename_repairs_without_duplicate_cards(self):
        with patch.object(self.store, "_publish_cards", side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.accept()
        self.assertEqual("pending", self.outbox()["status"])
        self.assertEqual("waiting_for_user", self.current_task()["status"])
        artifact = self.path.parent / "ranked-output" / (self.task_id + ".json")
        before = artifact.read_bytes()
        self.store = WorkflowStore(self.path)
        receipt = self.store.repair_ranked_outputs()[0]
        self.assertEqual(before, artifact.read_bytes())
        self.assertEqual(receipt["card_ids"], json.loads(before)["card_ids"])
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM wf_rank_output_cards WHERE task_id=?", (self.task_id,)).fetchone()[0])
        self.assertEqual("succeeded", self.current_task()["status"])

    def test_invalid_output_commits_no_artifact_or_outbox(self):
        self.result["cards"][0]["jobs"][1]["revision"] += 1
        with self.assertRaises(ReviewError):
            self.accept()
        self.assertIsNone(self.outbox())
        self.assertFalse((self.path.parent / "ranked-output").exists())
        self.assertEqual("running", self.current_task()["status"])

    def test_delayed_export_preserves_snapshot_but_does_not_publish_stale_card(self):
        with patch("ranked_output.write_ranked_output", side_effect=OSError(errno.ENOSPC, "disk full")):
            receipt = self.accept()
        company = next(item for item in self.store.state()["companies"] if item["id"] == "a")
        self.store.edit_company({"id": "a", "revision": company["revision"], "excluded": False,
                                 "note": "Only consider remote work."})
        repaired = self.store.repair_ranked_outputs()[0]
        exported = json.loads(Path(repaired["artifact_path"]).read_text())["cards"][0]
        self.assertEqual(company, exported["company"])
        self.assertEqual([], [card for card in self.store.feed()["cards"] if card["id"] in receipt["card_ids"]])
        with self.store.transaction() as db:
            self.assertEqual("stale", db.execute("SELECT status FROM wf_cards WHERE id=?", (receipt["card_ids"][0],)).fetchone()[0])

    def test_empty_ranking_exports_empty_cards_and_schedules_exploration_once(self):
        with patch("ranked_output.write_ranked_output", side_effect=OSError(errno.ENOSPC, "disk full")):
            receipt = self.accept({"cards": []})
        self.assertIsNone(receipt["next_task_id"])
        self.assertEqual([], [task for task in self.store.tasks() if task["kind"] == "explore"])
        receipt = self.store.repair_ranked_outputs()[0]
        self.assertIsNotNone(receipt["next_task_id"])
        self.assertEqual([], json.loads(Path(receipt["artifact_path"]).read_text())["cards"])
        self.assertEqual(receipt, self.store.repair_ranked_outputs(self.task_id)[0])
        self.assertEqual(1, len([task for task in self.store.tasks() if task["kind"] == "explore"]))

    def test_repair_does_not_complete_another_active_owner(self):
        with patch("ranked_output.write_ranked_output", side_effect=OSError(errno.ENOSPC, "disk full")):
            self.accept()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET status='running',lease_owner='new-owner',lease_token='new-token',error=NULL WHERE id=?", (self.task_id,))
        receipt = self.store.repair_ranked_outputs()[0]
        self.assertEqual("pending", receipt["artifact_status"])
        self.assertEqual("new-token", self.current_task()["lease_token"])
        self.assertEqual("running", self.current_task()["status"])

    def test_symlink_output_directory_is_not_followed(self):
        other = self.path.parent / "other"
        other.mkdir()
        (self.path.parent / "ranked-output").symlink_to(other, target_is_directory=True)
        receipt = self.accept()
        self.assertEqual("pending", receipt["artifact_status"])
        self.assertEqual([], list(other.iterdir()))
        self.assertEqual("waiting_for_user", self.current_task()["status"])

    def test_parent_directory_sync_failure_prevents_task_success(self):
        parent_inode = self.path.parent.stat().st_ino
        original_sync = os.fsync
        def sync(fd):
            if os.fstat(fd).st_ino == parent_inode:
                raise OSError(errno.EIO, "simulated parent directory sync failure")
            original_sync(fd)
        with patch("ranked_output.os.fsync", side_effect=sync):
            receipt = self.accept()
        self.assertEqual("pending", receipt["artifact_status"])
        self.assertEqual("waiting_for_user", self.current_task()["status"])
        self.assertEqual("ready", self.store.repair_ranked_outputs()[0]["artifact_status"])

    def test_three_concurrent_repairs_publish_and_schedule_only_once(self):
        with patch("ranked_output.write_ranked_output", side_effect=OSError(errno.ENOSPC, "disk full")):
            self.accept({"cards": []})
        stores = [WorkflowStore(self.path) for _ in range(3)]
        barrier = threading.Barrier(3, timeout=5)
        def simultaneous_write(*args):
            barrier.wait()
            return write_ranked_output(*args)
        with patch("ranked_output.write_ranked_output", side_effect=simultaneous_write):
            with ThreadPoolExecutor(max_workers=3) as pool:
                receipts = list(pool.map(lambda store: store.repair_ranked_outputs()[0], stores))
        self.assertTrue(all(receipt == receipts[0] for receipt in receipts))
        self.assertEqual("ready", receipts[0]["artifact_status"])
        self.assertEqual(1, len([task for task in self.store.tasks() if task["kind"] == "explore"]))
        self.assertEqual(1, self.current_task()["attempts"])
        directory = self.path.parent / "ranked-output"
        self.assertEqual([directory / (self.task_id + ".json")], list(directory.iterdir()))

    def test_failed_concurrent_writer_returns_the_winners_ready_receipt(self):
        with patch("ranked_output.write_ranked_output", side_effect=OSError(errno.ENOSPC, "disk full")):
            self.accept()
        stores = [WorkflowStore(self.path) for _ in range(2)]
        barrier = threading.Barrier(2, timeout=5)
        winner_done = threading.Event()
        writer_count = iter(range(2))
        def competing_write(*args):
            writer = next(writer_count)
            barrier.wait()
            if writer == 1:
                if not winner_done.wait(5):
                    raise AssertionError("The successful repair did not complete.")
                raise OSError(errno.ENOSPC, "disk full")
            return write_ranked_output(*args)
        def repair(store):
            receipt = store.repair_ranked_outputs()[0]
            winner_done.set()
            return receipt
        with patch("ranked_output.write_ranked_output", side_effect=competing_write):
            with ThreadPoolExecutor(max_workers=2) as pool:
                receipts = list(pool.map(repair, stores))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual("ready", receipts[0]["artifact_status"])
        self.assertIsNone(self.outbox()["error"])


if __name__ == "__main__":
    unittest.main()
