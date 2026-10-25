"""Durable, private exports of accepted ranker output.

SQLite owns the result. An outbox row is committed with the cards, then written
to an atomic JSON file. A task succeeds only after that file is durable. A crash
or disk error leaves recoverable export work, never a second model invocation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

from store import encode, now


RANKED_OUTPUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS wf_rank_outputs (
 task_id TEXT PRIMARY KEY REFERENCES wf_tasks(id),
 payload TEXT NOT NULL, receipt TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','ready')),
 error TEXT, created_at TEXT NOT NULL, materialized_at TEXT
);
CREATE TABLE IF NOT EXISTS wf_rank_output_cards (
 card_id TEXT PRIMARY KEY REFERENCES wf_cards(id),
 task_id TEXT NOT NULL REFERENCES wf_rank_outputs(task_id)
);
"""


def write_ranked_output(directory: Path, task_id: str, payload: str) -> Path:
    """Write one immutable snapshot, without following output-directory links."""
    if str(uuid.UUID(task_id)) != task_id:
        raise ValueError("Rank output requires a canonical task UUID.")
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".{task_id}.{uuid.uuid4()}.tmp"
    try:
        os.fchmod(directory_fd, 0o700)
        output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=directory_fd)
        with os.fdopen(output_fd, "w", encoding="utf-8") as output:
            output.write(payload + "\n")
            output.flush()
            os.fsync(output.fileno())
        destination = task_id + ".json"
        os.replace(temporary, destination, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
        # The first export may have created ranked-output itself. Its entry in
        # the database directory must be durable before SQLite reports success.
        # Every writer syncs it, so concurrent writers cannot depend on the
        # creator reaching this point first.
        parent_fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return directory / destination
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


class RankedOutputMixin:
    def _queue_ranked_output(self, db, task, cards, receipt):
        """Commit this with the validated cards; no file operation occurs here."""
        payload = {"schema_version": 1, "task_id": task["id"],
                   "card_ids": receipt["card_ids"], "cards": cards, "created_at": now()}
        db.execute("INSERT INTO wf_rank_outputs VALUES (?,?,?,'pending',NULL,?,NULL)",
                   (task["id"], encode(payload), encode(receipt), payload["created_at"]))
        for card in cards:
            db.execute("INSERT INTO wf_rank_output_cards VALUES (?,?)", (card["id"], task["id"]))
        # The existing durable wait state prevents lease recovery from repeating
        # the ranker. This is an output-I/O wait, not an application question.
        self._release_task(db, task["id"], "waiting_for_user", result={
            "card_ids": receipt["card_ids"], "artifact_status": "pending",
        }, error="ranked_output_pending")

    @staticmethod
    def _pending_rank_card_ids(db):
        return {r[0] for r in db.execute("""SELECT c.card_id FROM wf_rank_output_cards c
            JOIN wf_rank_outputs o ON o.task_id=c.task_id WHERE o.status='pending'""")}

    def repair_ranked_outputs(self, task_id=None):
        """Retry accepted exports and return their receipts; never run a ranker.

        Safe for concurrent runner ticks. File writes use immutable DB payloads;
        the final transaction permits only one publication and follow-up task.
        A pending receipt means the task must not be failed or sent to an agent.
        """
        with self.transaction() as db:
            if task_id is None:
                rows = db.execute("SELECT * FROM wf_rank_outputs WHERE status='pending' ORDER BY created_at,task_id").fetchall()
            else:
                rows = db.execute("SELECT * FROM wf_rank_outputs WHERE task_id=?", (task_id,)).fetchall()
        receipts = []
        for row in rows:
            if row["status"] == "ready":
                receipts.append(json.loads(row["receipt"]))
                continue
            try:
                path = write_ranked_output(self.path.resolve().parent / "ranked-output", row["task_id"], row["payload"])
            except OSError as error:
                # The payload can contain private job notes. Record only the
                # error class/code, never payload text or environment details.
                message = f"Ranked output write failed ({type(error).__name__}; errno={error.errno})."
                with self.transaction(True) as db:
                    current = db.execute("SELECT * FROM wf_rank_outputs WHERE task_id=?", (row["task_id"],)).fetchone()
                    if current["status"] == "ready":
                        receipts.append(json.loads(current["receipt"]))
                    else:
                        db.execute("UPDATE wf_rank_outputs SET error=? WHERE task_id=?", (message, row["task_id"]))
                        receipts.append({**json.loads(current["receipt"]), "artifact_status": "pending", "artifact_error": message})
                continue
            with self.transaction(True) as db:
                current = db.execute("SELECT * FROM wf_rank_outputs WHERE task_id=?", (row["task_id"],)).fetchone()
                if current["status"] == "ready":
                    receipts.append(json.loads(current["receipt"]))
                    continue
                task = self.task(self._task(db, row["task_id"]))
                receipt = json.loads(current["receipt"])
                if task["status"] != "waiting_for_user" or task["error"] != "ranked_output_pending":
                    receipts.append({**receipt, "artifact_status": "pending", "artifact_error":
                                     "Ranked output task is no longer waiting for export."})
                    continue
                selected = [job["key"] for card in json.loads(current["payload"])["cards"] for job in card["jobs"]]
                receipt.update(artifact_status="ready", artifact_path=str(path))
                self._release_task(db, task["id"], "succeeded", result={
                    "selected": selected, "card_ids": receipt["card_ids"],
                    "artifact_status": "ready", "artifact_path": str(path),
                })
                db.execute("UPDATE wf_rank_outputs SET status='ready',error=NULL,materialized_at=? WHERE task_id=?", (now(), task["id"]))
                self._publish_cards(db)
                # An empty ranking tries one bounded source crawl after the
                # catalog; completion and scheduling stay in one transaction.
                if not selected and not task["payload"].get("after_explore"):
                    receipt["next_task_id"] = self._enqueue_task(db, "explore", {
                        **task["payload"], "candidates": [],
                    }, task["dedupe_key"], resource="browser")
                db.execute("UPDATE wf_rank_outputs SET receipt=? WHERE task_id=?", (encode(receipt), task["id"]))
                receipts.append(receipt)
        return receipts
