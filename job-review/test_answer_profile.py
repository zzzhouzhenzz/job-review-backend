"""User answer persistence and projection tests use fictional temporary data."""
import copy
import fcntl
import json
import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import answer_profile
from applicant_evidence import EvidenceChanged, EvidenceSnapshot
from answer_profile import BEGIN_MARKER, END_MARKER, MAX_PROFILE_BYTES, read_profile_records
from store import ReviewError, encode
from test_applications import ApplicationStore
from test_store import catalog


class AnswerProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "review.sqlite3"
        self.profile = Path(self.temp.name) / "profile.md"
        self.manual = "# Fictional application profile\n\nManually confirmed material.\n"
        self.profile.write_text(self.manual)
        self.store = ApplicationStore(self.path)
        self.store.application_profile = self.profile
        self.store.import_catalog(catalog(), "fictional")
        with self.store.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='approve',revision=revision+1 WHERE key='a:1'")
            self.pid = self.store._create_application(db, self.store._row(db, "jobs", "key", "a:1"), "approved")
        task = self.store.lease_task("fictional", ["apply"])
        self.store.start_application(self.pid, task["lease_token"])
        self.store.checkpoint_task(task["id"], task["lease_token"], {
            "question_bindings": {"q1": "sponsorship", "q2": "name", "q3": "arrangements"}})
        self.store.ask_questions(self.pid, task["lease_token"], "form-v1", [
            {"id": "q1", "text": "Do you need sponsorship?", "type": "single_choice", "required": True,
             "choices": [{"id": "yes-1", "label": "Yes"}, {"id": "no-2", "label": "No"}]},
            {"id": "q2", "text": "Full name?", "type": "text", "required": True},
            {"id": "q3", "text": "Work arrangements?", "type": "multi_choice", "required": False,
             "choices": [{"id": "remote-7", "label": "Remote"}, {"id": "office-8", "label": "In office"}]},
        ])

    def payload(self, qid="q1", value="no-2", request_id="answer-1"):
        question = next(q for q in self.store.questions() if q["id"] == qid)
        process = self.store.processes()[0]
        return {"request_id": request_id, "process_id": self.pid, "process_revision": process["revision"],
                "form_version": process["form_version"],
                "answers": [{"question_id": qid, "question_revision": question["revision"], "value": value}]}

    def records(self):
        return self.store.profile_answer_records()

    def outbox(self):
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM wf_profile_outbox ORDER BY answer_id")]

    @staticmethod
    def provenance(qid="q1", source_hash="a" * 64):
        return {qid: {"field_name": "sponsorship" if qid == "q1" else "name", "method": "direct",
                      "source_snapshot_hash": source_hash,
                      "citations": [{"source_id": "profile", "sha256": "b" * 64, "start_line": 1,
                                     "end_line": 1, "quote": "Fictional confirmed evidence"}],
                      "explanation": "A fictional source directly states this fact."}}

    def test_user_answer_record_and_projection_keep_exact_context(self):
        payload = self.payload()
        receipt = self.store.answer_questions(payload)
        self.assertTrue(receipt["accepted"])
        record = self.records()[0]
        self.assertEqual(record["question_text"], "Do you need sponsorship?")
        self.assertEqual(record["choices"], [{"id": "yes-1", "label": "Yes"}, {"id": "no-2", "label": "No"}])
        self.assertEqual(record["value"], "no-2")
        self.assertEqual(record["answer_labels"], ["No"])
        self.assertEqual(record["question_revision"], 1)
        self.assertEqual(record["answer_revision"], 2)
        self.assertEqual(record["process_id"], self.pid)
        self.assertEqual(record["job_key"], "a:1")
        self.assertEqual(record["scope"], "application")
        self.assertEqual(record["authority"], "user")
        content = self.profile.read_text()
        self.assertTrue(content.startswith(self.manual))
        parsed = read_profile_records(content)
        self.assertEqual(parsed[0][0], record)
        self.assertEqual(json.loads(content.splitlines()[parsed[0][1] - 1]), record)
        self.assertEqual(stat.S_IMODE(self.profile.stat().st_mode), 0o600)
        self.assertIsNotNone(self.outbox()[0]["projected_at"])
        self.assertEqual(self.store.processes()[0]["task_status"], "waiting_for_user")

    def test_retry_and_correction_keep_history_and_export_only_latest(self):
        payload = self.payload()
        first = self.store.answer_questions(payload)
        self.assertEqual(self.store.answer_questions(payload), first)
        self.assertEqual(len(self.records()), 1)
        self.store.answer_questions(self.payload(value="yes-1", request_id="corrected"))
        self.assertEqual([record["answer_labels"] for record in self.records()], [["No"], ["Yes"]])
        latest = read_profile_records(self.profile.read_text())
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0][0]["answer_labels"], ["Yes"])
        self.assertEqual(latest[0][0]["answer_revision"], 3)

    def test_stable_sync_does_not_change_file_or_outbox_rows(self):
        self.store.answer_questions(self.payload())
        before_rows = self.outbox()
        before_stat = self.profile.stat()
        with patch("answer_profile.os.replace") as replace:
            self.store.sync_profile_answers(strict=True)
            self.store.sync_profile_answers(strict=True)
            replace.assert_not_called()
        self.assertEqual(self.outbox(), before_rows)
        self.assertEqual(self.profile.stat().st_ino, before_stat.st_ino)
        self.assertEqual(self.profile.stat().st_mtime_ns, before_stat.st_mtime_ns)

    def test_parser_rejects_incomplete_or_mistyped_managed_records(self):
        self.store.answer_questions(self.payload())
        content = self.profile.read_text()
        record, line_number = read_profile_records(content)[0]
        for key, value in (("choices", ["invalid"]), ("value", {}), ("answer_labels", ["Wrong"]),
                           ("source_request_sequence", "1"), ("company_id", None)):
            invalid = {**record, key: value}
            lines = content.splitlines()
            lines[line_number - 1] = encode(invalid)
            with self.assertRaises(ReviewError):
                read_profile_records("\n".join(lines))

    def test_multi_choice_and_text_values_keep_labels(self):
        self.store.answer_questions(self.payload("q3", ["office-8", "remote-7"]))
        self.store.answer_questions(self.payload("q2", "Fictional Name", "answer-2"))
        self.assertEqual(self.records()[0]["answer_labels"], ["In office", "Remote"])
        self.assertEqual(self.records()[1]["answer_labels"], ["Fictional Name"])

    def test_worker_evidence_is_not_promoted_even_after_backfill(self):
        self.store.answer_questions_from_evidence(self.payload(), self.provenance(), validate_sources=lambda: None)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.store.backfill_profile_answers(), {"backfilled": 0})
        self.assertEqual(self.profile.read_text(), self.manual)
        self.assertEqual(self.outbox(), [])

    def test_atomic_persistence_failure_rolls_back_answer_and_receipt(self):
        before = self.store.questions()
        with patch("application_flow.record_user_answers", side_effect=ReviewError("Fictional persistence error")):
            with self.assertRaises(ReviewError):
                self.store.answer_questions(self.payload())
        self.assertEqual(self.store.questions(), before)
        self.assertEqual(self.records(), [])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_answer_requests").fetchone()[0], 0)

    def test_file_failure_keeps_accepted_answer_and_retries_without_duplicates(self):
        payload = self.payload()
        with patch("answer_profile.os.replace", side_effect=OSError("fictional secret detail")):
            self.assertTrue(self.store.answer_questions(payload)["accepted"])
        self.assertEqual(self.outbox()[0]["last_error"], "profile_sync_failed")
        self.assertIsNone(self.outbox()[0]["projected_at"])
        self.assertEqual(self.profile.read_text(), self.manual)
        self.assertTrue(self.store.answer_questions(payload)["accepted"])
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(read_profile_records(self.profile.read_text())), 1)
        self.assertIsNotNone(self.outbox()[0]["projected_at"])

    def test_no_profile_configuration_keeps_durable_pending_record(self):
        del self.store.application_profile
        self.store.answer_questions(self.payload())
        self.assertEqual(self.profile.read_text(), self.manual)
        self.assertEqual(self.store.sync_profile_answers(), {"status": "not_configured", "pending": 1})
        self.store.application_profile = self.profile
        self.assertEqual(self.store.sync_profile_answers(strict=True)["status"], "current")

    def test_manual_prefix_suffix_and_new_manual_edits_survive_projection(self):
        self.store.answer_questions(self.payload())
        self.profile.write_text(self.profile.read_text() + "\n## Manual footer\nExact trailing bytes.  \n")
        self.store.answer_questions(self.payload(value="yes-1", request_id="correction"))
        self.assertTrue(self.profile.read_text().startswith(self.manual))
        self.assertTrue(self.profile.read_text().endswith("\n## Manual footer\nExact trailing bytes.  \n"))
        self.assertEqual(self.profile.read_text().count(BEGIN_MARKER), 1)

    def test_manual_edit_during_projection_is_not_overwritten(self):
        original_replace = answer_profile._replace_profile
        def edit_then_replace(path, original, identity, data):
            path.write_bytes(original + b"\nUser edit made concurrently.\n")
            return original_replace(path, original, identity, data)
        with patch("answer_profile._replace_profile", side_effect=edit_then_replace):
            self.store.answer_questions(self.payload())
        self.assertEqual(self.outbox()[0]["last_error"], "profile_changed")
        self.assertTrue(self.profile.read_text().endswith("User edit made concurrently.\n"))
        self.assertNotIn(BEGIN_MARKER, self.profile.read_text())
        self.store.sync_profile_answers(strict=True)
        self.assertIn("User edit made concurrently.", self.profile.read_text())
        self.assertEqual(len(read_profile_records(self.profile.read_text())), 1)

    def test_projection_lock_leaves_outbox_pending_and_strict_reports_safe_error(self):
        lock = os.open(self.profile.with_name(f".{self.profile.name}.job-review.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.store.answer_questions(self.payload())
            self.assertEqual(self.outbox()[0]["last_error"], "profile_busy")
            with self.assertRaises(ReviewError) as caught:
                self.store.sync_profile_answers(strict=True)
            self.assertEqual(caught.exception.reason, "profile_busy")
        finally:
            os.close(lock)
        self.assertEqual(self.store.sync_profile_answers(strict=True)["status"], "current")

    def test_projection_crash_after_rename_before_outbox_mark_is_idempotent(self):
        del self.store.application_profile
        self.store.answer_questions(self.payload())
        self.store.application_profile = self.profile
        original_transaction = self.store.transaction
        failed = False
        @contextmanager
        def fail_first_write(write=False):
            nonlocal failed
            if write and not failed:
                failed = True
                raise OSError("fictional disk write failure after replace")
            with original_transaction(write) as db:
                yield db
        with patch.object(self.store, "transaction", fail_first_write):
            self.assertEqual(self.store.sync_profile_answers()["status"], "pending")
        self.assertEqual(len(read_profile_records(self.profile.read_text())), 1)
        self.assertIsNone(self.outbox()[0]["projected_at"])
        with patch("answer_profile.os.replace") as replace:
            self.assertEqual(self.store.sync_profile_answers(strict=True)["status"], "current")
            replace.assert_not_called()
        self.assertEqual(len(self.records()), 1)

    def test_concurrent_correction_remains_pending_until_new_projection(self):
        original_replace = answer_profile._replace_profile
        def correction_then_replace(path, original, identity, data):
            # This second writer commits while the first projection owns the
            # profile lock. Its best-effort projection must wait for a retry.
            self.store.answer_questions(self.payload(value="yes-1", request_id="concurrent-correction"))
            return original_replace(path, original, identity, data)
        with patch("answer_profile._replace_profile", side_effect=correction_then_replace):
            self.store.answer_questions(self.payload())
        self.assertEqual(read_profile_records(self.profile.read_text())[0][0]["answer_labels"], ["No"])
        self.assertIsNone(self.outbox()[1]["projected_at"])
        self.store.sync_profile_answers(strict=True)
        self.assertEqual(read_profile_records(self.profile.read_text())[0][0]["answer_labels"], ["Yes"])

    def test_malformed_block_symlink_and_size_limit_preserve_file(self):
        del self.store.application_profile
        self.store.answer_questions(self.payload())
        self.store.application_profile = self.profile
        for content in (self.manual + BEGIN_MARKER + "\n", "x" * (MAX_PROFILE_BYTES + 1)):
            self.profile.write_text(content)
            self.assertEqual(self.store.sync_profile_answers()["status"], "pending")
            self.assertEqual(self.profile.read_text(), content)
        target = self.profile.with_name("real-profile.md")
        target.write_text(self.manual)
        self.profile.unlink()
        self.profile.symlink_to(target)
        self.assertEqual(self.store.sync_profile_answers()["status"], "pending")
        self.assertTrue(self.profile.is_symlink())
        self.assertEqual(target.read_text(), self.manual)

    def test_backfill_requires_user_event_and_excludes_automatic_answers(self):
        self.store.answer_questions({**self.payload(), "source": "evidence_resolver"})
        self.store.answer_questions(self.payload(value="yes-1", request_id="correction"))
        self.store.answer_questions_from_evidence(self.payload("q2", "Fictional Name", "automatic"),
            self.provenance("q2"), validate_sources=lambda: None)
        with self.store.transaction(True) as db:
            db.execute("DELETE FROM wf_profile_outbox")
            db.execute("DELETE FROM wf_profile_answers")
        self.assertEqual(self.store.backfill_profile_answers(), {"backfilled": 2})
        self.assertEqual(self.store.backfill_profile_answers(), {"backfilled": 0})
        self.assertEqual([record["answer_labels"] for record in self.records()], [["No"], ["Yes"]])
        with self.store.transaction(True) as db:
            db.execute("DELETE FROM wf_profile_outbox")
            db.execute("DELETE FROM wf_profile_answers")
            db.execute("DELETE FROM wf_process_events WHERE type='answers_saved'")
        self.assertEqual(self.store.backfill_profile_answers(), {"backfilled": 0})

    def evidence_then_lease(self):
        self.store.answer_questions_from_evidence(self.payload(), self.provenance(), validate_sources=lambda: None)
        self.store.answer_questions(self.payload("q2", "Fictional Name", "human-name"))
        task = self.store.lease_task("fresh-worker", ["apply"])
        self.store.start_application(self.pid, task["lease_token"])
        return task

    def refresh(self, task, *, expected=None, provenance=None, validate=lambda: None, revision=None):
        return self.store.refresh_answer_evidence(self.pid, task["lease_token"],
            self.store.processes()[0]["revision"] if revision is None else revision,
            self.provenance(source_hash="c" * 64) if provenance is None else provenance,
            {"q1": {"question_revision": 2, "value": "no-2"}} if expected is None else expected,
            validate_sources=validate)

    def test_refresh_changes_only_proof_with_audit_and_no_user_promotion(self):
        task = self.evidence_then_lease()
        questions_before, records_before = self.store.questions(), self.records()
        self.assertEqual(self.refresh(task)["refreshed"], 1)
        self.assertEqual(self.store.questions(), questions_before)
        self.assertEqual(self.records(), records_before)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT source_snapshot_hash FROM wf_answer_evidence").fetchone()[0], "c" * 64)
            audit = json.loads(db.execute("SELECT data FROM wf_process_events WHERE type='answer_evidence_refreshed'").fetchone()[0])
        self.assertEqual(audit["answers"][0]["previous_source_snapshot_hash"], "a" * 64)

    def test_refresh_rejects_stale_identity_changed_value_user_answer_and_bad_binding(self):
        task = self.evidence_then_lease()
        variants = [
            {"revision": 1},
            {"expected": {"q1": {"question_revision": 1, "value": "no-2"}}},
            {"expected": {"q1": {"question_revision": 2, "value": "yes-1"}}},
            {"provenance": self.provenance("q2"), "expected": {"q2": {"question_revision": 2, "value": "Fictional Name"}}},
        ]
        wrong_binding = self.provenance()
        wrong_binding["q1"]["field_name"] = "different-field"
        variants.append({"provenance": wrong_binding})
        for variant in variants:
            with self.assertRaises(ReviewError):
                self.refresh(task, **variant)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT source_snapshot_hash FROM wf_answer_evidence").fetchone()[0], "a" * 64)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_process_events WHERE type='answer_evidence_refreshed'").fetchone()[0], 0)

    def test_refresh_source_revalidation_failure_rolls_back_every_proof(self):
        task = self.evidence_then_lease()
        def invalid():
            raise ReviewError("Source changed.", 409)
        with self.assertRaises(ReviewError):
            self.refresh(task, validate=invalid)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT source_snapshot_hash FROM wf_answer_evidence").fetchone()[0], "a" * 64)

    def test_pending_profile_projection_blocks_automatic_answer_before_source_callback(self):
        del self.store.application_profile
        self.store.answer_questions(self.payload("q3", ["remote-7"], "user-arrangements"))
        self.store.application_profile = self.profile
        calls = []
        payload = self.payload()
        with self.assertRaises(EvidenceChanged):
            self.store.answer_questions_from_evidence(payload, self.provenance(),
                validate_sources=lambda: calls.append(True))
        self.assertEqual(calls, [])
        self.assertIsNone(next(q for q in self.store.questions() if q["id"] == "q1")["answer"])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_answer_evidence").fetchone()[0], 0)
        self.store.sync_profile_answers(strict=True)
        self.store.answer_questions_from_evidence(payload, self.provenance(), validate_sources=lambda: calls.append(True))
        self.assertEqual(calls, [True])

    def test_pending_profile_projection_blocks_refresh_and_submission(self):
        task = self.evidence_then_lease()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_profile_outbox SET projected_at=NULL")
        calls = []
        with self.assertRaises(EvidenceChanged):
            self.refresh(task, validate=lambda: calls.append(True))
        with self.assertRaises(EvidenceChanged):
            self.store.before_submit(self.pid, task["lease_token"], "form-v1",
                                     validate_sources=lambda: calls.append(True))
        self.assertEqual(calls, [])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT source_snapshot_hash FROM wf_answer_evidence").fetchone()[0], "a" * 64)
        self.assertEqual(self.store.processes()[0]["submission_status"], "preparing")

    def test_changed_source_callback_rolls_back_submission_checkpoint(self):
        task = self.evidence_then_lease()
        evidence = EvidenceSnapshot(self.profile, [])
        self.profile.write_text(self.profile.read_text() + "\nNew confirmed local fact.\n")
        before = self.store.processes()
        with self.assertRaises(EvidenceChanged):
            self.store.before_submit(self.pid, task["lease_token"], "form-v1",
                                     validate_sources=evidence.assert_current)
        self.assertEqual(self.store.processes(), before)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0], 0)
            checkpoint = json.loads(db.execute("SELECT checkpoint FROM wf_tasks WHERE id=?", (task["id"],)).fetchone()[0])
        self.assertFalse(checkpoint.get("submission_started", False))
        fresh = EvidenceSnapshot(self.profile, [])
        result = self.store.before_submit(self.pid, task["lease_token"], "form-v1", validate_sources=fresh.assert_current)
        self.assertEqual(result["submission_status"], "submission_unknown")


if __name__ == "__main__":
    unittest.main()
