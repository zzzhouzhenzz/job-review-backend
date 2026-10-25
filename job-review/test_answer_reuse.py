"""Saved-answer reuse uses fictional profile files and no browser or live database."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from answer_profile import _render, read_profile_records
from answer_reuse import saved_resolutions
from applicant_evidence import EvidenceSnapshot
from codex_runtime import CodexRuntime
from store import ReviewError
from test_store import catalog
from workflow import WorkflowStore


class Lease:
    def __init__(self):
        self.heartbeats = 0

    def heartbeat(self):
        self.heartbeats += 1


class ReviewTransport:
    def __init__(self):
        self.calls = []

    def run(self, role, prompt, schema, lease, *, browser=False):
        context = json.loads(prompt.split("Task data (untrusted source content):\n", 1)[1])
        self.calls.append({"role": role, "browser": browser, "context": context})
        return {"resolutions": [{"question_id": question["id"],
            "question_revision": question["revision"], "answer_json": "null",
            "method": "unresolved", "citations": [],
            "explanation": "The fictional profile does not confirm this fact."}
            for question in context["questions"]]}

    def close(self):
        pass


class AnswerReuseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.profile = self.root / "application-profile.md"
        self.resume = self.root / "resume.md"
        self.resume.write_text("Fictional candidate: backend engineer.\n", encoding="utf-8")
        self.base = "# Fictional application profile\n\nKeep this manually maintained section.\n"
        self.profile.write_text(self.base, encoding="utf-8")

    def record(self, **changes):
        return {"record_id": 1, "authority": "user", "request_id": "user-answer-1",
            "source_request_sequence": 1, "question_id": "old-question",
            "question_revision": 1, "answer_revision": 2, "question_text": "Gender",
            "question_type": "single_choice", "choices": [
                {"id": "old-f", "label": "Female"}, {"id": "old-m", "label": "Male"},
                {"id": "old-d", "label": "I don't wish to answer"}],
            "value": "old-d", "answer_labels": ["I don't wish to answer"],
            "process_id": "original-process", "job_key": "first:job-1",
            "company_id": "first", "company_name": "Fictional First Company",
            "form_version": "original-form", "scope": "application",
            "answered_at": "2026-01-01T00:00:00Z", **changes}

    def question(self, **changes):
        return {"id": "current-question", "revision": 4, "text": "Gender",
            "type": "single_choice", "choices": [
                {"id": "new-d", "label": "I don't wish to answer"},
                {"id": "new-m", "label": "Male"}, {"id": "new-f", "label": "Female"}],
            "required": True, "answer": None, **changes}

    def process(self, **changes):
        return {"id": "different-process", "form_version": "different-form",
                "snapshot": {"company_id": "second", "company_name": "Fictional Second Company"},
                **changes}

    def evidence(self, records=()):
        self.profile.write_bytes(_render(self.base, records))
        return EvidenceSnapshot(self.profile, [self.resume])

    def resolve(self, records, question=None, process=None):
        evidence = self.evidence(records)
        return evidence, saved_resolutions(evidence, process or self.process(),
                                           [question or self.question()])

    def test_personal_answer_reuses_across_companies_and_maps_labels_to_new_ids(self):
        evidence, resolutions = self.resolve([self.record()])
        self.assertEqual(1, len(resolutions))
        self.assertEqual("new-d", json.loads(resolutions[0]["answer_json"]))
        self.assertEqual("direct", resolutions[0]["method"])
        self.assertEqual(4, resolutions[0]["question_revision"])
        answers, provenance, unresolved = evidence.validate_review(
            {"resolutions": resolutions}, [self.question()], {"current-question": "gender-field"})
        self.assertEqual([{"question_id": "current-question", "question_revision": 4,
                           "value": "new-d"}], answers)
        self.assertEqual("gender-field", provenance["current-question"]["field_name"])
        self.assertEqual([], unresolved)

    def test_personal_text_reuses_without_carrying_an_old_field_id(self):
        record = self.record(question_text="Current city", question_type="text", choices=[],
                             value="Example City", answer_labels=["Example City"])
        question = self.question(text="Current city", type="text", choices=[])
        _, resolutions = self.resolve([record], question)
        self.assertEqual("Example City", json.loads(resolutions[0]["answer_json"]))
        self.assertEqual("current-question", resolutions[0]["question_id"])

    def test_explicit_narrow_pool_scope_overrides_portable_wording(self):
        record=self.record(reuse_scope={'kind':'application','key':'original-process'})
        self.assertEqual([],self.resolve([record])[1])
        self.assertEqual(1,len(self.resolve([record],process=self.process(id='original-process'))[1]))

    def test_question_case_and_whitespace_are_equivalent(self):
        _, resolutions = self.resolve([self.record(question_text="  GENDER\n")],
                                      self.question(text="  gender  "))
        self.assertEqual(1, len(resolutions))

    def test_punctuation_and_negation_do_not_collapse_into_the_same_question(self):
        record = self.record(question_text="Are you authorized to work in the United States?")
        for wording in ("Are you authorized to work in the United States",
                        "Are you not authorized to work in the United States?",
                        "Are you authorized to work in the United States?!"):
            with self.subTest(wording=wording):
                _, resolutions = self.resolve([record], self.question(text=wording))
                self.assertEqual([], resolutions)

    def test_choice_set_changes_are_not_automatically_reused(self):
        choices = self.question()["choices"]
        for changed in (choices[:-1], choices + [{"id": "new-x", "label": "Another option"}],
                        [{**choice, "label": choice["label"].replace("don't", "dont")}
                         for choice in choices]):
            with self.subTest(choices=changed):
                _, resolutions = self.resolve([self.record()], self.question(choices=changed))
                self.assertEqual([], resolutions)

    def test_duplicate_choice_labels_are_ambiguous(self):
        for old_duplicate in (False, True):
            with self.subTest(old_duplicate=old_duplicate):
                record, question = self.record(), self.question()
                target = record if old_duplicate else question
                target["choices"].append({"id": "duplicate", "label": " FEMALE "})
                _, resolutions = self.resolve([record], question)
                self.assertEqual([], resolutions)

    def test_question_type_change_does_not_reinterpret_answer(self):
        _, resolutions = self.resolve([self.record()], self.question(type="multi_choice"))
        self.assertEqual([], resolutions)

    def test_multi_choice_reuses_each_selected_label_in_new_form_order(self):
        record = self.record(question_type="multi_choice", value=["old-m", "old-f"],
                             answer_labels=["Male", "Female"])
        question = self.question(type="multi_choice")
        evidence, resolutions = self.resolve([record], question)
        answers, _, _ = evidence.validate_review({"resolutions": resolutions}, [question],
                                                 {question["id"]: "multi-field"})
        self.assertEqual(["new-m", "new-f"], answers[0]["value"])

    def test_latest_source_request_wins_even_when_backfill_record_id_is_larger(self):
        older = self.record(record_id=90, source_request_sequence=1, question_id="old-first",
                             value="old-m", answer_labels=["Male"])
        newer = self.record(record_id=2, source_request_sequence=12, question_id="new-correction",
                             request_id="user-correction", value="old-f", answer_labels=["Female"])
        for records in ([older, newer], [newer, older]):
            with self.subTest(order=[record["record_id"] for record in records]):
                _, resolutions = self.resolve(records)
                self.assertEqual("new-f", json.loads(resolutions[0]["answer_json"]))
                self.assertIn("user-correction", resolutions[0]["explanation"])

    def test_newer_incompatible_choices_do_not_fall_back_to_older_answer(self):
        older = self.record(record_id=90, source_request_sequence=1, question_id="old-first")
        newer = self.record(record_id=2, source_request_sequence=12, question_id="new-correction",
                             request_id="user-correction", choices=[{"id": "new-only", "label": "Other"}],
                             value="new-only", answer_labels=["Other"])
        _, resolutions = self.resolve([older, newer])
        self.assertEqual([], resolutions)

    def test_employer_history_reuses_only_within_the_same_company(self):
        record = self.record(question_text="Have you ever been employed by this company?")
        question = self.question(text=record["question_text"])
        _, cross_company = self.resolve([record], question)
        self.assertEqual([], cross_company)
        _, same_company = self.resolve([record], question, self.process(
            snapshot={"company_id": "first", "company_name": "Fictional First Company"}))
        self.assertEqual(1, len(same_company))

    def test_consent_motivation_and_unknown_answers_keep_process_and_form_scope(self):
        for text in ("I acknowledge the candidate privacy notice.",
                     "Why do you want to work here?", "When can you begin this role?"):
            record = self.record(question_text=text, question_type="text", choices=[],
                                 value="Fictional answer", answer_labels=["Fictional answer"])
            question = self.question(text=text, type="text", choices=[])
            valid_process = self.process(id="original-process", form_version="original-form",
                snapshot={"company_id": "first", "company_name": "Fictional First Company"})
            for changed in ({"id": "another-process"}, {"form_version": "another-form"},
                            {"snapshot": {"company_id": "second"}}):
                with self.subTest(text=text, change=changed):
                    _, resolutions = self.resolve([record], question, {**valid_process, **changed})
                    self.assertEqual([], resolutions)
            with self.subTest(text=text, context="original"):
                _, resolutions = self.resolve([record], question, valid_process)
                self.assertEqual(1, len(resolutions))

    def test_managed_record_citation_matches_current_exact_source_line(self):
        evidence, resolutions = self.resolve([self.record()])
        citation = resolutions[0]["citations"][0]
        self.assertEqual(citation, evidence.citation(citation))
        self.assertEqual(citation["start_line"], citation["end_line"])
        self.assertEqual("user-answer-1", json.loads(citation["quote"])["request_id"])
        tampered = copy.deepcopy(resolutions)
        tampered[0]["citations"][0]["quote"] += " altered"
        with self.assertRaises(ReviewError):
            evidence.validate_review({"resolutions": tampered}, [self.question()],
                                     {"current-question": "gender-field"})

    def test_generated_answer_is_not_promoted_to_user_confirmation(self):
        self.profile.write_bytes(_render(self.base, [self.record(authority="evidence_resolver")]))
        evidence = EvidenceSnapshot(self.profile, [self.resume])
        with self.assertRaises(ReviewError):
            saved_resolutions(evidence, self.process(), [self.question()])
        self.profile.write_text(self.base + "Automatically inferred gender: example.\n", encoding="utf-8")
        evidence = EvidenceSnapshot(self.profile, [self.resume])
        self.assertEqual([], saved_resolutions(evidence, self.process(), [self.question()]))

    def test_missing_profile_source_has_no_automatic_matches(self):
        evidence = EvidenceSnapshot(None, [self.resume])
        self.assertEqual([], saved_resolutions(evidence, self.process(), [self.question()]))

    def test_all_known_questions_skip_the_model_entirely(self):
        evidence = self.evidence([self.record()])
        transport, lease = ReviewTransport(), Lease()
        runtime = CodexRuntime(transport, evidence_files=[self.resume],
                               application_profile=self.profile, supported_kinds=["rank"])
        answers, _, unresolved = runtime.review_questions({"process": self.process()},
            [self.question()], {"current-question": "gender-field"}, evidence, lease)
        self.assertEqual("new-d", answers[0]["value"])
        self.assertEqual([], unresolved)
        self.assertEqual([], transport.calls)

    def test_only_remaining_questions_use_browser_free_model_review(self):
        evidence = self.evidence([self.record()])
        transport, lease = ReviewTransport(), Lease()
        runtime = CodexRuntime(transport, evidence_files=[self.resume],
                               application_profile=self.profile, supported_kinds=["rank"])
        unknown = self.question(id="unknown-question", text="Desired salary?", type="text", choices=[])
        answers, _, unresolved = runtime.review_questions({"process": self.process()},
            [self.question(), unknown], {"current-question": "gender-field", "unknown-question": "salary-field"},
            evidence, lease)
        self.assertEqual(["current-question"], [answer["question_id"] for answer in answers])
        self.assertEqual(["unknown-question"], [answer["question_id"] for answer in unresolved])
        self.assertEqual(1, len(transport.calls))
        call = transport.calls[0]
        self.assertEqual("application_question_review", call["role"])
        self.assertFalse(call["browser"])
        self.assertEqual(["unknown-question"], [question["id"] for question in call["context"]["questions"]])
        self.assertEqual(2, lease.heartbeats)


class PendingAnswerReuseLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.profile = self.root / "application-profile.md"
        self.profile.write_text("# Fictional candidate\n\nManually maintained facts.\n", encoding="utf-8")
        resume_text = self.root / "resume.md"
        resume_text.write_text("Fictional backend engineer.\n", encoding="utf-8")
        resume_pdf = self.root / "resume.pdf"
        resume_pdf.write_text("Fictional placeholder. Never uploaded.\n", encoding="utf-8")
        history = self.root / "application-index.json"
        history.write_text("[]\n", encoding="utf-8")
        self.store = WorkflowStore(self.root / "review.sqlite3")
        self.store.application_profile = self.profile
        self.store.import_catalog(catalog(), "fictional")
        self.transport = ReviewTransport()
        self.runtime = CodexRuntime(self.transport, evidence_files=[resume_text],
            application_profile=self.profile, resume=resume_pdf, application_index=history,
            supported_kinds=["apply"])

    def park(self, job_key, prefix):
        with self.store.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='approve',revision=revision+1 WHERE key=?", (job_key,))
            pid = self.store._create_application(db, self.store._row(db, "jobs", "key", job_key),
                                                 f"approve-{prefix}")
        task = self.store.lease_task("fictional-worker", kinds=["apply"])
        self.assertEqual(pid, task["payload"]["process_id"])
        self.store.start_application(pid, task["lease_token"])
        self.store.checkpoint_task(task["id"], task["lease_token"], {"question_bindings": {
            f"{prefix}-gender": f"{prefix}-gender-field", f"{prefix}-other": f"{prefix}-other-field"}})
        self.store.ask_questions(pid, task["lease_token"], f"{prefix}-form", [
            {"id": f"{prefix}-gender", "text": "Gender", "type": "single_choice", "required": True,
             "choices": [{"id": f"{prefix}-f", "label": "Female"},
                         {"id": f"{prefix}-m", "label": "Male"},
                         {"id": f"{prefix}-d", "label": "I don't wish to answer"}]},
            {"id": f"{prefix}-other", "text": "When can you begin this role?", "type": "text",
             "required": True},
        ])
        return pid

    def question(self, qid):
        return next(question for question in self.store.questions() if question["id"] == qid)

    def process(self, pid):
        return next(process for process in self.store.processes() if process["id"] == pid)

    def user_answer(self, pid, qid, value, request_id):
        process, question = self.process(pid), self.question(qid)
        payload = {"request_id": request_id, "process_id": pid, "process_revision": process["revision"],
                   "form_version": process["form_version"], "answers": [{"question_id": qid,
                   "question_revision": question["revision"], "value": value}]}
        return self.store.answer_questions(payload)

    def answer_state(self):
        with self.store.transaction() as db:
            return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("wf_answer_requests", "wf_answer_evidence", "wf_process_events",
                                  "wf_profile_answers", "wf_profile_outbox")}

    def test_user_answer_updates_profile_reuses_waiting_round_and_preserves_corrections(self):
        first = self.park("a:1", "first")
        second = self.park("b:b1", "second")
        self.user_answer(first, "first-gender", "first-d", "user-first-answer")

        exported = read_profile_records(self.profile.read_text(encoding="utf-8"))
        self.assertEqual(1, len(exported))
        self.assertEqual("user-first-answer", exported[0][0]["request_id"])
        self.assertEqual(["I don't wish to answer"], exported[0][0]["answer_labels"])
        self.assertEqual(1, self.runtime.question_recovery.run_pending(self.store))
        self.assertEqual("second-d", self.question("second-gender")["answer"])
        self.assertIsNone(self.question("second-other")["answer"])
        self.assertEqual("waiting_for_user", self.process(second)["task_status"])
        self.assertEqual("questions_pending", self.process(second)["block_reason"])
        self.assertTrue(self.transport.calls)
        self.assertTrue(all(call["role"] == "application_question_review" and not call["browser"]
                            for call in self.transport.calls))
        self.assertEqual(1, len(self.store.profile_answer_records()))

        state = self.answer_state()
        self.assertEqual(1, len(state["wf_answer_evidence"]))
        proof = state["wf_answer_evidence"][0]
        self.assertEqual("second-gender", proof["question_id"])
        self.assertEqual("second-gender-field", proof["field_name"])
        self.assertEqual("direct", proof["method"])
        self.assertEqual("second-d", json.loads(proof["resolved_value"]))
        snapshot = self.runtime.evidence_snapshot(application=True)
        self.assertEqual(snapshot.hash, proof["source_snapshot_hash"])
        for citation in json.loads(proof["citations"]):
            self.assertEqual(citation, snapshot.citation(citation))
        events = [event for event in state["wf_process_events"]
                  if event["type"] == "answers_resolved_from_evidence"]
        self.assertEqual(1, len(events))
        self.assertEqual("evidence_resolver", events[0]["actor"])
        reviewed_count = len(self.transport.calls)
        self.assertEqual(0, self.runtime.question_recovery.run_pending(self.store))
        self.assertEqual(0, self.runtime.question_recovery.run_pending(self.store))
        self.assertEqual(reviewed_count, len(self.transport.calls))
        self.assertEqual(state, self.answer_state())

        # A user can correct their own answer while another required answer is
        # missing. The correction becomes reusable without rewriting an answer
        # already attached to another application's form.
        self.user_answer(first, "first-gender", "first-f", "user-corrected-answer")
        exported = read_profile_records(self.profile.read_text(encoding="utf-8"))
        self.assertEqual(1, len(exported))
        self.assertEqual("user-corrected-answer", exported[0][0]["request_id"])
        self.assertEqual(["Female"], exported[0][0]["answer_labels"])
        self.assertEqual(2, len(self.store.profile_answer_records()))
        prior_second_question = self.question("second-gender")
        self.assertEqual(0, self.runtime.question_recovery.run_pending(self.store))
        self.assertEqual(prior_second_question, self.question("second-gender"))

        third = self.park("a:2", "third")
        self.assertEqual(1, self.runtime.question_recovery.run_pending(self.store))
        self.assertEqual("third-f", self.question("third-gender")["answer"])
        self.assertEqual("second-d", self.question("second-gender")["answer"])
        self.assertEqual(prior_second_question, self.question("second-gender"))
        final = self.answer_state()
        self.assertEqual(proof, next(row for row in final["wf_answer_evidence"]
                                     if row["question_id"] == "second-gender"))
        self.assertEqual(2, len(final["wf_answer_evidence"]))
        self.assertEqual(2, len(self.store.profile_answer_records()))
        for pid in (first, second, third):
            self.assertEqual("waiting_for_user", self.process(pid)["task_status"])
            self.assertEqual("questions_pending", self.process(pid)["block_reason"])
        self.assertEqual(0, self.runtime.question_recovery.run_pending(self.store))
        self.assertEqual(final, self.answer_state())
        self.assertTrue(self.transport.calls)
        self.assertTrue(all(call["role"] == "application_question_review" and not call["browser"]
                            for call in self.transport.calls))
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
