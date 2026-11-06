"""Offline adversarial tests for the experimental release signing channel.

Only temporary synthetic releases and fresh test keys are used. These tests do
not connect to a provider, read a deployment key or approve a production release.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('strict_release_sign', ROOT / 'tools/release_sign.py')
signing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(signing)


class ReleaseSigningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='release-signing-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release = self.root / 'release'
        self.files = self.release / 'files'
        self.files.mkdir(parents=True)
        self.program = self.files / 'app.py'
        self.program.write_text('print("synthetic release")\n', encoding='utf-8')
        self.program.chmod(0o644)
        self.private = ed25519.Ed25519PrivateKey.generate()
        self.trusted = self.root / 'independently-trusted.pem'
        self.trusted.write_bytes(self.public_pem(self.private))
        self.bom = signing.build_bom(self.files)
        self.seal(self.bom)

    @staticmethod
    def public_pem(private):
        return private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def seal(self, bom, *, private=None, raw=None):
        private = private or self.private
        raw = raw if raw is not None else json.dumps(bom, sort_keys=True).encode()
        public = self.public_pem(private)
        key_id = hashlib.sha256(public).hexdigest()[:16]
        trust = self.release / 'trust'
        trust.mkdir(exist_ok=True)
        (trust / f'release-pub-{key_id}.pem').write_bytes(public)
        (self.release / 'BOM.json').write_bytes(raw)
        signature = {'schema': signing.SCHEMA_SIG, 'algorithm': 'ed25519',
                     'key_id': key_id, 'signed_file': 'BOM.json',
                     'signed_sha256': hashlib.sha256(raw).hexdigest(),
                     'signature_hex': private.sign(raw).hex()}
        (self.release / 'BOM.json.sig.json').write_text(json.dumps(signature))

    def verify(self, trusted=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = signing.cmd_verify(SimpleNamespace(
                release_dir=str(self.release), trusted_key=str(trusted or self.trusted)))
        return code, json.loads(output.getvalue())

    def test_legitimate_release_requires_external_trust_and_has_no_production_approval(self):
        code, receipt = self.verify()
        self.assertEqual(code, 0)
        self.assertTrue(receipt['publisher_verified'])
        self.assertFalse(receipt['production_approval'])
        self.assertEqual(receipt['files_checked'], 1)

    def test_self_signed_replacement_cannot_choose_its_own_publisher(self):
        attacker = ed25519.Ed25519PrivateKey.generate()
        self.program.write_text('print("replacement")\n')
        self.seal(signing.build_bom(self.files), private=attacker)
        with self.assertRaisesRegex(SystemExit, 'independently trusted publisher'):
            self.verify()

    def test_missing_trust_anchor_fails_closed(self):
        with self.assertRaisesRegex(SystemExit, 'trusted-key is required'):
            signing.cmd_verify(SimpleNamespace(release_dir=str(self.release)))

    def test_bundled_trust_anchor_is_rejected_even_with_parent_components(self):
        bundled = next((self.release / 'trust').glob('*.pem'))
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        aliases = (bundled, elsewhere / '..' / 'release' / 'trust' / bundled.name)
        for path in aliases:
            with self.subTest(path=path), self.assertRaisesRegex(SystemExit, 'outside the release'):
                self.verify(path)

    @unittest.skipIf(os.name == 'nt', 'requires POSIX modes')
    def test_world_writable_or_special_modes_are_not_normalized_during_verification(self):
        self.program.chmod(0o755)
        self.bom = signing.build_bom(self.files)
        self.seal(self.bom)
        for mode in (0o777, 0o4755, 0o2755):
            self.program.chmod(mode)
            with self.subTest(mode=mode):
                code, receipt = self.verify()
                self.assertEqual(code, 1)
                self.assertIn('mode mismatch', receipt['problems'][0])

    def test_unlisted_bytecode_in_formerly_ignored_directory_fails(self):
        hidden = self.files / '__pycache__'
        hidden.mkdir()
        (hidden / 'unlisted.pyc').write_bytes(b'unlisted synthetic bytecode')
        code, receipt = self.verify()
        self.assertEqual(code, 1)
        self.assertEqual(receipt['unlisted_files'], ['__pycache__/unlisted.pyc'])

    @unittest.skipIf(os.name == 'nt', 'requires unprivileged link creation')
    def test_runtime_file_and_directory_symlinks_are_refused(self):
        external = self.root / 'external.py'
        external.write_bytes(self.program.read_bytes())
        self.program.unlink()
        self.program.symlink_to(external)
        with self.assertRaisesRegex(SystemExit, 'symbolic link'):
            self.verify()
        self.program.unlink()
        self.program.write_bytes(external.read_bytes())
        (self.files / 'linked-directory').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, 'symbolic link'):
            self.verify()

    @unittest.skipIf(os.name == 'nt', 'requires unprivileged link creation')
    def test_external_hardlink_is_refused(self):
        external = self.root / 'external.py'
        external.write_bytes(self.program.read_bytes())
        self.program.unlink()
        os.link(external, self.program)
        with self.assertRaisesRegex(SystemExit, 'without links'):
            self.verify()

    def test_signed_path_traversal_is_refused_before_hashing(self):
        for name in ('../outside.py', '/tmp/outside.py', './app.py', 'a//b', 'a\\b', 'x:stream'):
            self.bom['files'][0]['path'] = name
            self.seal(self.bom)
            with self.subTest(path=name), self.assertRaisesRegex(SystemExit, 'BOM file path'):
                self.verify()

    def test_duplicate_file_entries_and_inconsistent_count_are_refused(self):
        self.bom['files'].append(dict(self.bom['files'][0]))
        self.bom['file_count'] = 2
        self.seal(self.bom)
        with self.assertRaisesRegex(SystemExit, 'duplicate'):
            self.verify()
        self.bom['files'].pop()
        self.seal(self.bom)
        with self.assertRaisesRegex(SystemExit, 'file count'):
            self.verify()

    def test_duplicate_json_keys_are_refused_even_if_signature_is_valid(self):
        encoded = json.dumps(self.bom).encode()
        raw = b'{"file_count":999,' + encoded[1:]
        self.seal(self.bom, raw=raw)
        with self.assertRaisesRegex(SystemExit, 'invalid release JSON'):
            self.verify()

    def test_tampered_runtime_and_signature_fail(self):
        self.program.write_text('changed bytes')
        code, receipt = self.verify()
        self.assertEqual(code, 1)
        self.assertFalse(receipt['publisher_verified'])
        (self.release / 'BOM.json').write_text('{}')
        with self.assertRaisesRegex(SystemExit, 'signature verification FAILED'):
            self.verify()

    def bundle_args(self, source, release, key):
        return SimpleNamespace(source=str(source), out=str(release), key=str(key),
                               passphrase_env='TEST_RELEASE_PASSPHRASE', label='test',
                               source_archive='', source_archive_sha256='', signer='test')

    def encrypted_private(self, destination):
        destination.write_bytes(self.private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b'test-only-passphrase')))
        destination.chmod(0o600)

    @unittest.skipIf(os.name == 'nt', 'requires POSIX modes')
    def test_bundler_normalizes_actual_staged_permissions_and_roundtrips(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'app.py').write_text('print("test")\n')
        (source / 'app.py').chmod(0o664)
        (source / '.DS_Store').write_text('not a runtime file')
        key = self.root / 'private.pem'
        self.encrypted_private(key)
        output = self.root / 'bundled'
        with mock.patch.dict(os.environ, TEST_RELEASE_PASSPHRASE='test-only-passphrase'):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(signing.cmd_bundle(self.bundle_args(source, output, key)), 0)
        self.assertEqual((output / 'files/app.py').stat().st_mode & 0o7777, 0o644)
        self.assertFalse((output / 'files/.DS_Store').exists())
        self.release = output
        self.assertEqual(self.verify()[0], 0)

    def test_bundler_refuses_overlapping_trees_and_a_packaged_private_key(self):
        source = self.root / 'source'
        source.mkdir()
        key = self.root / 'private.pem'
        self.encrypted_private(key)
        with self.assertRaisesRegex(SystemExit, 'must not overlap'):
            signing.cmd_bundle(self.bundle_args(source, source / 'release', key))
        inside = source / 'private.pem'
        self.encrypted_private(inside)
        with self.assertRaisesRegex(SystemExit, 'private signing key must be outside'):
            signing.cmd_bundle(self.bundle_args(source, self.root / 'out', inside))

    @unittest.skipIf(os.name == 'nt', 'requires POSIX permissions')
    def test_keygen_creates_private_key_with_private_permissions(self):
        with mock.patch.dict(os.environ, TEST_RELEASE_PASSPHRASE='test-only-passphrase'):
            with contextlib.redirect_stdout(io.StringIO()):
                result = signing.cmd_keygen(SimpleNamespace(
                    out_dir=str(self.root / 'keys'), passphrase_env='TEST_RELEASE_PASSPHRASE'))
        self.assertEqual(result, 0)
        private = next((self.root / 'keys').glob('release-priv-*'))
        self.assertEqual(private.stat().st_mode & 0o7777, 0o600)


if __name__ == '__main__':
    unittest.main()
