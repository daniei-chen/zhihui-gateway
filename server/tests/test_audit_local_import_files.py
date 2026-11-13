"""Credential write boundaries in temporary directories, no native client process."""
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock

from server.services import keyimport


class LocalImportFileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'provider_config.json'
        self.original = b'{"config": {}, "synthetic_secret": "before"}\n'
        self.path.write_bytes(self.original)
        self.path.chmod(0o600)

    def assert_no_temporary(self):
        self.assertEqual(list(self.root.glob('.*.wbimport-*.tmp')), [])

    def test_private_file_remains_private_and_preserves_owner(self):
        before = self.path.stat()
        old_mask = os.umask(0o022)
        try:
            keyimport._atomic_write_text(self.path, '{"synthetic_secret":"after"}\n')
        finally:
            os.umask(old_mask)
        after = self.path.stat()
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(after.st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text())['synthetic_secret'], 'after')
        self.assert_no_temporary()

    def test_existing_public_permissions_are_restricted(self):
        self.path.chmod(0o644)
        keyimport._atomic_write_text(self.path, '{}')
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assert_no_temporary()

    def test_old_predictable_temporary_link_is_never_used(self):
        victim = self.root / 'outside.txt'
        victim.write_text('preserve')
        predictable = self.path.with_name(self.path.name + '.wbimport.tmp')
        try:
            predictable.symlink_to(victim)
        except OSError as exc:
            self.skipTest(f'filesystem cannot create a synthetic symlink: {exc}')
        keyimport._atomic_write_text(self.path, '{}')
        self.assertEqual(victim.read_text(), 'preserve')
        self.assertTrue(predictable.is_symlink())

    def test_symlink_target_is_rejected_before_writing(self):
        victim = self.root / 'outside.txt'
        victim.write_text('preserve')
        self.path.unlink()
        try:
            self.path.symlink_to(victim)
        except OSError as exc:
            self.skipTest(f'filesystem cannot create a synthetic symlink: {exc}')
        with self.assertRaises(keyimport.ImportRefused):
            keyimport._atomic_write_text(self.path, 'changed')
        self.assertEqual(victim.read_text(), 'preserve')
        self.assertTrue(self.path.is_symlink())
        self.assert_no_temporary()

    def test_linked_parent_hardlink_and_directory_are_rejected(self):
        linked = self.root / 'linked'
        target = self.root / 'real'
        target.mkdir()
        (target / 'config.json').write_text('preserve')
        try:
            linked.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'filesystem cannot create a synthetic symlink: {exc}')
        hardlink = self.root / 'hardlink'
        os.link(self.path, hardlink)
        for path in (linked / 'config.json', hardlink, target):
            with self.subTest(path=path.name), self.assertRaises(keyimport.ImportRefused):
                keyimport._atomic_write_text(path, 'changed')
        self.assertEqual((target / 'config.json').read_text(), 'preserve')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assert_no_temporary()

    def test_precommit_fsync_failure_preserves_original_and_cleans_temporary(self):
        with mock.patch.object(keyimport.os, 'fsync', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                keyimport._atomic_write_text(self.path, 'changed')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assert_no_temporary()

    def test_replace_failure_preserves_original_and_cleans_temporary(self):
        with mock.patch.object(keyimport.os, 'replace', side_effect=OSError('synthetic replace failure')):
            with self.assertRaises(OSError):
                keyimport._atomic_write_text(self.path, 'changed')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assert_no_temporary()

    def test_concurrent_file_change_is_not_overwritten(self):
        private = keyimport._private_metadata

        def concurrent_change(*args, **kwargs):
            private(*args, **kwargs)
            self.path.write_text('concurrent edit must survive')

        with mock.patch.object(keyimport, '_private_metadata', side_effect=concurrent_change):
            with self.assertRaises(keyimport.ImportRefused):
                keyimport._atomic_write_text(self.path, 'stale replacement')
        self.assertEqual(self.path.read_text(), 'concurrent edit must survive')
        self.assert_no_temporary()

    def test_backup_is_private_exact_and_does_not_follow_old_backup_name(self):
        victim = self.root / 'outside.txt'
        victim.write_text('preserve')
        predictable = self.path.with_name(self.path.name + '.wbimport-' + time.strftime('%Y%m%d-%H%M%S') + '.bak')
        try:
            predictable.symlink_to(victim)
        except OSError as exc:
            self.skipTest(f'filesystem cannot create a synthetic symlink: {exc}')
        self.path.chmod(0o644)
        backup = keyimport._backup(self.path)
        self.assertNotEqual(backup, predictable)
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(victim.read_text(), 'preserve')
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_failed_import_never_copy_backs_through_a_changed_link(self):
        victim = self.root / 'outside.txt'
        victim.write_text('preserve')

        def fail_after_change(*args):
            self.path.rename(self.root / 'original-aside.json')
            self.path.symlink_to(victim)
            raise OSError('synthetic failure after target changed')

        fragment = {'providerRule': {'providerId': 'synthetic', 'config': {}}, 'providerModelRules': []}
        with mock.patch.object(keyimport, '_ensure_writable', return_value=self.path), \
                mock.patch.object(keyimport, '_atomic_write_text', side_effect=fail_after_change):
            with self.assertRaises(OSError):
                keyimport.import_zcode(fragment=fragment)
        self.assertEqual(victim.read_text(), 'preserve')
        self.assertEqual((self.root / 'original-aside.json').read_bytes(), self.original)
        self.assertEqual(len(list(self.root.glob('*.bak'))), 1)

    def test_portable_branch_avoids_posix_only_operations(self):
        real_os = os
        calls = []

        class PortableOS:
            name = 'nt'

            def __getattr__(self, name):
                if name in {'O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK', 'fchown', 'fchmod'}:
                    raise AttributeError(name)
                return getattr(real_os, name)

            def open(self, *args, **kwargs):
                calls.append(('open', kwargs))
                return real_os.open(*args, **kwargs)

            def replace(self, *args, **kwargs):
                calls.append(('replace', kwargs))
                return real_os.replace(*args, **kwargs)

        with mock.patch.object(keyimport, 'os', PortableOS()):
            keyimport._atomic_write_text(self.path, '{}')
        self.assertTrue(calls)
        self.assertTrue(all(not kwargs for _, kwargs in calls))
        self.assertEqual(self.path.read_text(), '{}')
        self.assert_no_temporary()


if __name__ == '__main__':
    unittest.main()
