"""Product-version migration must not alter provider targets or release guards."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import product
from server.services import updater


class ProductOriginTests(unittest.TestCase):
    def test_version_is_product_not_component_baseline(self):
        value = product.identity()
        self.assertEqual(value['version'], '0.0.3')
        self.assertEqual(value['manager_baseline'], 'v10.7.22+audit.20261029.2')

    def test_updates_stay_in_owner_repository(self):
        self.assertEqual(updater.MANAGER_REPO, 'daniei-chen/zhihui-gateway')
        with patch.object(updater, '_gh_get', return_value={}) as get:
            updater._fetch_remote_versions()
        get.assert_called_once_with('https://api.github.com/repos/daniei-chen/zhihui-gateway/releases/latest')

    def test_product_versions_compare_without_legacy_marker(self):
        self.assertTrue(updater._version_newer('v0.0.2', 'v0.0.1'))
        self.assertFalse(updater._version_newer('v0.0.1', 'v0.0.2'))
        self.assertFalse(updater._version_newer('v0.0.2', 'v0.0.2'))

    def test_legacy_cache_cannot_cross_repository_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'cache.json'
            with patch.object(updater, '_VERSION_CACHE_FILE', path):
                for repository in [None, 'another/project']:
                    path.write_text(json.dumps({'repository': repository, 'manager': {'latest': 'v99.0.0'}}))
                    self.assertEqual(updater._read_cache(), {})
                path.write_text(json.dumps({'repository': product.REPOSITORY, 'checked_at': 100}))
                self.assertEqual(updater._read_cache()['checked_at'], 100)

    def test_source_commit_unknown_is_not_fabricated(self):
        with tempfile.TemporaryDirectory() as temp:
            value = product.metadata(Path(temp), managed=True)
            self.assertIsNone(value['source_commit'])
            self.assertFalse(value['web_update_enabled'])
            self.assertFalse(value['provider_endpoints_changed'])

    def test_only_matching_provenance_is_displayed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'server').mkdir()
            path = root / 'server/release-provenance.json'
            value = {'repository': product.REPOSITORY, 'version': product.identity()['version'], 'source_commit': 'a' * 40}
            path.write_text(json.dumps(value))
            self.assertEqual(product.metadata(root, managed=True)['source_commit'], 'a' * 40)
            value['version'] = '0.0.1'
            path.write_text(json.dumps(value))
            self.assertIsNone(product.metadata(root, managed=True)['source_commit'])

    def test_managed_update_still_fails_before_spawning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'deploy').mkdir()
            (root / 'deploy/managed-release.json').write_text('{}')
            with patch.object(updater.config, 'ROOT', root), patch.object(updater.subprocess, 'Popen') as spawn:
                ok, reason = updater.start_update('manager')
            self.assertFalse(ok)
            self.assertIn('受控', reason)
            spawn.assert_not_called()

    def test_malformed_provenance_does_not_break_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'server').mkdir()
            path = root / 'server/release-provenance.json'
            for raw in ['null', '[]', '42', '{', '{"source_commit":null}']:
                path.write_text(raw)
                self.assertIsNone(product.metadata(root, managed=True)['source_commit'])

    def test_runtime_status_cannot_replace_release_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'status.json'
            state.write_text(json.dumps({'version': 'v99.0.0', 'product': {'repository': 'wrong/project'}}))
            with patch.object(updater, 'STATUS_FILE', state), \
                    patch.object(updater, 'can_control_docker', return_value=False), \
                    patch.object(updater, '_lock_active', return_value=False), \
                    patch.object(updater, 'current_version', return_value='v0.0.2'):
                value = updater.read_status()
            self.assertEqual(value['version'], 'v0.0.2')
            self.assertEqual(value['product']['repository'], product.REPOSITORY)


if __name__ == '__main__':
    unittest.main()
