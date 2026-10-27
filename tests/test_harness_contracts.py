"""Check the safety boundary without attempting any real protected write."""
import os
from pathlib import Path
import tempfile
import unittest

from zhihui_harness.isolation import protected_reason


class PathBoundaryTest(unittest.TestCase):
    def test_only_null_sink_is_allowed_among_system_device_paths(self):
        self.assertIsNone(protected_reason(os.devnull))
        if os.name == 'posix':
            self.assertIsNotNone(protected_reason('/dev/zero'))
            self.assertIsNotNone(protected_reason('/dev/sda'))

    def test_guards_are_restored_before_runner_persists_evidence(self):
        import builtins
        import io
        import socket
        from zhihui_harness import pytest_plugin
        targets = [(builtins, 'open'), (io, 'open'), (os, 'open'),
                   (socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                   (socket, 'getaddrinfo'), (socket, 'create_connection')]
        originals = [getattr(target, name) for target, name in targets]
        with tempfile.TemporaryDirectory() as value:
            try:
                pytest_plugin._install_net_guard('block')
                pytest_plugin._install_path_guard(Path(value))
                self.assertTrue(all(getattr(target, name) is not old
                                    for (target, name), old in zip(targets, originals)))
            finally:
                pytest_plugin.pytest_unconfigure(None)
            self.assertEqual([getattr(target, name) for target, name in targets], originals)

    def test_native_system_roots_are_classified_without_accessing_them(self):
        for directory in ('etc', 'opt', 'var', 'home', 'root', 'run', 'tmp', 'usr', 'proc', 'sys'):
            root = Path(Path.cwd().anchor) / directory
            with self.subTest(path=str(root)):
                self.assertIsNotNone(protected_reason(root / 'qa-must-not-write'))

    def test_isolation_subtree_overrides_system_location(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            self.assertIsNone(protected_reason(root / 'opt/workbuddy-manager-v1/data', root))
            self.assertIsNotNone(protected_reason(root / 'opt/workbuddy-manager-v1/data'))

    def test_production_markers_outside_isolation_are_protected(self):
        candidate = Path.cwd().anchor + 'workspace/workbuddy-manager/data/manager.db'
        self.assertIsNotNone(protected_reason(candidate))

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink containment')
    def test_symlink_escape_is_classified_by_resolved_destination(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / 'escape').symlink_to('/etc', target_is_directory=True)
            self.assertIsNotNone(protected_reason(root / 'escape/passwd', root))


if __name__ == '__main__':
    unittest.main(verbosity=2)
