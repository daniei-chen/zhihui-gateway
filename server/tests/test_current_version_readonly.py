"""Reading version status cannot mutate an immutable release directory."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server import config
from server.services import updater


class ImmutableVersionReadTest(unittest.TestCase):
    def test_stale_release_marker_is_preserved_and_code_version_is_returned(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            marker = root / '.version'
            marker.write_text('v1.0.0\n')
            digest = hashlib.sha256(marker.read_bytes()).hexdigest()
            before = marker.stat().st_mtime_ns
            with mock.patch.object(config, 'ROOT', root), mock.patch.object(updater, '_app_version', return_value='2.8.3'):
                self.assertEqual(updater.current_version(), 'v2.8.3')
                self.assertEqual(updater.current_version(), 'v2.8.3')
            self.assertEqual(hashlib.sha256(marker.read_bytes()).hexdigest(), digest)
            self.assertEqual(marker.stat().st_mtime_ns, before)

    def test_missing_release_marker_is_not_created_by_status_read(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            with mock.patch.object(config, 'ROOT', root), mock.patch.object(updater, '_app_version', return_value='2.8.3'):
                self.assertEqual(updater.current_version(), 'v2.8.3')
            self.assertEqual(list(root.iterdir()), [])

    def test_unknown_code_version_falls_back_to_existing_marker_without_mutation(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            marker = root / '.version'
            marker.write_text('v1.2.3\n')
            with mock.patch.object(config, 'ROOT', root), mock.patch.object(updater, '_app_version', return_value='0.0.0'):
                self.assertEqual(updater.current_version(), 'v1.2.3')
            self.assertEqual(marker.read_text(), 'v1.2.3\n')

    def test_build_metadata_is_not_a_new_product_version(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            marker = root / '.version'
            marker.write_text('v0.0.2+stats.20261029.1\n')
            with mock.patch.object(config, 'ROOT', root), mock.patch.object(updater, '_app_version', return_value='0.0.2'):
                self.assertEqual(updater.current_version(), 'v0.0.2')
            self.assertEqual(marker.read_text(), 'v0.0.2+stats.20261029.1\n')
