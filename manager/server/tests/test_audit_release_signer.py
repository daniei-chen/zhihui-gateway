"""Offline Ed25519 signer regressions; every key and output is synthetic."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


_SCRIPT = Path(__file__).resolve().parents[2] / 'deploy/sign-release-ed25519.py'
_SPEC = importlib.util.spec_from_file_location('audit_ed25519_signer', _SCRIPT)
signer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(signer)


class ReleaseSignerSafetyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'deploy').mkdir()
        (self.root / 'server').mkdir()
        (self.root / 'server/old.py').write_text('old = 1\n')
        (self.root / 'server/new.py').write_text('new = 1\n')
        for path in (self.root / 'server').iterdir():
            path.chmod(0o644)
        self.manifest = self.root / 'deploy/managed-release.json'
        self.manifest.write_text(json.dumps({'files': {
            'server/old.py': {'candidate_sha256': '0' * 64, 'mode': 0o644}}}))
        self.signature = self.manifest.with_suffix('.sig')
        self.signature.write_bytes(b'previous detached signature')
        self.before = self.manifest.read_bytes()
        self.before_signature = self.signature.read_bytes()
        self.args = ['--manifest', str(self.manifest)]

    def unchanged(self):
        self.assertEqual(self.manifest.read_bytes(), self.before)
        self.assertEqual(self.signature.read_bytes(), self.before_signature)

    def test_add_dry_run_does_not_mutate_manifest_or_signature(self):
        with mock.patch.object(signer, 'sign_file') as sign:
            self.assertEqual(signer.main(self.args + ['--add', 'server/new.py', '--dry-run']), 0)
        self.unchanged()
        sign.assert_not_called()

    def test_sign_only_dry_run_never_requires_key_or_signs(self):
        with mock.patch.object(signer, 'resolve_key') as key, mock.patch.object(signer, 'sign_file') as sign:
            self.assertEqual(signer.main(self.args + ['--sign-only', '--dry-run']), 0)
        self.unchanged()
        key.assert_not_called()
        sign.assert_not_called()

    def test_dry_run_rejects_bad_modes_using_the_real_validation(self):
        (self.root / 'server/old.py').chmod(0o777)
        with self.assertRaises(SystemExit) as error:
            signer.main(self.args + ['--dry-run'])
        self.assertEqual(error.exception.code, 4)
        self.unchanged()

    def test_invalid_add_never_persists_partial_additions(self):
        outside = self.root / 'outside.py'
        outside.write_text('outside = 1\n')
        for relative in ('../outside.py', str(outside), 'server/../outside.py', 'server//new.py'):
            with self.subTest(relative=relative), self.assertRaises(SystemExit):
                signer.main(self.args + ['--add', 'server/new.py', '--add', relative, '--dry-run'])
            self.unchanged()

    def test_directory_symlink_cannot_supply_a_source(self):
        (self.root / 'linked').symlink_to(self.root / 'server', target_is_directory=True)
        with self.assertRaises(SystemExit):
            signer.main(self.args + ['--add', 'linked/new.py', '--dry-run'])
        self.unchanged()

    def test_manifest_cannot_hash_its_detached_signature(self):
        with self.assertRaises(SystemExit):
            signer.main(self.args + ['--add', 'deploy/managed-release.sig', '--dry-run'])
        self.unchanged()

    def test_missing_key_leaves_manifest_untouched(self):
        with mock.patch.dict(os.environ, {'WB_SIGN_KEY': ''}):
            with self.assertRaises(SystemExit) as error:
                signer.main(self.args + ['--add', 'server/new.py'])
        self.assertEqual(error.exception.code, 2)
        self.unchanged()

    def test_signing_failure_preserves_previous_signature(self):
        failed = subprocess.CompletedProcess([], 1, b'', b'synthetic signing failure')
        with mock.patch.object(signer.subprocess, 'run', return_value=failed):
            with self.assertRaises(SystemExit):
                signer.sign_file(self.root / 'synthetic-key', self.manifest, self.signature)
        self.unchanged()
        self.assertEqual(sorted(p.name for p in self.signature.parent.iterdir()),
                         ['managed-release.json', 'managed-release.sig'])

    def test_rebuild_add_sign_and_verify_round_trip(self):
        key, trust = self.root / 'private.pem', self.root / 'public.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'Ed25519', '-out', str(key)],
                       check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(trust)],
                       check=True, capture_output=True)
        self.assertEqual(signer.main(self.args + [
            '--add', 'server/new.py', '--key', str(key), '--trust', str(trust)]), 0)
        release = json.loads(self.manifest.read_text())
        self.assertEqual(set(release['files']), {'server/old.py', 'server/new.py'})
        self.assertEqual(release['signature_algorithm'], 'ed25519')
        signer.verify_file(trust, self.manifest, self.signature)
        self.assertEqual(self.signature.stat().st_mode & 0o777, 0o644)

    def test_wrong_signing_key_preserves_previous_trusted_signature(self):
        key, wrong, trust = self.root / 'private.pem', self.root / 'wrong.pem', self.root / 'public.pem'
        for path in (key, wrong):
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'Ed25519', '-out', str(path)],
                           check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(trust)],
                       check=True, capture_output=True)
        signer.sign_file(key, self.manifest, self.signature, trust=trust)
        previous = self.signature.read_bytes()
        with self.assertRaises(SystemExit) as error:
            signer.main(self.args + ['--sign-only', '--key', str(wrong), '--trust', str(trust)])
        self.assertEqual(error.exception.code, 3)
        self.assertEqual(self.signature.read_bytes(), previous)
        signer.verify_file(trust, self.manifest, self.signature)


if __name__ == '__main__':
    unittest.main()
