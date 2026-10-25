"""Fictional application recovery; never contacts employers or Chrome."""
import json
import unittest
from orchestrator import Orchestrator, TaskLease

from codex_runtime import CodexRuntime
from readiness import BrowserRecoverable, BrowserNotReady
from applicant_evidence import EvidenceChanged
from posting_recovery import PostingReviewInvalid
from store import encode, material_hash, now
import test_codex_runtime as fixtures


class PostingRecoveryTests(unittest.TestCase):
    setUp = fixtures.CodexRuntimeTests.setUp
    application = fixtures.CodexRuntimeTests.application
    runtime = fixtures.CodexRuntimeTests.runtime
    prepared = fixtures.CodexRuntimeTests.prepared

    def clarification_task(self):
        # Establish a fictional approval with an explicitly unknown cadence.
        with self.store.transaction(True) as db:
            for row in db.execute('SELECT key,data FROM jobs').fetchall():
                data = json.loads(row['data'])
                data['arrangement'] = 'In office; weekly cadence not stated.'
                db.execute('UPDATE jobs SET data=? WHERE key=?', (encode(data), row['key']))
        task = self.application()
        self.profile.write_text('Confirmed preference: I accept onsite work anywhere in the Bay Area.\n')
        return task

    def review(self, role, prompt):
        self.assertEqual(role, 'application_posting_review')
        context = json.loads(prompt.split('Task data (untrusted source content):\n')[1])
        source = context['applicant_evidence']['sources'][0]
        return {'resolutions': [{'question_id': 'office_schedule_compatibility',
            'question_revision': 1, 'answer_json': '"compatible"', 'method': 'derived',
            'citations': [{'source_id': 'profile', 'sha256': source['sha256'],
                'start_line': 1, 'end_line': 1, 'quote': self.profile.read_text().strip()}],
            'explanation': 'Explicit onsite preference covers this newly specified cadence.'}]}

    def changed(self):
        value = self.prepared('changed')
        value['observed_job']['arrangement'] = 'In office four days per week.'
        value['posting_unchanged'] = False
        return value

    def test_cadence_reconciles_then_prepares_again_preserving_approval(self):
        task = self.clarification_task()
        original = self.store.processes()[0]['snapshot']
        def reprepare(role, prompt):
            self.assertEqual(role, 'application_prepare')
            ctx = json.loads(prompt.split('Task data (untrusted source content):\n')[1])
            self.assertEqual('In office four days per week.', ctx['process']['snapshot']['arrangement'])
            self.assertIn('posting_clarification', ctx)
            return self.prepared('blocked')
        runtime = self.runtime([self.changed(), self.review, reprepare])
        runtime.apply(self.store, task)
        self.assertEqual(original, self.store.processes()[0]['snapshot'])
        saved = self.store.tasks()[0]['checkpoint']['posting_clarification']
        self.assertEqual(original['material_hash'], saved['approved_material_hash'])
        self.assertEqual([('application_prepare', True), ('application_posting_review', False),
                          ('application_prepare', True)], runtime.transport.calls)

    def test_pay_change_cannot_be_approved_by_preference_reviewer(self):
        task = self.clarification_task()
        changed = self.changed()
        changed['observed_job']['salary'] = 'Lower than approved salary'
        runtime = self.runtime([changed])
        runtime.apply(self.store, task)
        self.assertEqual(1, len(runtime.transport.calls))
        self.assertEqual('posting_changed', self.store.processes()[0]['block_reason'])

    def test_unresolved_preference_stays_blocked(self):
        task = self.clarification_task()
        unresolved = {'resolutions': [{'question_id': 'office_schedule_compatibility',
            'question_revision': 1, 'answer_json': 'null', 'method': 'unresolved',
            'citations': [], 'explanation': 'Current preference conflicts with required cadence.'}]}
        runtime = self.runtime([self.changed(), unresolved])
        runtime.apply(self.store, task)
        self.assertEqual('posting_changed', self.store.processes()[0]['block_reason'])
        self.assertNotIn('posting_clarification', self.store.tasks()[0]['checkpoint'])

    def test_stale_evidence_invalidates_saved_clarification(self):
        from posting_recovery import validate_proof
        task = self.clarification_task()
        runtime = self.runtime([self.changed(), self.review, self.prepared('blocked')])
        runtime.apply(self.store, task)
        proof = self.store.tasks()[0]['checkpoint']['posting_clarification']
        snapshot = self.store.processes()[0]['snapshot']
        self.assertIsNotNone(validate_proof(snapshot, proof, runtime.evidence_snapshot(application=True)))
        self.profile.write_text('I now require remote work only.\n')
        self.assertIsNone(validate_proof(snapshot, proof, runtime.evidence_snapshot(application=True)))

    def test_accepted_clarification_reaches_single_submission_and_receipt(self):
        task = self.clarification_task()
        changed = self.changed()
        ready = {**changed, 'outcome': 'ready', 'posting_unchanged': True}
        def submit(role, prompt):
            self.assertEqual('application_submit', role)
            process = self.store.processes()[0]
            with self.store.transaction() as db:
                attempts = db.execute('SELECT id FROM wf_submission_attempts').fetchall()
            self.assertEqual(1, len(attempts))
            return {'outcome': 'submitted', 'url': 'https://example.com/receipt',
                'evidence': 'Fictional receipt', 'observed_at': now(), 'reference': 'fixture',
                'task_id': task['id'], 'process_id': process['id'], 'attempt_id': attempts[0]['id'],
                'job_id': process['snapshot']['job_id'], 'form_version': process['form_version'],
                'confirmed_job_title': process['snapshot']['title']}
        runtime = self.runtime([changed, self.review, ready, submit])
        runtime.apply(self.store, task)
        self.assertEqual('submitted', self.store.processes()[0]['submission_status'])
        self.assertEqual('succeeded', self.store.tasks()[0]['status'])
        self.assertIn('posting_clarification', self.store.tasks()[0]['checkpoint'])

    def test_specific_known_schedule_is_not_an_unknown_schedule(self):
        from posting_recovery import candidate
        task = self.application()
        snapshot = self.store.processes()[0]['snapshot']
        snapshot['arrangement'] = 'Hybrid, two office days per week.'
        observed = {k: snapshot.get(k) for k in ('title','url','location','arrangement','salary','level','gaps')}
        observed['arrangement'] = 'Hybrid, four office days per week.'
        self.assertFalse(candidate(snapshot, observed))

    def test_work_mode_change_is_rejected_before_reviewer(self):
        from posting_recovery import candidate
        self.clarification_task()
        snapshot = self.store.processes()[0]['snapshot']
        for arrangement in ('Fully remote forever.', 'Hybrid, two office days.', 'In office; cadence unknown.'):
            observed = self.changed()['observed_job']
            observed['arrangement'] = arrangement
            self.assertFalse(candidate(snapshot, observed), arrangement)

    def test_typed_tab_failure_requests_health_recovery(self):
        task = self.application()
        result = self.prepared('blocked')
        result['blocker_reason'] = 'browser_tab_unavailable'
        runtime = self.runtime([result])
        with self.assertRaises(BrowserRecoverable):
            runtime.apply(self.store, task)
        self.assertEqual('running', self.store.tasks()[0]['status'])

    def test_temporary_site_failure_requests_bounded_recovery_before_submission(self):
        task = self.application()
        def reply(role, prompt):
            self.assertEqual('application_prepare', role)
            self.assertIn('reload that same draft once', prompt)
            self.assertIn('no submission was attempted', prompt)
            self.assertIn('Do not confirm a form-resubmission prompt', prompt)
            result = self.prepared('blocked')
            result.update(blocker_reason='application_page_transient',
                          blocker='The fictional site still requests a refresh after one reload.')
            return result
        runtime = self.runtime([reply])
        with self.assertRaises(BrowserRecoverable) as caught:
            runtime.apply(self.store, task)
        self.assertEqual('application_page_transient', caught.exception.reason)
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_progress_checkpoint_preserves_upload_and_clarification_evidence(self):
        task = self.application()
        lease = TaskLease(self.store, task)
        lease.checkpoint({'resume_upload_evidence': {'filename': 'fictional.pdf'},
                          'posting_clarification': {'proof': 'fictional'}})
        lease.checkpoint({'question_bindings': {'question': 'field'}})
        lease.checkpoint({'prepared_form_hash': 'fictional-hash'})
        saved = self.store.tasks()[0]['checkpoint']
        self.assertEqual({'filename': 'fictional.pdf'}, saved['resume_upload_evidence'])
        self.assertEqual({'proof': 'fictional'}, saved['posting_clarification'])
        self.assertEqual({'question': 'field'}, saved['question_bindings'])

    def test_login_is_human_action_not_transient_retry(self):
        task = self.application()
        result = self.prepared('blocked')
        result.update(blocker_reason='login_required', blocker='Fictional employer sign-in required.')
        runtime = self.runtime([result])
        with self.assertRaises(BrowserNotReady) as caught:
            runtime.apply(self.store, task)
        self.assertEqual('login_required', caught.exception.reason)

    def multiline_profile(self):
        # Reproduce the production failure's structure using fictional text:
        # line 25 was quoted correctly; line 27 was cut after its first sentence.
        lines = ['Fictional source context.'] * 24
        lines += ['- Confirmed preference: onsite work in the Bay Area is acceptable.', '',
                  '  Four office days are acceptable. This preference also covers San Francisco.  ']
        self.profile.write_text('\n'.join(lines) + '\n')
        return lines

    def multiline_review(self, *, truncate=False, after=None):
        def reply(role, prompt):
            self.assertEqual('application_posting_review', role)
            context = json.loads(prompt.split('Task data (untrusted source content):\n')[1])
            source = context['applicant_evidence']['sources'][0]
            lines = self.profile.read_text().splitlines()
            quotes = [lines[24], lines[26]]
            if truncate:
                quotes[1] = lines[26].split('.', 1)[0] + '.'
            result = {'resolutions': [{'question_id': 'office_schedule_compatibility',
                'question_revision': 1, 'answer_json': '"compatible"', 'method': 'derived',
                'citations': [{'source_id': 'profile', 'sha256': source['sha256'],
                    'start_line': line, 'end_line': line, 'quote': quote}
                    for line, quote in zip((25, 27), quotes)],
                'explanation': 'Explicit fictional preference covers the unchanged work mode and cadence.'}]}
            if after is not None:
                after(prompt, context)
            return result
        return reply

    def test_truncated_second_citation_is_corrected_locally_without_browser_replay(self):
        task = self.clarification_task()
        lines = self.multiline_profile()
        def check_correction(prompt, context):
            self.assertIn('entire cited lines including all sentences/whitespace', prompt)
            self.assertEqual({'code': 'citation_text_mismatch', 'review_attempt': 1,
                              'maximum_review_attempts': 2}, context['validation_feedback'])
            self.assertNotIn('previous_review', context)
        runtime = self.runtime([self.changed(), self.multiline_review(truncate=True),
                                self.multiline_review(after=check_correction), self.prepared('blocked')])
        runtime.apply(self.store, task)
        self.assertEqual([('application_prepare', True), ('application_posting_review', False),
                          ('application_posting_review', False), ('application_prepare', True)], runtime.transport.calls)
        proof = self.store.tasks()[0]['checkpoint']['posting_clarification']
        citations = proof['review']['resolutions'][0]['citations']
        self.assertEqual(lines[24], citations[0]['quote'])
        self.assertEqual(lines[26], citations[1]['quote'])
        self.assertEqual(1, sum(e['type'] == 'posting_clarification_resolved'
                                for e in self.store.processes()[0]['events']))

    def test_repeated_invalid_citation_fails_internal_task_without_human_question(self):
        task = self.clarification_task()
        self.multiline_profile()
        with self.store.transaction(True) as db:
            self.store._release_task(db, task['id'], 'queued')
        runtime = self.runtime([fixtures.BROWSER, self.changed(), self.multiline_review(truncate=True),
                                self.multiline_review(truncate=True)])
        self.addCleanup(runtime.close)
        runner = Orchestrator(self.store, runtime)
        self.addCleanup(runner.readiness.close)
        runner.readiness.start()
        runner.readiness.wait(2)
        with self.assertLogs('orchestrator', level='ERROR'), self.assertRaises(PostingReviewInvalid) as caught:
            runner.run_once(kinds=['apply'])
        self.assertEqual('posting_review_invalid', caught.exception.reason)
        saved = next(row for row in self.store.tasks() if row['id'] == task['id'])
        self.assertEqual('failed', saved['status'])
        self.assertNotIn('posting_clarification', saved['checkpoint'])
        self.assertFalse(saved['checkpoint'].get('submission_started'))
        self.assertEqual([], self.store.questions())
        self.assertEqual([('linkedin_session', True), ('application_prepare', True),
                          ('application_posting_review', False), ('application_posting_review', False)], runtime.transport.calls)
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_resource_locks').fetchone()[0])
        self.assertFalse(any(e['type'] == 'posting_clarification_resolved'
                             for e in self.store.processes()[0]['events']))

    def test_source_change_during_invalid_result_prevents_correction_retry(self):
        task = self.clarification_task()
        self.multiline_profile()
        def change_source(prompt, context):
            self.profile.write_text('New confirmed preference: remote work only.\n')
        runtime = self.runtime([self.changed(), self.multiline_review(truncate=True, after=change_source)])
        with self.assertRaises(EvidenceChanged):
            runtime.apply(self.store, task)
        self.assertEqual([('application_prepare', True), ('application_posting_review', False)], runtime.transport.calls)
        self.assertNotIn('posting_clarification', self.store.tasks()[0]['checkpoint'])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_corrected_result_cannot_relax_semantic_guard(self):
        task = self.clarification_task()
        self.multiline_profile()
        unresolved = {'resolutions': [{'question_id': 'office_schedule_compatibility',
            'question_revision': 1, 'answer_json': 'null', 'method': 'unresolved',
            'citations': [], 'explanation': 'The fictional evidence does not prove compatibility.'}]}
        runtime = self.runtime([self.changed(), self.multiline_review(truncate=True), unresolved])
        runtime.apply(self.store, task)
        self.assertEqual('posting_changed', self.store.processes()[0]['block_reason'])
        self.assertNotIn('posting_clarification', self.store.tasks()[0]['checkpoint'])
        self.assertEqual([('application_prepare', True), ('application_posting_review', False),
                          ('application_posting_review', False)], runtime.transport.calls)


if __name__ == '__main__':
    unittest.main()
