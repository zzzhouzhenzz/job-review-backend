"""Operator-installed, deployment-bound evidence for native browser pooling.

No HTTP route or model result may install this setting. The host collects current
identity before dispatch. Resolution rechecks local evidence and deployment
files; an old checkpoint alone never grants transport capabilities. Version 3
verifies functional application tab affinity and concurrent fixture requests
admitted by one host FIFO gate. Native Chrome turns cannot overlap. This
does not claim permanent Chrome access isolation.
"""
from __future__ import annotations

from contextlib import nullcontext
import copy
import difflib
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import re
import stat
import uuid

from store import ReviewError, encode

SETTING_KEY = 'application_pool_transport_proof'
SOURCE_ROOT = Path(__file__).resolve().parent
SOURCE_NAMES = ('pooled_browser_session.py', 'codex_transport.py', 'codex_appserver.py',
                'codex_runtime.py', 'application_pool.py', 'application_pool_proof.py',
                'orchestrator.py', 'readiness.py', 'task_store.py', 'pool_application.py', 'shared_browser_session.py',
                'chrome_access_gate.py')
CONFIG_KEYS = frozenset({'application_pool_size', 'supported_kinds', 'config_sha256',
    'browser_transport', 'browser_session_policy', 'cwd', 'chrome_access_capacity', 'chrome_access_policy'})
WORKERS = {'apply-1', 'apply-2', 'apply-3'}
PROOF_VERSION = 3
GUARANTEE = 'functional_application_tab_affinity'
CHROME_ACCESS = {'capacity': 1, 'policy': 'chrome_fifo_v1'}
COMPATIBILITY_SOURCES = frozenset({'codex_runtime.py', 'readiness.py',
    'application_pool_proof.py', 'applicant_evidence.py'})
COMPATIBILITY_KIND = 'reviewed_runtime_compatibility'


def _require(value, message):
    if not value:
        raise ReviewError(message, 409, reason='application_pool_proof_invalid')


def _host():
    return {'node': platform.node(), 'system': platform.system(), 'release': platform.release(),
            'mac_version': platform.mac_ver()[0], 'machine': platform.machine(), 'uid': os.getuid()}


@lru_cache(maxsize=256)
def _cached_hash(path, device, inode, size, mtime_ns, ctime_ns):
    del device, inode, size, mtime_ns, ctime_ns
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _file(path):
    path = Path(path).resolve(strict=True)
    before = path.stat()
    _require(stat.S_ISREG(before.st_mode), 'Deployment evidence must reference regular files.')
    signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    digest = _cached_hash(str(path), *signature)
    after = path.stat()
    _require(signature == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
             'Deployment file changed while reading its identity.')
    return {'path': str(path), 'sha256': digest}


def _bundle(path, identifier):
    path = Path(path).resolve(strict=True)
    plist_path = path / 'Contents' / 'Info.plist'
    info = plistlib.loads(plist_path.read_bytes())
    executable = info.get('CFBundleExecutable')
    _require(info.get('CFBundleIdentifier') == identifier and isinstance(executable, str)
             and executable and Path(executable).name == executable, 'Application bundle identity is invalid.')
    version, build = info.get('CFBundleShortVersionString'), info.get('CFBundleVersion')
    _require(isinstance(version, str) and version and isinstance(build, str) and build,
             'Application bundle version is missing.')
    return {'path': str(path), 'identifier': identifier, 'version': version, 'build': build,
            'info_plist': _file(plist_path), 'executable': _file(path / 'Contents' / 'MacOS' / executable)}


