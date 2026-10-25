"""Supported origin upload grants use current host facts, never form text."""
import copy
import tempfile
import unittest
from pathlib import Path

from orchestrator import TaskLease
from test_store import catalog
from upload_approval import ResumeUploadApproval, origin
from workflow import WorkflowStore


class UploadApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = WorkflowStore(self.root / 'test.sqlite3', low_watermark=0)
        self.store.grant_project_consent({'request_id': 'consent', 'policy_version': 1, 'accepted': True})
        self.store.import_catalog(catalog(), 'fixture')
        card = self.store.feed()['cards'][0]
        result = self.store.receive_decision({'request_id': 'apply', 'action': 'apply',
            'card_id': card['id'], 'card_revision': card['revision'], 'selected': [card['jobs'][0]['key']]})
        self.pid = result['process_ids'][0]
        self.task = self.store.lease_task('fixture', kinds=['apply'])
        process = self.store.start_application(self.pid, self.task['lease_token'])
        self.resume = self.root / 'resume.pdf'
        self.resume.write_bytes(b'%PDF-1.4\nfictional resume')
        self.lease = TaskLease(self.store, self.task)
        self.url = process['snapshot']['url']
        self.approval = ResumeUploadApproval(self.lease, self.pid, self.url, self.resume)
        self.request = {'serverName': 'cua_repl', 'mode': 'form', 'message': 'untrusted display text',
            'requestedSchema': {'type': 'object', 'properties': {}}, '_meta': {
                'codex_approval_kind': 'mcp_tool_call', 'connector_id': 'browser-use',
                'tool_name': 'upload_browser_files', 'file_transfer': 'upload',
                'origin': origin(self.url), 'tool_params': {'origin': origin(self.url)},
                'persist': ['session', 'always'], 'connector_name': 'Chrome', 'tool_title': 'Upload browser files'}}

    def test_exact_origin_current_resume_and_lease_grant_session_once(self):
        self.assertEqual({'action': 'accept', 'content': {}, '_meta': {'persist': 'session'}},
                         self.approval(self.request))
        self.assertEqual('cancel', self.approval(self.request)['action'])
        self.assertEqual(1, len(self.approval.decisions))
        self.assertEqual(self.approval.sha256, self.approval.decisions[0]['resume_sha256'])

    def test_unknown_or_strict_requests_are_never_accepted(self):
        cases = [dict(self.request, serverName='untrusted'), dict(self.request, mode='url'),
                 dict(self.request, mode='openai/userVerification'),
                 dict(self.request, requestedSchema={'type': 'object', 'properties': {'approve': {'type': 'boolean'}}})]
        for change in ({'codex_strict_auto_review': True}, {'approvals_reviewer': 'user'},
                       {'codex_request_type': 'approval_request'}, {'codex_requires_user_input': True},
                       {'origin': 'https://unapproved.example'}, {'tool_params': {'origin': 'https://other.example'}},
                       {'tool_name': 'access_browser_origin'}, {'file_transfer': 'download'}, {'persist': 'always'}):
            altered = copy.deepcopy(self.request)
            altered['_meta'].update(change)
            cases.append(altered)
        for request in cases:
            with self.subTest(request=request):
                self.assertEqual({'action': 'cancel', 'content': None}, self.approval(request))
        self.assertEqual([], self.approval.decisions)
        self.assertEqual(len(cases), len(self.approval.rejections))
        self.assertTrue(all(set(item) == {'reason', 'mismatches', 'at'}
                            for item in self.approval.rejections))

    def test_rejections_are_bounded_and_do_not_copy_untrusted_values(self):
        request = copy.deepcopy(self.request)
        request['_meta']['origin'] = 'https://unapproved.example/secret'
        request['message'] = 'private form text'
        for _ in range(40):
            self.assertEqual('cancel', self.approval(request)['action'])
        self.assertEqual(32, len(self.approval.rejections))
        self.assertEqual(['origin'], self.approval.rejections[0]['mismatches'])
        self.assertNotIn('secret', repr(self.approval.rejections))
        self.assertNotIn('private form text', repr(self.approval.rejections))

    def test_changed_resume_is_cancelled(self):
        self.resume.write_bytes(b'%PDF-1.4\nchanged')
        self.assertEqual('cancel', self.approval(self.request)['action'])

    def test_native_provenance_fields_do_not_change_approval_authority(self):
        self.request['_meta'].update({'x-codex-turn-metadata': 'opaque private provenance',
                                      'callId': 'opaque tool call'})
        strict = copy.deepcopy(self.request)
        strict['_meta']['codex_strict_auto_review'] = True
        self.assertEqual('cancel', self.approval(strict)['action'])
        self.assertEqual('accept', self.approval(self.request)['action'])
        self.assertNotIn('opaque', repr(self.approval.decisions))

    def test_symlink_cannot_replace_verified_resume(self):
        other = self.root / 'other.pdf'
        other.write_bytes(self.resume.read_bytes())
        self.resume.unlink()
        self.resume.symlink_to(other)
        self.assertEqual('cancel', self.approval(self.request)['action'])

    def test_revoked_consent_and_stale_lease_cancel(self):
        with self.store.transaction(True) as db:
            db.execute('DELETE FROM wf_project_consent')
        self.assertEqual('cancel', self.approval(self.request)['action'])
        self.store.grant_project_consent({'request_id': 'renew', 'policy_version': 1, 'accepted': True})
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET lease_until='2000-01-01' WHERE id=?", (self.task['id'],))
        self.assertEqual('cancel', self.approval(self.request)['action'])

    def test_submission_attempt_never_grants_upload_again(self):
        self.store.before_submit(self.pid, self.task['lease_token'], 'fictional-form')
        self.assertEqual('cancel', self.approval(self.request)['action'])

    def test_changed_job_authorization_cancels(self):
        with self.store.transaction(True) as db:
            db.execute('UPDATE companies SET excluded=1')
        self.assertEqual('cancel', self.approval(self.request)['action'])
