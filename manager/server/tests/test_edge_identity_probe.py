"""Manager identity bridge: current authority, credential class, and safe failure."""
import base64
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import config, db, security, tokensvc


class EdgeIdentityProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.original = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR,
                        config.QTG_IDENTITY_SECRET_FILE)
        config.DB_PATH = cls.root / 'manager.db'
        config.USERS_FILE = cls.root / 'users.json'
        config.STATIC_DIR = cls.root / 'absent-static'
        config.QTG_IDENTITY_SECRET_FILE = str(cls.root / 'identity-secret')
        db._conn = None
        db.connect()
        from server.main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        with db._lock:
            db._close_connection_locked()
        (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR,
         config.QTG_IDENTITY_SECRET_FILE) = cls.original
        cls.tmp.cleanup()

    def setUp(self):
        self.client.cookies.clear()
        security._fail.clear()
        security._user_fail.clear()
        Path(config.QTG_IDENTITY_SECRET_FILE).write_text('test-bridge-secret-' * 4)
        Path(config.QTG_IDENTITY_SECRET_FILE).chmod(0o600)
        security.save_users({'secret': 'manager-test-' * 8, 'api_keys': [],
            'users': [{'username': '管理员', 'role': 'admin', 'pwd_hash': 'unused'}]})

    def tearDown(self):
        self.client.cookies.clear()
        security._fail.clear()
        security._user_fail.clear()

    def cookie(self):
        return {'Cookie': f'{config.COOKIE_NAME}={security.issue_token("管理员", "admin")}'}

    def probe(self, headers):
        result = self.client.get('/api/authz/probe', headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn('no-store', result.headers['cache-control'])
        token = result.headers['x-qtg-identity']
        self.assertTrue(token.isascii())
        body = token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))

    def test_current_role_and_unicode_subject_are_preserved(self):
        cookie = self.cookie()
        self.assertEqual(self.probe(cookie)['sub'], '管理员')
        cfg = security.load_users()
        cfg['users'][0]['role'] = 'viewer'
        security.save_users(cfg)
        ident = self.probe(cookie)
        self.assertEqual((ident['role'], ident['scopes'], ident['auth_method']),
                         ('viewer', ['read'], 'edge_session'))

    def test_revoked_or_deleted_session_is_denied(self):
        cookie = self.cookie()
        security.revoke_sessions('管理员')
        self.assertEqual(self.client.get('/api/authz/probe', headers=cookie).status_code, 401)
        cookie = self.cookie()
        cfg = security.load_users()
        cfg['users'] = []
        security.save_users(cfg)
        self.assertEqual(self.client.get('/api/authz/probe', headers=cookie).status_code, 401)

    def test_account_admin_bridge_is_read_only_not_an_add_only_dead_end(self):
        cookie = self.cookie()
        cfg = security.load_users()
        cfg['users'][0]['role'] = 'account_admin'
        security.save_users(cfg)
        ident = self.probe(cookie)
        self.assertEqual((ident['role'], ident['scopes']), ('viewer', ['read']))

    def test_token_class_scope_and_stable_subject(self):
        for scope, role, scopes in [('admin', 'admin', ['*']),
                                   ('readonly', 'viewer', ['read']),
                                   ('keys', 'internal_service', ['keys:read', 'keys:write'])]:
            row = tokensvc.create_token('bridge-' + scope, scope)
            ident = self.probe({'Authorization': 'Bearer ' + row['token']})
            self.assertEqual(ident['sub'], 'manager-token:' + str(row['id']))
            self.assertEqual((ident['role'], ident['scopes'], ident['auth_method']),
                             (role, scopes, 'admin_token'))
            self.assertEqual(ident['exp'] - ident['iat'], 60)
            self.assertNotIn(row['token'], json.dumps(ident))

    def test_no_authentication_or_forged_identity_headers_is_denied(self):
        for headers in ({}, {'X-User': 'admin', 'X-QTG-Role': 'admin', 'X-QTG-Identity': 'forged'}):
            result = self.client.get('/api/authz/probe', headers=headers)
            self.assertEqual(result.status_code, 401)
            self.assertNotIn('x-qtg-identity', result.headers)

    def test_secret_rotation_takes_effect_without_restart(self):
        cookie = self.cookie()
        with patch('server.edgeidentity.time', SimpleNamespace(time=lambda:1800000000)), \
                patch('server.edgeidentity.secrets.token_hex', return_value='fixed-nonce'):
            first = self.client.get('/api/authz/probe', headers=cookie).headers['x-qtg-identity']
            Path(config.QTG_IDENTITY_SECRET_FILE).write_text('rotated-identity-key-' * 4)
            second = self.client.get('/api/authz/probe', headers=cookie).headers['x-qtg-identity']
        self.assertEqual(first.split('.')[1], second.split('.')[1])
        self.assertNotEqual(first.split('.')[2], second.split('.')[2])

    def test_missing_short_or_public_secret_fails_closed_without_path_leak(self):
        secret = Path(config.QTG_IDENTITY_SECRET_FILE)
        for content in ('', 'too-short'):
            secret.write_text(content)
            result = self.client.get('/api/authz/probe', headers=self.cookie())
            self.assertEqual(result.status_code, 503)
            self.assertNotIn(str(secret), result.text)
        secret.unlink()
        self.assertEqual(self.client.get('/api/authz/probe', headers=self.cookie()).status_code, 503)
        if os.name == 'posix':
            secret.write_text('public-file-' * 8)
            secret.chmod(0o644)
            self.assertEqual(self.client.get('/api/authz/probe', headers=self.cookie()).status_code, 503)

    def test_probe_does_not_read_business_keys_or_settings(self):
        with patch('server.keysvc.list_keys', side_effect=AssertionError('business read')):
            self.probe(self.cookie())

    def test_keys_service_cannot_read_other_management_domains(self):
        row = tokensvc.create_token('restricted-service', 'keys')
        headers = {'Authorization': 'Bearer ' + row['token']}
        for path in ('/api/accounts', '/api/logs', '/api/status', '/api/settings/github-mirror'):
            result = self.client.get(path, headers=headers)
            self.assertEqual(result.status_code, 403, (path, result.text))
        self.assertEqual(self.client.get('/api/keys', headers=headers).status_code, 200)
