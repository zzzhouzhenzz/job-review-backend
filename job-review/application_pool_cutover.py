"""Read-only planning and explicit, backed-up application preparation cutover.

This module never contacts Chrome, stops workers, runs a model, or submits a form.
The operator supplies exact source files and verified stopped-owner/tab evidence.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from urllib.parse import quote

from readiness import HUMAN_BROWSER_REASONS
from store import ReviewError, encode, material_hash, now


def _read(path):
    db = sqlite3.connect('file:' + quote(str(Path(path).resolve())) + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    return db


def _fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _file(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise ReviewError('Cutover evidence must be a regular local file.', 409)
    content = path.read_bytes()
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(content).hexdigest()}


def _rows(db, sql, parameters=()):
    return [dict(row) for row in db.execute(sql, parameters)]


def _fresh_authority(value):
    if value is None:
        return None
    if (not isinstance(value,dict) or set(value) != {'version','source','source_reference',
            'process_ids','allow_failed_preparation_reset'} or value['version'] != 1
            or value['source'] != 'user_chat' or not isinstance(value['source_reference'],str)
            or not value['source_reference'].strip() or value['allow_failed_preparation_reset'] is not True
            or not isinstance(value['process_ids'],list) or not value['process_ids']
            or any(not isinstance(pid,str) or not pid for pid in value['process_ids'])
            or len(set(value['process_ids'])) != len(value['process_ids'])):
        raise ReviewError('Fresh preparation authority must identify the exact user instruction and applications.',409)
    return json.loads(encode(value))


def _items(db, fresh_preparation_authority=None):
    fresh_preparation_authority=_fresh_authority(fresh_preparation_authority)
    from project_consent import ProjectConsentMixin
    consent=ProjectConsentMixin._project_consent(db) if fresh_preparation_authority else None
    items = []
    for process in _rows(db, 'SELECT * FROM wf_processes ORDER BY created_at,id'):
        pid = process['id']
        task = dict(db.execute('SELECT * FROM wf_tasks WHERE id=?', (process['task_id'],)).fetchone())
        job = dict(db.execute('SELECT * FROM jobs WHERE key=?', (process['job_key'],)).fetchone())
        company = dict(db.execute('SELECT * FROM companies WHERE id=?', (job['company_id'],)).fetchone())
        entry = dict(db.execute('SELECT * FROM queue WHERE id=?', (pid,)).fetchone())
        observations = _rows(db, 'SELECT * FROM wf_observations WHERE job_key=?', (job['key'],))
        attempts = _rows(db, 'SELECT * FROM wf_submission_attempts WHERE process_id=? ORDER BY started_at,id', (pid,))
        events = _rows(db, 'SELECT * FROM wf_process_events WHERE process_id=? ORDER BY id', (pid,))
        locks = _rows(db, 'SELECT * FROM wf_resource_locks WHERE task_id=? ORDER BY resource', (task['id'],))
        questions = _rows(db, 'SELECT * FROM wf_questions WHERE process_id=? ORDER BY id', (pid,))
        checkpoint = json.loads(task['checkpoint'])
        reconciled = {json.loads(event['data']).get('attempt_id') for event in events
                      if event['type'] == 'submission_attempt_reconciled'
                      and json.loads(event['data']).get('outcome') == 'not_started'}
        unresolved = [a for a in attempts if a['confirmed_at'] is None and a['id'] not in reconciled]
        failed_reset = (entry['status']=='failed' and fresh_preparation_authority is not None
                        and pid in fresh_preparation_authority['process_ids'] and consent['granted'])
        authority = (not company['excluded'] and job['decision'] == 'approve'
                     and (entry['status'] in ('queued', 'in_progress', 'needs_input') or failed_reset)
                     and material_hash(json.loads(job['data'])) == json.loads(process['snapshot'])['material_hash']
                     and not any(o['availability'] == 'closed' for o in observations))
        if process['submission_status'] == 'submitted':
            classification = 'submitted_excluded'
        elif (process['submission_status'] == 'submission_unknown' or checkpoint.get('submission_started')
              or unresolved or any(a['confirmed_at'] for a in attempts)):
            classification = 'reconciliation_required'
        elif locks or task['lease_token'] or task['status'] == 'running':
            classification = 'owner_stop_required'
        elif task['status'] in ('cancelled', 'succeeded') or process['submission_status'] not in ('not_started', 'preparing'):
            classification = 'closed_excluded'
        elif not authority:
            classification = 'authorization_required'
        elif process['block_reason'] in HUMAN_BROWSER_REASONS or task['error'] in HUMAN_BROWSER_REASONS:
            classification = 'independent_blocker'
        else:
            classification = 'fresh_preparation_candidate'
        # Fence complete rows, not only a timestamp that a writer could preserve.
        fence = {'process': process, 'task': task, 'job': job, 'company': company,
                 'queue': entry, 'observations': observations, 'attempts': attempts,
                 'events': events, 'locks': locks, 'questions': questions}
        if fresh_preparation_authority:
            fence['project_consent']=consent
        items.append({'process_id': pid, 'task_id': task['id'], 'job_key': process['job_key'],
                      'classification': classification, 'original_block_reason': process['block_reason'],
                      'fence': fence})
    return items


def build_plan(database, *, profile=None, external_records=None, evidence_files=(),
               fresh_preparation_authority=None):
    """Read-only inventory. Source bytes, answers, authority and state are fenced."""
    with closing(_read(database)) as db:
        fresh_preparation_authority=_fresh_authority(fresh_preparation_authority)
        items = _items(db,fresh_preparation_authority)
        records = [{**json.loads(r['record']), 'record_id': r['id']}
                   for r in db.execute('SELECT id,record FROM wf_profile_answers ORDER BY id')]
        requests = _rows(db, 'SELECT * FROM wf_answer_requests ORDER BY id')
        owners = _rows(db, "SELECT * FROM wf_tasks WHERE status='running' OR lease_token IS NOT NULL ORDER BY id")
        locks = _rows(db, 'SELECT * FROM wf_resource_locks ORDER BY resource')
    external = json.loads(encode(external_records or []))
    sources = set(map(str, evidence_files))
    for record in external:
        sources.add(record.get('source', {}).get('path', ''))
    files_by_path = {item['path']: item for item in (_file(path) for path in sources)}
    files = [files_by_path[path] for path in sorted(files_by_path)]
    material = {'version': 2, 'database': str(Path(database).resolve()), 'items': items,
                'user_answer_records': records, 'answer_requests': requests,
                'active_owners': owners, 'resource_locks': locks,
                'profile': _file(profile) if profile is not None else None,
                'external_records': external, 'evidence_files': files}
    if fresh_preparation_authority is not None:
        material['fresh_preparation_authority']=fresh_preparation_authority
    return {**material, 'created_at': now(), 'plan_hash': _fingerprint(material)}


def _verify_plan(plan):
    material = {k: v for k, v in plan.items() if k not in {'created_at', 'plan_hash'}}
    if plan.get('version') != 2 or _fingerprint(material) != plan.get('plan_hash'):
        raise ReviewError('Cutover plan was changed after review.', 409)
    current = build_plan(plan['database'], profile=plan['profile']['path'] if plan.get('profile') else None,
                         external_records=plan['external_records'],
                         evidence_files=[item['path'] for item in plan['evidence_files']],
                         fresh_preparation_authority=plan.get('fresh_preparation_authority'))
    if current['plan_hash'] != plan['plan_hash']:
        raise ReviewError('Application state or source evidence changed. Build a fresh cutover plan.', 409)


def _private_write(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as handle:
        handle.write(encode(value) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def _proofs(plan, selected, stop_evidence):
    references = {item['path'] for item in plan['evidence_files']}
    def referenced(value):
        return isinstance(value, str) and bool(value.strip()) and str(Path(value).resolve()) in references
    if (not isinstance(stop_evidence, dict) or stop_evidence.get('dispatch_stopped') is not True
            or stop_evidence.get('native_workers_stopped') is not True
            or not referenced(stop_evidence.get('evidence_reference'))):
        raise ReviewError('Cutover needs stopped dispatch and native-owner proof in a reviewed evidence file.', 409)
    markers = stop_evidence.get('task_session_resets', {})
    if not isinstance(markers, dict):
        raise ReviewError('Cutover needs per-task session reset evidence.', 409)
    for item in selected:
        marker = markers.get(item['task_id'])
        if (not isinstance(marker, dict) or marker.get('task_id') != item['task_id']
                or marker.get('worker_id') not in ('apply-1', 'apply-2', 'apply-3')
                or 'legacy_thread_id' not in marker
                or marker['legacy_thread_id'] is not None and not isinstance(marker['legacy_thread_id'], str)
                or marker.get('safe_preparation_reset') is not True
                or marker.get('stopped_worker_confirmed') is not True
                or marker.get('owned_tabs_closed') is not True
                or not referenced(marker.get('evidence_reference'))):
            raise ReviewError('Each reset needs exact task, worker, stopped-owner and owned-tab closure evidence.', 409)
    return markers


def _backup(store, plan, target, stop_evidence):
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    database = target / 'before.sqlite3'
    # Open with private permissions before SQLite creates pages. Backup includes WAL.
    os.close(os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    with closing(store.connect()) as source, closing(sqlite3.connect(database)) as destination:
        source.backup(destination)
        if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ReviewError('Cutover backup failed its integrity check.', 409)
    _private_write(target / 'plan.json', plan)
    _private_write(target / 'stop-evidence.json', stop_evidence)
    copied = []
    sources = plan['evidence_files'] + ([plan['profile']] if plan.get('profile') else [])
    for index, source in enumerate(sources):
        destination = target / f"source-{index:03d}-{Path(source['path']).name}"
        data = Path(source['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != source['sha256']:
            raise ReviewError('Cutover source changed during backup.', 409)
        with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as handle:
            handle.write(data)
        copied.append({**source, 'backup_file': destination.name})
    directory = store.question_pool.directory
    if directory.exists():
        if directory.is_symlink() or any(path.is_symlink() for path in directory.rglob('*')):
            raise ReviewError('Question-pool backup cannot follow symbolic links.', 409)
        shutil.copytree(directory, target / 'question-pool')
    _private_write(target / 'sources.json', copied)


def apply_plan(store, plan, *, backup_directory, stop_evidence, selected_process_ids,
               seeded_records=None, external_records=None, transport_proof=None):
    """Apply a reviewed plan after exact stopped-owner and tab-closure evidence.

    Seeding is append-only and recoverable. If a later fence rejects the reset,
    already-seeded accepted answers remain valid; tasks are unchanged. Backups
    and an operator audit retain the old execution and question state.
    """
    if Path(store.path).resolve() != Path(plan['database']).resolve():
        raise ReviewError('Cutover database does not match the plan.', 409)
    from application_pool import current_transport_proof
    verified_proof=current_transport_proof(store)
    if transport_proof is not None and transport_proof != verified_proof:
        raise ReviewError('Cutover transport proof is not the current verified deployment proof.',409)
    if getattr(store,'application_pool_proof_required',False) and verified_proof is None:
        raise ReviewError('Verify and install the current native transport proof before cutover.',409)
    transport_proof=verified_proof
    selected = set(selected_process_ids)
    candidates = {i['process_id']: i for i in plan['items'] if i['classification'] == 'fresh_preparation_candidate'}
    if not selected or not selected.issubset(candidates):
        raise ReviewError('Only reviewed safe preparation candidates can be reset.', 409)
    seeds = plan['user_answer_records'] if seeded_records is None else seeded_records
    external = plan['external_records'] if external_records is None else external_records
    if seeds != plan['user_answer_records'] or external != plan['external_records']:
        raise ReviewError('Answer seeds must match the reviewed cutover plan.', 409)
    markers = _proofs(plan, [candidates[pid] for pid in selected], stop_evidence)
    _verify_plan(plan)
    if plan['active_owners'] or plan['resource_locks']:
        raise ReviewError('An old resource or task owner remains. Drain it before cutover.', 409)
    target = Path(backup_directory)
    _backup(store, plan, target, stop_evidence)
    _verify_plan(plan)
    pool = store.question_pool
    with pool.hold('recovery', 'application-pool-cutover', lease_seconds=300):
        _verify_plan(plan)
        pool.seed_user_records(seeds)
        if external:
            pool.seed_external_user_records(external)
        with store.transaction(True) as db:
            pool._assert(db)
            # The lock guards pool writers; SQLite guards the final task/authority
            # check and reset against writers that do not use the question lock.
            if _items(db,plan.get('fresh_preparation_authority')) != plan['items'] or db.execute('SELECT 1 FROM wf_resource_locks').fetchone() or db.execute(
                    "SELECT 1 FROM wf_tasks WHERE status='running' OR lease_token IS NOT NULL").fetchone():
                raise ReviewError('Application state changed during cutover. No tasks were reset.', 409)
            archived = _rows(db, 'SELECT * FROM wf_questions WHERE superseded=0 AND answer IS NULL ORDER BY id')
            stamp = now()
            for pid in sorted(selected):
                process = store._process(db, pid)
                task = store._task(db, process['task_id'])
                previous = candidates[pid]['fence']
                # This operator-only transition has exact user authority and
                # complete state fences. Normal automatic retries are unchanged.
                if previous['queue']['status']=='failed':
                    authority=plan.get('fresh_preparation_authority')
                    if not authority or pid not in authority['process_ids']:
                        raise ReviewError('Failed application reset lacks fresh user authority.',409)
                    db.execute("UPDATE queue SET status='queued',updated_at=? WHERE id=?",(stamp,pid))
                store._check_application(db, process)
                # Exact stopped-owner/closed-tab evidence was checked above.
                # Preserve prior ownership as history; never adopt it for a fresh run.
                db.execute('UPDATE wf_apply_tab_bindings SET retired_at=?,retirement_evidence=? '
                    'WHERE task_id=? AND retired_at IS NULL',
                    (stamp,encode({'plan_hash':plan['plan_hash'],'stop_evidence':stop_evidence}),task['id']))
                checkpoint = {'preparation_reset': {'plan_hash': plan['plan_hash'],
                    'evidence_reference': stop_evidence['evidence_reference'],
                    'fresh_preparation_authority':plan.get('fresh_preparation_authority'),
                    'previous_block_reason': process['block_reason'], 'previous_checkpoint': json.loads(task['checkpoint'])},
                    'pool_session_reset': markers[task['id']]}
                if transport_proof:
                    checkpoint['pool_transport_proof'] = transport_proof
                db.execute('UPDATE wf_questions SET superseded=1,updated_at=? WHERE process_id=? AND superseded=0', (stamp, pid))
                db.execute("""UPDATE wf_tasks SET checkpoint=?,resource='browser',status='queued',
                    error=NULL,lease_token=NULL,lease_owner=NULL,lease_until=NULL,failure_count=0,
                    available_at=?,updated_at=? WHERE id=?""", (encode(checkpoint), stamp, stamp, task['id']))
                db.execute("UPDATE wf_processes SET submission_status='preparing',block_reason=NULL,form_version=NULL,revision=revision+1,updated_at=? WHERE id=?", (stamp, pid))
                db.execute("UPDATE queue SET status='queued',updated_at=? WHERE id=?", (stamp, pid))
                db.execute("""UPDATE wf_apply_slots SET task_id=NULL,state='available',stage='idle',
                    stage_started_at=?,updated_at=?,answer_hash=NULL,answer_revision=NULL,legacy_affinity=NULL
                    WHERE task_id=?""", (stamp, stamp, task['id']))
                store._process_event(db, pid, 'application_pool_preparation_reset',
                    {'plan_hash': plan['plan_hash'], 'previous': previous, 'stop_evidence': stop_evidence,
                     'fresh_preparation_authority':plan.get('fresh_preparation_authority')}, 'operator')
            # Archive old pending rounds, including excluded apps, without changing
            # their process/task/receipt state or inventing an answer.
            db.execute('UPDATE wf_questions SET superseded=1,updated_at=? WHERE superseded=0 AND answer IS NULL', (stamp,))
            for pid in sorted({q['process_id'] for q in archived}):
                store._process_event(db, pid, 'application_pool_questions_archived',
                    {'plan_hash': plan['plan_hash'], 'question_ids': [q['id'] for q in archived if q['process_id'] == pid]}, 'operator')
            db.execute("INSERT OR REPLACE INTO wf_settings(key,value) VALUES('application_pool_cutover',?)",
                       (encode({'plan_hash': plan['plan_hash'], 'at': stamp}),))
            pool._bump(db)
            store._pool_bump(db)
        pool.repair()
        snapshot = pool.snapshot()
    with store.transaction() as db:
        integrity = db.execute('PRAGMA quick_check').fetchone()[0]
        foreign_keys = db.execute('PRAGMA foreign_key_check').fetchall()
    result = {'plan_hash': plan['plan_hash'], 'reset_process_ids': sorted(selected),
              'backup_directory': str(target), 'pending_count': snapshot['question_pool']['pending_count'],
              'integrity': integrity, 'foreign_key_errors': len(foreign_keys)}
    _private_write(target / 'result.json', result)
    if integrity != 'ok' or foreign_keys or result['pending_count']:
        raise ReviewError('Cutover verification failed. Keep dispatch stopped and inspect the saved result.', 409)
    return result


def main():
    parser = argparse.ArgumentParser(description='Read-only application pool cutover planner.')
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--external-records', type=Path, help='Reviewed profile-only seed JSON list.')
    parser.add_argument('--evidence-file', type=Path, action='append', default=[])
    parser.add_argument('--fresh-preparation-authority',type=Path,
                        help='Exact reviewed user authority JSON for named failed applications.')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    external = json.loads(args.external_records.read_text()) if args.external_records else None
    authority=json.loads(args.fresh_preparation_authority.read_text()) if args.fresh_preparation_authority else None
    plan = build_plan(args.database, profile=args.profile, external_records=external,
                      evidence_files=args.evidence_file,fresh_preparation_authority=authority)
    _private_write(args.output, plan)
    print(json.dumps({'plan_hash': plan['plan_hash'], 'counts': {
        kind: sum(i['classification'] == kind for i in plan['items'])
        for kind in sorted({i['classification'] for i in plan['items']})}}))


if __name__ == '__main__':
    main()
