"""Real lifecycle, fictional tasks, scripted agent transport. No employer calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_runtime import CodexRuntime
from orchestrator import TaskLease
from store import ReviewError, now
from test_store import catalog
from workflow import WorkflowStore


def unresolved_review(role, prompt):
    context = json.loads(prompt.split('Task data (untrusted source content):\n', 1)[1])
    return {'resolutions': [{'question_id': q['id'], 'question_revision': q['revision'],
        'answer_json': 'null', 'method': 'unresolved', 'citations': [],
        'explanation': 'Not recorded in the fictional profile.'} for q in context['questions']]}


BROWSER = {"chrome": {"attached": True, "session_id": "test-profile"},
           "linkedin": {"authenticated": True, "session_id": "test-profile", "tab_id": "tab-1",
                        "url": "https://www.linkedin.com/jobs/"}, "blocker": "", "blocker_reason": ""}


class ScriptedTransport:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def run(self, role, prompt, schema, lease, *, browser=False):
        self.calls.append((role, browser))
        answer = self.responses.pop(0)
        if callable(answer):
            return answer(role, prompt)
        return answer

    def close(self):
        pass


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WorkflowStore(self.root / "review.sqlite3")
        self.store.grant_project_consent({"request_id": "fixture-consent", "policy_version": 1, "accepted": True})
        self.store.import_catalog(catalog(), "fictional")
        self.profile = self.root / "profile.txt"
        self.profile.write_text("Fictional candidate. Backend engineer.")
        self.resume = self.root / "resume.pdf"
        self.resume.write_text("Fictional placeholder; never uploaded.")
        self.index = self.root / "applications.json"
        self.index.write_text("[]")

    def runtime(self, responses, kinds=("explore", "rank", "apply")):
        transport = ScriptedTransport(responses)
        return CodexRuntime(transport, evidence_files=[self.profile], application_profile=self.profile,
                            resume=self.resume, application_index=self.index, supported_kinds=kinds)

    def application(self):
        card = self.store.feed()["cards"][0]
        self.store.receive_decision({"request_id": "test-apply", "action": "apply", "card_id": card["id"],
            "card_revision": card["revision"], "selected": [card["jobs"][0]["key"]]})
        task = self.store.lease_task("test", kinds=["apply"])
        task["browser"] = BROWSER
        self.store.start_application(task["payload"]["process_id"], task["lease_token"])
        return task

    def prepared(self, outcome="ready"):
        snapshot = self.store.processes()[0]["snapshot"]
        return {"outcome": outcome, "job_id": snapshot["job_id"], "job_url": snapshot["url"],
                "observed_job": {k: snapshot.get(k) for k in ("title", "url", "location", "arrangement", "salary", "level", "gaps")},
                "posting_unchanged": True,
                "posting_evidence": "Read exact fictional job page", "form_url": "https://example.com/form",
                "tab_id": "form-tab", "form_fields": [{"name": "name", "value": "Test Candidate"}],
                "questions": [], "blocker": "", "blocker_reason": "", "answer_bindings": []}

    def test_browser_preparation_is_a_separate_read_only_role(self):
        task = self.application()
        runtime = self.runtime([BROWSER])
        self.assertEqual(BROWSER, runtime.prepare_browser(task, TaskLease(self.store, task)))
        self.assertEqual([("linkedin_session", True)], runtime.transport.calls)

    def test_browser_turn_gets_only_current_lease_fenced_user_guidance(self):
        task = self.application()
        guidance = {'instruction_id': 'fictional-guidance', 'task_id': task['id'],
                    'text': 'I completed sign-in in the existing draft. Please check it.',
                    'authority': 'user'}
        def inspect(role, prompt):
            instructions, data = prompt.split('Task data (untrusted source content):\n', 1)
            self.assertEqual(guidance, json.loads(data)['user_unblock_instruction'])
            self.assertIn('Verify claims from fresh evidence.', instructions)
            self.assertIn('does not supply structured question answers or new legal consent', instructions)
            self.assertNotIn(guidance['text'], instructions)
            return BROWSER
        runtime = self.runtime([inspect])
        with patch.object(self.store, 'unblock_instruction_context', return_value=guidance) as context:
            runtime.prepare_browser(task, TaskLease(self.store, task))
        context.assert_called_once_with(task['id'], task['lease_token'], purpose='readiness')

    def test_stale_guidance_is_absent_from_browser_prompt(self):
        task = self.application()
        def inspect(role, prompt):
            data = json.loads(prompt.split('Task data (untrusted source content):\n', 1)[1])
            self.assertNotIn('user_unblock_instruction', data)
            return BROWSER
        runtime = self.runtime([inspect])
        with patch.object(self.store, 'unblock_instruction_context', return_value=None):
            runtime.prepare_browser(task, TaskLease(self.store, task))

    def test_browser_preparation_relays_only_matching_scoped_read_authorization(self):
        task = self.application()
        authorization = {'task_id': task['id'], 'attempt': task['attempts'],
            'scope': 'read_existing_linkedin_tab', 'tab_id': 'fictional-linkedin-tab',
            'question': 'Allow this existing LinkedIn tab read through Chrome/CUA?',
            'answer': 'Allow this LinkedIn read', 'source_chat_id': 'fictional-chat',
            'confirmed_at': '2026-10-18T00:00:00Z'}
        task['checkpoint'] = {'browser_read_authorization': authorization}
        def inspect(role, prompt):
            instructions, data = prompt.split('Task data (untrusted source content):\n', 1)
            self.assertEqual(authorization, json.loads(data)['browser_read_authorization'])
            self.assertIn("The host has recorded the user's explicit approval for this task attempt", instructions)
            self.assertIn('not permission to bypass any tool gate.', instructions)
            self.assertNotIn(authorization['question'], instructions)
            self.assertNotIn(authorization['answer'], instructions)
            self.assertIn('Use only that existing tab for this scoped retry.', instructions)
            self.assertIn('do not open or substitute a different tab.', instructions)
            self.assertIn('Keep all tool approval controls in force', instructions)
            self.assertIn('Never use a separate Chrome profile, direct CDP', instructions)
            return BROWSER
        runtime = self.runtime([inspect])
        self.assertEqual(BROWSER, runtime.prepare_browser(task, TaskLease(self.store, task)))
        self.assertEqual([('linkedin_session', True)], runtime.transport.calls)

    def test_browser_preparation_ignores_mismatched_or_incomplete_read_authorization(self):
        task = self.application()
        valid = {'task_id': task['id'], 'attempt': task['attempts'],
                 'scope': 'read_existing_linkedin_tab', 'tab_id': 'fictional-linkedin-tab'}
        invalid = [None, 'not a record', {**valid, 'task_id': 'another-task'},
                   {**valid, 'attempt': task['attempts'] - 1}, {**valid, 'attempt': True},
                   {**valid, 'scope': 'all_browser_actions'}, {**valid, 'tab_id': ''},
                   {key: value for key, value in valid.items() if key != 'attempt'}]
        def inspect(role, prompt):
            instructions, data = prompt.split('Task data (untrusted source content):\n', 1)
            context = json.loads(data)
            self.assertEqual(task['kind'], context['kind'])
            self.assertEqual(self.store.project_consent(), context['project_consent'])
            self.assertNotIn("The host has recorded the user's explicit approval", instructions)
            return BROWSER
        for authorization in invalid:
            with self.subTest(authorization=authorization):
                task['checkpoint'] = {'browser_read_authorization': authorization}
                runtime = self.runtime([inspect])
                runtime.prepare_browser(task, TaskLease(self.store, task))

    def test_application_receives_current_evidence_and_missing_source_stops_before_form(self):
        task = self.application()
        def inspect(role, prompt):
            context = json.loads(prompt.split('Task data (untrusted source content):\n', 1)[1])
            sources = context['applicant_evidence']['sources']
            self.assertEqual('1: Updated fictional confirmed fact.', sources[0]['numbered_text'])
            return self.prepared('blocked')
        runtime = self.runtime([inspect])
        self.profile.write_text('Updated fictional confirmed fact.')
        runtime.apply(self.store, task)
        self.assertEqual([('application_prepare', True)], runtime.transport.calls)
        self.profile.unlink()
        with self.assertRaises(ReviewError):
            runtime.apply(self.store, task)
        self.assertEqual(1, len(runtime.transport.calls))

    def test_application_prompt_preserves_verified_upload_and_reports_actual_gate(self):
        task = self.application()
        def inspect(role, prompt):
            self.assertEqual('application_prepare', role)
            instructions = prompt.split('Task data (untrusted source content):\n', 1)[0]
            for requirement in (
                    'inspect any existing resume attachment',
                    'Preserve and reuse it',
                    'verify that it matches the\napproved resume',
                    'do not replace or upload it again',
                    'Do not assume a match from an\nunverified attachment',
                    'exact observed error and attachment\nstate',
                    'cancelled or denied upload approval is distinct from missing Chrome\nfile-URL access',
                    'do not infer that the extension setting is disabled',
                    'Do not change browser security or extension settings, or bypass an\nupload approval gate'):
                self.assertIn(requirement, instructions)
            prepared = self.prepared('blocked')
            prepared['blocker'] = 'Upload approval was cancelled; no file attached.'
            return prepared
        runtime = self.runtime([inspect])
        runtime.apply(self.store, task)
        self.assertEqual([('application_prepare', True)], runtime.transport.calls)
        self.assertEqual('waiting_for_user', self.store.processes()[0]['task_status'])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_application_context_reads_optional_upload_evidence_from_current_checkpoint(self):
        task = self.application()
        runtime = self.runtime([])
        self.assertIsNone(runtime._application_context(self.store, task)['resume_upload_evidence'])
        proof = {'task_id': task['id'], 'process_id': task['payload']['process_id'],
                 'job_id': self.store.processes()[0]['snapshot']['job_id'],
                 'tab_id': 'fictional-form-tab', 'form_url': 'https://example.com/form',
                 'filename': 'resume.pdf', 'bytes': 40, 'sha256': 'a' * 64,
                 'observed_text': 'Successfully Uploaded!', 'screenshot_path': '/fictional/upload.png'}
        self.store.checkpoint_task(task['id'], task['lease_token'],
            {'question_bindings': {'q1': 'occupation'}, 'resume_upload_evidence': proof})
        context = runtime._application_context(self.store, task)
        self.assertEqual(proof, context['resume_upload_evidence'])
        self.assertEqual({'q1': 'occupation'}, context['question_bindings'])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_missing_question_field_parks_without_answer_review_or_submission(self):
        task = self.application()
        prepared = self.prepared('questions')
        prepared['questions'] = [{'text': 'Accept fictional application terms?', 'field_name': 'Apply',
            'type': 'single_choice', 'choices': [{'id': 'agree', 'label': 'I agree'}],
            'required': True, 'reason': 'Specific consent is missing.'}]
        runtime = self.runtime([prepared])
        runtime.apply(self.store, task)
        self.assertEqual('application_snapshot_invalid', self.store.processes()[0]['block_reason'])
        self.assertEqual('waiting_for_user', self.store.processes()[0]['task_status'])
        self.assertEqual([], self.store.questions())
        self.assertEqual([('application_prepare', True)], runtime.transport.calls)
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_resource_locks').fetchone()[0])

    def test_duplicate_question_bindings_remain_invalid(self):
        task = self.application()
        prepared = self.prepared('questions')
        prepared['questions'] = [{'text': label, 'field_name': 'name', 'type': 'text',
            'choices': [], 'required': True, 'reason': 'Missing'} for label in ('First?', 'Second?')]
        runtime = self.runtime([prepared])
        runtime.apply(self.store, task)
        self.assertEqual('application_snapshot_invalid', self.store.processes()[0]['block_reason'])
        self.assertEqual([], self.store.questions())
        self.assertEqual([('application_prepare', True)], runtime.transport.calls)

    def test_button_consent_stays_unanswered_and_preparation_stage_is_explicit(self):
        task = self.application()
        prepared = self.prepared('questions')
        prepared['form_fields'].append({'name': 'Apply', 'value': ''})
        prepared['questions'] = [{'text': 'Accept fictional application terms?', 'field_name': 'Apply',
            'type': 'single_choice', 'choices': [{'id': 'agree', 'label': 'I agree'}],
            'required': True, 'reason': 'Specific consent is missing.'}]
        def prepare(role, prompt):
            instructions = prompt.split('Task data (untrusted source content):\n', 1)[0]
            self.assertIn('Current host stage: application_prepare.', instructions)
            self.assertIn('This stage does not clear a prior tool denial or grant missing consent.', instructions)
            self.assertIn('Every question field_name must also appear exactly once in form_fields', instructions)
            return prepared
        runtime = self.runtime([prepare, unresolved_review])
        runtime.apply(self.store, task)
        self.assertEqual('questions_pending', self.store.processes()[0]['block_reason'])
        question = self.store.questions()[0]
        self.assertIsNone(question['answer'])
        self.assertEqual({'Apply'}, set(self.store.tasks()[0]['checkpoint']['question_bindings'].values()))
        self.assertEqual([('application_prepare', True), ('application_question_review', False)], runtime.transport.calls)
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_known_question_is_answered_with_evidence_and_queues_same_task(self):
        task = self.application()
        prepared = self.prepared('questions')
        prepared['form_fields'].append({'name': 'occupation', 'value': ''})
        prepared['questions'] = [{'text': 'Occupation?', 'field_name': 'occupation', 'type': 'text',
                                  'choices': [], 'required': True, 'reason': 'Worker missed profile.'}]
        def resolve(role, prompt):
            self.assertEqual('application_question_review', role)
            context = json.loads(prompt.split('Task data (untrusted source content):\n', 1)[1])
            q, source = context['questions'][0], context['applicant_evidence']['sources'][0]
            return {'resolutions': [{'question_id': q['id'], 'question_revision': q['revision'],
                'answer_json': '"Backend engineer"', 'method': 'direct',
                'citations': [{'source_id': source['source_id'], 'sha256': source['sha256'],
                              'start_line': 1, 'end_line': 1, 'quote': self.profile.read_text()}],
                'explanation': 'Explicit occupation in profile.'}]}
        runtime = self.runtime([prepared, resolve])
        runtime.apply(self.store, task)
        self.assertEqual('queued', self.store.processes()[0]['task_status'])
        self.assertEqual(task['id'], self.store.tasks()[0]['id'])
        self.assertEqual('Backend engineer', self.store.questions()[0]['answer'])
        self.assertEqual([('application_prepare', True), ('application_question_review', False)], runtime.transport.calls)
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM wf_answer_evidence').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])
        self.profile.write_text('Corrected fictional occupation.')
        resumed = self.store.lease_task('test', kinds=['apply'])
        resumed['browser'] = BROWSER
        self.store.start_application(task['payload']['process_id'], resumed['lease_token'])
        runtime = self.runtime([unresolved_review])
        runtime.apply(self.store, resumed)
        self.assertEqual([('application_question_review', False)], runtime.transport.calls)
        self.assertEqual('applicant_evidence_changed', self.store.processes()[0]['block_reason'])

    def test_profile_append_refreshes_unchanged_automatic_answer_before_form(self):
        from applicant_evidence import EvidenceSnapshot
        task = self.application()
        pid = task['payload']['process_id']
        self.store.checkpoint_task(task['id'], task['lease_token'], {'question_bindings': {'q1': 'occupation'}})
        process = self.store.ask_questions(pid, task['lease_token'], 'first-form', [
            {'id': 'q1', 'text': 'Occupation?', 'type': 'text', 'choices': [], 'required': True,
             'scope': 'application', 'reason': 'Missing'}])
        runtime = self.runtime([])
        old = runtime.evidence_snapshot(application=True)
        source = old.sources['profile']
        citation = {'source_id': 'profile', 'sha256': source['sha256'], 'start_line': 1,
                    'end_line': 1, 'quote': source['content']}
        runtime.save_evidence_answers(self.store, process,
            [{'question_id': 'q1', 'question_revision': 1, 'value': 'Backend engineer'}],
            {'q1': {'field_name': 'occupation', 'method': 'direct', 'source_snapshot_hash': old.hash,
                    'citations': [citation], 'explanation': 'Confirmed occupation.'}}, old)
        self.profile.write_text(self.profile.read_text() + '\nAn unrelated confirmed detail.')
        resumed = self.store.lease_task('test', kinds=['apply'])
        resumed['browser'] = BROWSER
        self.store.start_application(pid, resumed['lease_token'])
        def fresh_review(role, prompt):
            context = json.loads(prompt.split('Task data (untrusted source content):\n', 1)[1])
            self.assertEqual({'q1': 'Backend engineer'}, context['candidate_saved_answers'])
            source = context['applicant_evidence']['sources'][0]
            q = context['questions'][0]
            return {'resolutions': [{'question_id': q['id'], 'question_revision': q['revision'],
                'answer_json': '"Backend engineer"', 'method': 'direct',
                'citations': [{**citation, 'sha256': source['sha256']}], 'explanation': 'Still confirmed.'}]}
        runtime = self.runtime([fresh_review, self.prepared('blocked')])
        runtime.apply(self.store, resumed)
        self.assertEqual([('application_question_review', False), ('application_prepare', True)], runtime.transport.calls)
        self.assertEqual('Backend engineer', self.store.questions()[0]['answer'])
        self.assertEqual(2, self.store.questions()[0]['revision'])
        with self.store.transaction() as db:
            self.assertEqual(runtime.evidence_snapshot(application=True).hash,
                db.execute('SELECT source_snapshot_hash FROM wf_answer_evidence').fetchone()[0])

    def test_new_question_round_preserves_existing_user_answer_and_binding(self):
        task = self.application()
        pid = task['payload']['process_id']
        self.store.checkpoint_task(task['id'], task['lease_token'], {'question_bindings': {'q1': 'start_date'}})
        process = self.store.ask_questions(pid, task['lease_token'], 'first-form', [
            {'id': 'q1', 'text': 'Start date?', 'type': 'text', 'choices': [], 'required': True,
             'scope': 'application', 'reason': 'Missing'}])
        self.store.answer_questions({'request_id': 'user-answer', 'process_id': pid,
            'process_revision': process['revision'], 'form_version': process['form_version'],
            'answers': [{'question_id': 'q1', 'question_revision': 1, 'value': 'Next month'}]})
        resumed = self.store.lease_task('test', kinds=['apply'])
        resumed['browser'] = BROWSER
        self.store.start_application(pid, resumed['lease_token'])
        prepared = self.prepared('questions')
        prepared['form_fields'] += [{'name': 'start_date', 'value': 'Next month'}, {'name': 'other_fact', 'value': ''}]
        prepared['answer_bindings'] = [{'question_id': 'q1', 'question_revision': 2,
            'answer_json': '"Next month"', 'field_name': 'start_date', 'field_value': 'Next month'}]
        prepared['questions'] = [{'text': 'Another missing fact?', 'field_name': 'other_fact', 'type': 'text',
                                 'choices': [], 'required': True, 'reason': 'Missing'}]
        def inspect_new_form(role, prompt):
            from store import digest
            context = json.loads(prompt.split('Task data (untrusted source content):\n', 1)[1])
            self.assertEqual(digest({'url': prepared['form_url'], 'fields': prepared['form_fields']}),
                             context['process']['form_version'])
            return unresolved_review(role, prompt)
        self.runtime([prepared, inspect_new_form]).apply(self.store, resumed)
        current = {q['id']: q for q in self.store.questions()}
        self.assertEqual(2, len(current))
        self.assertEqual('Next month', current['q1']['answer'])
        self.assertEqual(2, current['q1']['revision'])
        self.assertEqual(self.store.processes()[0]['form_version'], current['q1']['form_version'])
        self.assertEqual('start_date', self.store.tasks()[0]['checkpoint']['question_bindings']['q1'])

    def test_source_change_during_prepare_never_reaches_submit(self):
        task = self.application()
        def prepare(role, prompt):
            self.profile.write_text('Changed facts while worker prepared.')
            return self.prepared()
        runtime = self.runtime([prepare])
        with self.assertRaises(ReviewError):
            runtime.apply(self.store, task)
        self.assertEqual([('application_prepare', True)], runtime.transport.calls)
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_evidence_change_cannot_hide_explicit_human_browser_blocker(self):
        from readiness import BrowserNotReady, HUMAN_BROWSER_REASONS
        task = self.application()
        for reason in sorted(HUMAN_BROWSER_REASONS):
            with self.subTest(reason=reason):
                def prepare(role, prompt):
                    self.profile.write_text('Changed evidence: ' + reason)
                    result = self.prepared('blocked')
                    result.update(blocker_reason=reason, blocker='Fictional human action required')
                    return result
                runtime = self.runtime([prepare])
                with self.assertRaises(BrowserNotReady) as caught:
                    runtime.apply(self.store, task)
                self.assertEqual(reason, caught.exception.reason)
                self.assertEqual([('application_prepare', True)], runtime.transport.calls)
                with self.store.transaction() as db:
                    self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_rank_uses_stored_context_without_browser(self):
        task = self.application()
        runtime = self.runtime([{"cards": []}])
        result = runtime.rank({"candidates": [], "excluded_company_ids": []}, TaskLease(self.store, task))
        self.assertEqual({"cards": []}, result)
        self.assertEqual([("ranker", False)], runtime.transport.calls)

    def test_submit_cannot_start_before_checkpoint_and_observed_receipt(self):
        task = self.application()
        def submit(role, prompt):
            self.assertEqual("application_submit", role)
            self.assertEqual("submission_unknown", self.store.processes()[0]["submission_status"])
            with self.store.transaction() as db:
                self.assertEqual(1, db.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0])
                attempt_id = db.execute("SELECT id FROM wf_submission_attempts").fetchone()[0]
            process = self.store.processes()[0]
            return {"outcome": "submitted", "url": "https://example.com/receipt",
                    "evidence": "Fictional application received", "observed_at": now(), "reference": "test-123",
                    "task_id": task["id"], "process_id": process["id"], "attempt_id": attempt_id,
                    "job_id": process["snapshot"]["job_id"], "form_version": process["form_version"],
                    "confirmed_job_title": process["snapshot"]["title"]}
        runtime = self.runtime([self.prepared(), submit])
        runtime.apply(self.store, task)
        self.assertEqual("submitted", self.store.processes()[0]["submission_status"])
        self.assertEqual("succeeded", self.store.tasks()[0]["status"])
        self.assertEqual([("application_prepare", True), ("application_submit", True)], runtime.transport.calls)

    def test_missing_fact_round_parks_and_resumes_same_task(self):
        task = self.application()
        answer = self.prepared("questions")
        answer["form_fields"].append({"name": "start_date", "value": ""})
        answer["questions"] = [{"text": "Fictional required start date?", "field_name": "start_date", "type": "text", "choices": [],
                                "required": True, "reason": "Not in profile"}]
        self.runtime([answer, unresolved_review]).apply(self.store, task)
        process = self.store.processes()[0]
        question = self.store.questions()[0]
        self.assertEqual("waiting_for_user", process["task_status"])
        self.assertIsNone(self.store.tasks()[0]["lease_token"])
        self.store.answer_questions({"request_id": "answer", "process_id": process["id"],
            "process_revision": process["revision"], "form_version": process["form_version"], "answers": [
                {"question_id": question["id"], "question_revision": question["revision"], "value": "Next month"}]})
        resumed = self.store.lease_task("test", kinds=["apply"])
        self.assertEqual(task["id"], resumed["id"])
        resumed["browser"] = BROWSER
        self.store.start_application(process["id"], resumed["lease_token"])
        ready = self.prepared()
        ready["form_fields"].append({"name": "start_date", "value": "Next month"})
        saved = self.store.questions()[0]
        ready["answer_bindings"] = [{"question_id": saved["id"], "question_revision": saved["revision"],
            "answer_json": '"Next month"', "field_name": "start_date", "field_value": "Next month"}]
        self.runtime([ready, {"outcome": "unknown"}]).apply(self.store, resumed)
        self.assertEqual("submission_unknown", self.store.processes()[0]["submission_status"])
        self.assertIsNone(self.store.lease_task("no-retry", kinds=["apply"]))

    def test_changed_job_never_calls_submit(self):
        task = self.application()
        ready = self.prepared()
        ready["job_id"] = "a-different-role"
        runtime = self.runtime([ready])
        runtime.apply(self.store, task)
        self.assertEqual("posting_changed", self.store.processes()[0]["block_reason"])
        self.assertEqual(1, len(runtime.transport.calls))
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0])

    def test_current_posting_facts_are_compared_without_trusting_hash_echo(self):
        task = self.application()
        ready = self.prepared()
        ready["observed_job"]["salary"] = "Different compensation"
        runtime = self.runtime([ready])
        runtime.apply(self.store, task)
        self.assertEqual("posting_changed", self.store.processes()[0]["block_reason"])
        self.assertEqual(1, len(runtime.transport.calls))

    def test_wrong_submission_attempt_does_not_confirm(self):
        task = self.application()
        runtime = self.runtime([self.prepared(), {"outcome": "submitted", "attempt_id": "different"}])
        with self.assertRaises(ReviewError):
            runtime.apply(self.store, task)
        self.assertEqual("submission_unknown", self.store.processes()[0]["submission_status"])

    def test_saved_answer_cannot_be_silently_changed_by_agent(self):
        questions = [{"id": "q", "revision": 2, "type": "single_choice", "answer": "no",
                      "choices": [{"id": "no", "label": "No"}, {"id": "yes", "label": "Yes"}]}]
        prepared = {"form_fields": [{"name": "consent", "value": "Yes"}], "answer_bindings": [
            {"question_id": "q", "question_revision": 2, "answer_json": '"no"',
             "field_name": "consent", "field_value": "Yes"}]}
        with self.assertRaises(ReviewError):
            CodexRuntime._validate_answers(questions, prepared, {"q": "consent"})

    def test_saved_answers_stay_bound_to_the_original_form_field(self):
        questions = [{"id": "q1", "revision": 2, "type": "text", "answer": "No"},
                     {"id": "q2", "revision": 2, "type": "text", "answer": "No"}]
        prepared = {"form_fields": [{"name": "citizenship", "value": "No"},
                                     {"name": "sponsorship", "value": "Yes"}],
                    "answer_bindings": [{"question_id": q["id"], "question_revision": 2,
                         "answer_json": '"No"', "field_name": "citizenship", "field_value": "No"} for q in questions]}
        with self.assertRaises(ReviewError):
            CodexRuntime._validate_answers(questions, prepared, {"q1": "citizenship", "q2": "sponsorship"})

    def test_prior_application_never_repeats_submission(self):
        task = self.application()
        runtime = self.runtime([self.prepared("already_applied")])
        runtime.apply(self.store, task)
        self.assertEqual("prior_application_found", self.store.processes()[0]["block_reason"])
        self.assertEqual(1, len(runtime.transport.calls))

    def test_runtime_requires_confirmed_inputs(self):
        with self.assertRaises(ValueError):
            CodexRuntime(ScriptedTransport([]))
        runtime = self.runtime([], kinds=("rank",))
        self.assertEqual(("rank",), runtime.supported_kinds)


if __name__ == "__main__":
    unittest.main()
