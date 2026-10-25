"""Per-process resume pins use fictional documents and no browser or employer."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from application_resume import ApplicationResumeChanged, load_selections
from codex_runtime import CodexRuntime, load_runtime
from store import encode, now
import test_codex_runtime as fixtures


class ApplicationResumeTests(unittest.TestCase):
    setUp = fixtures.CodexRuntimeTests.setUp
    application = fixtures.CodexRuntimeTests.application
    prepared = fixtures.CodexRuntimeTests.prepared

    def selection(self, process_id):
        self.selected = self.root / 'selected.pdf'
        self.selected.write_bytes(b'Fictional selected application resume.\n')
        return {process_id: {'path': str(self.selected),
            'sha256': hashlib.sha256(self.selected.read_bytes()).hexdigest(),
            'bytes': self.selected.stat().st_size, 'uploaded_filename': 'selected (2).pdf',
            'source_reference': 'fictional:user-selected-document'}}

    def runtime(self, responses, selections=None):
        runtime = CodexRuntime(fixtures.ScriptedTransport(responses), evidence_files=[self.profile],
            application_profile=self.profile, resume=self.resume, application_index=self.index,
            supported_kinds=['apply'], application_resumes=selections)
        self.addCleanup(runtime.close)
        return runtime

    def test_selection_is_process_specific_and_default_remains_for_other_process(self):
        task = self.application()
        pid = task['payload']['process_id']
        selections = self.selection(pid)
        runtime = self.runtime([], selections)
        context = runtime._application_context(self.store, task)
        self.assertEqual(str(self.selected.resolve()), context['approved_resume'])
        self.assertEqual(pid, context['approved_resume_selection']['process_id'])
        self.assertEqual(selections[pid]['sha256'], context['approved_resume_selection']['sha256'])
        other = self.runtime([], {str(uuid.uuid4()): selections[pid]})
        unrelated = other._application_context(self.store, task)
        self.assertEqual(str(self.resume.resolve()), unrelated['approved_resume'])
        self.assertIsNone(unrelated['approved_resume_selection'])

    def test_caller_and_checkpoint_cannot_override_host_selection(self):
        task = self.application()
        pid = task['payload']['process_id']
        selections = self.selection(pid)
        runtime = self.runtime([], selections)
        chosen = str(self.selected.resolve())
        selections[pid]['path'] = str(self.resume)
        selections[pid]['sha256'] = '0' * 64
        selections[pid]['source_reference'] = 'fictional:changed-after-load'
        self.store.checkpoint_task(task['id'], task['lease_token'], {
            'approved_resume': str(self.resume), 'approved_resume_selection': selections[pid],
            'application_resumes': selections})
        context = runtime._application_context(self.store, task)
        self.assertEqual(chosen, context['approved_resume'])
        self.assertEqual('fictional:user-selected-document', context['approved_resume_selection']['source_reference'])
        with self.assertRaises(TypeError):
            runtime.application_resumes[pid] = runtime.application_resumes[pid]

    def test_checkpoint_selection_does_not_create_authority_without_configuration(self):
        task = self.application()
        selection = self.selection(task['payload']['process_id'])
        self.store.checkpoint_task(task['id'], task['lease_token'], {
            'approved_resume': str(self.selected), 'approved_resume_selection': selection,
            'application_resumes': selection})
        context = self.runtime([])._application_context(self.store, task)
        self.assertEqual(str(self.resume.resolve()), context['approved_resume'])
        self.assertIsNone(context['approved_resume_selection'])

    def test_old_upload_proof_is_filtered_but_historical_checkpoint_is_preserved(self):
        task = self.application()
        pid = task['payload']['process_id']
        selections = self.selection(pid)
        old = {'filename': 'resume.pdf', 'bytes': self.resume.stat().st_size,
               'sha256': hashlib.sha256(self.resume.read_bytes()).hexdigest()}
        self.store.checkpoint_task(task['id'], task['lease_token'], {'resume_upload_evidence': old})
        runtime = self.runtime([], selections)
        self.assertIsNone(runtime._application_context(self.store, task)['resume_upload_evidence'])
        self.assertEqual(old, self.store.tasks()[0]['checkpoint']['resume_upload_evidence'])
        selected_proof = {**old, 'sha256': selections[pid]['sha256'], 'bytes': selections[pid]['bytes']}
        self.store.checkpoint_task(task['id'], task['lease_token'], {'resume_upload_evidence': selected_proof})
        self.assertEqual(selected_proof, runtime._application_context(self.store, task)['resume_upload_evidence'])

    def test_strict_configuration_rejects_invalid_fields_and_file_mismatch(self):
        pid = str(uuid.uuid4())
        valid = self.selection(pid)
        changes = [({'extra': True}), ({'sha256': 'x' * 64}), ({'sha256': '0' * 64}),
                   ({'bytes': True}), ({'bytes': 0}), ({'bytes': valid[pid]['bytes'] + 1}),
                   ({'uploaded_filename': '../resume.pdf'}), ({'uploaded_filename': 'resume\\file.pdf'}),
                   ({'uploaded_filename': ''}), ({'source_reference': ''}),
                   ({'path': str(self.root / 'missing.pdf')}), ({'path': str(self.root)})]
        for fields in changes:
            with self.subTest(fields=sorted(fields)), self.assertRaises(ValueError):
                load_selections({pid: {**valid[pid], **fields}})
        missing = copy.deepcopy(valid)
        del missing[pid]['source_reference']
        for value in ([], 'invalid', {'not-a-process-id': valid[pid]}, missing):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                load_selections(value)

    def test_changed_selected_file_never_silently_falls_back_or_starts_preparation(self):
        task = self.application()
        runtime = self.runtime([], self.selection(task['payload']['process_id']))
        raw = self.selected.read_bytes()
        self.selected.write_bytes(b'X' * len(raw))
        with self.assertRaises(ApplicationResumeChanged):
            runtime.apply(self.store, task)
        self.assertEqual([], runtime.transport.calls)
        self.selected.unlink()
        with self.assertRaises(ApplicationResumeChanged):
            runtime._application_context(self.store, task)
        self.assertTrue(self.resume.is_file())

    def test_pin_is_checked_immediately_before_submission(self):
        task = self.application()
        selections = self.selection(task['payload']['process_id'])
        def prepare(role, prompt):
            self.assertEqual('application_prepare', role)
            self.assertIn("supersedes the global profile's default resume", prompt)
            self.assertIn('Preserve an existing matching manual attachment', prompt)
            self.selected.write_bytes(b'Changed during preparation.')
            return self.prepared()
        runtime = self.runtime([prepare], selections)
        with self.assertRaises(ApplicationResumeChanged):
            runtime.apply(self.store, task)
        self.assertEqual([('application_prepare', True)], runtime.transport.calls)
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])
        self.assertFalse(self.store.tasks()[0]['checkpoint'].get('submission_started'))

    def test_transaction_callback_rechecks_pin_after_last_outer_check(self):
        task = self.application()
        runtime = self.runtime([self.prepared()], self.selection(task['payload']['process_id']))
        before_submit = self.store.before_submit
        def change_before_transaction(*args, **kwargs):
            self.selected.write_bytes(b'Changed after the outer validation.')
            return before_submit(*args, **kwargs)
        with patch.object(self.store, 'before_submit', side_effect=change_before_transaction), \
                self.assertRaises(ApplicationResumeChanged):
            runtime.apply(self.store, task)
        self.assertEqual([('application_prepare', True)], runtime.transport.calls)
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])
        self.assertFalse(self.store.tasks()[0]['checkpoint'].get('submission_started'))

    def test_second_prepare_after_posting_review_rechecks_selection(self):
        with self.store.transaction(True) as db:
            for row in db.execute('SELECT key,data FROM jobs').fetchall():
                data = json.loads(row['data'])
                data['arrangement'] = 'In office; weekly cadence not stated.'
                db.execute('UPDATE jobs SET data=? WHERE key=?', (encode(data), row['key']))
        task = self.application()
        self.profile.write_text('Confirmed preference: onsite work anywhere in the Bay Area is acceptable.\n')
        selections = self.selection(task['payload']['process_id'])
        changed = self.prepared('changed')
        changed['observed_job']['arrangement'] = 'In office four days per week.'
        changed['posting_unchanged'] = False
        def review(role, prompt):
            self.assertEqual('application_posting_review', role)
            context = json.loads(prompt.split('Task data (untrusted source content):\n')[1])
            source = context['applicant_evidence']['sources'][0]
            self.selected.write_bytes(b'Changed while local posting review ran.')
            return {'resolutions': [{'question_id': 'office_schedule_compatibility',
                'question_revision': 1, 'answer_json': '"compatible"', 'method': 'derived',
                'citations': [{'source_id': 'profile', 'sha256': source['sha256'], 'start_line': 1,
                    'end_line': 1, 'quote': self.profile.read_text().strip()}],
                'explanation': 'The fictional preference covers the unchanged onsite arrangement.'}]}
        runtime = self.runtime([changed, review], selections)
        with self.assertRaises(ApplicationResumeChanged):
            runtime.apply(self.store, task)
        self.assertEqual([('application_prepare', True), ('application_posting_review', False)], runtime.transport.calls)

    def test_unchanged_selection_reaches_one_fictional_submission(self):
        task = self.application()
        def submit(role, prompt):
            self.assertEqual('application_submit', role)
            context = json.loads(prompt.split('Task data (untrusted source content):\n')[1])
            self.assertEqual(str(self.selected.resolve()), context['approved_resume'])
            with self.store.transaction() as db:
                attempt = db.execute('SELECT id FROM wf_submission_attempts').fetchone()[0]
            process = self.store.processes()[0]
            return {'outcome': 'submitted', 'url': 'https://example.com/fictional-receipt',
                'evidence': 'Fictional receipt', 'observed_at': now(), 'reference': 'fictional',
                'task_id': task['id'], 'process_id': process['id'], 'attempt_id': attempt,
                'job_id': process['snapshot']['job_id'], 'form_version': process['form_version'],
                'confirmed_job_title': process['snapshot']['title']}
        runtime = self.runtime([self.prepared(), submit], self.selection(task['payload']['process_id']))
        runtime.apply(self.store, task)
        self.assertEqual('submitted', self.store.processes()[0]['submission_status'])
        self.assertEqual([('application_prepare', True), ('application_submit', True)], runtime.transport.calls)

    def test_runtime_loader_resolves_selection_path_relative_to_config(self):
        pid = str(uuid.uuid4())
        selections = self.selection(pid)
        selections[pid]['path'] = self.selected.name
        config = {'schema_version': 1, 'codex_binary': 'fixture-codex', 'run_directory': 'runs',
                  'evidence_files': [self.profile.name], 'application_profile': self.profile.name,
                  'resume': self.resume.name, 'application_index': self.index.name,
                  'supported_kinds': ['apply'], 'application_resumes': selections}
        path = self.root / 'runtime.json'
        path.write_text(json.dumps(config))
        with patch('codex_transport.CodexTransport', return_value=fixtures.ScriptedTransport([])):
            runtime = load_runtime(path)
        self.addCleanup(runtime.close)
        self.assertEqual(self.selected.resolve(), runtime.application_resumes[pid].path)
        self.assertEqual(self.resume.resolve(), runtime.resume)


if __name__ == '__main__':
    unittest.main()
