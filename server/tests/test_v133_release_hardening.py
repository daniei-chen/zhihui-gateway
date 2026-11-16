"""v1.3.3 release hardening contracts."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from server import main


ROOT = Path(__file__).resolve().parents[2]
RELEASE_STATE = ROOT / 'deploy/release_state.py'
HELPER_UNIT = ROOT / 'deploy/workbuddy-web-helper.service'


def _release_state():
    spec = importlib.util.spec_from_file_location('release_state_v133', RELEASE_STATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseStateRecoveryTests(unittest.TestCase):
    def test_missing_pointer_never_stops_service(self):
        state = _release_state()
        events = []
        with self.assertRaisesRegex(RuntimeError, 'pointer missing'):
            state.activate('missing', 'new', lambda: events.append('stop'), lambda: None, lambda: True)
        self.assertEqual(events, [])

    @unittest.skipIf(os.name == 'nt', 'directory symlink contract is POSIX-specific')
    def test_stop_failure_never_changes_pointer_or_starts(self):
        state = _release_state()
        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'old').mkdir()
            pointer = root / 'current'
            pointer.symlink_to('old', target_is_directory=True)

            def failed_stop():
                events.append('stop')
                raise OSError('synthetic stop failure')

            with self.assertRaisesRegex(OSError, 'stop failure'):
                state.activate(pointer, 'new', failed_stop, lambda: events.append('start'), lambda: True)
            self.assertEqual(os.readlink(pointer), 'old')
        self.assertEqual(events, ['stop'])

    @unittest.skipIf(os.name == 'nt', 'directory symlink fsync contract is POSIX-specific')
    def test_rollback_failure_reports_both_errors_and_never_claims_success(self):
        state = _release_state()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'old').mkdir()
            (root / 'new').mkdir()
            pointer = root / 'current'
            pointer.symlink_to('old', target_is_directory=True)
            calls = {'starts': 0}

            def start():
                calls['starts'] += 1

            with mock.patch.object(state, 'point', side_effect=OSError('synthetic pointer failure')):
                with self.assertRaisesRegex(RuntimeError, 'rollback did not fully verify'):
                    state.activate(pointer, 'new', lambda: None, start, lambda: True)
            self.assertEqual(calls['starts'], 1)


class ReleaseIdentityEndpointTests(unittest.TestCase):
    def test_it_exposes_only_release_identity(self):
        metadata = {
            'version': '1.3.3',
            'source_commit': 'a' * 40,
            'repository': 'daniei-chen/zhihui-gateway',
        }
        with mock.patch('server.release_identity.current', return_value='v1.3.3'), \
                mock.patch('server.product.metadata', return_value=metadata):
            response = TestClient(main.app).get('/api/release')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'service': 'zhihui-gateway',
            'release_id': 'v1.3.3',
            'version': '1.3.3',
            'source_commit': 'a' * 40,
        })
        self.assertIn('no-store', response.headers.get('cache-control', ''))


class HelperResourceContractTests(unittest.TestCase):
    def test_memory_is_finite_and_high_is_below_max(self):
        text = HELPER_UNIT.read_text(encoding='utf-8')
        values = {}
        for line in text.splitlines():
            if line.startswith(('MemoryHigh=', 'MemoryMax=')):
                key, value = line.split('=', 1)
                self.assertNotIn(key, values)
                if value.endswith('G'):
                    values[key] = int(value[:-1]) * 1024
                elif value.endswith('M'):
                    values[key] = int(value[:-1])
                else:
                    self.fail('unexpected memory suffix: ' + value)
        self.assertIn('MemoryHigh', values)
        self.assertIn('MemoryMax', values)
        self.assertLess(values['MemoryHigh'], values['MemoryMax'])
        self.assertLess(values['MemoryMax'], 1536)
        self.assertIn('MemorySwapMax=256M', text)
        installer = ROOT / 'deploy/install-resource-budget.sh'
        self.assertTrue(os.access(installer, os.X_OK))
        self.assertIn('MemorySwapMax=256M', installer.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
