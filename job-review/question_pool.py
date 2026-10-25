"""Shared question registry and crash-repairable Markdown answer publication.

The host owns the lock and all writes. An applying worker may reason while its
bounded lock is held, but no SQLite write transaction spans that reasoning.
SQLite is authoritative; Markdown files are private, reproducible projections.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
import uuid

from answer_reuse import normalize, reuse_scope
from store import ReviewError, encode, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS wf_qpool_lock (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), token TEXT, owner_kind TEXT,
 owner_id TEXT, acquired_at TEXT, expires_at TEXT, generation INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO wf_qpool_lock(singleton) VALUES(1);
CREATE TABLE IF NOT EXISTS wf_qpool_waiters (
 token TEXT PRIMARY KEY, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wf_qpool_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL DEFAULT 0,
 answer_revision INTEGER NOT NULL DEFAULT 0, published_revision INTEGER NOT NULL DEFAULT -1);
INSERT OR IGNORE INTO wf_qpool_state(singleton) VALUES(1);
CREATE TABLE IF NOT EXISTS wf_qpool_questions (
 id TEXT PRIMARY KEY, record TEXT NOT NULL, scope_kind TEXT NOT NULL,
 scope_key TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wf_qpool_links (
 question_id TEXT PRIMARY KEY, pool_id TEXT NOT NULL REFERENCES wf_qpool_questions(id),
 process_id TEXT NOT NULL, worker_id TEXT NOT NULL, form_version TEXT NOT NULL,
 question_revision INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS wf_qpool_link_pool ON wf_qpool_links(pool_id);
CREATE TABLE IF NOT EXISTS wf_qpool_answers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, pool_id TEXT NOT NULL REFERENCES wf_qpool_questions(id),
 source_question_id TEXT NOT NULL, answer_revision INTEGER NOT NULL,
 value TEXT NOT NULL, labels TEXT NOT NULL, markdown TEXT NOT NULL,
 source TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(source_question_id,answer_revision));
CREATE TABLE IF NOT EXISTS wf_qpool_batches (
 id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
 records TEXT NOT NULL, created_at TEXT NOT NULL);
"""
_START = '<!-- job-review-answer '
_END = '<!-- /job-review-answer -->'


def _deadline(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='microseconds')


def _hash_bytes(data):
    try:
        import blake3
    except ImportError as error:
        raise ReviewError('BLAKE3 dependency missing; install requirements-pool.txt before enabling the pool.',
                          503, reason='question_pool_hash_unavailable') from error
    return blake3.blake3(data).hexdigest()


