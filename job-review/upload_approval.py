"""Scope supported Chrome upload elicitations to the current approved task.

Chrome's permission is origin-scoped, not file-scoped. This host approves it only
during resume preparation, under a current job lease and a verified resume pin.
It never approves URL challenges, device verification, shell or generic tools.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

from store import now


def origin(url):
    value = urlsplit(url)
    if value.scheme != 'https' or not value.hostname or value.username or value.password:
        raise ValueError('Resume destination must be an HTTPS origin.')
    return 'https://' + value.hostname + (':' + str(value.port) if value.port not in {None, 443} else '')


def fingerprint(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 20 * 1024 * 1024:
            raise ValueError('Approved resume must be a bounded regular file.')
        content = stream.read(20 * 1024 * 1024 + 1)
    return hashlib.sha256(content).hexdigest()


class ResumeUploadApproval:
    """A host object, never constructed from model or employer-provided text."""
    def __init__(self, lease, process_id, approved_url, resume):
        self.lease, self.process_id = lease, process_id
        self.origin = origin(approved_url)
        self.resume = Path(resume)
        self.sha256 = fingerprint(self.resume)
        self.decisions = []
        self.rejections = []

    def _reject(self, reason, *, mismatches=()):
        # Fixed categories and host-defined field names only. Never copy an
        # elicitation message, URL, schema value, or arbitrary metadata to logs.
        if len(self.rejections) < 32:
            self.rejections.append({'reason': reason, 'mismatches': list(mismatches), 'at': now()})
        return {'action': 'cancel', 'content': None}

    def _current(self):
        self.lease.heartbeat()
        store = self.lease.store
        store.require_project_consent()
        with store.transaction() as db:
            process, row = store._application_lease(db, self.process_id, self.lease.token)
            store._check_application(db, process)
            if process['submission_status'] != 'preparing' or row['id'] != self.lease.task_id:
                raise ValueError('Resume upload is outside preparation.')
            if (origin(json.loads(process['snapshot'])['url']) != self.origin
                    or json.loads(row['checkpoint']).get('submission_started')):
                raise ValueError('Resume preparation authority changed.')
            if db.execute('SELECT 1 FROM wf_submission_attempts WHERE process_id=?', (self.process_id,)).fetchone():
                raise ValueError('Resume upload cannot resume a submission attempt.')
        if fingerprint(self.resume) != self.sha256:
            raise ValueError('The approved resume changed during preparation.')

    def __call__(self, request):
        response = {'action': 'cancel', 'content': None}
        if any(item['action'] == 'accept' for item in self.decisions):
            return self._reject('already_accepted')
        if not isinstance(request, dict) or request.get('serverName') != 'cua_repl':
            return self._reject('unsupported_server')
        if request.get('mode') not in {'form', 'openai/form', 'openaiForm'}:
            return self._reject('unsupported_mode')
        meta = request.get('_meta')
        if not isinstance(meta, dict):
            return self._reject('missing_metadata')
        expected = {'codex_approval_kind': 'mcp_tool_call', 'connector_id': 'browser-use',
                    'tool_name': 'upload_browser_files', 'file_transfer': 'upload',
                    'origin': self.origin, 'tool_params': {'origin': self.origin}}
        mismatches = [key for key, value in expected.items() if meta.get(key) != value]
        if mismatches:
            return self._reject('metadata_mismatch', mismatches=mismatches)
        # The installed node_repl host adds these two provenance fields. They
        # are opaque diagnostics, never approval authority or response content.
        transport_keys = {'x-codex-turn-metadata', 'callId'}
        if set(meta) - set(expected) - {'persist', 'connector_name', 'tool_title'} - transport_keys:
            return self._reject('unsupported_metadata')
        if meta.get('persist') not in ('session', ['session', 'always']):
            return self._reject('unsupported_persistence')
        # This is the installed CUA createElicitation default for uploads.
        # No text/schema supplied by a website can expand the accepted contract.
        if request.get('requestedSchema') != {'type': 'object', 'properties': {}}:
            schema = request.get('requestedSchema')
            diagnostic = []
            if isinstance(schema, dict):
                if '$schema' in schema and schema['$schema'] is None:
                    diagnostic.append('null_schema_uri')
                if 'required' in schema and schema['required'] is None:
                    diagnostic.append('null_required')
                if schema.get('required') == []:
                    diagnostic.append('empty_required')
                if set(schema) - {'type', 'properties', '$schema', 'required'}:
                    diagnostic.append('other_schema_fields')
            return self._reject('unsupported_schema', mismatches=diagnostic)
        try:
            self._current()
        except Exception as error:
            self.decisions.append({'action': 'cancel', 'reason': type(error).__name__, 'at': now()})
            return response
        self.decisions.append({'action': 'accept', 'origin': self.origin, 'resume_sha256': self.sha256,
                               'task_id': self.lease.task_id, 'at': now(), 'persist': 'session'})
        return {'action': 'accept', 'content': {}, '_meta': {'persist': 'session'}}