def _config(value):
    _require(isinstance(value, dict) and not set(value) - CONFIG_KEYS
             and value.get('application_pool_size') == 3
             and isinstance(value.get('supported_kinds'), list)
             and 'apply' in value['supported_kinds']
             and set(value['supported_kinds']) <= {'apply', 'rank', 'explore'}
             and type(value.get('chrome_access_capacity')) is int and value['chrome_access_capacity'] == 1
             and value.get('chrome_access_policy') == CHROME_ACCESS['policy'],
             'Provide the explicit pool runtime configuration identity.')
    # These are allowlisted local settings, never environment, tokens or headers.
    serialized = json.dumps(value, sort_keys=True, allow_nan=False)
    _require(len(serialized) <= 200000, 'Runtime identity is too large.')
    for key in ('config_sha256', 'browser_transport', 'browser_session_policy', 'cwd', 'chrome_access_policy'):
        _require(key not in value or isinstance(value[key], str), 'Runtime configuration values are invalid.')
    if 'config_sha256' in value:
        _require(re.fullmatch('[0-9a-f]{64}', value['config_sha256']) is not None, 'Runtime configuration digest is invalid.')
    return json.loads(serialized)


def collect_identity(transport, runtime_config_identity, *,
                     browser_bundle=Path('/Applications/Google Chrome.app'),
                     codex_bundle=Path('/Applications/Codex.app')):
    """Collect real host/binary/bundle/source identity; no model or subprocess."""
    _require(transport.browser_transport == 'app-server'
             and transport.browser_session_policy in {'per_task', 'shared'},
             'Application pooling requires the app-server transport.')
    for value in (transport.timeout, transport.heartbeat_interval):
        _require(type(value) in (int, float) and math.isfinite(value) and value > 0,
                 'Transport timing configuration is invalid.')
    config = _config(runtime_config_identity)
    _require(type(transport.chrome_access_capacity) is int and transport.chrome_access_capacity == 1
             and transport.chrome_access_policy == CHROME_ACCESS['policy'],
             'Application pooling requires one shared Chrome connection and the FIFO policy.')
    observed = {'browser_transport': transport.browser_transport,
                'browser_session_policy': transport.browser_session_policy,
                'chrome_access_capacity': transport.chrome_access_capacity,
                'chrome_access_policy': transport.chrome_access_policy,
                'cwd': str(Path(transport.cwd).resolve(strict=True))}
    _require(all(key not in config or config[key] == value for key, value in observed.items()),
             'Runtime configuration identity differs from the actual transport.')
    return {'version': 1, 'host': _host(),
            'sources': {name: _file(SOURCE_ROOT / name) for name in SOURCE_NAMES},
            'codex_binary': {'configured_path': str(Path(transport.binary).absolute()), **_file(transport.binary)},
            'browser_bundle': _bundle(browser_bundle, 'com.google.Chrome'),
            'codex_bundle': _bundle(codex_bundle, 'com.openai.codex'),
            'runtime': {'browser_transport': transport.browser_transport,
                'browser_session_policy': transport.browser_session_policy,
                'chrome_access': dict(CHROME_ACCESS),
                'cwd': str(Path(transport.cwd).resolve(strict=True)),
                'timeout_seconds': transport.timeout, 'heartbeat_interval_seconds': transport.heartbeat_interval,
                'pipeline': config}}


def _current_at(identity, source_root):
    _require(isinstance(identity, dict) and set(identity) == {'version', 'host', 'sources',
        'codex_binary', 'browser_bundle', 'codex_bundle', 'runtime'} and identity['version'] == 1,
        'Deployment identity fields are incomplete.')
    _require(identity['host'] == _host(), 'Deployment proof belongs to a different host.')
    sources = identity['sources']
    _require(isinstance(sources, dict) and set(sources) == set(SOURCE_NAMES),
             'Deployment source identity is incomplete.')
    for name in SOURCE_NAMES:
        _require(sources[name] == _file(Path(source_root) / name), 'Deployment source code changed.')
    binary = identity['codex_binary']
    _require(isinstance(binary, dict) and set(binary) == {'configured_path', 'path', 'sha256'}
             and Path(binary['configured_path']).is_absolute()
             and binary == {'configured_path': binary['configured_path'], **_file(binary['configured_path'])},
             'Codex binary changed.')
    for key, identifier in [('browser_bundle', 'com.google.Chrome'), ('codex_bundle', 'com.openai.codex')]:
        _require(identity[key] == _bundle(identity[key]['path'], identifier), 'Application bundle changed.')
    runtime = identity['runtime']
    _require(isinstance(runtime, dict) and set(runtime) == {'browser_transport', 'browser_session_policy',
        'cwd', 'timeout_seconds', 'heartbeat_interval_seconds', 'pipeline', 'chrome_access'}
        and runtime['browser_transport'] == 'app-server'
        and runtime['browser_session_policy'] in {'per_task', 'shared'}
        and runtime['chrome_access'] == CHROME_ACCESS
        and type(runtime['chrome_access'].get('capacity')) is int, 'Runtime identity is incomplete.')
    for key in ('cwd',):
        path = Path(runtime[key])
        _require(path.is_absolute() and path.is_dir() and str(path.resolve()) == str(path), 'Runtime path changed.')
    for key in ('timeout_seconds', 'heartbeat_interval_seconds'):
        _require(type(runtime[key]) in (int, float) and math.isfinite(runtime[key]) and runtime[key] > 0,
                 'Runtime timing identity is invalid.')
    _config(runtime['pipeline'])


