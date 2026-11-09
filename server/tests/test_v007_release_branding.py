import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config, product, productupdates, release_identity, security
from server.routers import system
from server.services import updater


class ReleaseBrandingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='zhihui-release-brand-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.install = self.root / 'release'
        (self.install / 'deploy').mkdir(parents=True)
        self.marker = self.install / 'deploy/managed-release.json'
        self.marker.write_text('{}')
        for name, content in (('manager.db', b'synthetic-main-ledger'), ('wallet.db', b'synthetic-wallet'),
                              ('users.json', b'synthetic-users'), ('account.json', b'synthetic-account')):
            (self.data / name).write_bytes(content)
        self.protected = {name: (self.data / name).read_bytes()
                          for name in ('manager.db', 'wallet.db', 'users.json', 'account.json')}
        for target, name, value in ((config, 'ROOT', self.install), (config, 'DATA_DIR', self.data),
                                    (updater, 'STATUS_FILE', self.data / 'update-status.json'),
                                    (updater, 'LOG_FILE', self.data / 'update.log'),
                                    (updater, 'LOCK_FILE', self.data / 'update.lock')):
            context = patch.object(target, name, value)
            context.start()
            self.addCleanup(context.stop)
        for target, name, value in ((updater, 'current_version', 'v0.0.7'),
                                    (release_identity, 'current', 'v0.0.7+stable.20261104.1')):
            context = patch.object(target, name, return_value=value)
            context.start()
            self.addCleanup(context.stop)
        self.app = FastAPI()
        self.app.include_router(system.router)
        self.app.dependency_overrides[security.current_user] = lambda: {'username': 'synthetic', 'role': 'admin'}
        self.app.dependency_overrides[security.require_session_admin] = lambda: {'username': 'synthetic', 'role': 'admin'}
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def legacy(self, status=None):
        payload = {'running': False, 'ok': True, 'version': 'v1.0.79',
                   'logs': [{'ts': 1, 'level': 'info', 'text': 'legacy-shell-command-NEVER-SHOW'}]}
        if status is not None:
            payload.update(status)
        updater.STATUS_FILE.write_text(json.dumps(payload))
        updater.LOG_FILE.write_text('foreign/repository v1.0.79 synthetic-old-command')

    def receipt(self, **changes):
        value = {'schema': 1, 'repository': product.REPOSITORY,
                 'release_id': release_identity.current(), 'phase': 'activated', 'running': False,
                 'ok': True, 'events': [{'at': 1, 'phase': 'activated', 'text': 'DO-NOT-EXPOSE'}]}
        value.update(changes)
        (self.data / 'zhihui-release-status.json').write_text(json.dumps(value))

    def assert_business_unchanged(self):
        self.assertEqual({name: (self.data / name).read_bytes() for name in self.protected}, self.protected)

    def test_legacy_result_is_not_the_current_product_status_and_get_does_not_write(self):
        self.legacy()
        before = {path.name: path.read_bytes() for path in self.data.iterdir()}
        response = self.client.get('/api/system/update-status')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['version'], 'v0.0.7')
        self.assertEqual(body['product']['repository'], product.REPOSITORY)
        self.assertIsNone(body['ok'])
        self.assertFalse(body['running'])
        self.assertEqual(body['logs'], [])
        self.assertEqual(body['log_tail'], '')
        self.assertEqual(body['record_state'], 'absent')
        self.assertTrue(body['legacy_history_available'])
        self.assertNotIn('v1.0.79', response.text)
        self.assertEqual({path.name: path.read_bytes() for path in self.data.iterdir()}, before)

    def test_viewer_does_not_read_legacy_files_or_publisher_events(self):
        self.legacy()
        self.receipt()
        self.app.dependency_overrides[security.current_user] = lambda: {'role': 'viewer'}
        with patch.object(productupdates, '_exists', side_effect=AssertionError('legacy metadata not for viewer')):
            body = self.client.get('/api/system/update-status').json()
        self.assertEqual(body['logs'], [])
        self.assertFalse(body['legacy_history_available'])

    def test_unknown_or_foreign_receipt_never_invents_success(self):
        for change in ({'repository': 'foreign/project'}, {'release_id': 'v0.0.6'}, {'schema': 9}, {'schema': True}):
            with self.subTest(change=change):
                self.receipt(**change)
                body = productupdates.read_status(include_logs=True)
                self.assertEqual(body['record_state'], 'identity_mismatch')
                self.assertIsNone(body['ok'])
                self.assertEqual(body['logs'], [])
                self.assertEqual(body['version'], 'v0.0.7')

    def test_receipt_accepts_only_bounded_structured_phases_not_raw_commands(self):
        events = [{'at': index, 'phase': 'activated', 'text': 'synthetic-secret-command'} for index in range(150)]
        events.extend([{'phase': 'arbitrary-command'}, {'phase': {'invalid': True}}])
        self.receipt(events=events, version='v99.0.0', product={'repository': 'foreign/project'})
        body = productupdates.read_status(include_logs=True)
        self.assertEqual(body['record_state'], 'reported')
        self.assertEqual(body['record_verification'], 'not-verified-by-panel')
        self.assertLessEqual(len(body['logs']), 100)
        self.assertNotIn('synthetic-secret-command', json.dumps(body))
        self.assertEqual(body['version'], 'v0.0.7')

    def test_web_execution_is_retired_even_without_a_marker(self):
        for present in (True, False):
            if not present:
                self.marker.unlink()
            with patch.object(updater, 'start_update', side_effect=AssertionError('must not execute')):
                for target in ('manager', 'upstream', 'both'):
                    for source in ('release', 'uploaded'):
                        with self.subTest(marker=present, target=target, source=source):
                            response = self.client.post('/api/system/update', json={'target': target, 'source': source})
                            self.assertEqual(response.status_code, 409)
                            self.assertEqual(response.json()['detail']['code'], 'trusted_publisher_required')
        self.assert_business_unchanged()

    def test_missing_marker_does_not_restore_web_update_policy(self):
        for managed in (True, False):
            metadata = product.metadata(self.install, managed=managed)
            self.assertFalse(metadata['web_update_enabled'])
            self.assertEqual(metadata['update_policy'], 'trusted-publisher-required')

    def test_explicit_archive_preserves_history_and_never_touches_business_data(self):
        self.legacy()
        self.receipt()
        before = {path.name: path.read_bytes() for path in (updater.STATUS_FILE, updater.LOG_FILE)}
        publisher = (self.data / 'zhihui-release-status.json').read_bytes()
        with patch.object(security, 'audit'):
            response = self.client.delete('/api/system/update-status')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['archived'], 2)
        destination = next((self.data / 'release-history').iterdir())
        manifest = json.loads((destination / 'manifest.json').read_text())
        for name, content in before.items():
            self.assertEqual((destination / name).read_bytes(), content)
            self.assertEqual(manifest['files'][name]['sha256'], hashlib.sha256(content).hexdigest())
        self.assertFalse(updater.STATUS_FILE.exists())
        self.assertFalse(updater.LOG_FILE.exists())
        self.assertEqual((self.data / 'zhihui-release-status.json').read_bytes(), publisher)
        if os.name != 'nt':
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertEqual((destination / 'manifest.json').stat().st_mode & 0o777, 0o600)
        self.assert_business_unchanged()

    def test_lock_unknown_status_and_live_pid_all_block_archive(self):
        self.legacy()
        updater.LOCK_FILE.write_text('synthetic-unknown-lock')
        with self.assertRaises(ValueError):
            productupdates.archive_legacy_history()
        self.assertTrue(updater.LOCK_FILE.exists())
        updater.LOCK_FILE.unlink()
        for state in ({'running': True}, {'running': None}, {'running': False, 'pid': 1234}):
            self.legacy(state)
            with patch.object(updater, '_pid_alive', return_value=True), self.assertRaises(ValueError):
                productupdates.archive_legacy_history()
            self.assertTrue(updater.STATUS_FILE.exists())
            self.assertTrue(updater.LOG_FILE.exists())
        self.assert_business_unchanged()

    def test_log_without_end_state_is_unknown_not_dead(self):
        updater.LOG_FILE.write_text('synthetic old log without status')
        with self.assertRaises(ValueError):
            productupdates.archive_legacy_history()
        self.assertTrue(updater.LOG_FILE.exists())

    def test_fsync_failure_and_concurrent_change_preserve_originals(self):
        self.legacy()
        with patch.object(productupdates.os, 'fsync', side_effect=OSError('synthetic full disk')):
            with self.assertRaises(OSError):
                productupdates.archive_legacy_history()
        self.assertTrue(updater.STATUS_FILE.exists())
        self.assertTrue(updater.LOG_FILE.exists())
        def change(_path):
            updater.LOG_FILE.write_text('changed concurrently')
        with patch.object(productupdates, '_sync_directory', side_effect=change), self.assertRaises(ValueError):
            productupdates.archive_legacy_history()
        self.assertTrue(updater.STATUS_FILE.exists())
        self.assertEqual(updater.LOG_FILE.read_text(), 'changed concurrently')
        self.assert_business_unchanged()

    def test_malformed_and_hardlinked_receipts_stay_unknown(self):
        path = self.data / 'zhihui-release-status.json'
        path.write_text('{')
        self.assertEqual(productupdates.read_status()['record_state'], 'unavailable')
        for changes in ({'running': 'false'}, {'ok': 'true'}):
            self.receipt(**changes)
            self.assertEqual(productupdates.read_status()['record_state'], 'unavailable')
        self.receipt()
        os.link(path, self.data / 'synthetic-hardlink.json')
        self.assertEqual(productupdates.read_status()['record_state'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
