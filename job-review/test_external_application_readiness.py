"""Fictional exact-job readiness; no browser, network or production state."""
import json
import unittest
from unittest.mock import patch

import test_application_tab_affinity as fixtures
import test_codex_runtime as runtime_fixtures
from orchestrator import TaskLease
from readiness import ReadinessMonitor, BrowserNotReady
from store import ReviewError


class ExternalApplicationReadinessTests(unittest.TestCase):
    setUp=fixtures.ApplicationTabRuntimeTests.setUp
    task=fixtures.ApplicationTabRuntimeTests.task
    runtime=fixtures.ApplicationTabRuntimeTests.runtime

    def subject(self):
        task=self.task();runtime=self.runtime([]);runtime.application_pool_size=3
        calls=[]
        def native(role,prompt,schema,lease,**kwargs):
            calls.append((role,prompt))
            self.assertIn('application',role)
            if role=='application_tab_open':
                result,path=fixtures.opening_receipt(self.root,task)
                lease._last_native_browser_receipt=path
                return result
            binding=self.store.application_tab_binding(task['id'],lease.token,'apply-1',required=True)
            return {'outcome':'ready','tab_id':binding['tab_id'],'job_url':binding['job_url'],
                'handoff_preserved':True,'blocker':'','blocker_reason':''}
        runtime.transport.run=native
        monitor=ReadinessMonitor(self.store,runtime)
        self.addCleanup(monitor.close)
        return task,runtime,TaskLease(self.store,task),monitor,calls

    def test_external_job_opens_only_its_tab_and_does_not_require_linkedin(self):
        task,runtime,lease,monitor,calls=self.subject()
        result=monitor.prepare_browser(task,lease)
        self.assertEqual(['application_tab_open'],[role for role,prompt in calls])
        self.assertEqual('application',result['scope'])
        self.assertNotIn('linkedin',result)
        health=monitor.snapshot()
        self.assertTrue(health['ready'])
        self.assertEqual('application',health['operation_scope'])
        self.assertEqual('on_demand',health['checks'][2]['status'])
        self.assertEqual('passed',health['checks'][1]['status'])
        self.assertIn(task['job_url'],calls[0][1])
        self.assertIn('separate LinkedIn preflight',calls[0][1])

    def test_continuation_defers_exact_lookup_to_prepare_without_claiming_ready(self):
        task,runtime,lease,monitor,calls=self.subject()
        first=monitor.prepare_browser(task,lease)
        resumed=TaskLease(self.store,task)
        second=monitor.prepare_browser(task,resumed)
        self.assertEqual(first['application']['binding_id'],second['application']['binding_id'])
        self.assertEqual(['application_tab_open'],[role for role,prompt in calls])
        self.assertTrue(second['verification_deferred'])
        self.assertFalse(second['application']['verified'])
        self.assertFalse(second['chrome']['attached'])
        self.assertFalse(monitor.snapshot()['ready'])
        self.assertEqual('on_demand',monitor.snapshot()['checks'][1]['status'])

    def test_employer_denial_stops_without_linkedin_or_fallback_and_keeps_scope(self):
        task,runtime,lease,monitor,calls=self.subject()
        def denied(role,prompt,schema,lease,**kwargs):
            calls.append((role,prompt))
            return {'outcome':'blocked','tab_id':'','job_url':task['job_url'],
                'created_new':False,'handoff_preserved':False,'blocker':'Fictional employer origin denied.',
                'blocker_reason':'tool_approval_required'}
        runtime.transport.run=denied
        with self.assertRaises(BrowserNotReady) as caught:monitor.prepare_browser(task,lease)
        self.assertEqual('tool_approval_required',caught.exception.reason)
        self.assertEqual(['application_tab_open'],[role for role,prompt in calls])
        self.assertIsNone(self.store.application_tab_binding(task['id'],lease.token,'apply-1'))
        self.assertEqual('on_demand',monitor.snapshot()['checks'][2]['status'])
        with self.store.transaction() as db:
            event=json.loads(db.execute("SELECT data FROM wf_process_events WHERE type='blocked' ORDER BY id DESC LIMIT 1").fetchone()[0])
            self.assertEqual('application_tab_open',event['host_stage'])
            self.assertEqual(task['job_url'],event['target_url'])
            self.assertEqual(0,db.execute('SELECT count(*) FROM wf_submission_attempts').fetchone()[0])

    def test_foreign_readiness_result_is_rejected_without_adopting(self):
        task,runtime,lease,monitor,calls=self.subject()
        monitor.prepare_browser(task,lease)
        deferred=runtime.prepare_browser(task,lease)
        deferred['application']['tab_id']='foreign-tab'
        runtime.prepare_browser=lambda *args:deferred
        with self.assertRaises(ReviewError):monitor.prepare_browser(task,lease)
        self.assertEqual('fictional-tab',self.store.application_tab_binding(task['id'],lease.token,'apply-1')['tab_id'])

    def test_contradictory_opened_and_employer_denial_never_binds(self):
        task,runtime,lease,monitor,calls=self.subject()
        def contradictory(role,prompt,schema,lease,**kwargs):
            result,path=fixtures.opening_receipt(self.root,task)
            lease._last_native_browser_receipt=path
            return {**result,'blocker_reason':'tool_approval_required','blocker':'Fictional origin denied.'}
        runtime.transport.run=contradictory
        with self.assertRaises(BrowserNotReady) as error:monitor.prepare_browser(task,lease)
        self.assertEqual('tool_approval_required',error.exception.reason)
        self.assertIsNone(self.store.application_tab_binding(task['id'],lease.token,'apply-1'))

    def test_partial_opening_parks_and_preserves_tab_without_automatic_retry(self):
        task,runtime,lease,monitor,calls=self.subject()
        runtime.transport.run=lambda *args,**kwargs:{'outcome':'blocked','tab_id':'partial-created-tab',
            'job_url':task['job_url'],'created_new':True,'handoff_preserved':False,
            'blocker':'Fictional handoff failure after creation.','blocker_reason':'browser_tab_unavailable'}
        with self.assertRaises(BrowserNotReady) as error:monitor.prepare_browser(task,lease)
        self.assertEqual('browser_action_required',error.exception.reason)
        with self.store.transaction() as db:
            event=json.loads(db.execute("SELECT data FROM wf_process_events WHERE type='blocked' ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual('partial-created-tab',event['partial_tab_id'])
        self.assertTrue(event['created_new'])
        self.assertEqual('browser_tab_unavailable',event['observed_reason'])
        self.assertIsNone(self.store.application_tab_binding(task['id'],lease.token,'apply-1'))

    def test_contradictory_ready_and_employer_denial_never_reports_ready(self):
        task,runtime,lease,monitor,calls=self.subject()
        first=monitor.prepare_browser(task,lease)
        runtime.prepare_browser=lambda *args:{**first,'blocker':'Fictional origin denied.',
            'blocker_reason':'tool_approval_required'}
        with self.assertRaises(BrowserNotReady) as error:monitor.prepare_browser(task,lease)
        self.assertEqual('tool_approval_required',error.exception.reason)
        self.assertFalse(monitor.snapshot()['ready'])

    def test_claimed_attached_state_without_host_binding_is_rejected(self):
        task,runtime,lease,monitor,calls=self.subject()
        runtime.prepare_browser=lambda *args:{'scope':'application','chrome':{'attached':True,'session_id':'chrome:current-profile'},
            'application':{'verified':True,'tab_id':'fake','binding_id':'fake','process_id':task['payload']['process_id'],'url':task['job_url']}}
        with self.assertRaises(ReviewError):monitor.prepare_browser(task,lease)

    def test_linkedin_job_and_discovery_keep_existing_linkedin_gate(self):
        task,runtime,lease,monitor,calls=self.subject()
        pid=task['payload']['process_id']
        for hostname in ('www.linkedin.com','linkedin.com.','lnkd.in','www.lnkd.in'):
            with self.subTest(hostname=hostname),self.store.transaction(True) as db:
                snapshot=json.loads(db.execute('SELECT snapshot FROM wf_processes WHERE id=?',(pid,)).fetchone()[0])
                snapshot['url']='https://'+hostname+'/jobs/view/123'
                db.execute('UPDATE wf_processes SET snapshot=? WHERE id=?',(json.dumps(snapshot),pid))
            with patch.object(self.store,'_check_application'):
                self.assertEqual('linkedin',runtime.browser_readiness_scope(task,lease))
        with patch.object(self.store,'_check_application'):
            self.assertEqual('linkedin',runtime.browser_readiness_scope(task,lease))
            runtime.transport.run=lambda role,*args,**kwargs: calls.append((role,'')) or runtime_fixtures.BROWSER
            result=runtime.prepare_browser(task,lease)
            self.assertEqual('linkedin_session',calls[-1][0])
            self.assertTrue(result['linkedin']['authenticated'])
        self.assertEqual('linkedin',runtime.browser_readiness_scope({'kind':'explore'},lease))

    def test_unapproved_job_cannot_change_scope_to_skip_authority(self):
        task,runtime,lease,monitor,calls=self.subject()
        with patch.object(self.store,'_check_application',side_effect=ReviewError('Fictional revoked approval',409)):
            with self.assertRaises(ReviewError):monitor.prepare_browser(task,lease)
        self.assertFalse(calls)


if __name__=='__main__':unittest.main()
