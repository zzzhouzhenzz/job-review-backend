"""Evidence answer persistence uses fictional sources and temporary databases."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from store import ReviewError, digest, encode
from test_applications import ApplicationStore
from test_store import catalog


class EvidenceAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "review.sqlite3"
        self.store = ApplicationStore(self.path)
        self.store.import_catalog(catalog(), "fictional")
        with self.store.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='approve',revision=revision+1 WHERE key='a:1'")
            self.pid = self.store._create_application(db, self.store._row(db, "jobs", "key", "a:1"), "approved")
        self.task = self.store.lease_task("fictional-worker", ["apply"])
        self.store.start_application(self.pid, self.task["lease_token"])
        self.store.checkpoint_task(self.task["id"], self.task["lease_token"], {
            "question_bindings": {"q1": "contact_name", "q2": "project_summary"}})
        self.store.ask_questions(self.pid, self.task["lease_token"], "form-v1", [
            {"id": "q1", "text": "Contact name?", "type": "text", "required": True},
            {"id": "q2", "text": "Project summary?", "type": "text", "required": True},
        ])

    def process(self):
        return self.store.processes()[0]

    def payload(self, qids=("q1",), request_id="evidence-1"):
        questions = {q["id"]: q for q in self.store.questions()}
        return {"request_id": request_id, "process_id": self.pid,
                "process_revision": self.process()["revision"], "form_version": "form-v1",
                "answers": [{"question_id": qid, "question_revision": questions[qid]["revision"],
                             "value": "Fictional Applicant" if qid == "q1" else "Built a fictional service."}
                            for qid in qids]}

    @staticmethod
    def provenance(qids=("q1",)):
        return {qid: {
            "field_name": "contact_name" if qid == "q1" else "project_summary", "method": "direct",
            "source_snapshot_hash": "a" * 64,
            "citations": [{"source_id": "profile", "sha256": "b" * 64,
                           "start_line": 1, "end_line": 2,
                           "quote": " Name: Fictional Applicant\nBuilt a fictional service. "}],
            "explanation": "The fictional source states this answer.",
        } for qid in qids}

    def save(self, payload=None, provenance=None, validate_sources=lambda: None):
        payload = self.payload() if payload is None else payload
        provenance = self.provenance() if provenance is None else provenance
        return self.store.answer_questions_from_evidence(payload, provenance, validate_sources=validate_sources)

    def snapshot(self):
        tables = ("wf_questions", "wf_processes", "wf_tasks", "queue", "wf_answer_requests",
                  "wf_answer_evidence", "wf_process_events", "wf_resource_locks")
        with self.store.transaction() as db:
            return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in tables}

    def conflict(self, call):
        before = self.snapshot()
        with self.assertRaises(ReviewError) as caught:
            call()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(before, self.snapshot())
        return caught.exception

    def test_public_answers_keep_user_authority_and_replacement_behavior(self):
        payload = {**self.payload(), "actor": "evidence_resolver", "provenance": self.provenance()}
        self.store.answer_questions(payload)
        replacement = self.payload(request_id="user-replacement")
        replacement["answers"][0]["value"] = "User correction"
        self.store.answer_questions(replacement)
        self.assertEqual(self.store.questions()[0]["answer"], "User correction")
        self.assertEqual(self.process()["task_status"], "waiting_for_user")
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_answer_evidence").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT payload_hash FROM wf_answer_requests WHERE id=?",
                                        (payload["request_id"],)).fetchone()[0], digest(payload))
        saved = [e for e in self.process()["events"] if e["type"] == "answers_saved"]
        self.assertEqual([e["actor"] for e in saved], ["user", "user"])

    def test_partial_then_complete_preserves_provenance_and_resumes_same_task(self):
        first = self.save()
        self.assertEqual(first["task_ids"], [self.task["id"]])
        self.assertEqual(self.process()["task_status"], "waiting_for_user")
        self.assertEqual(self.process()["block_reason"], "questions_pending")
        self.assertEqual(self.store.state()["queue"][0]["status"], "needs_input")
        second = self.save(self.payload(("q2",), "evidence-2"), self.provenance(("q2",)))
        self.assertEqual(second["task_ids"], [self.task["id"]])
        self.assertEqual(len(self.store.tasks()), 1)
        self.assertEqual(self.process()["task_status"], "queued")
        self.assertIsNone(self.process()["block_reason"])
        self.assertEqual(self.store.state()["queue"][0]["status"], "queued")
        with self.store.transaction() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM wf_answer_evidence ORDER BY question_id")]
        self.assertEqual(len(rows), 2)
        for row in rows:
            provenance = self.provenance((row["question_id"],))[row["question_id"]]
            self.assertEqual(row["answer_revision"], 2)
            for field in ("field_name", "method", "source_snapshot_hash", "explanation"):
                self.assertEqual(row[field], provenance[field])
            self.assertEqual(json.loads(row["citations"]), provenance["citations"])
        self.assertEqual(json.loads(rows[0]["resolved_value"]), "Fictional Applicant")
        events = [e for e in self.process()["events"] if e["type"] == "answers_resolved_from_evidence"]
        self.assertEqual([e["actor"] for e in events], ["evidence_resolver"] * 2)
        self.assertEqual([e["data"]["resumed"] for e in events], [False, True])
        self.assertEqual(ApplicationStore(self.path).processes(), self.store.processes())

    def test_exact_replay_after_completion_does_not_recheck_sources_or_mutate(self):
        payload, provenance = self.payload(("q1", "q2")), self.provenance(("q1", "q2"))
        receipt = self.save(payload, provenance)
        before = self.snapshot()

        def should_not_validate():
            self.fail("An accepted replay is not a new source resolution.")

        self.assertEqual(self.save(payload, provenance, should_not_validate), receipt)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(ApplicationStore(self.path).answer_questions_from_evidence(
            payload, provenance, validate_sources=should_not_validate), receipt)

    def test_request_id_includes_provenance_and_cannot_cross_authority(self):
        payload, provenance = self.payload(), self.provenance()
        self.save(payload, provenance)
        changed = copy.deepcopy(provenance)
        changed["q1"]["explanation"] = "A different explanation."
        self.assertEqual(self.conflict(lambda: self.save(payload, changed)).reason, "request_id_conflict")
        self.assertEqual(self.conflict(lambda: self.store.answer_questions(payload)).reason, "request_id_conflict")
        # Even an exact match to the evidence fingerprint cannot grant a public replay.
        forged = {"source": "evidence_resolver", "payload": payload, "provenance": provenance,
                  "request_id": payload["request_id"]}
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_answer_requests SET payload_hash=? WHERE id=?", (digest(forged), payload["request_id"]))
        self.conflict(lambda: self.store.answer_questions(forged))

    def test_evidence_cannot_replay_a_public_receipt(self):
        payload = self.payload()
        self.store.answer_questions(payload)
        self.assertEqual(self.conflict(lambda: self.save(payload)).reason, "request_id_conflict")

    def test_evidence_cannot_overwrite_user_answers(self):
        self.store.answer_questions(self.payload(request_id="user-1"))
        self.conflict(lambda: self.save(self.payload(request_id="evidence-after-user")))
        self.assertEqual(self.store.questions()[0]["answer"], "Fictional Applicant")

    def test_evidence_cannot_overwrite_evidence_or_empty_answers(self):
        self.save()
        self.conflict(lambda: self.save(self.payload(request_id="evidence-replacement")))
        # An optional empty answer is still an explicit saved value.
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_questions SET required=0 WHERE id='q1'")
        payload = self.payload(request_id="user-empty")
        payload["answers"][0]["value"] = ""
        self.store.answer_questions(payload)
        self.conflict(lambda: self.save(self.payload(request_id="no-overwrite-empty")))

    def test_stale_process_form_and_question_leave_no_evidence(self):
        base = self.payload()
        for field, value in (("process_revision", 1), ("form_version", "old-form")):
            with self.subTest(field=field):
                self.conflict(lambda: self.save({**base, field: value}))
        changed = copy.deepcopy(base)
        changed["answers"][0]["question_revision"] = 0
        self.conflict(lambda: self.save(changed))
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_questions SET superseded=1 WHERE id='q1'")
        self.conflict(lambda: self.save(base))

    def test_original_question_field_binding_is_required(self):
        changed = self.provenance()
        changed["q1"]["field_name"] = "another_field"
        self.conflict(lambda: self.save(provenance=changed))
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET checkpoint='{}' WHERE id=?", (self.task["id"],))
        self.conflict(self.save)

    def test_source_callback_is_required_and_runs_before_writes_under_lock(self):
        before = self.snapshot()
        with self.assertRaises(ReviewError):
            self.store.answer_questions_from_evidence(self.payload(), self.provenance())
        calls = []

        def validate():
            calls.append(True)
            self.assertEqual(self.snapshot(), before)
            connection = sqlite3.connect(self.path, timeout=0)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("BEGIN IMMEDIATE")
            finally:
                connection.close()

        self.save(validate_sources=validate)
        self.assertEqual(calls, [True])

    def test_source_drift_and_late_event_failure_roll_back_everything(self):
        def drift():
            raise ReviewError("Fictional source changed.", 409)

        self.conflict(lambda: self.save(validate_sources=drift))
        original = self.store._process_event

        def failed_event(*args, **kwargs):
            raise ReviewError("Fictional event write failed.", 409)

        self.store._process_event = failed_event
        try:
            self.conflict(lambda: self.save(self.payload(("q1", "q2")), self.provenance(("q1", "q2"))))
        finally:
            self.store._process_event = original

    def test_quarantine_and_unknown_submission_reject_even_partial_answers(self):
        with self.store.transaction(True) as db:
            db.execute("INSERT INTO wf_resource_locks VALUES ('browser',?,?,1)", (self.task["id"], "old-lease"))
        self.conflict(self.save)
        with self.store.transaction(True) as db:
            db.execute("DELETE FROM wf_resource_locks")
            other = self.store._enqueue_task(db, "explore", {}, "fictional-other", resource="browser")
            db.execute("INSERT INTO wf_resource_locks VALUES ('browser',?,?,1)", (other, "other-lease"))
        self.conflict(self.save)
        with self.store.transaction(True) as db:
            db.execute("DELETE FROM wf_resource_locks")
            checkpoint = {"question_bindings": {"q1": "contact_name", "q2": "project_summary"},
                          "submission_started": True}
            db.execute("UPDATE wf_tasks SET checkpoint=? WHERE id=?", (encode(checkpoint), self.task["id"]))
        self.conflict(self.save)
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown' WHERE id=?", (self.pid,))
        self.conflict(self.save)

    def test_current_apply_authorization_is_required(self):
        with self.store.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='later' WHERE key='a:1'")
        self.conflict(self.save)

    def test_provenance_must_cover_exact_answers_with_valid_structure(self):
        before = self.snapshot()
        for provenance in ({}, self.provenance(("q1", "q2")),
                           {"q1": {**self.provenance()["q1"], "method": "unresolved"}},
                           {"q1": {**self.provenance()["q1"], "citations": []}}):
            with self.subTest(provenance=provenance):
                with self.assertRaises(ReviewError):
                    self.save(provenance=provenance)
                self.assertEqual(self.snapshot(), before)

    def test_new_round_preserves_answer_revision_and_evidence_and_replaces_only_unanswered(self):
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_questions SET required=0 WHERE id='q2'")
        self.save()
        with self.store.transaction() as db:
            evidence_before = [tuple(row) for row in db.execute("SELECT * FROM wf_answer_evidence")]
        resumed = self.store.lease_task("next-round-worker", ["apply"])
        self.store.start_application(self.pid, resumed["lease_token"])
        self.store.ask_questions(self.pid, resumed["lease_token"], "form-v2", [
            {"id": "q3", "text": "New required question?", "type": "text", "required": True}],
            preserve_answer_ids=["q1"])
        active = {q["id"]: q for q in self.store.questions()}
        self.assertEqual(set(active), {"q1", "q3"})
        self.assertEqual(active["q1"]["answer"], "Fictional Applicant")
        self.assertEqual(active["q1"]["revision"], 2)
        self.assertEqual(active["q1"]["form_version"], "form-v2")
        self.assertIsNone(active["q3"]["answer"])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT superseded FROM wf_questions WHERE id='q2'").fetchone()[0], 1)
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM wf_answer_evidence")], evidence_before)
        self.assertEqual(self.process()["task_status"], "waiting_for_user")
        self.store.answer_questions({"request_id": "new-round-user-answer", "process_id": self.pid,
            "process_revision": self.process()["revision"], "form_version": "form-v2", "answers": [
                {"question_id": "q3", "question_revision": 1, "value": "New user answer"}]})
        self.assertEqual(self.process()["task_status"], "queued")
        self.assertEqual(self.process()["task_id"], self.task["id"])

    def test_new_round_requires_exact_complete_preservation_and_fresh_question_ids(self):
        self.save(self.payload(("q1", "q2")), self.provenance(("q1", "q2")))
        resumed = self.store.lease_task("next-round-worker", ["apply"])
        self.store.start_application(self.pid, resumed["lease_token"])
        question = {"id": "q3", "text": "New question?", "type": "text", "required": True}
        for preserved in ((), ["q1"], ["q1", "q2", "missing"]):
            with self.subTest(preserved=preserved):
                self.conflict(lambda: self.store.ask_questions(
                    self.pid, resumed["lease_token"], "form-v2", [question], preserve_answer_ids=preserved))
        self.conflict(lambda: self.store.ask_questions(
            self.pid, resumed["lease_token"], "form-v2", [{**question, "id": "q1"}],
            preserve_answer_ids=["q1", "q2"]))
        self.store.ask_questions(self.pid, resumed["lease_token"], "form-v2", [question],
                                 preserve_answer_ids=["q1", "q2"])
        self.assertEqual([q["id"] for q in self.store.questions()], ["q1", "q2", "q3"])
        self.assertEqual([q["revision"] for q in self.store.questions()], [2, 2, 1])


if __name__ == "__main__":
    unittest.main()
