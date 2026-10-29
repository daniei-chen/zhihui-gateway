"""Regression coverage for renewal, platform scopes, and signed source closure."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import logging
import stat
import tempfile
import time
import types
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from server import config, security

ROOT = Path(__file__).resolve().parents[2]


def load_functions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    selected = [node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in selected:
        node.decorator_list = []
    unit = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                           *selected], type_ignores=[])
    ast.fix_missing_locations(unit)
    exec(compile(unit, str(path), 'exec'), namespace)
    return namespace


def request(path='/api/me', *, method='GET', cookie='', query='', platform=''):
    headers = [(b'host', b'audit.invalid')]
    if cookie:
        headers.append((b'cookie', f'{config.COOKIE_NAME}={cookie}'.encode()))
    if platform:
        headers.append((b'x-qtg-platform', platform.encode()))
    return Request({'type': 'http', 'method': method, 'scheme': 'https', 'path': path,
                    'raw_path': path.encode(), 'query_string': query.encode(), 'headers': headers,
                    'client': ('127.0.0.1', 1234), 'server': ('audit.invalid', 443)})


class RenewalRegressionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in {'DATA_DIR': Path(tmp.name), 'USERS_FILE': Path(tmp.name) / 'users.json'}.items():
            patcher = mock.patch.object(config, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.cfg = {'secret': 'SYNTHETIC-TEST-SECRET', 'users': [
            {'username': 'audit-admin', 'role': 'admin', 'sv': 0}]}
        for patcher in (mock.patch.object(security, 'load_users', side_effect=lambda: self.cfg),
                        mock.patch.object(security, 'save_users'),
                        mock.patch.object(security, 'audit'),
                        mock.patch.object(config, 'SESSION_IDLE_HOURS', 3),
                        mock.patch.object(config, 'SESSION_DAYS', 1)):
            patcher.start()
            self.addCleanup(patcher.stop)
        namespace = {'security': security, 'config': config, 'Request': Request,
                     'logger': logging.getLogger('audit-test')}
        self.middleware = load_functions(ROOT / 'server/main.py', {'cache_headers'}, namespace)['cache_headers']
        namespace = {'security': security, 'config': config, 'Request': Request, 'Depends': Depends,
                     'HTTPException': HTTPException, 'JSONResponse': JSONResponse,
                     'client_ip': lambda req: '127.0.0.1'}
        self.revoke = load_functions(ROOT / 'server/routers/auth.py', {'revoke_own_sessions'}, namespace)['revoke_own_sessions']
        now = int(time.time())
        self.payload = {'username': 'audit-admin', 'role': 'admin', 'sv': 0,
                        'iat': now - 4000, 'orig': now - 5000, 'exp': now + 70000}
        self.cookie = security._sign(json.dumps(self.payload), self.cfg['secret'])

    async def dispatch(self, handler, path='/api/me'):
        req = request(path, cookie=self.cookie)

        async def authenticated(incoming):
            user = security._cookie_identity(incoming, self.cfg)
            return handler(incoming, user)

        return await self.middleware(req, authenticated)

    async def test_revoke_keeps_deletion_and_does_not_reissue(self):
        response = await self.dispatch(self.revoke, '/api/sessions/revoke')
        cookies = response.headers.getlist('set-cookie')
        self.assertEqual(len(cookies), 1)
        self.assertIn('Max-Age=0', cookies[0])
        with self.assertRaises(HTTPException):
            security._cookie_identity(request(cookie=self.cookie), self.cfg)

    async def test_concurrent_revocation_cannot_promote_old_version(self):
        def handler(req, user):
            security.revoke_sessions(user['username'])
            return JSONResponse({'ok': True})

        response = await self.dispatch(handler)
        self.assertEqual(response.headers.getlist('set-cookie'), [])

    async def test_deleted_user_is_not_renewed(self):
        def handler(req, user):
            self.cfg['users'] = []
            return JSONResponse({'ok': True})

        self.assertEqual((await self.dispatch(handler)).headers.getlist('set-cookie'), [])

    async def test_error_response_is_not_renewed(self):
        response = await self.dispatch(lambda req, user: JSONResponse({'detail': 'denied'}, status_code=403))
        self.assertEqual(response.headers.getlist('set-cookie'), [])

    async def test_existing_cookie_is_not_overwritten(self):
        def handler(req, user):
            response = JSONResponse({'ok': True})
            response.set_cookie(config.COOKIE_NAME, 'replacement-owned-by-handler')
            return response

        values = (await self.dispatch(handler)).headers.getlist('set-cookie')
        self.assertEqual(len(values), 1)
        self.assertIn('replacement-owned-by-handler', values[0])

    async def test_normal_renewal_keeps_original_start_and_version(self):
        response = await self.dispatch(lambda req, user: JSONResponse({'ok': True}))
        cookies = SimpleCookie()
        cookies.load(response.headers['set-cookie'])
        payload = security._unsign(cookies[config.COOKIE_NAME].value, self.cfg['secret'])
        self.assertEqual(payload['orig'], self.payload['orig'])
        self.assertEqual(payload['exp'], self.payload['orig'] + config.SESSION_DAYS * 86400)
        self.assertEqual(payload['sv'], 0)
        self.assertGreater(payload['iat'], self.payload['iat'])

    def test_issue_token_checks_expected_version_on_its_own_snapshot(self):
        self.cfg['users'][0]['sv'] = 1
        with self.assertRaises(HTTPException):
            security.issue_token('audit-admin', '', expected_sv=0)

    def test_issue_token_reads_users_once(self):
        with mock.patch.object(security, 'load_users', return_value=self.cfg) as load:
            token = security.issue_token('audit-admin', '', expected_sv=0)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(security._unsign(token, self.cfg['secret'])['sv'], 0)

    def test_revocation_after_snapshot_only_yields_an_invalid_old_version(self):
        def load_snapshot_then_revoke():
            snapshot = copy.deepcopy(self.cfg)
            self.cfg['users'][0]['sv'] += 1
            return snapshot

        with mock.patch.object(security, 'load_users', side_effect=load_snapshot_then_revoke):
            token = security.issue_token('audit-admin', '', expected_sv=0)
        self.assertEqual(security._unsign(token, self.cfg['secret'])['sv'], 0)
        with self.assertRaises(HTTPException):
            security._cookie_identity(request(cookie=token), self.cfg)


class PlatformScopeRegressionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        namespace = {'__package__': 'server', 'security': security, '_QTG_INTERNAL': '/__qtg',
                     '_QTGP_ROUTE_PREFIXES': ('/api/accounts', '/api/stats/', '/api/models'),
                     'logger': logging.getLogger('audit-test')}
        self.middleware = load_functions(ROOT / 'server/main.py',
                                         {'_qtg_should_route', '_qtg_platform_route'}, namespace)['_qtg_platform_route']

    async def call(self, req, identity):
        seen = []

        def authenticate(incoming):
            incoming.state.token_id = 99
            return identity

        async def handler(incoming):
            seen.append(incoming.scope['path'])
            return JSONResponse({'ok': True})

        with mock.patch.object(security, '_current_user', side_effect=authenticate):
            response = await self.middleware(req, handler)
        return response, seen

    async def test_keys_scope_cannot_read_platform_data_by_header_or_query(self):
        for platform in ('qoder', 'trae'):
            for transport in ('header', 'query'):
                for path in ('/api/accounts', '/api/stats/summary', '/api/models'):
                    with self.subTest(platform=platform, transport=transport, path=path):
                        req = request(path, platform=platform if transport == 'header' else '',
                                      query=f'platform={platform}' if transport == 'query' else '')
                        response, seen = await self.call(req, {'username': 'svc', 'role': 'viewer', 'scope': 'keys'})
                        self.assertEqual(response.status_code, 403)
                        self.assertEqual(seen, [])

    async def test_read_scope_can_read_accounts_but_not_write(self):
        identity = {'username': 'svc', 'role': 'viewer', 'scope': 'read'}
        response, seen = await self.call(request('/api/accounts', platform='qoder'), identity)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, ['/__qtg/api/accounts'])
        response, seen = await self.call(request('/api/accounts', method='POST', platform='qoder'), identity)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(seen, [])

    async def test_forbidden_keeps_csrf_reason(self):
        async def handler(req):
            raise AssertionError('forbidden request reached the handler')

        with mock.patch.object(security, 'current_user', side_effect=HTTPException(
                403, headers={'X-Manager-Csrf-Reason': 'csrf_origin_mismatch'})):
            response = await self.middleware(request('/api/accounts', platform='qoder', method='POST'), handler)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers['X-Manager-Csrf-Reason'], 'csrf_origin_mismatch')


class SourceGateRegressionTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('audit_source_preflight', ROOT / 'deploy/source_preflight.py')
        self.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gate)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'server').mkdir()
        (self.root / 'deploy').mkdir()
        self.python = self.root / 'server/main.py'
        self.python.write_text('VALUE = 1\n', encoding='utf-8')
        self.python.chmod(0o444)
        self.marker = self.root / 'deploy/managed-release.json'
        self.signature = self.root / 'deploy/managed-release.sig'
        self.signature.write_bytes(b'synthetic-signature')
        self.manifest = {'signature_required': True, 'signature_algorithm': 'ed25519', 'files': {
            'server/main.py': {'mode': stat.S_IMODE(self.python.stat().st_mode),
                               'candidate_sha256': hashlib.sha256(self.python.read_bytes()).hexdigest()}}}
        self.write_manifest()

    def write_manifest(self):
        self.marker.write_text(json.dumps(self.manifest), encoding='utf-8')

    def checked(self):
        with mock.patch.object(self.gate, 'verify_signature') as verified:
            result = self.gate.check(self.root)
        verified.assert_called_once()
        return result

    def test_complete_valid_tree_passes(self):
        self.assertEqual(self.checked(), 1)

    def test_missing_manifest_fails_closed(self):
        self.marker.unlink()
        with self.assertRaisesRegex(RuntimeError, 'manifest missing'):
            self.gate.check(self.root)

    def test_unsigned_switch_fails_closed(self):
        self.manifest['signature_required'] = False
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, 'must require a signature'):
            self.gate.check(self.root)

    def test_empty_files_cannot_pass(self):
        self.manifest['files'] = {}
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, 'missing or empty'):
            self.checked()

    def test_bad_signature_cannot_pass(self):
        with mock.patch.object(self.gate, 'verify_signature', side_effect=RuntimeError('bad signature')):
            with self.assertRaisesRegex(RuntimeError, 'bad signature'):
                self.gate.check(self.root)

    def test_signature_algorithm_cannot_be_downgraded(self):
        self.manifest['signature_algorithm'] = 'rsa-sha256'
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, 'signature_algorithm=ed25519'):
            self.gate.check(self.root)

    def test_missing_signature_cannot_pass(self):
        self.signature.unlink()
        with self.assertRaisesRegex(RuntimeError, 'signature missing'):
            self.gate.check(self.root)

    def test_changed_listed_source_fails_hash_check(self):
        self.python.chmod(0o644)
        self.python.write_text('VALUE = 2\n', encoding='utf-8')
        self.python.chmod(0o444)
        with self.assertRaisesRegex(RuntimeError, 'hash mismatch'):
            self.checked()

    def test_unsigned_code_config_and_backup_files_are_rejected(self):
        for name in ('server/extra.py', 'server/schema.json', 'deploy/old.bak'):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text('{}', encoding='utf-8')
                try:
                    with self.assertRaisesRegex(RuntimeError, 'closure mismatch'):
                        self.checked()
                finally:
                    path.unlink()

    def test_runtime_bytecode_is_not_a_signed_source_member(self):
        cache = self.root / 'server/__pycache__'
        cache.mkdir()
        (cache / 'main.cpython-test.pyc').write_bytes(b'not imported by the gate')
        self.assertEqual(self.checked(), 1)

    def test_extra_symlink_is_rejected(self):
        link = self.root / 'server/link.py'
        try:
            link.symlink_to(self.python)
        except OSError as exc:
            self.skipTest(f'filesystem cannot create a symlink: {exc}')
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.checked()

    def test_managed_top_level_symlinks_are_supported(self):
        release = self.root / 'releases/current'
        release.mkdir(parents=True)
        for name in ('server', 'deploy'):
            base = self.root / name
            base.rename(release / name)
            try:
                base.symlink_to(release / name, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f'filesystem cannot create a symlink: {exc}')
        self.assertEqual(self.checked(), 1)


class BuildInputsRegressionTest(unittest.TestCase):
    def test_lockfile_is_copied_before_python_dependency_install(self):
        dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
        before_install = dockerfile.split('pip install --no-cache-dir', 1)[0]
        self.assertIn('COPY server/requirements.txt server/requirements.lock.txt /app/server/', before_install)
        self.assertIn('-r requirements.lock.txt', (ROOT / 'server/requirements.txt').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
