"""Keep login failure semantics while removing the missing-user fast path."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from server import config, security
from server.routers import auth


class LoginTimingRegressionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in [('DATA_DIR', root), ('DB_PATH', root / 'timing.db'),
                            ('USERS_FILE', root / 'users.json')]:
            patcher = mock.patch.object(config, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.user = {'username': 'fixture-admin', 'role': 'admin', 'pwd_hash': 'fixture-hash', 'sv': 0}
        self.cfg = {'secret': 'synthetic-login-timing-secret', 'users': [self.user]}
        security.save_users(self.cfg)
        self.request = mock.Mock()
        self.request.json = mock.AsyncMock(return_value={
            'username': 'fixture-admin', 'password': 'fixture-password'})
        self.mocks = {}
        values = {
            'login_blocked': False, 'load_users': self.cfg,
            'verify_pwd': False, 'dummy_verify': None, 'record_fail': None,
            'audit': None, 'clear_fail': None, 'insecure_cookie_warning': None,
            'issue_token': 'fixture-session', 'cookie_secure': True,
        }
        for name, value in values.items():
            patcher = mock.patch.object(security, name, return_value=value)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(auth, 'client_ip', return_value='192.0.2.1')
        patcher.start()
        self.addCleanup(patcher.stop)

    async def failure(self, status):
        with self.assertRaises(HTTPException) as result:
            await auth.login(self.request)
        self.assertEqual(result.exception.status_code, status)
        return result.exception.detail

    async def test_missing_user_runs_dummy_verification_and_same_failure_audit(self):
        self.mocks['load_users'].return_value = {'users': []}
        detail = await self.failure(401)
        self.mocks['dummy_verify'].assert_called_once_with('fixture-password')
        self.mocks['verify_pwd'].assert_not_called()
        self.mocks['record_fail'].assert_called_once_with('192.0.2.1', 'fixture-admin')
        self.assertEqual(self.mocks['audit'].call_args.args[1], 'login_failed')
        self.mocks['clear_fail'].assert_not_called()
        self.mocks['issue_token'].assert_not_called()

        self.mocks['load_users'].return_value = self.cfg
        self.assertEqual(await self.failure(401), detail)

    async def test_known_user_wrong_password_checks_only_real_hash(self):
        await self.failure(401)
        self.mocks['dummy_verify'].assert_not_called()
        self.mocks['verify_pwd'].assert_called_once_with('fixture-password', 'fixture-hash')
        self.mocks['record_fail'].assert_called_once_with('192.0.2.1', 'fixture-admin')
        self.mocks['issue_token'].assert_not_called()

    async def test_success_keeps_cookie_and_audit_contract(self):
        self.mocks['verify_pwd'].return_value = True
        response = await auth.login(self.request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {
            'ok': True, 'username': 'fixture-admin', 'role': 'admin'})
        cookie = response.headers['set-cookie']
        self.assertIn(f'{config.COOKIE_NAME}=fixture-session', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('Secure', cookie)
        self.mocks['dummy_verify'].assert_not_called()
        self.mocks['record_fail'].assert_not_called()
        self.mocks['clear_fail'].assert_called_once_with('192.0.2.1', 'fixture-admin')
        self.assertEqual(self.mocks['audit'].call_args.args[1], 'login')
        self.mocks['issue_token'].assert_called_once_with('fixture-admin', 'admin', expected_sv=0)

    async def test_blocked_login_stops_before_password_work(self):
        self.mocks['login_blocked'].return_value = True
        await self.failure(429)
        for name in ('load_users', 'verify_pwd', 'dummy_verify', 'record_fail', 'audit', 'issue_token'):
            self.mocks[name].assert_not_called()

    async def test_non_object_json_still_returns_bad_request_without_password_work(self):
        self.request.json.return_value = []
        await self.failure(400)
        for name in ('login_blocked', 'load_users', 'verify_pwd', 'dummy_verify', 'record_fail', 'audit'):
            self.mocks[name].assert_not_called()

    async def test_invalid_json_still_returns_bad_request_without_password_work(self):
        self.request.json.side_effect = ValueError('fixture invalid JSON')
        await self.failure(400)
        for name in ('login_blocked', 'load_users', 'verify_pwd', 'dummy_verify', 'record_fail', 'audit'):
            self.mocks[name].assert_not_called()


if __name__ == '__main__':
    unittest.main()
