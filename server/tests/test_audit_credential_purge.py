"""Credential cleanup must preserve original private recovery evidence."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


_SCRIPT = Path(__file__).resolve().parents[2] / 'deploy/purge_credentials.py'
_SPEC = importlib.util.spec_from_file_location('audit_credential_purge', _SCRIPT)
purge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(purge)


class CredentialPurgeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'users.json'
        self.original = json.dumps({'secret': 'synthetic-old-secret', 'api_keys': ['synthetic-old-key'],
                                    'users': [{'name': 'fixture', 'password': 'fixture hash', 'sv': 3}]})
        self.path.write_text(self.original)
        patcher = mock.patch.dict(os.environ, {'WB_USERS_FILE': str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_original_backup_and_rotated_credentials_are_private(self):
        before = self.path.stat()
        old_umask = os.umask(0o022)
        try:
            self.assertEqual(purge.main(), 0)
        finally:
            os.umask(old_umask)
        backups = list(self.root.glob('users.json.bak.*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)
        if os.name == 'posix':
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.path.stat().st_uid, self.path.stat().st_gid), (before.st_uid, before.st_gid))
        result = json.loads(self.path.read_text())
        self.assertNotEqual(result['secret'], 'synthetic-old-secret')
        self.assertEqual(result['api_keys'], [])
        self.assertEqual(result['users'][0]['sv'], 4)
        self.assertEqual(result['users'][0]['password'], 'fixture hash')

    def test_atomic_replace_failure_preserves_original_and_backup(self):
        with mock.patch.object(purge.os, 'replace', side_effect=OSError('synthetic disk failure')):
            self.assertEqual(purge.main(), 1)
        self.assertEqual(self.path.read_text(), self.original)
        self.assertEqual(next(self.root.glob('users.json.bak.*')).read_text(), self.original)
        self.assertEqual(list(self.root.glob('users.json.rotate.*')), [])

    def test_symlink_input_and_predictable_backup_cannot_overwrite_other_files(self):
        external = self.root / 'protected'
        external.write_bytes(b'outside sentinel')
        predictable = self.root / 'users.json.bak'
        try:
            predictable.symlink_to(external)
        except OSError as exc:
            self.skipTest(f'filesystem cannot create a synthetic symlink: {exc}')
        self.assertEqual(purge.main(), 0)
        self.assertEqual(external.read_bytes(), b'outside sentinel')
        self.path.unlink()
        self.path.symlink_to(external)
        self.assertEqual(purge.main(), 1)
        self.assertEqual(external.read_bytes(), b'outside sentinel')

    def test_malformed_user_list_never_rotates_or_creates_backup(self):
        self.path.write_text('{"users":["invalid"]}')
        self.assertEqual(purge.main(), 1)
        self.assertEqual(self.path.read_text(), '{"users":["invalid"]}')
        self.assertEqual(list(self.root.glob('users.json.bak.*')), [])


if __name__ == '__main__':
    unittest.main()
