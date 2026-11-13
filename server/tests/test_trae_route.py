"""Tests for the safe, atomic Trae route handoff writer."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def _load_route_module():
    spec = importlib.util.spec_from_file_location('trae_route', ROOT / 'deploy' / 'trae_route.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


route = _load_route_module()


class TraeRouteTest(unittest.TestCase):
    def test_only_loopback_http_endpoints_are_accepted(self) -> None:
        self.assertEqual(route.validate_endpoint(' http://127.0.0.1:7867/\n'),
                         'http://127.0.0.1:7867')
        self.assertEqual(route.validate_endpoint('http://[::1]:18080'),
                         'http://[::1]:18080')
        for value in (
            'https://127.0.0.1:7867',
            'http://10.0.0.2:7867',
            'http://127.0.0.1:7867/v1',
            'http://user:pass@127.0.0.1:7867',
            'http://127.0.0.1:0',
            'http://127.0.0.1:65536',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                route.validate_endpoint(value)

    def test_write_is_atomic_and_replaces_previous_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'var' / 'trae_gateway_base'
            route.write_route(path, route.validate_endpoint('http://127.0.0.1:7867'))
            self.assertEqual(path.read_text(encoding='ascii'), 'http://127.0.0.1:7867\n')
            route.write_route(path, route.validate_endpoint('http://127.0.0.1:7868'))
            self.assertEqual(path.read_text(encoding='ascii'), 'http://127.0.0.1:7868\n')
            self.assertFalse(any(path.parent.glob('.*.tmp')))

    def test_symlink_destination_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'route'
            pointed = root / 'pointed'
            pointed.write_text('keep', encoding='ascii')
            try:
                target.symlink_to(pointed)
            except OSError:
                self.skipTest('symlink creation is unavailable on this Windows runner')
            with self.assertRaises(ValueError):
                route.write_route(target, 'http://127.0.0.1:7867')
            self.assertEqual(pointed.read_text(encoding='ascii'), 'keep')


if __name__ == '__main__':
    unittest.main()
