"""Managed updates must refuse legacy effects, not merely hide UI buttons."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile
from starlette.requests import Request

from server import config, db, security
from server.routers import system
from server.services import updater
from server.tests.test_update_package import _make_package

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parent


def load_script(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManagedUpdateBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='managed-update-boundary-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / 'install'
        self.data = self.root / 'data'
        self.data.mkdir()
        (self.install / 'deploy').mkdir(parents=True)
        self.marker = self.install / 'deploy/managed-release.json'
        self.marker.write_text('{}', encoding='ascii')
        self.package = self.data / 'update-package.tar.gz'
        self.package.write_bytes(b'previous-package-preserve-exactly')
        (self.data / 'update-package.tar.part').write_bytes(b'previous-part-preserve-exactly')
        (self.data / 'manager.db').write_bytes(b'synthetic-current-ledger')
        (self.data / 'users.json').write_bytes(b'synthetic-current-permissions')
        (self.data / 'accounts.json').write_bytes(b'synthetic-current-credentials')
        self.metadata = {'name': 'old.tar.gz', 'version': 'v0.0.3', 'size': self.package.stat().st_size,
                         'sha256': hashlib.sha256(self.package.read_bytes()).hexdigest(),
                         'expected_sha256': 'a' * 64}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (('ROOT', self.install), ('DATA_DIR', self.data)):
            self.stack.enter_context(mock.patch.object(config, name, value))
        for name, value in (('PACKAGE_FILE', self.package), ('LOCK_FILE', self.data / 'update.lock'),
                            ('STATUS_FILE', self.data / 'update-status.json')):
            self.stack.enter_context(mock.patch.object(updater, name, value))
        self.stack.enter_context(mock.patch.object(db, 'get_setting', side_effect=lambda key, default=None:
                                                  dict(self.metadata) if key == updater.PACKAGE_META_KEY else default))
        self.settings = self.stack.enter_context(mock.patch.object(db, 'set_setting'))
        self.audit = self.stack.enter_context(mock.patch.object(security, 'audit'))
        self.spawn = self.stack.enter_context(mock.patch.object(updater.subprocess, 'Popen',
                                              side_effect=AssertionError('must not spawn')))
        self.user = {'username': 'synthetic-admin', 'role': 'admin'}
        app = FastAPI()
        app.include_router(system.router)
        app.dependency_overrides[security.require_session_admin] = lambda: self.user
        app.dependency_overrides[security.current_user] = lambda: self.user
        self.client = self.stack.enter_context(TestClient(app, raise_server_exceptions=False))
        self.before = self.snapshot()

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def assert_preserved(self):
        self.assertEqual(self.snapshot(), self.before)
        self.settings.assert_not_called()
        self.spawn.assert_not_called()

    def post_package(self, body=b'inert-invalid-package', expected='a' * 64):
        return self.client.post('/api/system/update-package',
                                files={'file': ('candidate.tar.gz', body, 'application/gzip')},
                                data={'expected_sha256': expected})

    def test_managed_upload_refuses_before_read_or_metadata_write(self):
        with mock.patch.object(UploadFile, 'read', side_effect=AssertionError('must not consume upload')):
            response = self.post_package()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('受控', response.text)
        self.assert_preserved()
        self.audit.assert_not_called()

    def test_matching_digest_truncated_duplicate_and_arbitrary_bytes_all_refused(self):
        candidate = _make_package(self.root / 'candidate.tar.gz')
        valid = candidate.read_bytes()
        candidate.unlink()
        duplicate = io.BytesIO()
        with tarfile.open(fileobj=duplicate, mode='w:gz') as archive:
            for version in (b'v0.0.4', b'v0.0.5'):
                info = tarfile.TarInfo('pkg/.version')
                info.size = len(version)
                archive.addfile(info, io.BytesIO(version))
        for body in (valid, valid[:-8192], duplicate.getvalue(), b'not-a-package'):
            with self.subTest(size=len(body)):
                response = self.post_package(body, hashlib.sha256(body).hexdigest())
                self.assertEqual(response.status_code, 409, response.text)
                self.assert_preserved()

    def test_environment_opt_out_and_empty_digest_do_not_reopen_upload(self):
        with mock.patch.dict(os.environ, {'WB_ALLOW_UNVERIFIED_PACKAGE': '1'}):
            for expected in ('', 'invalid', 'a' * 64):
                self.assertEqual(self.post_package(expected=expected).status_code, 409)
        self.assert_preserved()

    def test_managed_delete_preserves_existing_package_record_and_part(self):
        response = self.client.delete('/api/system/update-package')
        self.assertEqual(response.status_code, 409, response.text)
        self.assert_preserved()
        self.audit.assert_not_called()

    def test_managed_metadata_is_read_only_and_never_approval(self):
        self.metadata.update({'installable': True, 'publisher_verified': True, 'upload_enabled': True})
        response = self.client.get('/api/system/update-package')
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertTrue(value['present'])
        self.assertFalse(value['upload_enabled'])
        self.assertFalse(value['installable'])
        self.assertFalse(value['publisher_verified'])
        self.assertEqual(value['update_policy'], 'trusted-publisher-required')
        self.assert_preserved()

    def test_all_targets_and_sources_refuse_without_status_lock_or_spawn(self):
        for target in ('manager', 'upstream', 'both'):
            for source in ('release', 'uploaded'):
                response = self.client.post('/api/system/update', json={'target': target, 'source': source})
                self.assertEqual(response.status_code, 409, response.text)
        self.assert_preserved()

    def test_direct_service_writes_also_refuse(self):
        with self.assertRaises(ValueError):
            updater.record_package(name='new.tar.gz', size=100, sha256='b' * 64, version='v0.0.4')
        with self.assertRaises(ValueError):
            updater.clear_package()
        self.assert_preserved()

    def test_corrupt_or_directory_marker_still_blocks(self):
        self.marker.write_bytes(b'not-json')
        self.assertEqual(self.post_package().status_code, 409)
        self.marker.unlink()
        self.marker.mkdir()
        self.assertEqual(self.post_package().status_code, 409)
        self.assertEqual(self.client.delete('/api/system/update-package').status_code, 409)
        self.settings.assert_not_called()

    def test_marker_probe_error_fails_closed(self):
        original = Path.lstat
        marker = self.marker
        def denied(path, *args, **kwargs):
            if path == marker:
                raise PermissionError('synthetic policy unreadable')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'lstat', new=denied):
            self.assertEqual(self.post_package().status_code, 409)
            ok, reason = updater.start_update('manager')
        self.assertFalse(ok)
        self.assertTrue(reason)
        self.assert_preserved()

    def test_switch_to_managed_during_legacy_upload_blocks_commit(self):
        self.marker.unlink()
        incoming = _make_package(self.root / 'incoming.tar.gz').read_bytes()
        (self.root / 'incoming.tar.gz').unlink()
        before_package = self.package.read_bytes()
        original_read = UploadFile.read
        called = False
        marker = self.marker
        async def install_marker(file, size=-1):
            nonlocal called
            if not called:
                called = True
                marker.write_text('{}', encoding='ascii')
            return await original_read(file, size)
        with mock.patch.object(UploadFile, 'read', new=install_marker):
            response = self.post_package(incoming, hashlib.sha256(incoming).hexdigest())
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.package.read_bytes(), before_package)
        self.settings.assert_not_called()
        self.assertFalse(list(self.data.glob('update-upload-*.part')))

    def test_direct_handler_has_same_managed_policy(self):
        request = Request({'type': 'http', 'method': 'POST', 'path': '/api/system/update-package',
                           'headers': [], 'client': ('192.0.2.1', 1234)})
        upload = UploadFile(io.BytesIO(b'inert'), filename='inert.tar.gz')
        with self.assertRaises(HTTPException) as denied:
            asyncio.run(system.upload_update_package(request, upload, 'a' * 64, self.user))
        self.assertEqual(denied.exception.status_code, 409)
        self.assert_preserved()

    def test_legacy_cancellation_cleans_only_its_own_random_part(self):
        self.marker.unlink()
        before = self.package.read_bytes()
        shared_part = self.data / 'update-package.tar.part'
        previous_part = shared_part.read_bytes()
        upload = UploadFile(io.BytesIO(b'inert'), filename='inert.tar.gz')
        request = Request({'type': 'http', 'method': 'POST', 'path': '/api/system/update-package',
                           'headers': [], 'client': ('192.0.2.1', 1234)})
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()
        with mock.patch.object(UploadFile, 'read', new=cancelled):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(system.upload_update_package(request, upload, 'a' * 64, self.user))
        self.assertEqual(self.package.read_bytes(), before)
        self.assertEqual(shared_part.read_bytes(), previous_part)
        self.assertFalse(list(self.data.glob('update-upload-*.part')))
        self.settings.assert_not_called()

    def test_legacy_replace_failure_preserves_previous_and_cleans_own_part(self):
        self.marker.unlink()
        body = _make_package(self.root / 'candidate.tar.gz').read_bytes()
        (self.root / 'candidate.tar.gz').unlink()
        before = self.package.read_bytes()
        with mock.patch.object(system.os, 'replace', side_effect=OSError('synthetic rename failure')):
            response = self.post_package(body, hashlib.sha256(body).hexdigest())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.package.read_bytes(), before)
        self.assertFalse(list(self.data.glob('update-upload-*.part')))
        self.settings.assert_not_called()


class ManagedSessionBoundaryTest(unittest.TestCase):
    def setUp(self):
        from server import tokensvc
        self.tokensvc = tokensvc
        self.temp = tempfile.TemporaryDirectory(prefix='managed-session-boundary-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in {'ROOT': self.root, 'DATA_DIR': self.root / 'data',
                           'DB_PATH': self.root / 'data/manager.db',
                           'USERS_FILE': self.root / 'data/users.json'}.items():
            self.stack.enter_context(mock.patch.object(config, key, value))
        config.DATA_DIR.mkdir()
        (self.root / 'deploy').mkdir()
        (self.root / 'deploy/managed-release.json').write_text('{}', encoding='ascii')
        security.save_users({'secret': 'synthetic-local-boundary-' + 's' * 48,
            'role_policy_version': 2,
            'users': [{'username': name, 'role': role, 'sv': 1, 'pwd_hash': 'synthetic-no-login'}
                      for name, role in [('root', 'admin'), ('reader', 'account_admin'), ('viewer', 'viewer')]]})
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.connect()
        self.addCleanup(self.close_db)
        self.package = config.DATA_DIR / 'update-package.tar.gz'
        self.package.write_bytes(b'previous-synthetic-package')
        self.stack.enter_context(mock.patch.object(updater, 'PACKAGE_FILE', self.package))
        db.set_setting(updater.PACKAGE_META_KEY, {'name': 'previous.tar.gz', 'sha256': 'a' * 64})
        app = FastAPI()
        app.include_router(system.router)
        self.client = self.stack.enter_context(TestClient(app))
        self.cookie = {name: security.issue_token(name, role) for name, role in
                       [('root', 'admin'), ('reader', 'account_admin'), ('viewer', 'viewer')]}

    def close_db(self):
        if db._conn is not None:
            db._conn.close()
            db._conn = None

    def headers(self, name):
        return {'cookie': f'{config.COOKIE_NAME}={self.cookie[name]}', 'origin': 'http://testserver'}

    def upload(self, headers):
        return self.client.post('/api/system/update-package', headers=headers,
                                files={'file': ('fixture.tar.gz', b'inert', 'application/gzip')},
                                data={'expected_sha256': 'a' * 64})

    def test_real_sessions_keep_role_and_origin_guards(self):
        for name, expected in [('root', 409), ('reader', 403), ('viewer', 403)]:
            with self.subTest(name=name):
                self.assertEqual(self.upload(self.headers(name)).status_code, expected)
                self.assertEqual(self.client.delete('/api/system/update-package',
                                 headers=self.headers(name)).status_code, expected)
        self.assertEqual(self.upload({}).status_code, 401)
        self.assertEqual(self.upload({**self.headers('root'), 'origin': 'https://other.invalid'}).status_code, 403)
        self.assertEqual(self.package.read_bytes(), b'previous-synthetic-package')
        self.assertEqual(db.get_setting(updater.PACKAGE_META_KEY)['name'], 'previous.tar.gz')

    def test_read_only_metadata_available_to_both_human_admin_roles(self):
        for name in ('root', 'reader'):
            response = self.client.get('/api/system/update-package', headers=self.headers(name))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()['upload_enabled'])

    def test_revoked_cookie_and_admin_service_token_cannot_write(self):
        security.revoke_sessions('root')
        self.assertEqual(self.upload(self.headers('root')).status_code, 401)
        token = self.tokensvc.create_token('synthetic-update-boundary', 'admin')['token']
        self.assertEqual(self.upload({'Authorization': 'Bearer ' + token}).status_code, 403)
        self.assertEqual(self.package.read_bytes(), b'previous-synthetic-package')


class ManagedWorkerBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='managed-worker-boundary-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'deploy').mkdir()
        (self.root / 'deploy/managed-release.json').write_text('{}', encoding='ascii')
        self.worker = load_script(ROOT / 'deploy/update.py', 'managed_boundary_worker')

    def test_direct_worker_operations_refuse_before_reporter_or_effects(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.worker, 'INSTALL_DIR', self.root))
            for name in ('download', 'verify_download', '_sync_bundled_upstream', 'swap_dir',
                         'install_requirements', 'restart_service', '_exit_for_restart',
                         'fetch_release_via_api', 'fetch_release_via_redirect'):
                stack.enter_context(mock.patch.object(self.worker, name,
                                    side_effect=AssertionError('no effect: ' + name)))
            rep = mock.Mock()
            for function, args in ((self.worker.update_manager, (rep,)),
                                   (self.worker.update_upstream, (rep,)),
                                   (self.worker._release_root, (rep,))):
                with self.subTest(function=function.__name__):
                    with self.assertRaisesRegex(RuntimeError, 'verified deployment controller'):
                        function(*args)
            rep.assert_not_called()
            self.assertEqual(rep.mock_calls, [])

    def test_cli_all_targets_refuse_before_reporter(self):
        with mock.patch.object(self.worker, 'INSTALL_DIR', self.root), \
                mock.patch.object(self.worker, 'Reporter', side_effect=AssertionError('no Reporter')):
            for target in ('manager', 'upstream', 'both'):
                with mock.patch('sys.argv', ['update.py', '--target', target]), redirect_stdout(io.StringIO()):
                    self.assertEqual(self.worker.main(), 1)

    def test_cached_release_cannot_bypass_managed_policy(self):
        self.worker._release_cache = (self.root / 'previous-extract', {'synthetic': True})
        with mock.patch.object(self.worker, 'INSTALL_DIR', self.root):
            with self.assertRaisesRegex(RuntimeError, 'verified deployment controller'):
                self.worker._release_root(mock.Mock())

    def test_broken_marker_symlink_still_blocks(self):
        marker = self.root / 'deploy/managed-release.json'
        marker.unlink()
        try:
            marker.symlink_to(self.root / 'missing-marker-target')
        except OSError as exc:
            self.skipTest('filesystem cannot create a synthetic symlink: ' + str(exc))
        with mock.patch.object(config, 'ROOT', self.root), \
                mock.patch.object(self.worker, 'INSTALL_DIR', self.root):
            self.assertTrue(updater.managed_release())
            self.assertTrue(self.worker._managed_release())
            with self.assertRaisesRegex(RuntimeError, 'verified deployment controller'):
                self.worker.update_manager(mock.Mock())


class RetiredVerifierTest(unittest.TestCase):
    def test_legacy_cli_never_reads_extracts_or_approves(self):
        verifier = load_script(REPO / 'tools/verify_signed_release.py', 'retired_verifier')
        for args in ([], ['--release', 'absent', '--archive', 'absent'], ['--help']):
            with self.subTest(args=args), mock.patch('sys.argv', ['verify_signed_release.py', *args]), \
                    mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('no file reads')), \
                    mock.patch.object(Path, 'read_text', side_effect=AssertionError('no file reads')), \
                    redirect_stdout(io.StringIO()) as output:
                try:
                    code = verifier.main()
                except SystemExit as exc:
                    code = exc.code
                self.assertEqual(code, 2)
            self.assertIn('RETIRED', output.getvalue())
            self.assertNotIn('VERIFIED', output.getvalue())

    def test_historically_accepted_signed_fixture_is_now_rejected(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        verifier = load_script(REPO / 'tools/verify_signed_release.py', 'retired_verifier_fixture')
        with tempfile.TemporaryDirectory(prefix='retired-verifier-fixture-') as name:
            root = Path(name)
            key = Ed25519PrivateKey.generate()
            payload = b'inert-signed-text-not-bound-to-manifest'
            (root / 'trust-release-pub.pem').write_bytes(key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            (root / 'SIGNED-PAYLOAD.txt').write_bytes(payload)
            (root / 'SIGNED-PAYLOAD.sig').write_bytes(key.sign(payload))
            body = b'inert-fixture-file'
            (root / 'MANIFEST.sha256').write_text(
                hashlib.sha256(body).hexdigest() + '  server/main.py\n', encoding='ascii')
            archive_path = root / 'inert.tar.gz'
            with tarfile.open(archive_path, 'w:gz') as archive:
                member = tarfile.TarInfo('workspace/server/main.py')
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
            with mock.patch('sys.argv', ['verify_signed_release.py', '--release', str(root),
                                        '--archive', str(archive_path)]), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(verifier.main(), 2)
            self.assertIn('RETIRED', output.getvalue())
            self.assertNotIn('VERIFIED', output.getvalue())


if __name__ == '__main__':
    unittest.main()
