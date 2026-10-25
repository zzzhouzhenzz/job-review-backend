"""Offline startup/prompt prototype tests. No native, browser, or production I/O."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from applicant_evidence import EvidenceSnapshot, EvidenceChanged
from codex_runtime import CodexRuntime
from orchestrator import TaskLease
from question_pool import SharedQuestionPool
from readiness import ReadinessMonitor, BrowserNotReady
from store import ReviewError
import test_application_tab_affinity as affinity
import test_codex_runtime as fixtures


class EvidenceReferenceTests(unittest.TestCase):
    def test_reference_preserves_identity_and_freshness_without_inline_prose(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'profile.md';source.write_text('Fictional exact confirmed answer.\n')
            evidence=EvidenceSnapshot(source,[source])
            reference=evidence.reference_context()
            self.assertEqual(reference['snapshot_hash'],evidence.context()['snapshot_hash'])
            self.assertEqual(reference['sources'][0]['sha256'],hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertNotIn('numbered_text',reference['sources'][0])
            self.assertNotIn('content',reference['sources'][0])
            self.assertNotIn('Fictional exact confirmed answer',json.dumps(reference))
            self.assertIn('Fictional exact confirmed answer',json.dumps(evidence.context()))
            source.write_text('Changed fixture\n')
            with self.assertRaises(EvidenceChanged):evidence.assert_current()

    def test_rule_reference_is_atomic_shared_and_rejects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with ThreadPoolExecutor(max_workers=3) as workers:
                refs=list(workers.map(lambda unused:CodexRuntime._preparation_rules_reference('Fictional rules.\n',root),range(3)))
            self.assertEqual(refs,[refs[0]]*3)
            path=Path(refs[0]['path'])
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(path.read_text(),'Fictional rules.\n')
            self.assertEqual(len(list(path.parent.iterdir())),1)
            path.write_text('Changed rules')
            with self.assertRaises(ReviewError):CodexRuntime._preparation_rules_reference('Fictional rules.\n',root)


class WorkerStartupTests(unittest.TestCase):
    setUp=affinity.ApplicationTabRuntimeTests.setUp
    task=affinity.ApplicationTabRuntimeTests.task
    runtime=affinity.ApplicationTabRuntimeTests.runtime
    prepared=fixtures.CodexRuntimeTests.prepared

    def configured(self):
        task=self.task();runtime=self.runtime([]);runtime.application_pool_size=3
        runtime.transport.run_directory=self.root/'runs'
        runtime.transport.run_directory.mkdir(mode=0o700)
        self.store.question_pool=SharedQuestionPool(self.store,self.root/'question-pool').initialize()
        with self.store.question_pool.hold('recovery','fixture-empty'):
            self.store.question_pool.answer_snapshot()
        monitor=ReadinessMonitor(self.store,runtime);self.addCleanup(monitor.close)
        return task,runtime,monitor

    def scripted(self,task,runtime,*,prepare_result=None):
        calls=[]
        def run(role,prompt,schema,lease,**kwargs):
            context=json.loads(prompt.split('Task data (untrusted source content):\n',1)[1])
            calls.append({'role':role,'prompt':prompt,'context':context})
            if role=='application_tab_open':
                result,path=affinity.opening_receipt(self.root,task)
                lease._last_native_browser_receipt=path
                return result
            if role=='application_question_review':
                # This stage remains tool-free and must have readable inline facts.
                self.assertTrue(context['applicant_evidence']['sources'][0]['numbered_text'])
                return fixtures.unresolved_review(role,prompt)
            self.assertEqual(role,'application_prepare')
            self.assertIsNotNone(self.store.application_tab_binding(task['id'],lease.token,'apply-1'))
            return prepare_result or {**self.prepared('blocked'),'tab_id':'fictional-tab',
                'blocker_reason':'tool_approval_required','blocker':'Fixture stops before any action'}
        runtime.transport.run=run
        return calls

    def test_fresh_open_binds_before_compact_prepare_and_stops_at_denial(self):
        task,runtime,monitor=self.configured();calls=self.scripted(task,runtime)
        task['browser']=monitor.prepare_browser(task,TaskLease(self.store,task))
        with self.assertRaises(BrowserNotReady) as caught:runtime.apply(self.store,task)
        self.assertEqual(caught.exception.reason,'tool_approval_required')
        self.assertEqual([c['role'] for c in calls],['application_tab_open','application_prepare'])
        context=calls[-1]['context']
        self.assertNotIn('numbered_text',context['applicant_evidence']['sources'][0])
        self.assertNotIn('records',context['shared_answer_snapshot'])
        self.assertNotIn('markdown',context['shared_answer_snapshot'])
        self.assertTrue(Path(context['shared_answer_snapshot']['reference']['path']).is_file())
        rules=Path(context['preparation_rules']['path']).read_text()
        self.assertIn('Do not invent product use',rules)
        self.assertIn('upload approval gate',rules)
        self.assertIn('markHandoff()',rules)
        for guard in ('BEFORE any fill','Do not retry denied or uncertain work','do not submit','exact bound tab_id'):
            self.assertIn(guard.lower(),calls[-1]['prompt'].lower())
        with self.store.transaction() as db:
            event=json.loads(db.execute("SELECT data FROM wf_process_events WHERE type='blocked' ORDER BY id DESC LIMIT 1").fetchone()[0])
            self.assertEqual(event['reason'],'tool_approval_required')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0],0)

    def test_bound_resume_uses_one_prepare_turn_and_no_false_health_success(self):
        task,runtime,monitor=self.configured();calls=self.scripted(task,runtime)
        lease=TaskLease(self.store,task)
        monitor.prepare_browser(task,lease)
        calls.clear()
        task['browser']=monitor.prepare_browser(task,TaskLease(self.store,task))
        self.assertEqual(calls,[])
        self.assertTrue(task['browser']['verification_deferred'])
        self.assertFalse(monitor.snapshot()['ready'])
        with self.assertRaises(BrowserNotReady):runtime.apply(self.store,task)
        self.assertEqual([c['role'] for c in calls],['application_prepare'])
        self.assertFalse(calls[0]['context']['browser']['application']['verified'])

    def test_deferred_result_cannot_claim_verified_or_escape_exact_binding(self):
        task,runtime,monitor=self.configured();self.scripted(task,runtime)
        lease=TaskLease(self.store,task);monitor.prepare_browser(task,lease)
        result=runtime.prepare_browser(task,lease)
        for changes in ({'verified':True},{'tab_id':'foreign'},{'binding_id':'foreign'},{'process_id':'foreign'},{'url':'https://different.invalid'}):
            with self.subTest(changes=changes):
                modified=copy.deepcopy(result);modified['application'].update(changes)
                with patch.object(runtime,'prepare_browser',return_value=modified),self.assertRaises(ReviewError):
                    monitor.prepare_browser(task,lease)

    def test_deferred_denial_and_foreign_prepare_result_never_clear_or_adopt(self):
        task,runtime,monitor=self.configured()
        calls=self.scripted(task,runtime,prepare_result={**self.prepared(),'tab_id':'foreign-tab'})
        lease=TaskLease(self.store,task);monitor.prepare_browser(task,lease)
        task['browser']=monitor.prepare_browser(task,lease)
        with self.assertRaises(ReviewError):runtime.apply(self.store,task)
        self.assertEqual('fictional-tab',self.store.application_tab_binding(task['id'],lease.token,'apply-1')['tab_id'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0],0)

    def test_tool_free_question_review_keeps_full_inline_evidence(self):
        task,runtime,monitor=self.configured();calls=self.scripted(task,runtime)
        evidence=runtime.evidence_snapshot(application=True)
        question={'id':'fictional-question','revision':1,'text':'Unconfirmed fixture question?',
                  'type':'text','required':True,'choices':[],'answer':None}
        context=runtime._application_context(self.store,task)
        runtime.review_questions(context,[question],{'fictional-question':'field'},evidence,TaskLease(self.store,task))
        self.assertEqual([c['role'] for c in calls],['application_question_review'])
        self.assertIn('numbered_text',calls[0]['context']['applicant_evidence']['sources'][0])
        self.assertNotIn('preparation_rules',calls[0]['context'])

    def test_uncertain_submission_cannot_defer_or_open_another_tab(self):
        task,runtime,monitor=self.configured();calls=self.scripted(task,runtime)
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown' WHERE id=?",(task['payload']['process_id'],))
        with self.assertRaises(ReviewError):monitor.prepare_browser(task,TaskLease(self.store,task))
        self.assertEqual(calls,[])
        self.assertEqual(self.store.processes()[0]['submission_status'],'submission_unknown')


if __name__=='__main__':unittest.main()
