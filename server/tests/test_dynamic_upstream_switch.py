"""Contract tests for the atomic, loopback-only Trae route handoff."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server.routers import gateway


class DynamicUpstreamSwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        gateway._DYNAMIC_UPSTREAM_CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key = self.root / 'trae-key'
        self.key.write_text('test-key', encoding='utf-8')
        self.route = self.root / 'trae_gateway_base'
        self.spec = {
            'qoder': ('http://127.0.0.1:8799', str(self.key)),
            'trae': ('http://127.0.0.1:8080', str(self.key)),
        }
        self.spec_patch = mock.patch.object(gateway, '_THREEWAY_SPEC', self.spec)
        self.spec_patch.start()

    def tearDown(self) -> None:
        self.spec_patch.stop()
        gateway._DYNAMIC_UPSTREAM_CACHE.clear()
        self.tmp.cleanup()

    def test_loopback_route_is_cached_and_invalid_value_falls_back(self) -> None:
        self.route.write_text('http://127.0.0.1:18080\n', encoding='ascii')
        self.route.chmod(0o644)
        with mock.patch.dict(os.environ, {'WB_TRAE_UPSTREAM_FILE': str(self.route)}):
            self.assertEqual(gateway._threeway_upstream('trae')['base_url'],
                             'http://127.0.0.1:18080')
            # A cache hit must not require reparsing the file for every request.
            self.assertEqual(gateway._threeway_upstream('trae')['base_url'],
                             'http://127.0.0.1:18080')
            self.route.write_text('http://127.0.0.1:18081\n', encoding='ascii')
            self.assertEqual(gateway._threeway_upstream('trae')['base_url'],
                             'http://127.0.0.1:18081')
            self.route.write_text('https://attacker.example/\n', encoding='ascii')
            gateway._DYNAMIC_UPSTREAM_CACHE.clear()
            self.assertEqual(gateway._threeway_upstream('trae')['base_url'],
                             'http://127.0.0.1:8080')

    def test_route_handoff_never_changes_qoder(self) -> None:
        self.route.write_text('http://127.0.0.1:18080\n', encoding='ascii')
        with mock.patch.dict(os.environ, {'WB_TRAE_UPSTREAM_FILE': str(self.route)}):
            self.assertEqual(gateway._threeway_upstream('qoder')['base_url'],
                             'http://127.0.0.1:8799')

    @unittest.skipUnless(os.name == 'posix', 'POSIX mode bits are unavailable on this runner')
    def test_group_or_world_writable_route_is_ignored(self) -> None:
        self.route.write_text('http://127.0.0.1:18080\n', encoding='ascii')
        self.route.chmod(0o666)
        with mock.patch.dict(os.environ, {'WB_TRAE_UPSTREAM_FILE': str(self.route)}):
            self.assertEqual(gateway._threeway_upstream('trae')['base_url'],
                             'http://127.0.0.1:8080')


if __name__ == '__main__':
    unittest.main()
