"""All human admin pages remain readable without granting management writes."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config, db, security
from server.routers import accounts, auth, keys, proxies, redpackets, settings, tokens, upstreams


class PanelReaderHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_conn, db._conn = db._conn, None
        self.addCleanup(self.close_db)
        for name, value in {'DATA_DIR': Path(self.tmp.name),
                            'DB_PATH': Path(self.tmp.name) / 'manager.db',
                            'USERS_FILE': Path(self.tmp.name) / 'users.json'}.items():
            patch = mock.patch.object(config, name, value)
            patch.start(); self.addCleanup(patch.stop)
        security.save_users({'secret': 'synthetic-panel-reader-only', 'role_policy_version': 2,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'synthetic'},
                      {'username': 'alice', 'role': 'account_admin', 'pwd_hash': 'synthetic'}]})
        db.connect()
        app = FastAPI()
        for module in (accounts, auth, keys, proxies, redpackets, settings, tokens, upstreams):
            app.include_router(module.router)
        self.client = TestClient(app)
        self.client.cookies.set(config.COOKIE_NAME, security.issue_token('alice', 'account_admin'))

    def close_db(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = self.old_conn

    def test_masked_lists_are_readable_and_privilege_mutations_remain_forbidden(self):
        for path in ('/api/keys', '/api/tokens', '/api/users', '/api/upstreams',
                     '/api/settings/panel', '/api/settings/github-token', '/api/settings/github-mirror',
                     '/api/proxy-routes', '/api/proxy-routes/bindings', '/api/settings/proxy'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                for forbidden in ('pwd_hash', 'token_hash', 'synthetic-panel-reader-only'):
                    self.assertNotIn(forbidden, response.text)
        for method, path, body in (
            ('POST', '/api/keys', {'name': 'forbidden'}),
            ('PATCH', '/api/keys/1', {'quota': 0}),
            ('DELETE', '/api/keys/1', None),
            ('POST', '/api/keys/1/reset-usage', {}),
            ('POST', '/api/tokens', {'name': 'forbidden'}),
            ('PUT', '/api/settings/github-token', {'token': 'forbidden'}),
            ('PUT', '/api/settings/proxy', {}),
            ('POST', '/api/settings/upstream', {}),
            ('POST', '/api/users', {'username': 'forbidden', 'password': '12345678'}),
            ('PATCH', '/api/users/alice', {'role': 'admin'})):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.client.request(method, path, json=body).status_code, 403)

    def test_red_packet_read_does_not_disclose_a_claim_capability(self):
        original = {'id': 1, 'title': 'synthetic', 'code': 'synthetic-private-claim'}
        with mock.patch.object(redpackets.redpacket, 'list_packets', return_value=[original]), \
             mock.patch.object(redpackets.redpacket, 'packet_detail', return_value=original):
            self.assertEqual(self.client.get('/api/red-packets').json()[0]['code'], '')
            self.assertEqual(self.client.get('/api/red-packets/1').json()['code'], '')
        self.assertEqual(original['code'], 'synthetic-private-claim')

    def test_readonly_task_status_never_prepares_or_executes_a_task(self):
        with mock.patch.object(accounts.taskrun, 'available', side_effect=AssertionError('runtime preparation')), \
             mock.patch('subprocess.run', side_effect=AssertionError('process effect')):
            response = self.client.get('/api/task-run')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['available'])

    def test_proxy_identifiers_and_query_credentials_are_not_returned_to_reader(self):
        user = {'role': 'account_admin'}
        self.assertEqual(proxies._reader_url('https://private-user:***@proxy.example:443/?token=private', user),
                         'https://***:***@proxy.example:443')

    def test_qoder_and_trae_middleware_read_paths_keep_write_boundary(self):
        from server import main
        before = dict(main.app.dependency_overrides)
        main.app.dependency_overrides[security.current_user] = lambda: {'username': 'alice', 'role': 'account_admin'}
        try:
            with mock.patch.object(main.security, '_current_user', return_value={'username': 'alice', 'role': 'account_admin'}):
                # A known read handler returns its real-shaped synthetic data.
                for platform in ('qoder', 'trae'):
                    with mock.patch.object(main._qcf, 'build_aggregate', new=mock.AsyncMock()) as aggregate, \
                         mock.patch.object(main._qcf, '_rt'):
                        aggregate.return_value.qoder_accounts = []
                        aggregate.return_value.trae_accounts = []
                        aggregate.return_value.wb_accounts = []
                        aggregate.return_value.health = []
                        pool = TestClient(main.app).get('/api/accounts?platform=' + platform)
                        self.assertEqual(pool.status_code, 200, pool.text)
                    # Native key reads and routed account writes share the role.
                    with self.subTest(platform=platform):
                        response = TestClient(main.app).get('/api/keys?platform=' + platform)
                        self.assertEqual(response.status_code, 200, response.text)
                        denied = TestClient(main.app).post('/api/accounts/checkin-all?platform=' + platform, json={})
                        self.assertEqual(denied.status_code, 403, denied.text)
        finally:
            main.app.dependency_overrides.clear()
            main.app.dependency_overrides.update(before)

    def test_read_dependencies_do_not_turn_machine_keys_or_viewers_into_human_admins(self):
        for user in ({'role': 'viewer'}, {'role': 'readonly'},
                     {'role': 'account_admin', 'scope': 'read'}, {'role': 'viewer', 'scope': 'keys'}):
            with self.subTest(user=user), self.assertRaises(Exception):
                security.require_panel_reader(user)
        self.assertEqual(security.require_panel_reader({'role': 'account_admin'})['role'], 'account_admin')


if __name__ == '__main__':
    unittest.main()