def _sha(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ReviewError('Question projection must not be a symbolic link.', 409)
    temporary = path.parent / ('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


class SharedQuestionPool:
    def __init__(self, store, directory=None):
        self.store = store
        self.directory = Path(directory) if directory else Path(store.path).parent / 'question-pool'
        self._local = threading.local()
        self._signing_key = secrets.token_bytes(32)
        self._drafts = {}
        self._draft_lock = threading.Lock()

    def initialize(self):
        """Explicit additive migration; constructing this object never mutates DB."""
        with self.store.connect() as db:
            db.executescript(SCHEMA)
        return self

    def _assert(self, db, token=None):
        token = token or getattr(self._local, 'token', None)
        row = db.execute('SELECT * FROM wf_qpool_lock WHERE singleton=1').fetchone()
        if not token or row['token'] != token or row['expires_at'] <= now():
            raise ReviewError('Question-pool lock expired or belongs to another owner.', 409,
                              reason='question_pool_lock_stale')
        return row

    @contextmanager
    def hold(self, owner_kind, owner_id, *, timeout_seconds=30, lease_seconds=120):
        if owner_kind not in {'worker', 'answer_handler', 'recovery'} or not isinstance(owner_id, str) or not owner_id:
            raise ValueError('A question-pool owner kind and ID are required.')
        if getattr(self._local, 'token', None):
            raise ReviewError('Question-pool lock is not reentrant.', 409)
        for value in (timeout_seconds, lease_seconds):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Lock timeout and lease must be positive finite seconds.')
        token = str(uuid.uuid4())
        until = time.monotonic() + timeout_seconds
        with self.store.transaction(True) as db:
            db.execute('INSERT INTO wf_qpool_waiters VALUES(?,?,?,?)',
                       (token, owner_kind, owner_id, _deadline(timeout_seconds + 1)))
        acquired = False
        try:
            while True:
                with self.store.transaction(True) as db:
                    stamp = now()
                    result = db.execute('''UPDATE wf_qpool_lock SET token=?,owner_kind=?,owner_id=?,
                        acquired_at=?,expires_at=?,generation=generation+1
                        WHERE singleton=1 AND (token IS NULL OR expires_at<=?)''',
                        (token, owner_kind, owner_id, stamp, _deadline(lease_seconds), stamp))
                    if result.rowcount:
                        db.execute('DELETE FROM wf_qpool_waiters WHERE token=?', (token,))
                        acquired = True
                if acquired:
                    break
                if time.monotonic() >= until:
                    raise ReviewError('Question pool is busy. Retry after its current operation stops.',
                                      409, reason='question_pool_busy')
                time.sleep(min(.025, max(0, until - time.monotonic())))
            self._local.token = token
            self.repair()
            yield token
        finally:
            self._local.token = None
            with self.store.transaction(True) as db:
                db.execute('DELETE FROM wf_qpool_waiters WHERE token=?', (token,))
                db.execute('''UPDATE wf_qpool_lock SET token=NULL,owner_kind=NULL,owner_id=NULL,
                    acquired_at=NULL,expires_at=NULL WHERE singleton=1 AND token=?''', (token,))

    def check(self, token=None):
        with self.store.transaction() as db:
            self._assert(db, token)
        return True

    def renew(self, token=None, *, lease_seconds=120):
        if not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError('Lock lease must be positive finite seconds.')
        with self.store.transaction(True) as db:
            self._assert(db, token)
            db.execute('UPDATE wf_qpool_lock SET expires_at=? WHERE singleton=1', (_deadline(lease_seconds),))

    def lock_snapshot(self):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM wf_qpool_lock WHERE singleton=1').fetchone()
            active = row['token'] is not None and row['expires_at'] > now()
            waiters = [r[0] for r in db.execute('''SELECT DISTINCT owner_id FROM wf_qpool_waiters
                WHERE owner_kind='worker' AND expires_at>? ORDER BY owner_id''', (now(),))]
            return {'state': 'held' if active else 'free',
                    'owner_kind': row['owner_kind'] if active else None,
                    'owner_id': row['owner_id'] if active else None,
                    'acquired_at': row['acquired_at'] if active else None,
                    'waiting_worker_ids': waiters}

    @staticmethod
    def _bump(db, answers=False):
        db.execute('UPDATE wf_qpool_state SET revision=revision+1,answer_revision=answer_revision+? WHERE singleton=1',
                   (int(answers),))

    def _scope(self, question, process, proposed=None):
        kind = reuse_scope(question['text'])
        snapshot = json.loads(process['snapshot'])
        expected = {'kind': kind, 'key': '' if kind == 'personal' else
                    snapshot['company_id'] if kind == 'company' else process['id']}
        if proposed is not None and proposed != expected:
            # Explicit application-only disclosures narrow a normally portable
            # question. They must never be promoted back to personal scope.
            if proposed == {'kind': 'application', 'key': process['id']}:
                return proposed
            raise ReviewError('Question reuse scope does not match its confirmed context.', 409)
        return expected

    def register_questions(self, process_id, worker_id, questions, *, matches=None, scopes=None):
        """Register existing form questions; matching judgment is supplied by caller.

        `matches` maps local question IDs to existing pool IDs. All known blockers
        must be passed together. No DB transaction spans caller model reasoning.
        """
        if not isinstance(questions, list) or not questions or len(questions) > 50:
            raise ReviewError('Register between 1 and 50 questions.')
        matches, scopes = matches or {}, scopes or {}
        ids = [q if isinstance(q, str) else q.get('id') for q in questions]
        if len(ids) != len(set(ids)) or set(matches) - set(ids) or set(scopes) - set(ids):
            raise ReviewError('Question mappings must refer to unique current questions.')
        mappings = {}
        with self.store.transaction(True) as db:
            self._assert(db)
            process = self.store._process(db, process_id)
            for qid in ids:
                question = db.execute('SELECT * FROM wf_questions WHERE id=? AND process_id=? AND superseded=0',
                                      (qid, process_id)).fetchone()
                if question is None or question['form_version'] != process['form_version']:
                    raise ReviewError('Cannot register a stale form question.', 409)
                scope = self._scope(question, process, scopes.get(qid))
                prior = db.execute('SELECT * FROM wf_qpool_links WHERE question_id=?', (qid,)).fetchone()
                if prior:
                    if matches.get(qid, prior['pool_id']) != prior['pool_id']:
                        raise ReviewError('An existing question link cannot be silently changed.', 409)
                    mappings[qid] = prior['pool_id']
                    continue
                record = {'text': question['text'], 'type': question['type'],
                          'choices': json.loads(question['choices']), 'required': bool(question['required'])}
                pool_id = matches.get(qid)
                if pool_id:
                    target = db.execute('SELECT * FROM wf_qpool_questions WHERE id=?', (pool_id,)).fetchone()
                    if target is None or (target['scope_kind'], target['scope_key']) != (scope['kind'], scope['key']):
                        raise ReviewError('A question cannot link outside its reuse scope.', 409)
                    old = json.loads(target['record'])
                    if (old['type'] != record['type'] or
                            {normalize(x['label']) for x in old['choices']} !=
                            {normalize(x['label']) for x in record['choices']}):
                        raise ReviewError('Linked questions must have compatible answer choices.', 409)
                else:
                    pool_id = str(uuid.uuid4())
                    db.execute('INSERT INTO wf_qpool_questions VALUES(?,?,?,?,?)',
                               (pool_id, encode(record), scope['kind'], scope['key'], now()))
                db.execute('INSERT INTO wf_qpool_links VALUES(?,?,?,?,?,?,?)',
                           (qid, pool_id, process_id, worker_id, question['form_version'], question['revision'], now()))
                mappings[qid] = pool_id
            self._bump(db)
            self._publish(db)
        return {'mappings': mappings, 'answer_requests': self.linked_answer_requests(process_ids=[process_id])}

    def pending_questions(self):
        with self.store.transaction() as db:
            self._assert(db)
            result = []
            for row in db.execute('''SELECT p.* FROM wf_qpool_questions p
                WHERE NOT EXISTS(SELECT 1 FROM wf_qpool_answers a WHERE a.pool_id=p.id)
                AND EXISTS(SELECT 1 FROM wf_qpool_links l JOIN wf_questions q ON q.id=l.question_id
                    WHERE l.pool_id=p.id AND q.superseded=0 AND q.answer IS NULL) ORDER BY p.created_at,p.id'''):
                result.append({'id': row['id'], **json.loads(row['record']),
                    'scope': {'kind': row['scope_kind'], 'key': row['scope_key']},
                    'links': [dict(l) for l in db.execute('SELECT * FROM wf_qpool_links WHERE pool_id=?', (row['id'],))]})
            return result

    @staticmethod
    def _request_receipt(db, payload):
        if (not isinstance(payload, dict) or not isinstance(payload.get('request_id'), str)
                or not payload['request_id'].strip() or len(payload['request_id']) > 200):
            raise ReviewError('A valid answer request ID is required.')
        previous = db.execute('SELECT * FROM wf_answer_requests WHERE id=?', (payload['request_id'],)).fetchone()
        if previous is None:
            return None
        evidence = db.execute('SELECT 1 FROM wf_answer_evidence WHERE request_id=?', (payload['request_id'],)).fetchone()
        if evidence is not None or previous['payload_hash'] != _sha(payload):
            raise ReviewError('Request ID was reused with different answers.', 409, reason='request_id_conflict')
        return json.loads(previous['receipt'])

    def _validate_payload(self, db, payload):
        # Called before formatting and again under the shared writer lock. A
        # client revision is never authorization outside that second check.
        from question_sync import canonical_binding, mapped_answer
        self._request_receipt(db, payload)
        revision = db.execute('SELECT revision FROM wf_qpool_state WHERE singleton=1').fetchone()[0]
        expected = payload.get('expected_pool_revision')
        if 'expected_pool_revision' in payload and (type(expected) is not int or expected < 0):
            raise ReviewError('expected_pool_revision must be a nonnegative integer.')
        process = self.store._process(db, payload.get('process_id'))
        if payload.get('form_version') != process['form_version']:
            raise ReviewError('Question form changed before formatting.', 409)
        answers = payload.get('answers')
        if not isinstance(answers, list) or not 1 <= len(answers) <= 50:
            raise ReviewError('Provide between 1 and 50 explicit user answers.')
        records, seen, conflicting_revision = [], set(), False
        for answer in answers:
            if not isinstance(answer, dict):
                raise ReviewError('Each answer must be an object.')
            qid = answer.get('question_id')
            question = db.execute('SELECT * FROM wf_questions WHERE id=? AND process_id=? AND superseded=0',
                                  (qid, process['id'])).fetchone()
            if question is None or qid in seen or question['form_version'] != process['form_version']:
                raise ReviewError('Answer refers to a missing, duplicate, or stale question.', 409)
            seen.add(qid)
            value = self.store._validate_answer(question, answer.get('value'))
            link, accepted = canonical_binding(db, qid)
            # A linked form's stored answer is only a compatibility projection.
            # In particular, a held fan-out cannot reopen a canonical answer.
            saved = mapped_answer(question, accepted) if link else (
                json.loads(question['answer']) if question['answer'] is not None else None)
            same_answer = saved is not None and (set(saved) == set(value)
                if question['type'] == 'multi_choice' else saved == value)
            stale = question['revision'] != answer.get('question_revision')
            if stale and not same_answer:
                conflicting_revision = True
            if not stale:
                self.store._revision(question, answer.get('question_revision'))
            local = json.loads(question['answer']) if question['answer'] is not None else None
            if accepted is not None and not same_answer and local != saved and expected is None:
                raise ReviewError('The shared answer changed. Refresh before correcting.', 409,
                                  reason='question_sync_stale')
            choices = json.loads(question['choices'])
            labels = {x['id']: x['label'] for x in choices}
            selected = value if isinstance(value, list) else [value]
            records.append({'question_id': qid, 'question_revision': question['revision'] - 1 if same_answer else question['revision'],
                'already_answered': same_answer, 'question_text': question['text'], 'value': value,
                'labels': [labels[x] for x in selected] if choices else [value],
                'scope': self._scope(question, process, payload.get('pool_scopes', {}).get(qid)), 'process_id': process['id'],
                'form_version': process['form_version']})
        if not all(record['already_answered'] for record in records):
            if expected is not None and expected != revision:
                raise ReviewError('Shared questions changed. Refresh and review the saved answers.', 409,
                                  reason='question_sync_stale')
            if conflicting_revision:
                raise ReviewError('Question revision changed and the answer conflicts. Refresh before correcting.',
                                  409, reason='question_sync_stale')
            self.store._check_application(db, process)
            self.store._revision(process, payload.get('process_revision'))
        return records

    @staticmethod
    def _body(record):
        # Escape through JSON string notation in a code span, preserving exact
        # user words even when they include Markdown or apparent instructions.
        question = encode(record['question_text']).replace('`', '\\u0060')
        answer = encode(record['labels']).replace('`', '\\u0060')
        scope = encode(record['scope']).replace('`', '\\u0060')
        return f"## Question `{record['question_id']}`\n\nQuestion: `{question}`\n\nAnswer: `{answer}`\n\nScope: `{scope}`\n"

    def formatting_spec(self, payload):
        """Prepare trusted, signed context before the caller's lightweight LLM.

        This method reads existing form state, not the shared question pool.
        The formatter returns exact append_text; it has no key or write authority.
        """
        with self.store.transaction() as db:
            replay = self._request_receipt(db, payload)
            if replay is not None:
                return {'already_answered': True, 'replayed': True, 'receipt': replay}
            records = self._validate_payload(db, payload)
            if all(record['already_answered'] for record in records):
                process = self.store._process(db, payload['process_id'])
                return {'already_answered': True, 'receipt': {'accepted': True,
                    'already_answered': True, 'request_id': payload['request_id'],
                    'process_ids': [process['id']], 'task_ids': [process['task_id']]}}
        batch_id = str(uuid.uuid4())
        draft = {'payload': json.loads(encode(payload)), 'records': records, 'created_at': now()}
        with self._draft_lock:
            self._drafts = {k: v for k, v in self._drafts.items() if v['created_at'] > _deadline(-3600)}
            if len(self._drafts) >= 1000:
                raise ReviewError('Too many pending answer-formatting requests.', 503)
            self._drafts[batch_id] = draft
        blocks = []
        for index, record in enumerate(records):
            signature = hmac.new(self._signing_key, encode([batch_id, index, draft]).encode(), hashlib.sha256).hexdigest()
            metadata = {'batch_id': batch_id, 'index': index, 'signature': signature}
            blocks.append(_START + encode(metadata) + ' -->\n' + self._body(record) + _END + '\n')
        return {'batch_id': batch_id, 'records': records, 'append_text': '\n'.join(blocks),
                'instruction': 'Return the exact provided Markdown append_text. Preserve every word, ID, value, scope, and metadata marker. Do not add facts or commentary.'}

    describe_answer_batch = formatting_spec

    def _parse_append(self, append_text):
        if not isinstance(append_text, str) or not append_text or len(append_text.encode()) > 1_000_000:
            raise ReviewError('Answered Markdown must be a nonempty bounded string.')
        pattern = re.compile(r'<!-- job-review-answer (\{[^\n]+\}) -->\n(.*?)<!-- /job-review-answer -->\n?', re.DOTALL)
        blocks = list(pattern.finditer(append_text))
        if not blocks or pattern.sub('', append_text).strip():
            raise ReviewError('Answered Markdown contains unrecognized content.')
        batch_id, records, markdowns = None, [], []
        for index, block in enumerate(blocks):
            try:
                metadata = json.loads(block.group(1))
            except (ValueError, TypeError) as error:
                raise ReviewError('Answered Markdown metadata is not valid JSON.') from error
            if not isinstance(metadata, dict) or set(metadata) != {'batch_id', 'index', 'signature'} or metadata['index'] != index:
                raise ReviewError('Answered Markdown metadata is invalid.')
            batch_id = batch_id or metadata['batch_id']
            if metadata['batch_id'] != batch_id:
                raise ReviewError('One append operation must contain one complete answer batch.')
            with self._draft_lock:
                draft = self._drafts.get(batch_id)
            # Committed staging records permit retry after a process restart.
            if draft is None:
                with self.store.transaction() as db:
                    staged = db.execute('SELECT * FROM wf_qpool_batches WHERE id=?', (batch_id,)).fetchone()
                if staged is None:
                    raise ReviewError('Answer formatting context expired. Regenerate from the user answer.', 409)
                saved = json.loads(staged['records'])
                if index >= len(saved) or saved[index]['full_markdown'] != block.group(0).rstrip() + '\n':
                    raise ReviewError('A staged answer append cannot be changed.', 409)
                record = saved[index]['record']
            else:
                if index >= len(draft['records']):
                    raise ReviewError('Unexpected answer record.')
                signature = hmac.new(self._signing_key, encode([batch_id, index, draft]).encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, str(metadata['signature'])):
                    raise ReviewError('The formatter changed trusted answer metadata.', 409)
                record = draft['records'][index]
            if block.group(2) != self._body(record):
                raise ReviewError('The formatter changed the confirmed question, answer, or scope.', 409)
            records.append(record)
            markdowns.append(block.group(0).rstrip() + '\n')
        if draft is None:
            payload = json.loads(staged['payload'])
            expected_count = len(json.loads(staged['records']))
        else:
            payload, expected_count = draft['payload'], len(draft['records'])
        if len(records) != expected_count:
            raise ReviewError('The formatter omitted an answer.')
        return batch_id, payload, records, markdowns

    def save_answered_questions(self, append_text):
        """Single-input pure-code writer, called while the answer handler holds lock."""
        batch_id, payload, records, markdowns = self._parse_append(append_text)
        with self.store.transaction(True) as db:
            self._assert(db)
            prior = db.execute('SELECT * FROM wf_qpool_batches WHERE id=?', (batch_id,)).fetchone()
            replay = self._request_receipt(db, payload)
            current_records = [] if replay is not None else self._validate_payload(db, payload)
            already_answered = replay is not None or all(r['already_answered'] for r in current_records)
            if prior is None:
                db.execute('INSERT INTO wf_qpool_batches VALUES(?,?,?,?,?)',
                    (batch_id, _sha(payload), encode(payload), encode([{'record': r, 'full_markdown': m}
                        for r, m in zip(records, markdowns)]), now()))
        # Existing answer API remains the authority for form/consent/submission
        # guards. It is idempotent by request_id, including crash recovery.
        if not already_answered:
            self._apply_answer_payload(payload)
        with self.store.transaction(True) as db:
            self._assert(db)
            for record, markdown in zip(records, markdowns):
                if not already_answered and not record.get('already_answered'):
                    self._record_answer(db, record, markdown, {'authority': 'user', 'request_id': payload['request_id']})
            self._publish(db)
        return {'accepted': True, 'already_answered': already_answered, 'request_id': payload['request_id'],
                'answer_requests': self.linked_answer_requests(), 'answer_file': self.answer_snapshot()['manifest']}

    def _record_answer(self, db, record, markdown, source):
        qid = record['question_id']
        question = db.execute('SELECT * FROM wf_questions WHERE id=?', (qid,)).fetchone()
        if (question is None or question['answer'] is None or json.loads(question['answer']) != record['value']
                or question['revision'] != record['question_revision'] + 1):
            raise ReviewError('The saved user answer does not match the formatted record.', 409)
        self._record_profile_scope(db, qid, question['revision'], record['scope'])
        prior = db.execute('SELECT 1 FROM wf_qpool_answers WHERE source_question_id=? AND answer_revision=?',
                           (qid, question['revision'])).fetchone()
        if prior:
            return
        link = db.execute('SELECT * FROM wf_qpool_links WHERE question_id=?', (qid,)).fetchone()
        if link is not None:
            canonical = db.execute('SELECT * FROM wf_qpool_questions WHERE id=?', (link['pool_id'],)).fetchone()
            old_scope = {'kind': canonical['scope_kind'], 'key': canonical['scope_key']}
            if old_scope != record['scope']:
                if record['scope'] != {'kind': 'application', 'key': question['process_id']}:
                    raise ReviewError('An accepted answer cannot widen the registered question scope.', 409)
                db.execute('DELETE FROM wf_qpool_links WHERE question_id=?', (qid,))
                link = None
        if link is None:
            pool_id = str(uuid.uuid4())
            qrecord = {'text': question['text'], 'type': question['type'], 'choices': json.loads(question['choices']),
                       'required': bool(question['required'])}
            db.execute('INSERT INTO wf_qpool_questions VALUES(?,?,?,?,?)',
                       (pool_id, encode(qrecord), record['scope']['kind'], record['scope']['key'], now()))
            process = self.store._process(db, question['process_id'])
            db.execute('INSERT INTO wf_qpool_links VALUES(?,?,?,?,?,?,?)',
                       (qid, pool_id, process['id'], 'unassigned', question['form_version'], question['revision'], now()))
        else:
            pool_id = link['pool_id']
        db.execute('INSERT INTO wf_qpool_answers(pool_id,source_question_id,answer_revision,value,labels,markdown,source,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (pool_id, qid, question['revision'], encode(record['value']), encode(record['labels']), markdown, encode(source), now()))
        self._bump(db, answers=True)

    def _apply_answer_payload(self, payload, *, shared_sources=None):
        """Use the original answer guards with the same transaction's lock fence."""
        return self.store._answer_questions(payload, mutation_fence=self._assert, shared_sources=shared_sources)

    @staticmethod
    def _submission_history_cleared(db, process, task, checkpoint):
        """Recognize only the existing operator's durable not-started recovery."""
        attempts = db.execute('SELECT * FROM wf_submission_attempts WHERE process_id=?',
                              (process['id'],)).fetchall()
        if not attempts:
            return True
        if (len(attempts) != 1 or attempts[0]['task_id'] != task['id']
                or attempts[0]['confirmed_at'] or attempts[0]['receipt']
                or checkpoint.get('submission_started') or checkpoint.get('submission_attempt_id')
                or checkpoint.get('reconciled_submission_attempt_id') != attempts[0]['id']):
            return False
        events = db.execute("SELECT data FROM wf_process_events WHERE process_id=? "
                            "AND type='submission_attempt_reconciled' AND actor='operator'",
                            (process['id'],)).fetchall()
        return any((event := json.loads(row['data'])).get('attempt_id') == attempts[0]['id']
                   and event.get('outcome') == 'not_started'
                   and event.get('external_item_count') == 0
                   and event.get('submission_replayed') is False
                   and event.get('fresh_preparation_required') is True for row in events)

    def _audit_deferred(self, deferred):
        if not deferred:
            return
        with self.store.transaction(True) as db:
            self._assert(db)
            for process_id, data in deferred.items():
                previous = db.execute("SELECT data FROM wf_process_events WHERE process_id=? "
                    "AND type='shared_answer_deferred' ORDER BY id DESC LIMIT 1", (process_id,)).fetchone()
                # Recovery polls must not append the same deferral indefinitely.
                if previous is None or json.loads(previous['data']) != data:
                    self.store._process_event(db, process_id, 'shared_answer_deferred', data, 'question_pool')

    def linked_answer_requests(self, *, process_ids=None):
        """Return fenced payloads, never silently dispatch or broaden permissions."""
        grouped, deferred = {}, {}
        with self.store.transaction() as db:
            self._assert(db)
            rows = db.execute('''SELECT l.*,a.id AS answer_id,a.labels,a.source_question_id,a.answer_revision,
                    p.scope_kind,p.scope_key FROM wf_qpool_links l
                JOIN wf_qpool_questions p ON p.id=l.pool_id
                JOIN wf_qpool_answers a ON a.pool_id=l.pool_id
                WHERE a.id=(SELECT MAX(a2.id) FROM wf_qpool_answers a2 WHERE a2.pool_id=l.pool_id)
                ORDER BY l.process_id,l.question_id''').fetchall()
            for row in rows:
                if process_ids is not None and row['process_id'] not in process_ids:
                    continue
                if row['process_id'] in deferred:
                    continue
                process = self.store._process(db, row['process_id'])
                task = self.store._task(db, process['task_id'])
                question = db.execute('SELECT * FROM wf_questions WHERE id=? AND superseded=0', (row['question_id'],)).fetchone()
                checkpoint = json.loads(task['checkpoint'])
                if (question is None or question['answer'] is not None or question['form_version'] != process['form_version']
                        or question['form_version'] != row['form_version'] or question['revision'] != row['question_revision']
                        or process['submission_status'] != 'preparing' or task['status'] != 'waiting_for_user'
                        or task['resource_quarantined'] or task['lease_token'] or checkpoint.get('submission_started')
                        or not self._submission_history_cleared(db, process, task, checkpoint)):
                    continue
                questions_waiting = process['block_reason'] == task['error'] == 'questions_pending'
                browser_blocked = self.store._waiting_browser_question_blocker(db, process, task)
                if not (questions_waiting or browser_blocked):
                    continue
                try:
                    self.store._check_application(db, process)
                except ReviewError as error:
                    if error.status != 409:
                        raise
                    deferred[process['id']] = {'reason': error.reason or 'application_authorization_changed',
                        'detail': str(error), 'process_revision': process['revision'],
                        'source_answer_id': row['answer_id']}
                    continue
                choices = json.loads(question['choices'])
                labels = json.loads(row['labels'])
                by_label = {normalize(x['label']): x['id'] for x in choices}
                if len(by_label) != len(choices):
                    continue
                if question['type'] == 'text':
                    value = labels[0]
                else:
                    if any(normalize(label) not in by_label for label in labels):
                        continue
                    value = [by_label[normalize(label)] for label in labels]
                    if question['type'] == 'single_choice':
                        if len(value) != 1:
                            continue
                        value = value[0]
                group = grouped.setdefault(process['id'], {'payload': {'process_id': process['id'],
                    'process_revision': process['revision'], 'form_version': process['form_version'], 'answers': []}, 'sources': []})
                group['payload']['answers'].append({'question_id': question['id'], 'question_revision': question['revision'], 'value': value})
                group['sources'].append({'question_id': question['id'], 'pool_id': row['pool_id'],
                    'source_question_id': row['source_question_id'], 'source_answer_revision': row['answer_revision'],
                    'scope': {'kind': row['scope_kind'], 'key': row['scope_key']}})
        self._audit_deferred(deferred)
        for group in grouped.values():
            group['payload']['request_id'] = 'question-pool:' + _sha(group)
        return list(grouped.values())

    def seed_user_records(self, records):
        """Import existing accepted profile records explicitly, in source order.

        Existing question rows and user request receipts must independently agree;
        caller-supplied text cannot invent acceptance or widen a historical scope.
        """
        if not isinstance(records, list):
            raise ReviewError('Seed records must be an explicit list.')
        with self.store.transaction(True) as db:
            self._assert(db)
            for source in sorted(records, key=lambda x: (x.get('source_request_sequence', 0), x.get('record_id', 0))):
                if source.get('authority') != 'user':
                    raise ReviewError('Only user-confirmed historical records can seed answers.', 409)
                request = db.execute('SELECT * FROM wf_answer_requests WHERE id=?', (source.get('request_id'),)).fetchone()
                question = db.execute('SELECT * FROM wf_questions WHERE id=?', (source.get('question_id'),)).fetchone()
                if request is None or question is None:
                    raise ReviewError('Historical answer receipt is missing.', 409)
                accepted = json.loads(request['request_body'])
                if accepted.get('process_id') != question['process_id'] or source.get('process_id') != question['process_id']:
                    raise ReviewError('Historical answer context does not match.', 409)
                entry = next((a for a in accepted.get('answers', []) if a.get('question_id') == question['id']), None)
                if entry is None or entry.get('value') != source.get('value') or entry.get('question_revision') != source.get('question_revision'):
                    raise ReviewError('Historical answer differs from the accepted user request.', 409)
                # Corrections remain in source history, but old values must not
                # replace the current effective value during seed.
                process = self.store._process(db, question['process_id'])
                scope = source.get('reuse_scope', {'kind': 'application', 'key': process['id']})
                scope = self._scope(question, process, scope)
                labels = {x['id']: x['label'] for x in json.loads(question['choices'])}
                value = source['value']
                selected = value if isinstance(value, list) else [value]
                expected_labels = [labels[x] for x in selected] if labels else [value]
                if source.get('answer_labels') != expected_labels or source.get('question_text') != question['text']:
                    raise ReviewError('Historical display text differs from its accepted question or answer.', 409)
                record = {'question_id': question['id'], 'question_revision': source['question_revision'],
                    'question_text': source['question_text'], 'value': source['value'], 'labels': source['answer_labels'],
                    'scope': scope, 'process_id': process['id'], 'form_version': source['form_version']}
                self._record_historical(db, question, record, source)
            self._publish(db)
        return self.answer_snapshot()['manifest']

    def seed_external_user_records(self, records):
        """Operator-only import of explicit, source-verified profile answers.

        This does not read prose for meaning or infer user authority. The caller
        supplies reviewed records and exact local source citations. No HTTP route
        should expose this method. Revisions preserve correction chronology.
        """
        if not isinstance(records, list) or len(records) > 1000:
            raise ReviewError('External seed requires a bounded explicit record list.')
        verified = []
        for original in records:
            record = json.loads(encode(original))
            identity, revision = record.get('external_id'), record.get('revision')
            if (record.get('authority') != 'user' or not isinstance(identity, str)
                    or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', identity) is None
                    or type(revision) is not int or revision < 1):
                raise ReviewError('External seed needs an explicit user source identity and revision.', 409)
            scope = record.get('reuse_scope')
            if (not isinstance(scope, dict) or set(scope) != {'kind', 'key'}
                    or scope['kind'] not in {'personal', 'company', 'application'}
                    or not isinstance(scope['key'], str)
                    or bool(scope['key']) != (scope['kind'] != 'personal')):
                raise ReviewError('External seed reuse scope must be explicit.', 409)
            source = record.get('source')
            if not isinstance(source, dict):
                raise ReviewError('External seed needs an exact local source citation.', 409)
            path = Path(source.get('path', ''))
            start, end = source.get('start_line'), source.get('end_line')
            if (not path.is_absolute() or not path.is_file() or path.is_symlink()
                    or type(start) is not int or type(end) is not int or not 1 <= start <= end):
                raise ReviewError('External seed source path or line span is invalid.', 409)
            data = path.read_bytes()
            if len(data) > 2_000_000 or hashlib.sha256(data).hexdigest() != source.get('sha256'):
                raise ReviewError('External seed source bytes changed.', 409)
            try:
                lines = data.decode('utf-8').splitlines()
            except UnicodeDecodeError as error:
                raise ReviewError('External seed source must be UTF-8 text.', 409) from error
            if end > len(lines) or '\n'.join(lines[start - 1:end]) != source.get('quote'):
                raise ReviewError('External seed quote does not match the exact source lines.', 409)
            question = record.get('question_text')
            choices, kind, value = record.get('choices', []), record.get('type'), record.get('value')
            if not isinstance(question, str) or not question.strip() or len(question) > 10000:
                raise ReviewError('External seed question text is invalid.')
            if kind not in {'text', 'single_choice', 'multi_choice'} or not isinstance(choices, list):
                raise ReviewError('External seed question type or choices are invalid.')
            labels = {}
            for choice in choices:
                if (not isinstance(choice, dict) or not isinstance(choice.get('id'), str)
                        or not choice['id'] or choice['id'] in labels
                        or not isinstance(choice.get('label'), str) or not choice['label']):
                    raise ReviewError('External seed choice mapping is invalid.')
                labels[choice['id']] = choice['label']
            if kind == 'text':
                if choices or not isinstance(value, str) or not value.strip() or len(value) > 20000:
                    raise ReviewError('External seed text answer is invalid.')
                selected = [value]
            elif kind == 'single_choice':
                if not isinstance(value, str) or value not in labels:
                    raise ReviewError('External seed choice answer is invalid.')
                selected = [labels[value]]
            else:
                if (not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value)
                        or len(value) != len(set(value)) or any(v not in labels for v in value)):
                    raise ReviewError('External seed choice answers are invalid.')
                selected = [labels[v] for v in value]
            try:
                stamp = datetime.fromisoformat(record['answered_at'].replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError('Timestamp lacks timezone')
            except (KeyError, AttributeError, ValueError) as error:
                raise ReviewError('External seed requires an explicit answer timestamp.', 409) from error
            verified.append((record, selected))
        seeded = 0
        with self.store.transaction(True) as db:
            self._assert(db)
            for record, selected in verified:
                # Source can change while waiting for the question lock.
                source = record['source']
                if hashlib.sha256(Path(source['path']).read_bytes()).hexdigest() != source['sha256']:
                    raise ReviewError('External seed source changed before commit.', 409)
                identity = 'external:' + record['external_id']
                revision = record['revision']
                prior = db.execute('SELECT * FROM wf_qpool_answers WHERE source_question_id=? ORDER BY answer_revision DESC LIMIT 1', (identity,)).fetchone()
                if prior and prior['answer_revision'] >= revision:
                    if prior['answer_revision'] == revision and json.loads(prior['source']) == record:
                        continue
                    raise ReviewError('External seed revision is stale or was reused with different content.', 409)
                scope = record['reuse_scope']
                canonical = {'text': record['question_text'], 'type': record['type'], 'choices': record.get('choices', []), 'required': True}
                if prior:
                    pool_id = prior['pool_id']
                    existing = db.execute('SELECT * FROM wf_qpool_questions WHERE id=?', (pool_id,)).fetchone()
                    if (json.loads(existing['record']) != canonical or
                            (existing['scope_kind'], existing['scope_key']) != (scope['kind'], scope['key'])):
                        raise ReviewError('External correction cannot change question identity or scope.', 409)
                else:
                    pool_id = str(uuid.uuid4())
                    db.execute('INSERT INTO wf_qpool_questions VALUES(?,?,?,?,?)',
                        (pool_id, encode(canonical), scope['kind'], scope['key'], now()))
                formatted = {'question_id': identity, 'question_text': record['question_text'],
                             'labels': selected, 'scope': scope}
                db.execute('''INSERT INTO wf_qpool_answers(pool_id,source_question_id,answer_revision,value,labels,markdown,source,created_at)
                    VALUES(?,?,?,?,?,?,?,?)''', (pool_id, identity, revision, encode(record['value']), encode(selected),
                    self._body(formatted), encode(record), record['answered_at']))
                self._bump(db, answers=True)
                seeded += 1
            self._publish(db)
        return {'seeded': seeded, 'answer_file': self.answer_snapshot()['manifest']}

    @staticmethod
    def _record_profile_scope(db, question_id, revision, scope):
        for profile in db.execute('SELECT id,record FROM wf_profile_answers WHERE question_id=? AND answer_revision=?',
                                  (question_id, revision)).fetchall():
            saved = json.loads(profile['record'])
            if saved.get('reuse_scope') == scope:
                continue
            saved['reuse_scope'] = scope
            db.execute('UPDATE wf_profile_answers SET record=? WHERE id=?', (encode(saved), profile['id']))
            db.execute('UPDATE wf_profile_outbox SET projected_at=NULL WHERE answer_id=?', (profile['id'],))

    def _record_historical(self, db, question, record, source, markdown=None):
        revision = record['question_revision'] + 1
        self._record_profile_scope(db, question['id'], revision, record['scope'])
        if db.execute('SELECT 1 FROM wf_qpool_answers WHERE source_question_id=? AND answer_revision=?', (question['id'], revision)).fetchone():
            return
        link = db.execute('SELECT * FROM wf_qpool_links WHERE question_id=?', (question['id'],)).fetchone()
        if link is not None:
            canonical = db.execute('SELECT * FROM wf_qpool_questions WHERE id=?', (link['pool_id'],)).fetchone()
            old_scope = {'kind': canonical['scope_kind'], 'key': canonical['scope_key']}
            if old_scope != record['scope']:
                if record['scope'] != {'kind': 'application', 'key': question['process_id']}:
                    raise ReviewError('Historical answer cannot widen a registered question scope.', 409)
                db.execute('DELETE FROM wf_qpool_links WHERE question_id=?', (question['id'],))
                link = None
        if link is None:
            pool_id = str(uuid.uuid4())
            qrecord = {'text': record['question_text'], 'type': question['type'],
                       'choices': json.loads(question['choices']), 'required': bool(question['required'])}
            db.execute('INSERT INTO wf_qpool_questions VALUES(?,?,?,?,?)',
                       (pool_id, encode(qrecord), record['scope']['kind'], record['scope']['key'], now()))
            db.execute('INSERT INTO wf_qpool_links VALUES(?,?,?,?,?,?,?)',
                       (question['id'], pool_id, question['process_id'], 'unassigned', question['form_version'], question['revision'], now()))
        else:
            pool_id = link['pool_id']
        db.execute('INSERT INTO wf_qpool_answers(pool_id,source_question_id,answer_revision,value,labels,markdown,source,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (pool_id, question['id'], revision, encode(record['value']), encode(record['labels']), markdown or self._body(record), encode(source), source.get('answered_at', now())))
        self._bump(db, answers=True)

    def _publish(self, db):
        self._assert(db)
        state = db.execute('SELECT * FROM wf_qpool_state WHERE singleton=1').fetchone()
        answers = db.execute('SELECT * FROM wf_qpool_answers ORDER BY id').fetchall()
        content = '# Answered application questions\n\n' + '\n'.join(row['markdown'] for row in answers)
        manifest_path = self.directory / 'answered-questions.current.json'
        manifest = None
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
            except (ValueError, OSError):
                manifest = None
        if not self._valid_manifest(manifest, state['answer_revision']):
            data = content.encode('utf-8')
            hashed = _hash_bytes(data)
            filename = f"answered-questions.v{state['answer_revision']}-{hashed[:16]}.md"
            path = self.directory / filename
            if not path.exists():
                _atomic(path, data)
            elif path.read_bytes() != data:
                raise ReviewError('Immutable answer snapshot was modified.', 409)
            manifest = {'revision': state['answer_revision'], 'file': filename, 'algorithm': 'blake3', 'digest': hashed}
            _atomic(manifest_path, (encode(manifest) + '\n').encode())
        pending = []
        for row in db.execute('''SELECT p.* FROM wf_qpool_questions p
                WHERE NOT EXISTS(SELECT 1 FROM wf_qpool_answers a WHERE a.pool_id=p.id)
                AND EXISTS(SELECT 1 FROM wf_qpool_links l JOIN wf_questions q ON q.id=l.question_id
                    WHERE l.pool_id=p.id AND q.superseded=0 AND q.answer IS NULL) ORDER BY p.created_at,p.id'''):
            record = json.loads(row['record'])
            links = [dict(link) for link in db.execute('''SELECT l.* FROM wf_qpool_links l
                JOIN wf_questions q ON q.id=l.question_id WHERE l.pool_id=? AND q.superseded=0
                AND q.answer IS NULL AND q.form_version=l.form_version AND q.revision=l.question_revision
                ORDER BY l.process_id,l.question_id''', (row['id'],))]
            metadata = {'id': row['id'], **record,
                'scope': {'kind': row['scope_kind'], 'key': row['scope_key']}, 'links': links}
            pending.append(f"## {row['id']}\n\nQuestion: {record['text']}\n\n"
                f"Scope: {row['scope_kind']} {row['scope_key']}\n\n```json\n{encode(metadata)}\n```\n")
        _atomic(self.directory / 'pending-questions.md', ('# Pending application questions\n\n' + '\n'.join(pending)).encode())
        self._assert(db)
        db.execute('UPDATE wf_qpool_state SET published_revision=? WHERE singleton=1', (state['revision'],))

    def question_enrichment(self, question_ids):
        """Read-only shared identity for existing per-application API objects."""
        with self.store.transaction() as db:
            revision = db.execute('SELECT revision FROM wf_qpool_state WHERE singleton=1').fetchone()[0]
            result = {}
            for qid in question_ids:
                row = db.execute('SELECT pool_id FROM wf_qpool_links WHERE question_id=?', (qid,)).fetchone()
                result[qid] = {'pool_question_id': row['pool_id'] if row else None, 'pool_revision': revision}
            return result

    def propagate_answers(self, apply=None):
        """Deterministically propagate accepted answers through existing guards.

        Caller may supply its answer writer; default uses the existing internal
        API. Every target is revision-fenced and rechecked while our lock is held.
        No model is invoked. A rejected target stays blocked, while others proceed.
        """
        process_ids, task_ids, rejected = [], [], []
        for request in self.linked_answer_requests():
            self.check()
            try:
                receipt = (apply(request['payload']) if apply else
                           self._apply_answer_payload(request['payload'], shared_sources=request['sources']))
            except ReviewError as error:
                rejected.append({'process_id': request['payload']['process_id'], 'reason': error.reason or 'operation_rejected'})
            else:
                process_ids.extend(receipt.get('process_ids', []))
                task_ids.extend(receipt.get('task_ids', []))
        return {'propagated_process_ids': sorted(set(process_ids)),
                'propagated_task_ids': sorted(set(task_ids)), 'rejected': rejected}

    @staticmethod
    def _manifest_shape(manifest):
        return (isinstance(manifest, dict) and type(manifest.get('revision')) is int
            and manifest['revision'] >= 0 and manifest.get('algorithm') == 'blake3'
            and isinstance(manifest.get('digest'), str)
            and re.fullmatch(r'[0-9a-f]{64}', manifest['digest']) is not None
            and isinstance(manifest.get('file'), str)
            and manifest['file'] == f"answered-questions.v{manifest['revision']}-{manifest['digest'][:16]}.md")

    def _valid_manifest(self, manifest, revision):
        return (self._manifest_shape(manifest) and manifest['revision'] == revision
            and not (self.directory / manifest['file']).is_symlink()
            and (self.directory / manifest['file']).is_file())

    def repair(self):
        """Recover committed answer batches and republish before shared reads."""
        with self.store.transaction(True) as db:
            self._assert(db)
            for batch in db.execute('SELECT * FROM wf_qpool_batches ORDER BY created_at,id').fetchall():
                payload = json.loads(batch['payload'])
                receipt = db.execute('SELECT * FROM wf_answer_requests WHERE id=?', (payload['request_id'],)).fetchone()
                if receipt is None or json.loads(receipt['request_body']) != payload:
                    continue  # An uncommitted draft is never authorization to answer.
                for item in json.loads(batch['records']):
                    record = item['record']
                    question = db.execute('SELECT * FROM wf_questions WHERE id=?', (record['question_id'],)).fetchone()
                    if question is None:
                        raise ReviewError('Committed answer question is missing during recovery.', 409)
                    self._record_historical(db, question, record,
                        {'authority': 'user', 'request_id': payload['request_id'], 'answered_at': receipt['created_at']},
                        markdown=item['full_markdown'])
            self._publish(db)

    def answer_snapshot(self, cached_hash=None):
        with self.store.transaction() as db:
            self._assert(db)
            state = db.execute('SELECT * FROM wf_qpool_state WHERE singleton=1').fetchone()
            if state['published_revision'] != state['revision']:
                raise ReviewError('Question files have an incomplete publication.', 409)
            manifest = json.loads((self.directory / 'answered-questions.current.json').read_text())
            if not self._valid_manifest(manifest, state['answer_revision']):
                raise ReviewError('Answer manifest is stale.', 409)
            changed = manifest['digest'] != cached_hash
            result = {'manifest': manifest, 'changed': changed}
            if changed:
                result['markdown'] = (self.directory / manifest['file']).read_text()
                result['records'] = [{'id': r['id'], 'pool_id': r['pool_id'], 'source_question_id': r['source_question_id'],
                    'answer_revision': r['answer_revision'], 'value': json.loads(r['value']), 'labels': json.loads(r['labels']),
                    'source': json.loads(r['source']), 'scope': {'kind': r['scope_kind'], 'key': r['scope_key']},
                    **json.loads(r['record'])} for r in db.execute('''SELECT a.*,p.record,p.scope_kind,p.scope_key
                        FROM wf_qpool_answers a JOIN wf_qpool_questions p ON p.id=a.pool_id ORDER BY a.id''')]
            return result

    def snapshot(self):
        """Safe diagnostic counts; never returns question/answer text or paths."""
        with self.store.transaction() as db:
            state = db.execute('SELECT * FROM wf_qpool_state WHERE singleton=1').fetchone()
            pending = db.execute('''SELECT COUNT(*) FROM wf_qpool_questions p
                WHERE NOT EXISTS(SELECT 1 FROM wf_qpool_answers a WHERE a.pool_id=p.id)
                AND EXISTS(SELECT 1 FROM wf_qpool_links l JOIN wf_questions q ON q.id=l.question_id
                    WHERE l.pool_id=p.id AND q.superseded=0 AND q.answer IS NULL)''').fetchone()[0]
            answered = db.execute('SELECT COUNT(DISTINCT pool_id) FROM wf_qpool_answers').fetchone()[0]
        manifest_path = self.directory / 'answered-questions.current.json'
        try:
            manifest = json.loads(manifest_path.read_text())
            file_status = {'revision': manifest['revision'], 'hash_algorithm': manifest['algorithm'], 'hash': manifest['digest']}
        except (OSError, ValueError, KeyError):
            file_status = {'revision': None, 'hash_algorithm': None, 'hash': None}
        return {'question_pool': {'pending_count': pending, 'answered_count': answered, 'revision': state['revision']},
                'question_lock': self.lock_snapshot(), 'answer_file': file_status}
