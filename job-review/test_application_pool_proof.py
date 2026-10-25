"""Fictional private proof fixtures; no native process or browser is invoked."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import application_pool_proof as proof
from store import ReviewError
from workflow import WorkflowStore


class ApplicationPoolProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.sources = self.root / 'source'
        self.sources.mkdir()
        for name in proof.SOURCE_NAMES:
            (self.sources / name).write_text('# Fictional transport source\n')
        source_patch = patch.object(proof, 'SOURCE_ROOT', self.sources)
        source_patch.start()
        self.addCleanup(source_patch.stop)
        self.binary = self.root / 'codex'
        self.binary.write_text('fictional executable\n')
        self.binary.chmod(0o700)
        self.chrome = self.bundle('Chrome', 'com.google.Chrome')
        self.codex = self.bundle('Codex', 'com.openai.codex')
        self.transport = SimpleNamespace(binary=self.binary, cwd=self.sources,
            run_directory=self.root / 'fixture-runs', timeout=900, heartbeat_interval=15,
            browser_transport='app-server', browser_session_policy='per_task',
            chrome_access_capacity=1, chrome_access_policy='chrome_fifo_v1')
        self.transport.run_directory.mkdir(mode=0o700)
        self.config = {'application_pool_size': 3, 'supported_kinds': ['apply'],
                       'chrome_access_capacity': 1, 'chrome_access_policy': 'chrome_fifo_v1'}
        self.identity = self.collect()
        self.store = WorkflowStore(self.root / 'fictional.sqlite3')
        self.document = self.make_proof()
        self.path = self.root / 'deployment-proof.json'
        self.write(self.path, self.document)

    def bundle(self, name, identifier):
        root = self.root / (name + '.app')
        (root / 'Contents' / 'MacOS').mkdir(parents=True)
        info = {'CFBundleIdentifier': identifier, 'CFBundleExecutable': name,
                'CFBundleShortVersionString': '1.2.3', 'CFBundleVersion': '123'}
        (root / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps(info))
        (root / 'Contents' / 'MacOS' / name).write_text('fictional bundle executable')
        return root

    def collect(self):
        return proof.collect_identity(self.transport, self.config,
            browser_bundle=self.chrome, codex_bundle=self.codex)

    def write(self, path, value):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        raw = json.dumps(value, sort_keys=True).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}

    def make_proof(self):
        workers = [{'worker_id': f'apply-{i}', 'task_id': str(uuid.uuid4()),
                    'thread_id': str(uuid.uuid4()), 'tab_id': f'fictional-owned-tab-{i}'} for i in (1, 2, 3)]
        base = datetime.now(timezone.utc) - timedelta(minutes=10)
        gate_queues = {stage: [] for stage in ('browser_before', 'peer_work', 'browser_after')}
        for index, worker in enumerate(workers):
            for number, stage in enumerate(('browser_before', 'session_only', 'peer_work', 'browser_after')):
                if stage == 'peer_work' and worker['worker_id'] == 'apply-3':
                    continue
                browser = stage != 'session_only'
                run = self.transport.run_directory / (worker['worker_id'] + '-' + stage)
                receipt = {'application_worker_id': worker['worker_id'], 'task_id': worker['task_id'],
                    'thread_id': worker['thread_id'], 'turn_id': str(uuid.uuid4()), 'status': 'completed',
                    'turn_completed': True, 'exit_code': 0, 'transport': 'app-server',
                    'browser': browser, 'browser_verified': browser, 'session_worker': not browser,
                    'resumed_thread_id': worker['thread_id'] if number else None,
                    'tools': [{'server': 'cua', 'tool': 'js'}] if browser else []}
                result = {'worker_id': worker['worker_id'], 'task_id': worker['task_id']}
                if browser:
                    result.update(status='verified', tab_id=worker['tab_id'], owned_tab_ids=[worker['tab_id']],
                                  handoff_tab_ids=[worker['tab_id']])
                offset = number * 100 + (index * 25 if browser else 0)
                receipt.update(started_at=(base + timedelta(seconds=offset)).isoformat(),
                    finished_at=(base + timedelta(seconds=offset + 20)).isoformat())
                if browser:
                    enqueued = (base + timedelta(seconds=number*100-8+index*.1)).isoformat()
                    ticket_id = str(uuid.uuid4())
                    receipt['chrome_access'] = {'capacity': 1, 'policy': 'chrome_fifo_v1',
                        'ticket_id': ticket_id, 'enqueued_at': enqueued,
                        'acquired_at': (base + timedelta(seconds=offset-2)).isoformat(),
                        'started_at': (base + timedelta(seconds=offset-1)).isoformat(),
                        'release_requested_at': (base + timedelta(seconds=offset+21)).isoformat()}
                    gate_queues[stage].append({'ticket_id': ticket_id, 'worker_id': worker['worker_id'],
                        'task_id': worker['task_id'], 'role': 'application_prepare', 'enqueued_at': enqueued,
                        'acquired_at': None, 'started_at': None, 'state': 'waiting'})
                worker[stage] = {'started_at': (base + timedelta(seconds=offset)).isoformat(),
                    'finished_at': (base + timedelta(seconds=offset + 20)).isoformat(),
                    'receipt': self.write(run / 'receipt.json', receipt),
                    'result': self.write(run / 'result.json', result)}
        gate_evidence = {}
        for stage, queue in gate_queues.items():
            number = ('browser_before', 'session_only', 'peer_work', 'browser_after').index(stage)
            common = {'capacity': 1, 'policy': 'chrome_fifo_v1', 'state': 'free',
                      'owner': None, 'blocked_by': []}
            evidence = {'version': 1, 'stage': stage,
                'gate_directory': str(self.transport.run_directory / '_chrome_access'),
                'requested_at': {item['worker_id']: (base + timedelta(seconds=number*100-9)).isoformat()
                                 for item in queue},
                'queued_at': (base + timedelta(seconds=number*100-5)).isoformat(),
                'queued': {**common, 'revision': number*100+3, 'queue': queue},
                'finished_at': (base + timedelta(seconds=number*100+80)).isoformat(),
                'final': {**common, 'revision': number*100+30, 'queue': []}}
            gate_evidence[stage] = self.write(self.transport.run_directory / (stage+'-chrome-access.json'), evidence)
        return {'version': 3, 'guarantee': 'functional_application_tab_affinity',
                'chrome_access': {'capacity': 1, 'policy': 'chrome_fifo_v1'},
                'identity': self.identity, 'workers': workers, 'gate_evidence': gate_evidence}

    def install(self):
        return proof.install(self.store, self.path, self.identity)

    def test_private_receipts_prove_three_sessions_and_preserved_tabs(self):
        marker = self.install()
        self.assertEqual(marker['version'], 3)
        self.assertEqual(marker['chrome_access'], {'capacity': 1, 'policy': 'chrome_fifo_v1'})
        self.assertEqual(marker['guarantee'], 'functional_application_tab_affinity')
        self.assertTrue(marker['application_tab_affinity_verified'])
        self.assertNotIn('three_owned_tabs_isolated', marker)
        self.assertTrue(marker['session_only_handoff_preserved'])
        self.assertTrue(marker['chrome_fifo_verified'])
        self.assertEqual(marker['deployment_identity_digest'], proof.fingerprint(self.identity))
        self.assertEqual(marker, proof.resolve(self.store, self.identity))
        with self.store.transaction() as db:
            self.assertEqual(marker, proof.resolve(db, self.identity))
        restarted = WorkflowStore(self.store.path)
        self.assertEqual(marker, proof.resolve(restarted, self.identity))
        for worker in self.document['workers']:
            result = json.loads(Path(worker['browser_after']['result']['path']).read_text())
            self.assertNotIn('foreign_tab_checks', result)

    def test_prior_proofs_are_preserved_but_cannot_grant_v3_policy(self):
        for version in (1, 2):
            with self.subTest(version=version):
                legacy = copy.deepcopy(self.document)
                legacy['version'] = version
                legacy.pop('chrome_access')
                if version == 1:
                    legacy.pop('guarantee')
                    for worker in legacy['workers']:
                        worker.pop('peer_work', None)
                reference = self.write(self.path, legacy)
                original_bytes = self.path.read_bytes()
                with self.assertRaises(ReviewError):
                    self.install()
                self.assertEqual(self.path.read_bytes(), original_bytes)
                with self.store.transaction(True) as db:
                    db.execute('INSERT OR REPLACE INTO wf_settings(key,value) VALUES(?,?)',
                        (proof.SETTING_KEY, json.dumps({'version': version, 'proof': reference, 'identity': self.identity,
                         'deployment_identity_digest': proof.fingerprint(self.identity)})))
                self.assertIsNone(proof.resolve(self.store, self.identity))
                self.assertEqual(self.path.read_bytes(), original_bytes)

    def test_guarantee_must_name_functional_affinity(self):
        self.document['guarantee'] = 'permanent_browser_security_isolation'
        self.write(self.path, self.document)
        with self.assertRaises(ReviewError):
            self.install()

    def test_exact_two_peers_work_while_third_worker_stays_idle(self):
        for variant in ('missing_peer', 'third_peer', 'missing_third_resume'):
            with self.subTest(variant=variant):
                changed = copy.deepcopy(self.document)
                if variant == 'missing_peer':
                    changed['workers'][0].pop('peer_work')
                elif variant == 'third_peer':
                    changed['workers'][2]['peer_work'] = changed['workers'][0]['peer_work']
                else:
                    changed['workers'][2].pop('browser_after')
                self.write(self.path, changed)
                with self.assertRaises(ReviewError):
                    self.install()

    def test_two_peer_browser_turns_must_not_overlap(self):
        first, second = self.document['workers'][:2]
        second['peer_work']['started_at'] = (datetime.fromisoformat(
            first['peer_work']['started_at']) + timedelta(seconds=1)).isoformat()
        self.write(self.path, self.document)
        with self.assertRaises(ReviewError):
            self.install()

    def test_global_phase_barriers_hold_for_paused_worker(self):
        for variant in ('peer_before_third_session_finishes', 'third_resumes_during_peer_work'):
            with self.subTest(variant=variant):
                changed = copy.deepcopy(self.document)
                third = changed['workers'][2]
                peer = changed['workers'][0]['peer_work']
                if variant == 'peer_before_third_session_finishes':
                    third['session_only']['finished_at'] = (datetime.fromisoformat(
                        peer['started_at']) + timedelta(seconds=1)).isoformat()
                else:
                    third['browser_after']['started_at'] = (datetime.fromisoformat(
                        peer['finished_at']) - timedelta(seconds=1)).isoformat()
                self.write(self.path, changed)
                with self.assertRaises(ReviewError):
                    self.install()

    def test_each_browser_phase_requires_verified_original_tab(self):
        for phase in ('browser_before', 'peer_work', 'browser_after'):
            with self.subTest(phase=phase):
                changed = copy.deepcopy(self.document)
                worker = changed['workers'][0]
                path = Path(worker[phase]['result']['path'])
                original = path.read_bytes()
                result = json.loads(original)
                result['status'] = 'unknown'
                worker[phase]['result'] = self.write(path, result)
                self.write(self.path, changed)
                with self.assertRaises(ReviewError):
                    self.install()
                path.write_bytes(original)

    def test_missing_or_boolean_only_proof_never_grants_flags(self):
        self.assertIsNone(proof.resolve(self.store, self.identity))
        self.write(self.path, {'three_owned_tabs_isolated': True, 'session_only_handoff_preserved': True})
        with self.assertRaises(ReviewError):
            self.install()
        self.assertIsNone(proof.resolve(self.store, self.identity))

    def test_changed_source_invalidates_even_cached_expected_identity(self):
        self.install()
        source = self.sources / 'codex_transport.py'
        source.write_text('# changed source\n')
        self.assertIsNone(proof.resolve(self.store, self.identity))
        self.assertIsNone(proof.resolve(self.store, self.collect()))

    def test_chrome_gate_source_is_required_and_version_bound(self):
        self.install()
        self.assertIn('chrome_access_gate.py', self.identity['sources'])
        (self.sources / 'chrome_access_gate.py').write_text('# changed FIFO gate\n')
        self.assertIsNone(proof.resolve(self.store, self.identity))

    def test_serial_policy_and_capacity_are_required_from_actual_transport(self):
        for field, value in [('chrome_access_capacity', 2), ('chrome_access_capacity', True),
                             ('chrome_access_policy', 'parallel')]:
            with self.subTest(field=field, value=value):
                original = getattr(self.transport, field)
                setattr(self.transport, field, value)
                with self.assertRaises(ReviewError):
                    self.collect()
                setattr(self.transport, field, original)
        for key in ('chrome_access_capacity', 'chrome_access_policy'):
            config = self.config.copy()
            self.config.pop(key)
            with self.assertRaises(ReviewError):
                self.collect()
            self.config = config

    def test_proof_cannot_claim_another_capacity_or_policy(self):
        for access in ({'capacity': 3, 'policy': 'chrome_fifo_v1'},
                       {'capacity': True, 'policy': 'chrome_fifo_v1'},
                       {'capacity': 1, 'policy': 'parallel'}):
            with self.subTest(access=access):
                self.document['chrome_access'] = access
                self.write(self.path, self.document)
                with self.assertRaises(ReviewError):
                    self.install()

    def test_tool_free_native_turns_can_overlap(self):
        turns = [worker['session_only'] for worker in self.document['workers']]
        self.assertEqual(len({turn['started_at'] for turn in turns}), 1)
        self.assertEqual(len({turn['finished_at'] for turn in turns}), 1)
        self.assertTrue(self.install()['session_only_handoff_preserved'])

    def test_raw_sequential_native_receipts_cannot_replace_actual_gate_evidence(self):
        self.document.pop('gate_evidence')
        self.write(self.path, self.document)
        with self.assertRaises(ReviewError):
            self.install()

    def test_gate_snapshot_requires_all_requests_before_admission_and_fifo(self):
        path = Path(self.document['gate_evidence']['browser_before']['path'])
        original = json.loads(path.read_text())
        for variant in ('missing_waiter', 'reversed_fifo', 'wrong_task', 'late_queue', 'uncertain_owner', 'other_gate'):
            with self.subTest(variant=variant):
                changed = copy.deepcopy(original)
                if variant == 'missing_waiter':
                    changed['queued']['queue'].pop()
                elif variant == 'reversed_fifo':
                    changed['queued']['queue'].reverse()
                elif variant == 'wrong_task':
                    changed['queued']['queue'][0]['task_id'] = str(uuid.uuid4())
                elif variant == 'late_queue':
                    changed['queued_at'] = self.document['workers'][1]['browser_before']['started_at']
                elif variant == 'uncertain_owner':
                    changed['final']['state'] = 'uncertain'
                    changed['final']['owner'] = changed['queued']['queue'][0]
                else:
                    changed['gate_directory'] = str(self.root / 'unrelated-gate')
                self.document['gate_evidence']['browser_before'] = self.write(path, changed)
                self.write(self.path, self.document)
                with self.assertRaises(ReviewError):
                    self.install()

    def test_browser_receipt_requires_exact_ticket_and_native_interval(self):
        worker = self.document['workers'][0]
        path = Path(worker['browser_before']['receipt']['path'])
        original = json.loads(path.read_text())
        for variant in ('missing_ticket', 'different_ticket', 'native_before_acquire', 'release_before_completion'):
            with self.subTest(variant=variant):
                changed = copy.deepcopy(original)
                if variant == 'missing_ticket':
                    changed.pop('chrome_access')
                elif variant == 'different_ticket':
                    changed['chrome_access']['ticket_id'] = str(uuid.uuid4())
                elif variant == 'native_before_acquire':
                    changed['chrome_access']['acquired_at'] = changed['finished_at']
                else:
                    changed['chrome_access']['release_requested_at'] = changed['started_at']
                worker['browser_before']['receipt'] = self.write(path, changed)
                self.write(self.path, self.document)
                with self.assertRaises(ReviewError):
                    self.install()

    def test_altered_gate_evidence_invalidates_installed_proof(self):
        self.install()
        path = Path(self.document['gate_evidence']['peer_work']['path'])
        path.write_text('{}')
        self.assertIsNone(proof.resolve(self.store, self.identity))

    def test_changed_codex_binary_invalidates(self):
        self.install()
        self.binary.write_text('updated fictional executable')
        self.assertIsNone(proof.resolve(self.store, self.identity))

    def test_retargeted_binary_symlink_invalidates(self):
        alternate = self.root / 'codex-new'
        alternate.write_text('new fictional executable')
        linked = self.root / 'codex-current'
        linked.symlink_to(self.binary)
        self.transport.binary = linked
        self.identity = self.collect()
        self.document['identity'] = self.identity
        self.write(self.path, self.document)
        self.install()
        linked.unlink(); linked.symlink_to(alternate)
        self.assertIsNone(proof.resolve(self.store, self.identity))

    def test_changed_browser_version_executable_or_codex_bundle_invalidates(self):
        for path in (self.chrome / 'Contents' / 'Info.plist',
                     self.chrome / 'Contents' / 'MacOS' / 'Chrome',
                     self.codex / 'Contents' / 'Info.plist'):
            with self.subTest(path=path.name):
                self.install()
                original = path.read_bytes()
                if path.suffix == '.plist':
                    info = plistlib.loads(original); info['CFBundleVersion'] = '999'
                    path.write_bytes(plistlib.dumps(info))
                else:
                    path.write_bytes(b'updated browser executable')
                self.assertIsNone(proof.resolve(self.store, self.identity))
                path.write_bytes(original)

    def test_behavioral_config_changed_but_fixture_root_can_differ(self):
        self.install()
        self.transport.run_directory = self.root / 'separate-production-runs'
        self.assertEqual(self.collect(), self.identity)
        self.transport.browser_session_policy = 'shared'
        self.assertIsNone(proof.resolve(self.store, self.collect()))

    def test_missing_altered_or_public_evidence_invalidates(self):
        self.install()
        path = Path(self.document['workers'][0]['browser_after']['result']['path'])
        original = path.read_bytes()
        path.write_text('{}')
        self.assertIsNone(proof.resolve(self.store, self.identity))
        path.write_bytes(original); path.chmod(0o644)
        self.assertIsNone(proof.resolve(self.store, self.identity))
        path.unlink()
        self.assertIsNone(proof.resolve(self.store, self.identity))

    def test_changed_proof_or_other_host_invalidates(self):
        self.install()
        self.document['identity'] = copy.deepcopy(self.identity)
        self.document['identity']['host']['node'] = 'another-fictional-host'
        self.write(self.path, self.document)
        self.assertIsNone(proof.resolve(self.store, self.identity))

    def test_incomplete_identity_and_unknown_config_keys_rejected(self):
        for key in self.identity:
            identity = copy.deepcopy(self.identity); identity.pop(key)
            with self.assertRaises(ReviewError):
                proof.install(self.store, self.path, identity)
        self.config['token'] = 'fictional-not-a-real-credential'
        with self.assertRaises(ReviewError):
            self.collect()

    def test_browser_overlap_or_reused_thread_or_wrong_owned_tab_rejected(self):
        original = copy.deepcopy(self.document)
        variants = []
        changed = copy.deepcopy(original)
        changed['workers'][1]['thread_id'] = changed['workers'][0]['thread_id']; variants.append(changed)
        changed = copy.deepcopy(original)
        changed['workers'][1]['browser_before']['started_at'] = (datetime.fromisoformat(
            changed['workers'][0]['browser_before']['finished_at']) - timedelta(seconds=1)).isoformat()
        variants.append(changed)
        for changed in variants:
            self.write(self.path, changed)
            with self.assertRaises(ReviewError):
                self.install()
        self.document = original
        worker = original['workers'][0]
        path = Path(worker['browser_after']['result']['path'])
        result = json.loads(path.read_text()); result['owned_tab_ids'] = [original['workers'][1]['tab_id']]
        worker['browser_after']['result'] = self.write(path, result)
        self.write(self.path, original)
        with self.assertRaises(ReviewError):
            self.install()

    def test_session_only_cannot_use_browser_tools_or_change_native_thread(self):
        worker = self.document['workers'][0]
        path = Path(worker['session_only']['receipt']['path'])
        original = json.loads(path.read_text())
        for field, value in [('tools', [{'server': 'cua', 'tool': 'js'}]),
                             ('resumed_thread_id', str(uuid.uuid4()))]:
            with self.subTest(field=field):
                receipt = {**original, field: value}
                worker['session_only']['receipt'] = self.write(path, receipt)
                self.write(self.path, self.document)
                with self.assertRaises(ReviewError):
                    self.install()


if __name__ == '__main__':
    unittest.main()
