"""Applicant evidence checks use fictional files in temporary directories only."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from applicant_evidence import EvidenceSnapshot, MAX_SOURCE_BYTES
from store import ReviewError


class ApplicantEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.profile = self.root / "profile.md"
        self.profile.write_text("# Fictional candidate\nLocation: Example City\nStart date: Next month\n", encoding="utf-8")
        self.resume = self.root / "resume.md"
        self.resume.write_text("# Fictional resume\nBackend engineer at Example Systems.\n", encoding="utf-8")
        self.history = self.root / "applications.json"
        self.history.write_text('[{"company":"Example Employer","status":"submitted"}]\n', encoding="utf-8")
        self.snapshot = self.load()

    def load(self):
        return EvidenceSnapshot(self.profile, [self.resume], self.history)

    def question(self, qid="q1", **changes):
        return {"id": qid, "revision": 3, "type": "text", "text": "When can you start?",
                "choices": [], "required": True, "answer": None, **changes}

    def citation(self, source_id="profile", start=3, end=3):
        source = self.snapshot.sources[source_id]
        return {"source_id": source_id, "sha256": source["sha256"], "start_line": start,
                "end_line": end, "quote": "\n".join(source["content"].splitlines()[start - 1:end])}

    def resolution(self, qid="q1", value="Next month", **changes):
        return {"question_id": qid, "question_revision": 3, "answer_json": json.dumps(value),
                "method": "direct", "citations": [self.citation()],
                "explanation": "The confirmed fictional profile supplies the start date.", **changes}

    def review(self, resolutions, questions=None, bindings=None):
        return self.snapshot.validate_review(
            {"resolutions": resolutions}, questions if questions is not None else [self.question()],
            bindings if bindings is not None else {"q1": "start_date"})

    def test_new_snapshot_reads_current_files_and_old_snapshot_detects_change(self):
        self.profile.write_text("# Fictional candidate\nLocation: New Example City\n", encoding="utf-8")
        with self.assertRaises(ReviewError) as caught:
            self.snapshot.assert_current()
        self.assertEqual(caught.exception.status, 409)
        fresh = self.load()
        self.assertNotEqual(fresh.hash, self.snapshot.hash)
        self.assertIn("New Example City", fresh.context()["sources"][0]["numbered_text"])
        fresh.assert_current()

    def test_sources_must_be_present_complete_and_utf8_text(self):
        invalid = self.root / "invalid.md"
        for label, raw in (("empty", b""), ("whitespace", b" \n\t"), ("binary", b"name\x00value"),
                           ("invalid utf8", b"\xff"), ("oversize", b"a" * (MAX_SOURCE_BYTES + 1))):
            with self.subTest(label=label):
                invalid.write_bytes(raw)
                with self.assertRaises(ReviewError):
                    EvidenceSnapshot(invalid, [])
        with self.assertRaises(ReviewError):
            EvidenceSnapshot(self.root / "missing.md", [])
        with self.assertRaises(ReviewError):
            EvidenceSnapshot(None, [])

    def test_total_source_limit_is_enforced_across_individually_valid_files(self):
        limit = len(self.profile.read_bytes()) + len(self.resume.read_bytes()) - 1
        with patch("applicant_evidence.MAX_TOTAL_BYTES", limit), self.assertRaises(ReviewError):
            EvidenceSnapshot(self.profile, [self.resume])

    def test_context_numbers_lines_and_identifies_exact_source_bytes(self):
        context = self.snapshot.context()
        self.assertEqual(context["snapshot_hash"], self.snapshot.hash)
        self.assertEqual([source["source_id"] for source in context["sources"]],
                         ["profile", "resume_text_1", "application_history"])
        source = context["sources"][0]
        self.assertEqual(source["numbered_text"],
                         "1: # Fictional candidate\n2: Location: Example City\n3: Start date: Next month")
        self.assertEqual(source["sha256"], hashlib.sha256(self.profile.read_bytes()).hexdigest())
        self.assertEqual(source["path"], str(self.profile.resolve()))
        self.assertEqual(source["kind"], "confirmed_profile")
        self.assertNotIn("content", source)

    def test_citation_requires_exact_hash_range_and_quote(self):
        valid = self.citation(start=2, end=3)
        self.assertEqual(self.snapshot.citation(valid), valid)
        for changes in ({"source_id": "unknown"}, {"sha256": "0" * 64}, {"start_line": 0},
                        {"start_line": True}, {"end_line": 1}, {"end_line": 4},
                        {"end_line": "3"}, {"quote": "Location: Another City\nStart date: Next month"}):
            with self.subTest(changes=changes), self.assertRaises(ReviewError):
                self.snapshot.citation({**valid, **changes})
        with self.assertRaises(ReviewError):
            self.snapshot.citation("profile:3")

    def test_citation_rejects_blank_quote_and_excessive_line_span(self):
        self.profile.write_text("\n" + "\n".join(f"Fictional line {i}" for i in range(101)), encoding="utf-8")
        self.snapshot = self.load()
        for start, end in ((1, 1), (2, 102)):
            with self.subTest(start=start, end=end), self.assertRaises(ReviewError):
                self.snapshot.citation(self.citation(start=start, end=end))

    def test_supported_answer_has_exact_binding_and_provenance(self):
        answers, provenance, unresolved = self.review([self.resolution()])
        self.assertEqual(answers, [{"question_id": "q1", "question_revision": 3, "value": "Next month"}])
        self.assertEqual(provenance["q1"]["field_name"], "start_date")
        self.assertEqual(provenance["q1"]["source_snapshot_hash"], self.snapshot.hash)
        self.assertEqual(provenance["q1"]["citations"], [self.citation()])
        self.assertEqual(provenance["q1"]["method"], "direct")
        self.assertEqual(unresolved, [])

    def test_review_covers_each_unanswered_question_once_and_preserves_inputs(self):
        questions = [self.question(), self.question("q2"), self.question("answered", answer="Saved by user")]
        original = copy.deepcopy(questions)
        resolutions = [self.resolution(), self.resolution("q2")]
        answers, _, _ = self.review(resolutions, questions, {"q1": "start_date", "q2": "availability"})
        self.assertEqual([answer["question_id"] for answer in answers], ["q1", "q2"])
        self.assertEqual(questions, original)
        invalid_rounds = [resolutions[:1], [resolutions[0], resolutions[0]],
                          [resolutions[0], self.resolution("unknown")],
                          [resolutions[0], self.resolution("q2", question_revision=2)],
                          [resolutions[0], self.resolution("answered")]]
        for invalid in invalid_rounds:
            with self.subTest(invalid=invalid), self.assertRaises(ReviewError):
                self.review(invalid, questions, {"q1": "start_date", "q2": "availability"})

    def test_resolved_answer_needs_original_field_binding_and_citations(self):
        for bindings in ({}, {"q1": ""}, {"q1": None}):
            with self.subTest(bindings=bindings), self.assertRaises(ReviewError):
                self.review([self.resolution()], bindings=bindings)
        for citations in (None, [], [self.citation()] * 21):
            with self.subTest(citations=citations), self.assertRaises(ReviewError):
                self.review([self.resolution(citations=citations)])

    def test_single_choice_uses_observed_id_not_label_or_coerced_type(self):
        question = self.question(type="single_choice", choices=[{"id": "choice_a", "label": "Next month"}])
        answers, _, _ = self.review([self.resolution(value="choice_a")], [question])
        self.assertEqual(answers[0]["value"], "choice_a")
        for value in ("Next month", "unknown", ["choice_a"], True, 1, None):
            with self.subTest(value=value), self.assertRaises(ReviewError):
                self.review([self.resolution(value=value)], [question])

    def test_multi_choice_rejects_unknown_duplicate_or_wrong_type_values(self):
        question = self.question(type="multi_choice", choices=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}])
        answers, _, _ = self.review([self.resolution(value=["b", "a"])], [question])
        self.assertEqual(answers[0]["value"], ["b", "a"])
        for value in ([], ["a", "a"], ["a", "missing"], [1], "a", None):
            with self.subTest(value=value), self.assertRaises(ReviewError):
                self.review([self.resolution(value=value)], [question])
        answers, _, _ = self.review([self.resolution(value=[])], [{**question, "required": False}])
        self.assertEqual(answers[0]["value"], [])

    def test_text_answer_requires_nonempty_text(self):
        for value in ("", " \n", True, 1, ["Next month"], None):
            with self.subTest(value=value), self.assertRaises(ReviewError):
                self.review([self.resolution(value=value)])

    def test_application_history_alone_cannot_supply_a_personal_answer(self):
        history = self.citation("application_history", 1, 1)
        with self.assertRaises(ReviewError):
            self.review([self.resolution(citations=[history])])
        answers, provenance, _ = self.review([self.resolution(citations=[self.citation(), history])])
        self.assertEqual(len(answers), 1)
        self.assertEqual(len(provenance["q1"]["citations"]), 2)

    def test_unknown_fact_remains_unresolved_without_answer_or_provenance(self):
        explanation = "The fictional profile does not state this employer-specific preference."
        unresolved = self.resolution(value=None, method="unresolved", citations=[], explanation=explanation)
        answers, provenance, missing = self.review([unresolved], bindings={})
        self.assertEqual(answers, [])
        self.assertEqual(provenance, {})
        self.assertEqual(missing, [{"question_id": "q1", "reason": explanation}])
        with self.assertRaises(ReviewError):
            self.review([{**unresolved, "answer_json": '"inferred answer"'}])

    def test_changed_or_removed_source_prevents_accepting_review(self):
        resolution = self.resolution()
        self.resume.write_text("Revised fictional resume.\n", encoding="utf-8")
        with self.assertRaises(ReviewError) as caught:
            self.review([resolution])
        self.assertEqual(caught.exception.status, 409)
        self.snapshot = self.load()
        self.profile.unlink()
        with self.assertRaises(ReviewError):
            self.review([self.resolution()])

    def test_malformed_review_is_rejected(self):
        for result in (None, [], {}, {"resolutions": "not an array"}):
            with self.subTest(result=result), self.assertRaises(ReviewError):
                self.snapshot.validate_review(result, [self.question()], {"q1": "start_date"})
        for changes in ({"answer_json": "not json"}, {"method": "guess"}, {"explanation": ""}):
            with self.subTest(changes=changes), self.assertRaises(ReviewError):
                self.review([self.resolution(**changes)])


if __name__ == "__main__":
    unittest.main()
