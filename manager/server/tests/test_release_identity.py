"""No release writes, host fallbacks or mutable environment identity override."""
import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from server import config, migrations, observability, operational, release_identity
from server.routers import system
from server.services import updater


class ReleaseIdentityTests(unittest.TestCase):
    def test_logs_migrations_and_health_share_release_local_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / '.version'
            marker.write_text('v0.0.3+steward.fixture\n')
            before = (hashlib.sha256(marker.read_bytes()).hexdigest(), marker.stat().st_mtime_ns)
            with patch.object(config, 'ROOT', root), patch.dict(os.environ, {'WB_APP_VERSION': 'v0.0.1+stale'}):
                for read in (release_identity.current, migrations._app_version,
                             observability._release_id, operational._release_id):
                    self.assertEqual(read(), 'v0.0.3+steward.fixture')
            self.assertEqual(before, (hashlib.sha256(marker.read_bytes()).hexdigest(), marker.stat().st_mtime_ns))

    def test_missing_or_malformed_identity_is_unknown_not_another_host_release(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'ROOT', Path(directory)):
            marker = Path(directory) / '.version'
            self.assertEqual(release_identity.current(), 'unknown')
            self.assertFalse(marker.exists())
            for raw in (b'v0.0.3\nsecond-line', b'x' * 130, b'\xff', b'', b'v0.0.3 secret=x'):
                with self.subTest(raw=raw):
                    marker.write_bytes(raw)
                    self.assertEqual(release_identity.current(), 'unknown')

    def test_product_version_and_exact_release_are_separate_on_status_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.version').write_text('v0.0.3+steward.fixture\n')
            with patch.object(config, 'ROOT', root), \
                    patch.object(updater, '_app_version', return_value='0.0.3'), \
                    patch.object(system.wb2api, 'get_status', AsyncMock(return_value={'connected': True, 'total': 3})):
                value = asyncio.run(system.versions({'role': 'account_admin'}))
            self.assertEqual(value['manager'], 'v0.0.3')
            self.assertEqual(value['release_id'], 'v0.0.3+steward.fixture')
            self.assertIsNone(value['upstream_dir'])


if __name__ == '__main__':
    unittest.main()
