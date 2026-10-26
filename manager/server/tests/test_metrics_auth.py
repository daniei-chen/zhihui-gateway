"""K08: actual HTTP and static fallback must share metrics authentication."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from server import config, db, security, tokensvc


class MetricsAuthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='metrics-auth-')
        self.root = Path(self.temp.name)
        self.patches = contextlib.ExitStack()
        with db._lock:
            db._close_connection_locked()
        for key, value in {
            'DB_PATH': self.root / 'metrics.db', 'USERS_FILE': self.root / 'users.json',
            'METRICS_TOKEN': 'wbm_' + 'test-collector-' * 4, 'METRICS_TOKEN_FILE': '',
        }.items():
            self.patches.enter_context(mock.patch.object(config, key, value))
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'qa-session-signing-' * 4,
            'users': [{'username': 'operator', 'role': 'admin', 'pwd_hash': 'unused'},
                      {'username': 'reader', 'role': 'viewer', 'pwd_hash': 'unused'}],
        }), encoding='utf-8')
        db.connect()
        from server import main
        self.main = main
        self.client = TestClient(main.app)
        security._fail.clear()
        security._user_fail.clear()

    def tearDown(self):
        self.client.close()
        with db._lock:
            db._close_connection_locked()
        security._fail.clear()
        security._user_fail.clear()
        self.patches.close()
        self.temp.cleanup()

    def collector(self, value=None):
        return {'Authorization': 'Bearer ' + (value or config.METRICS_TOKEN)}

    def test_anonymous_and_wrong_bearer_cannot_query_metric_data(self):
        with mock.patch.object(db, 'query', wraps=db.query) as query:
            for headers in ({}, self.collector('wbm_' + 'wrong' * 12)):
                result = self.client.get('/metrics', headers=headers)
                self.assertEqual(result.status_code, 401, result.text)
                self.assertNotIn('workbuddy_requests', result.text)
            query.assert_not_called()

    def test_collector_can_scrape_but_cannot_read_or_write_management(self):
        response = self.client.get('/metrics', headers=self.collector())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('workbuddy_up 1', response.text)
        self.assertIn('no-store', response.headers['cache-control'])
        self.assertNotIn(config.METRICS_TOKEN, response.text)
        for path in ('/api/keys', '/api/settings/upstream', '/api/tokens'):
            denied = self.client.get(path, headers=self.collector())
            self.assertEqual(denied.status_code, 401, (path, denied.text))
        denied = self.client.post('/api/keys', headers=self.collector(), json={'name': 'forbidden'})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(db.query_one('SELECT COUNT(*) AS n FROM api_keys')['n'], 0)

    def test_admin_tokens_qualify_and_other_existing_scopes_do_not(self):
        for scope, expected in (('admin', 200), ('readonly', 403), ('keys', 403)):
            with self.subTest(scope=scope):
                token = tokensvc.create_token('metrics-' + scope, scope)['token']
                self.client.cookies.clear()
                result = self.client.get('/metrics', headers={'Authorization': 'Bearer ' + token})
                self.assertEqual(result.status_code, expected, result.text)

    def test_role_is_checked_for_cookie_sessions(self):
        for name, role, status in (('operator', 'admin', 200), ('reader', 'viewer', 403)):
            self.client.cookies.clear()
            self.client.cookies.set(config.COOKIE_NAME, security.issue_token(name, role))
            self.assertEqual(self.client.get('/metrics').status_code, status)

    def test_direct_fallback_call_cannot_bypass_dependency_injection(self):
        request = Request({'type': 'http', 'path': '/metrics', 'headers': [],
                           'method': 'GET', 'client': ('127.0.0.1', 1234)})
        with self.assertRaises(HTTPException) as denied:
            self.main.prometheus_metrics(request)
        self.assertEqual(denied.exception.status_code, 401)

    def test_prefix_alias_uses_the_same_authority(self):
        with TestClient(self.main.StripBasePathMiddleware(self.main.app, '/wb')) as client:
            self.assertEqual(client.get('/wb/metrics').status_code, 401)
            self.assertEqual(client.get('/wb/metrics', headers=self.collector()).status_code, 200)

    def test_credential_file_rotates_without_restart_and_stale_value_is_rejected(self):
        path = self.root / 'metrics-token'
        old = config.METRICS_TOKEN
        path.write_text(old + '\n')
        path.chmod(0o600)
        with mock.patch.object(config, 'METRICS_TOKEN_FILE', str(path)):
            self.assertEqual(self.client.get('/metrics', headers=self.collector(old)).status_code, 200)
            new = 'wbm_' + 'new-qa-collector-' * 4
            path.write_text(new + '\n')
            self.assertEqual(self.client.get('/metrics', headers=self.collector(old)).status_code, 401)
            self.assertEqual(self.client.get('/metrics', headers=self.collector(new)).status_code, 200)

    def test_missing_weak_or_misclassified_credential_never_opens_anonymous_access(self):
        for value in ('', 'wbm_short', 'wbt_' + 'a' * 64, 'wbm_' + 'two words' * 5):
            with mock.patch.object(config, 'METRICS_TOKEN', value):
                result = self.client.get('/metrics', headers={'Authorization': 'Bearer ' + value})
                self.assertEqual(result.status_code, 401)
        with mock.patch.object(config, 'METRICS_TOKEN_FILE', str(self.root / 'missing')):
            self.assertEqual(self.client.get('/metrics', headers=self.collector()).status_code, 401)

    @unittest.skipUnless(os.name == 'posix', 'POSIX file mode and O_NOFOLLOW')
    def test_shared_or_symlink_credential_file_fails_closed(self):
        path = self.root / 'metrics-token'
        path.write_text(config.METRICS_TOKEN)
        path.chmod(0o644)
        with mock.patch.object(config, 'METRICS_TOKEN_FILE', str(path)):
            self.assertEqual(self.client.get('/metrics', headers=self.collector()).status_code, 401)
        path.chmod(0o600)
        link = self.root / 'metrics-link'
        link.symlink_to(path)
        with mock.patch.object(config, 'METRICS_TOKEN_FILE', str(link)):
            self.assertEqual(self.client.get('/metrics', headers=self.collector()).status_code, 401)


if __name__ == '__main__':
    unittest.main()
