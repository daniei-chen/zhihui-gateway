"""Check the safety boundary without attempting any real protected write."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

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


class NetworkEvidenceTest(unittest.TestCase):
    def _state(self):
        return {'net_attempts': [], 'net_attempt_count': 0,
                'net_blocked': 0, 'net_blocked_samples': [],
                'started_at': time.time(), 'installed': True,
                'iso_root': 'synthetic', 'run_id': 'synthetic',
                'net_policy': 'block', 'env_problems': [],
                'protected_writes': [], 'outside_writes': [],
                'outside_write_count': 0}

    def test_blocked_host_is_recorded_after_loopback_sample_limit(self):
        import socket
        from zhihui_harness import pytest_plugin as plugin
        with (mock.patch.dict(plugin._STATE, self._state(), clear=True),
              mock.patch.object(plugin, '_PATCHES', []),
              mock.patch.object(socket, 'getaddrinfo', return_value=[]) as resolve,
              mock.patch.dict(os.environ, {'PYTEST_CURRENT_TEST': 'synthetic-case (call)'})):
            try:
                plugin._install_net_guard('block')
                for _ in range(plugin._MAX_SAMPLES + 5):
                    socket.getaddrinfo('127.0.0.1', 1)
                with self.assertRaises(plugin.NetworkBlocked):
                    socket.getaddrinfo('blocked.invalid', 1)
                self.assertEqual(resolve.call_count, plugin._MAX_SAMPLES + 5)
                self.assertEqual(plugin._STATE['net_attempt_count'], plugin._MAX_SAMPLES + 6)
                self.assertEqual(len(plugin._STATE['net_attempts']), plugin._MAX_SAMPLES)
                self.assertEqual(plugin._STATE['net_blocked'], 1)
                blocked = plugin._STATE['net_blocked_samples']
                self.assertEqual(len(blocked), 1)
                self.assertEqual(blocked[0]['host'], 'blocked.invalid')
                self.assertEqual(blocked[0]['test'], 'synthetic-case (call)')
            finally:
                plugin.pytest_unconfigure(None)

    def test_final_stats_include_observation_of_stats_fetch_and_are_snapshotted(self):
        from zhihui_harness import pytest_plugin as plugin
        state = self._state()
        state['net_attempts'] = [{'host': '127.0.0.1'}]
        state['net_attempt_count'] = 1

        def fake_stats():
            plugin._STATE['net_attempts'].append({'host': '127.0.0.1'})
            plugin._STATE['net_attempt_count'] += 1
            return {'total': 0}

        with (mock.patch.dict(plugin._STATE, state, clear=True),
              mock.patch.object(plugin, '_fake_provider_stats', side_effect=fake_stats),
              mock.patch.dict(os.environ, {'ZH_ISO_REPORT': ''})):
            plugin.pytest_sessionfinish(None, 0)
            network = plugin._STATE['final_report']['network']
            self.assertEqual(network['attempt_count'], 2)
            self.assertEqual(len(network['attempts']), 2)
            self.assertEqual(network['sample_count'], 2)
            self.assertFalse(network['samples_truncated'])
            plugin._STATE['net_attempts'].append({'host': 'later.invalid'})
            self.assertEqual(len(network['attempts']), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