def _current(identity):
    return _current_at(identity, SOURCE_ROOT)


def fingerprint(identity):
    """Digest a complete, current host-collected deployment identity."""
    _current(identity)
    return hashlib.sha256(encode(identity).encode()).hexdigest()


def _private_bytes(path, expected_hash=None):
    path = Path(path)
    _require(path.is_absolute() and not path.is_symlink(), 'Proof evidence must be an absolute regular local file.')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                 and not info.st_mode & 0o077 and info.st_size <= 2_000_000,
                 'Proof evidence must be a bounded private file owned by this user.')
        raw = source.read(2_000_001)
        after = os.fstat(source.fileno())
        _require((info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) ==
                 (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                 'Proof evidence changed while reading.')
    digest = hashlib.sha256(raw).hexdigest()
    _require(expected_hash is None or expected_hash == digest, 'Proof evidence bytes changed.')
    return raw, {'path': str(path.resolve()), 'sha256': digest}


def _private_json(path, expected_hash=None):
    raw, reference = _private_bytes(path, expected_hash)
    return json.loads(raw), reference


def _byte_evidence(reference):
    _require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'}
             and isinstance(reference['sha256'], str)
             and re.fullmatch('[0-9a-f]{64}', reference['sha256']) is not None,
             'Proof evidence needs an exact path and SHA256.')
    return _private_bytes(reference['path'], reference['sha256'])[0]


def _evidence(reference):
    return json.loads(_byte_evidence(reference))


