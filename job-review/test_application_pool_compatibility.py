"""Exact operator compatibility audits. Fictional files, no native/browser calls."""
import copy
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import application_pool_proof as proof
import test_application_pool_proof as fixtures
from store import ReviewError
from workflow import WorkflowStore


class ApplicationPoolCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ApplicationPoolProofTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.sources = self.fixture.root, self.fixture.sources
        self.store, self.baseline = self.fixture.store, self.fixture.identity
        (self.sources / 'applicant_evidence.py').write_text('# original reference helper\n')
        self.candidate = self.root / 'candidate'
        shutil.copytree(self.sources, self.candidate)
        for name in proof.COMPATIBILITY_SOURCES:
            (self.candidate / name).write_text('# reviewed candidate ' + name + '\n')
        self.manifest_path = self.root / 'runtime-manifest.json'
        self.patch_path = self.root / 'reviewed.patch'
        self.tests_path = self.root / 'tests.json'
        self.log_path = self.root / 'tests.log'
        self.audit_path = self.root / 'compatibility.json'
        self.review()

    def private_bytes(self, path, value):
        path.write_bytes(value); path.chmod(0o600)
        return {'path': str(path), 'sha256': hashlib.sha256(value).hexdigest()}

    def review(self):
        self.manifest = [{'file': name,
            'before_sha256': hashlib.sha256((self.sources / name).read_bytes()).hexdigest(),
            'after_sha256': hashlib.sha256((self.candidate / name).read_bytes()).hexdigest()}
            for name in sorted(proof.COMPATIBILITY_SOURCES)]
        reference = self.fixture.write(self.manifest_path, self.manifest)
        delta = ''.join(line for row in self.manifest for line in difflib.unified_diff(
            (self.sources / row['file']).read_text().splitlines(keepends=True),
            (self.candidate / row['file']).read_text().splitlines(keepends=True),
            fromfile='a/' + row['file'], tofile='b/' + row['file'])).encode()
        self.private_bytes(self.patch_path, delta)
        log = self.private_bytes(self.log_path, b'Fictional fixture: 12 passed, 2 skipped.\n')
        self.test_report = {'version': 1, 'status': 'passed', 'source_manifest_sha256': reference['sha256'],
            'suites': [{'command': 'fictional offline tests', 'run': 14, 'passed': 12, 'skipped': 2, 'log': log}]}
        self.fixture.write(self.tests_path, self.test_report)

    def prepare(self):
        return proof.prepare_compatibility(self.fixture.path, self.baseline,
            installed_source_root=self.sources, target_source_root=self.candidate,
            source_manifest_file=self.manifest_path, patch_file=self.patch_path,
            tests_file=self.tests_path, audit_file=self.audit_path)

    def deploy(self):
        for name in proof.COMPATIBILITY_SOURCES:
            (self.sources / name).write_bytes((self.candidate / name).read_bytes())
        return self.fixture.collect()

    def install(self):
        self.prepare(); current = self.deploy()
        return current, proof.install_compatibility(self.store, self.audit_path, current)

    def assert_no_setting(self):
        with self.store.transaction() as db:
            self.assertIsNone(db.execute('SELECT value FROM wf_settings WHERE key=?', (proof.SETTING_KEY,)).fetchone())

    def test_prepare_validates_explicit_installed_root_without_target_runtime_dependency(self):
        original = self.fixture.path.read_bytes()
        with patch.object(proof, 'SOURCE_ROOT', self.candidate):
            reference = self.prepare()  # Isolated adapter inspecting actual installed root.
        self.assert_no_setting()
        self.assertEqual(reference['path'], str(self.audit_path))
        self.assertEqual(self.audit_path.stat().st_mode & 0o777, 0o600)
        audit = json.loads(self.audit_path.read_text())
        self.assertNotIn(str(self.candidate), self.audit_path.read_text())
        self.assertEqual(audit['baseline_identity'], self.baseline)
        self.assertEqual(audit['native_identity_digest'], hashlib.sha256(proof.encode(self.baseline).encode()).hexdigest())
        current = self.deploy()
        marker = proof.install_compatibility(self.store, self.audit_path, current)
        shutil.rmtree(self.candidate)
        self.assertEqual(proof.resolve(self.store, current), marker)
        self.assertEqual(proof.resolve(WorkflowStore(self.store.path), current), marker)
        with self.store.transaction() as db:
            self.assertEqual(proof.resolve(db, current), marker)
        self.assertEqual(marker['compatibility_reference'], reference)
        self.assertEqual(marker['deployment_identity_digest'], proof.fingerprint(current))
        self.assertEqual(marker['native_identity_digest'], audit['native_identity_digest'])
        self.assertNotEqual(marker['deployment_identity_digest'], marker['native_identity_digest'])
        self.assertEqual(marker['evidence_reference'], str(self.fixture.path))
        self.assertEqual(self.fixture.path.read_bytes(), original)

    def test_no_compatibility_implicit_on_changed_runtime(self):
        self.fixture.install()
        current = self.deploy()
        self.assertIsNone(proof.resolve(self.store, self.baseline))
        self.assertIsNone(proof.resolve(self.store, current))
        with self.assertRaises(ReviewError): proof.install(self.store, self.fixture.path, current)
        self.assertFalse(self.audit_path.exists())

    def test_prepare_rejects_changed_baseline_before_any_audit_or_setting(self):
        (self.sources / 'codex_runtime.py').write_text('# already deployed too early\n')
        with self.assertRaises(ReviewError): self.prepare()
        self.assertFalse(self.audit_path.exists()); self.assert_no_setting()

    def test_prepare_rejects_invalid_historical_receipt(self):
        path = Path(self.fixture.document['workers'][0]['browser_after']['receipt']['path'])
        value = json.loads(path.read_text()); value['turn_completed'] = False
        self.fixture.write(path, value)
        with self.assertRaises(ReviewError): self.prepare()
        self.assertFalse(self.audit_path.exists()); self.assert_no_setting()

    def test_prepare_rejects_any_unreviewed_or_native_source_change(self):
        for name in ('codex_appserver.py', 'codex_transport.py', 'chrome_access_gate.py',
                     'pooled_browser_session.py', 'shared_browser_session.py', 'application_pool.py',
                     'orchestrator.py', 'task_store.py', 'pool_application.py'):
            with self.subTest(name=name):
                path = self.candidate / name; original = path.read_bytes()
                path.write_bytes(b'# native change\n')
                with self.assertRaises(ReviewError): self.prepare()
                path.write_bytes(original)
        self.assertFalse(self.audit_path.exists()); self.assert_no_setting()

    def test_prepare_rejects_inexact_patch_or_manifest_or_tests(self):
        cases = ['patch', 'manifest', 'tests', 'log_hash']
        for case in cases:
            with self.subTest(case=case):
                self.review()
                if case == 'patch': self.private_bytes(self.patch_path, b'Not this reviewed source delta\n')
                if case == 'manifest': self.fixture.write(self.manifest_path, self.manifest[:-1])
                if case in ('tests', 'log_hash'):
                    value = copy.deepcopy(self.test_report)
                    if case == 'tests': value['source_manifest_sha256'] = '0' * 64
                    else: value['suites'][0]['log']['sha256'] = None
                    self.fixture.write(self.tests_path, value)
                with self.assertRaises(ReviewError): self.prepare()
                self.assertFalse(self.audit_path.exists()); self.assert_no_setting()

    def test_prepare_never_overwrites_existing_audit(self):
        self.prepare(); original = self.audit_path.read_bytes()
        with self.assertRaises(FileExistsError): self.prepare()
        self.assertEqual(self.audit_path.read_bytes(), original)

    def test_install_requires_exact_deployed_target(self):
        self.prepare()
        with self.assertRaises(ReviewError): proof.install_compatibility(self.store, self.audit_path, self.baseline)
        self.assert_no_setting()
        current = self.deploy()
        (self.sources / 'readiness.py').write_text('# another unreviewed change\n')
        with self.assertRaises(ReviewError): proof.install_compatibility(self.store, self.audit_path, current)
        self.assert_no_setting()

    def test_all_current_sources_and_supplemental_helper_remain_pinned(self):
        current, marker = self.install()
        for name in (*proof.SOURCE_NAMES, 'applicant_evidence.py'):
            with self.subTest(name=name):
                path = self.sources / name; original = path.read_bytes()
                path.write_bytes(b'# unreviewed post-install change\n')
                self.assertIsNone(proof.resolve(self.store, current))
                self.assertIsNone(proof.resolve(self.store, self.fixture.collect()))
                path.write_bytes(original)
        self.assertEqual(proof.resolve(self.store, current), marker)

    def test_audit_manifest_patch_test_log_and_original_evidence_tamper_all_fail_closed(self):
        current, marker = self.install()
        paths = [self.audit_path, self.manifest_path, self.patch_path, self.tests_path, self.log_path,
                 self.fixture.path, Path(self.fixture.document['gate_evidence']['browser_before']['path']),
                 Path(self.fixture.document['workers'][0]['browser_after']['result']['path'])]
        for path in paths:
            with self.subTest(path=path.name):
                original = path.read_bytes(); path.write_bytes(original + b' ')
                self.assertIsNone(proof.resolve(self.store, current))
                path.write_bytes(original)
        self.assertEqual(proof.resolve(self.store, current), marker)

    def test_retargeted_unsafe_external_identity_invalidates_compatibility(self):
        current, marker = self.install()
        original = self.fixture.binary.read_bytes()
        self.fixture.binary.write_bytes(b'changed binary')
        self.assertIsNone(proof.resolve(self.store, current))
        self.fixture.binary.write_bytes(original)
        for field, value in [('browser_session_policy', 'shared'), ('timeout', 100),
                             ('heartbeat_interval', 30), ('cwd', self.root)]:
            with self.subTest(field=field):
                previous = getattr(self.fixture.transport, field)
                setattr(self.fixture.transport, field, value)
                self.assertIsNone(proof.resolve(self.store, self.fixture.collect()))
                setattr(self.fixture.transport, field, previous)
        self.assertEqual(proof.resolve(self.store, current), marker)

    def test_rehashed_forged_native_receipt_still_fails_semantic_validation(self):
        self.prepare(); current = self.deploy()
        audit = json.loads(self.audit_path.read_text())
        native = copy.deepcopy(self.fixture.document)
        observation = native['workers'][0]['session_only']
        receipt_path = Path(observation['receipt']['path'])
        receipt = json.loads(receipt_path.read_text())
        receipt['tools'] = [{'server': 'cua', 'tool': 'js'}]
        observation['receipt'] = self.fixture.write(receipt_path, receipt)
        audit['native_proof'] = self.fixture.write(self.fixture.path, native)
        self.fixture.write(self.audit_path, audit)
        with self.assertRaises(ReviewError):
            proof.install_compatibility(self.store, self.audit_path, current)
        self.assert_no_setting()

    def test_rehashed_audit_cannot_approve_new_native_configuration(self):
        self.prepare(); self.deploy()
        self.fixture.transport.timeout = 100
        current = self.fixture.collect()
        audit = json.loads(self.audit_path.read_text())
        audit['target_identity'] = current
        self.fixture.write(self.audit_path, audit)
        with self.assertRaises(ReviewError):
            proof.install_compatibility(self.store, self.audit_path, current)
        self.assert_no_setting()

    def test_public_or_missing_review_evidence_cannot_grant_capability(self):
        current, _ = self.install()
        self.log_path.chmod(0o644)
        self.assertIsNone(proof.resolve(self.store, current))
        self.log_path.chmod(0o600)
        self.audit_path.unlink()
        self.assertIsNone(proof.resolve(self.store, current))


if __name__ == '__main__': unittest.main()