def _timestamp(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(stamp.tzinfo is not None and stamp <= datetime.now(timezone.utc), 'Observation time is invalid.')
    return stamp


def _uuid(value):
    return isinstance(value, str) and str(uuid.UUID(value)) == value


def _verify(proof, identity):
    # The normal path always checks actual installed source and external identity.
    _current(identity)
    return _verify_observations(proof, identity)


def _verify_observations(proof, identity):
    """Validate immutable historical observations, not current runtime execution.

    Callers must either validate identity against the current deployment or
    validate the exact operator compatibility relation. This function grants no
    setting or capability by itself. All native receipt/FIFO checks remain here.
    """
    digest = hashlib.sha256(encode(identity).encode()).hexdigest()
    _require(isinstance(proof, dict) and set(proof) == {'version', 'guarantee', 'identity', 'workers', 'chrome_access', 'gate_evidence'}
             and proof['version'] == PROOF_VERSION and proof['guarantee'] == GUARANTEE
             and proof['chrome_access'] == CHROME_ACCESS and type(proof['chrome_access'].get('capacity')) is int
             and proof['identity'] == identity, 'Proof version, guarantee or deployment identity does not match.')
    workers = proof['workers']
    _require(isinstance(workers, list) and len(workers) == 3
             and {w['worker_id'] for w in workers} == WORKERS, 'Proof needs all three actual workers.')
    for key in ('task_id', 'thread_id', 'tab_id'):
        _require(len({w[key] for w in workers}) == 3, 'Worker tasks, native threads and tabs must be distinct.')
    paths, turns = set(), set()
    phases = {stage: [] for stage in ('browser_before', 'session_only', 'peer_work', 'browser_after')}
    browser_receipts = {stage: {} for stage in ('browser_before', 'peer_work', 'browser_after')}
    for worker in workers:
        stages = ('browser_before', 'session_only', 'browser_after') if worker['worker_id'] == 'apply-3' else (
            'browser_before', 'session_only', 'peer_work', 'browser_after')
        _require(set(worker) == {'worker_id', 'task_id', 'thread_id', 'tab_id', *stages}
                 and _uuid(worker['task_id']) and _uuid(worker['thread_id'])
                 and isinstance(worker['tab_id'], str) and worker['tab_id'], 'Worker observation identity is invalid.')
        previous_end = None
        for stage in stages:
            observed = worker[stage]
            _require(isinstance(observed, dict) and set(observed) == {'started_at', 'finished_at', 'receipt', 'result'},
                     'Each observation needs an actual receipt and result.')
            start, end = _timestamp(observed['started_at']), _timestamp(observed['finished_at'])
            _require(start < end and (previous_end is None or previous_end <= start), 'Observation stages are out of order.')
            previous_end = end
            phases[stage].append((start, end))
            receipt, result = _evidence(observed['receipt']), _evidence(observed['result'])
            receipt_path, result_path = Path(observed['receipt']['path']), Path(observed['result']['path'])
            _require(receipt_path.name == 'receipt.json' and result_path.name == 'result.json'
                     and receipt_path.parent.resolve() == result_path.parent.resolve(),
                     'The result must belong to its actual host receipt directory.')
            for reference in (observed['receipt'], observed['result']):
                path = str(Path(reference['path']).resolve())
                _require(path not in paths, 'Observation evidence cannot be reused.')
                paths.add(path)
            browser = stage != 'session_only'
            _require(isinstance(receipt, dict) and receipt.get('status') == 'completed'
                and receipt.get('transport') == 'app-server' and receipt.get('turn_completed') is True
                and type(receipt.get('exit_code')) is int and receipt['exit_code'] == 0
                and receipt.get('application_worker_id') == worker['worker_id']
                and receipt.get('task_id') == worker['task_id'] and receipt.get('thread_id') == worker['thread_id']
                and receipt.get('started_at') == observed['started_at']
                and receipt.get('finished_at') == observed['finished_at']
                and receipt.get('browser') is browser and receipt.get('browser_verified') is browser
                and _uuid(receipt.get('turn_id')) and receipt['turn_id'] not in turns,
                'Observation receipt does not prove a completed owned native turn.')
            turns.add(receipt['turn_id'])
            _require(isinstance(result, dict) and result.get('worker_id') == worker['worker_id']
                     and result.get('task_id') == worker['task_id'], 'Observation result belongs to another worker.')
            if stage != 'browser_before':
                _require(receipt.get('resumed_thread_id') == worker['thread_id'], 'Observation did not resume its owned thread.')
            if not browser:
                _require(receipt.get('session_worker') is True and receipt.get('tools') == []
                         and 'chrome_access' not in receipt,
                         'Session-only observation used tools or a browser.')
            else:
                _require(isinstance(receipt.get('tools'), list) and receipt['tools']
                         and result.get('status') == 'verified'
                         and result.get('tab_id') == worker['tab_id']
                         and result.get('owned_tab_ids') == [worker['tab_id']]
                         and result.get('handoff_tab_ids') == [worker['tab_id']],
                         'Browser observation does not establish its owned handoff tab.')
                browser_receipts[stage][worker['worker_id']] = (worker, receipt, start, end, receipt_path)
    # Positive-duration observations cover every actual browser turn. No two
    # Chrome connections may overlap, including the two peers' middle phase.
    browser_turns = sorted(interval for stage in ('browser_before', 'peer_work', 'browser_after')
                           for interval in phases[stage])
    _require(all(end <= next_start for (_, end), (next_start, _) in zip(browser_turns, browser_turns[1:])),
             'Native Chrome observations overlap; shared connection capacity must be one.')
    sequence = ('browser_before', 'session_only', 'peer_work', 'browser_after')
    for before, after in zip(sequence, sequence[1:]):
        _require(max(end for _, end in phases[before]) <= min(start for start, _ in phases[after]),
                 'Fixture phases must finish before the next phase starts.')
    _verify_gate(proof['gate_evidence'], browser_receipts, paths)
    return digest


def _verify_gate(references, phases, paths):
    """Tie the real pre-admission FIFO snapshot to every native receipt."""
    _require(isinstance(references, dict) and set(references) == set(phases),
             'Each browser phase needs actual concurrent gate evidence.')
    directory, prior_revision, tickets = None, 0, set()
    for stage, receipts in phases.items():
        reference = references[stage]
        evidence = _evidence(reference)
        path = Path(reference['path']).resolve()
        _require(str(path) not in paths, 'Gate evidence cannot reuse a native receipt.')
        paths.add(str(path))
        _require(set(evidence) == {'version', 'stage', 'gate_directory', 'requested_at',
                                  'queued_at', 'queued', 'finished_at', 'final'}
                 and evidence['version'] == 1 and evidence['stage'] == stage,
                 'Gate evidence is incomplete or belongs to another phase.')
        expected_directory = str(path.parent / '_chrome_access')
        _require(evidence['gate_directory'] == expected_directory
                 and (directory is None or directory == expected_directory),
                 'All fixture requests must use one private output gate.')
        directory = expected_directory
        queued, final = evidence['queued'], evidence['final']
        for state in (queued, final):
            _require(isinstance(state, dict) and state.get('capacity') == 1
                     and type(state.get('capacity')) is int and state.get('policy') == CHROME_ACCESS['policy']
                     and state.get('state') == 'free' and state.get('owner') is None
                     and state.get('blocked_by') == [] and type(state.get('revision')) is int,
                     'Gate evidence has a different policy, owner or unresolved blocker.')
        _require(prior_revision < queued['revision'] < final['revision'] and final.get('queue') == [],
                 'Gate revisions must advance and all fixture tickets must finish.')
        prior_revision = final['revision']
        queue, requests = queued.get('queue'), evidence['requested_at']
        _require(isinstance(queue, list) and len(queue) == len(receipts)
                 and {item['worker_id'] for item in queue} == set(receipts)
                 and isinstance(requests, dict) and set(requests) == set(receipts),
                 'All active workers must be queued together before admission.')
        queued_at, finished_at = _timestamp(evidence['queued_at']), _timestamp(evidence['finished_at'])
        previous_release = None
        for item in queue:
            worker, receipt, start, end, receipt_path = receipts[item['worker_id']]
            access = receipt.get('chrome_access')
            _require(receipt_path.parent.parent.resolve() == path.parent
                     and item.get('task_id') == worker['task_id'] and item.get('role') == 'application_prepare'
                     and item.get('state') == 'waiting' and item.get('acquired_at') is None
                     and item.get('started_at') is None and _uuid(item.get('ticket_id'))
                     and item['ticket_id'] not in tickets,
                     'The queued ticket must belong to its exact fixture worker and task.')
            tickets.add(item['ticket_id'])
            _require(isinstance(access, dict) and set(access) == {'capacity', 'policy', 'ticket_id',
                     'enqueued_at', 'acquired_at', 'started_at', 'release_requested_at'}
                     and access['capacity'] == 1 and type(access['capacity']) is int
                     and access['policy'] == CHROME_ACCESS['policy']
                     and access['ticket_id'] == item['ticket_id']
                     and access['enqueued_at'] == item['enqueued_at'],
                     'Native receipt is missing its actual gate ticket.')
            requested = _timestamp(requests[item['worker_id']])
            enqueued, acquired = _timestamp(access['enqueued_at']), _timestamp(access['acquired_at'])
            native_started, release = _timestamp(access['started_at']), _timestamp(access['release_requested_at'])
            _require(requested <= enqueued <= queued_at <= acquired <= native_started <= start < end <= release <= finished_at
                     and (previous_release is None or previous_release <= acquired),
                     'Gate admission must follow FIFO order and contain the verified native turn.')
            previous_release = release


def _marker(reference, digest):
    return {'version': PROOF_VERSION, 'guarantee': GUARANTEE,
            'chrome_access': dict(CHROME_ACCESS),
            'chrome_fifo_verified': True,
            'application_tab_affinity_verified': True, 'session_only_handoff_preserved': True,
            'evidence_reference': reference['path'], 'deployment_identity_digest': digest}


def install(store, proof_file, expected_identity):
    """Operator-only installation. Validation precedes the one settings write."""
    try:
        proof, reference = _private_json(proof_file)
        digest = _verify(proof, expected_identity)
        setting = {'version': PROOF_VERSION, 'proof': reference, 'identity': expected_identity,
                   'deployment_identity_digest': digest}
        with store.transaction(True) as db:
            # Verify again under the transaction before granting this capability.
            current, _ = _private_json(reference['path'], reference['sha256'])
            _verify(current, expected_identity)
            db.execute('INSERT INTO wf_settings(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (SETTING_KEY, encode(setting)))
        return _marker(reference, digest)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise ReviewError('Deployment proof or evidence is invalid.', 409,
                          reason='application_pool_proof_invalid') from error


def resolve(store_or_db, expected_identity):
    """Return verified capability flags, or no capability on stale/missing proof."""
    try:
        context = store_or_db.transaction() if hasattr(store_or_db, 'transaction') else nullcontext(store_or_db)
        with context as db:
            row = db.execute('SELECT value FROM wf_settings WHERE key=?', (SETTING_KEY,)).fetchone()
        if row is None:
            return None
        setting = json.loads(row['value'])
        if 'compatibility' in setting:
            _require(set(setting) == {'version', 'proof', 'identity', 'deployment_identity_digest', 'compatibility'}
                     and setting['version'] == PROOF_VERSION and setting['identity'] == expected_identity,
                     'Installed compatibility belongs to another deployment.')
            audit = _evidence(setting['compatibility'])
            marker = _verify_compatibility(audit, expected_identity, setting['compatibility'])
            _require(setting['proof'] == audit['native_proof']
                     and setting['deployment_identity_digest'] == marker['deployment_identity_digest'],
                     'Installed compatibility references changed.')
            return marker
        _require(set(setting) == {'version', 'proof', 'identity', 'deployment_identity_digest'}
                 and setting['version'] == PROOF_VERSION and setting['identity'] == expected_identity,
                 'Installed proof belongs to another deployment.')
        proof = _evidence(setting['proof'])
        digest = _verify(proof, expected_identity)
        _require(digest == setting['deployment_identity_digest'], 'Installed proof digest changed.')
        return _marker(setting['proof'], digest)
    except (ReviewError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _compatibility_manifest(reference):
    records = _evidence(reference)
    _require(isinstance(records, list) and records, 'A reviewed source manifest is required.')
    names = set()
    for row in records:
        _require(isinstance(row, dict) and set(row) == {'file', 'before_sha256', 'after_sha256'}
                 and row['file'] in COMPATIBILITY_SOURCES and row['file'] not in names
                 and all(isinstance(row[key], str) and re.fullmatch('[0-9a-f]{64}', row[key])
                         for key in ('before_sha256', 'after_sha256'))
                 and row['before_sha256'] != row['after_sha256'],
                 'Compatibility can cover only exact reviewed runtime source changes.')
        names.add(row['file'])
    return records


def _compatibility_tests(reference, source_manifest):
    report = _evidence(reference)
    _require(isinstance(report, dict) and set(report) == {'version', 'status', 'source_manifest_sha256', 'suites'}
             and type(report['version']) is int and report['version'] == 1 and report['status'] == 'passed'
             and report['source_manifest_sha256'] == source_manifest['sha256']
             and isinstance(report['suites'], list) and report['suites'],
             'Tests must refer to the exact reviewed source manifest.')
    for suite in report['suites']:
        _require(isinstance(suite, dict) and set(suite) == {'command', 'run', 'passed', 'skipped', 'log'}
                 and isinstance(suite['command'], str) and suite['command'].strip()
                 and all(type(suite[key]) is int and suite[key] >= 0 for key in ('run', 'passed', 'skipped'))
                 and suite['passed'] > 0 and suite['run'] == suite['passed'] + suite['skipped'],
                 'Each reviewed test suite must have passing counts and exact log evidence.')
        _require(isinstance(suite['log'], dict) and set(suite['log']) == {'path', 'sha256'},
                 'A test log reference is required.')
        _byte_evidence(suite['log'])


def _compatibility_changes(baseline, target, supplemental):
    _require(set(baseline) == set(target) and all(baseline[key] == target[key]
             for key in target if key != 'sources'),
             'Native host, binary, browser, configuration and runtime identity must stay unchanged.')
    _require(set(baseline['sources']) == set(SOURCE_NAMES) == set(target['sources']),
             'Compatibility source identity is incomplete.')
    _require(isinstance(supplemental, dict) and set(supplemental) == {'applicant_evidence.py'},
             'The applicant reference helper must be pinned separately.')
    changes = []
    all_sources = [(name, baseline['sources'][name], target['sources'][name]) for name in SOURCE_NAMES]
    extra = supplemental['applicant_evidence.py']
    _require(isinstance(extra, dict) and set(extra) == {'baseline', 'target'},
             'Supplementary source identity is invalid.')
    all_sources.append(('applicant_evidence.py', extra['baseline'], extra['target']))
    for name, old, new in all_sources:
        _require(all(isinstance(value, dict) and set(value) == {'path', 'sha256'}
                     and isinstance(value['path'], str) and Path(value['path']).is_absolute()
                     and isinstance(value['sha256'], str) and re.fullmatch('[0-9a-f]{64}', value['sha256'])
                     for value in (old, new)) and old['path'] == new['path'],
                 'Compatibility cannot relocate or omit source identities.')
        if old != new:
            _require(name in COMPATIBILITY_SOURCES, 'Native transport, session or gate code changed.')
            changes.append({'file': name, 'before_sha256': old['sha256'], 'after_sha256': new['sha256']})
    return sorted(changes, key=lambda item: item['file'])


def prepare_compatibility(proof_file, baseline_identity, *, installed_source_root,
                          target_source_root, source_manifest_file, patch_file, tests_file, audit_file):
    """Operator-only preparation BEFORE deployment; writes one private audit only.

    The isolated adapter can inspect an explicit installed source root. No root
    override is stored for resolution. The target directory is read only here;
    it is never a runtime dependency and cannot supply current installed bytes.
    """
    installed = Path(installed_source_root).resolve(strict=True)
    candidate = Path(target_source_root).resolve(strict=True)
    _require(installed != candidate and installed.is_dir() and candidate.is_dir(),
             'Prepare requires distinct installed and reviewed candidate source directories.')
    _current_at(baseline_identity, installed)
    native, native_ref = _private_json(proof_file)
    native_digest = _verify_observations(native, baseline_identity)
    _, manifest_ref = _private_json(source_manifest_file)
    records = _compatibility_manifest(manifest_ref)
    patch, patch_ref = _private_bytes(patch_file)
    _, tests_ref = _private_json(tests_file)
    _compatibility_tests(tests_ref, manifest_ref)
    target = copy.deepcopy(baseline_identity)
    for name in SOURCE_NAMES:
        target['sources'][name]['sha256'] = _file(candidate / name)['sha256']
    old_extra = _file(installed / 'applicant_evidence.py')
    supplemental = {'applicant_evidence.py': {'baseline': old_extra,
        'target': {**old_extra, 'sha256': _file(candidate / 'applicant_evidence.py')['sha256']}}}
    changes = _compatibility_changes(baseline_identity, target, supplemental)
    _require(sorted(records, key=lambda item: item['file']) == changes,
             'Reviewed source manifest does not match the actual baseline and candidate.')
    expected_patch = ''.join(line for item in changes for line in difflib.unified_diff(
        (installed / item['file']).read_text().splitlines(keepends=True),
        (candidate / item['file']).read_text().splitlines(keepends=True),
        fromfile='a/' + item['file'], tofile='b/' + item['file'])).encode()
    _require(patch == expected_patch, 'Reviewed patch does not match the exact candidate source changes.')
    audit = {'version': 1, 'kind': COMPATIBILITY_KIND,
        'prepared_at': datetime.now(timezone.utc).isoformat(),
        'native_proof': native_ref, 'baseline_identity': baseline_identity,
        'native_identity_digest': native_digest, 'target_identity': target,
        'source_manifest': manifest_ref, 'patch': patch_ref, 'tests': tests_ref,
        'supplementary_sources': supplemental}
    # Recheck immediately before creating the immutable audit. No SQLite write
    # or native/model call occurs in this operator preparation step.
    _current_at(baseline_identity, installed)
    _verify_observations(_evidence(native_ref), baseline_identity)
    _require(_file(installed / 'applicant_evidence.py') == old_extra,
             'Applicant reference source changed during preparation.')
    path = Path(audit_file)
    _require(path.is_absolute() and not path.is_symlink(), 'Audit needs an absolute private output path.')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write((encode(audit) + '\n').encode()); output.flush(); os.fsync(output.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(directory)
    finally: os.close(directory)
    return _private_json(path)[1]


def _verify_compatibility(audit, expected_identity, audit_ref):
    digest = fingerprint(expected_identity)  # Always the installed current code.
    _require(isinstance(audit, dict) and set(audit) == {'version', 'kind', 'prepared_at',
        'native_proof', 'baseline_identity', 'native_identity_digest', 'target_identity',
        'source_manifest', 'patch', 'tests', 'supplementary_sources'}
        and type(audit['version']) is int and audit['version'] == 1 and audit['kind'] == COMPATIBILITY_KIND
        and audit['target_identity'] == expected_identity,
        'Compatibility audit does not match the current deployment.')
    _timestamp(audit['prepared_at'])
    changes = _compatibility_changes(audit['baseline_identity'], expected_identity, audit['supplementary_sources'])
    records = _compatibility_manifest(audit['source_manifest'])
    _require(sorted(records, key=lambda item: item['file']) == changes,
             'Compatibility source changes differ from the reviewed manifest.')
    extra = audit['supplementary_sources']['applicant_evidence.py']['target']
    _require(extra == _file(SOURCE_ROOT / 'applicant_evidence.py'), 'Applicant reference helper changed.')
    _require(isinstance(audit['patch'], dict) and set(audit['patch']) == {'path', 'sha256'},
             'Reviewed patch reference is invalid.')
    _byte_evidence(audit['patch'])
    _compatibility_tests(audit['tests'], audit['source_manifest'])
    native = _evidence(audit['native_proof'])
    native_digest = _verify_observations(native, audit['baseline_identity'])
    _require(native_digest == audit['native_identity_digest'], 'Original native proof identity changed.')
    return {**_marker(audit['native_proof'], digest),
        'native_identity_digest': native_digest, 'compatibility_reference': audit_ref,
        'verification_basis': 'native_proof_with_reviewed_runtime_compatibility'}


def install_compatibility(store, audit_file, expected_identity):
    """Operator-only install after reviewed deployment; no HTTP/model entry point."""
    try:
        audit, reference = _private_json(audit_file)
        marker = _verify_compatibility(audit, expected_identity, reference)
        setting = {'version': PROOF_VERSION, 'proof': audit['native_proof'],
            'identity': expected_identity, 'deployment_identity_digest': marker['deployment_identity_digest'],
            'compatibility': reference}
        with store.transaction(True) as db:
            current = _evidence(reference)
            _verify_compatibility(current, expected_identity, reference)
            db.execute('INSERT INTO wf_settings(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (SETTING_KEY, encode(setting)))
        return marker
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise ReviewError('Reviewed runtime compatibility audit is invalid.', 409,
                          reason='application_pool_proof_invalid') from error
