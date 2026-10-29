"""Synthetic role migration and exact account-intake boundary, no provider calls."""
from __future__ import annotations

import copy
import concurrent.futures
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config, db, security, userroles
from server.routers import accounts, auth, settings


def _revoke_worker(filename, ready, go):
    config.USERS_FILE = Path(filename)
    ready.set()
    if not go.wait(15):
        raise RuntimeError('synthetic test start timed out')
    for _ in range(8):
        security.revoke_sessions('alice')


class UserRoleMigrationTest(unittest.TestCase):
    def test_existing_admin_is_root_and_every_other_user_is_restricted_once(self):
        original = {'secret': 'synthetic-only', 'users': [
            {'username': 'admin', 'role': 'admin', 'pwd_hash': 'synthetic-hash', 'sv': 7},
            {'username': 'alice', 'role': 'admin', 'pwd_hash': 'synthetic-hash', 'sv': 3},
            {'username': 'bob', 'role': 'viewer', 'pwd_hash': 'synthetic-hash'}]}
        before = copy.deepcopy(original)
        migrated, changed = userroles.migration(original)
        self.assertEqual(original, before)
        self.assertEqual(changed, 2)
        self.assertEqual([u['role'] for u in migrated['users']], ['admin', 'account_admin', 'account_admin'])
        self.assertEqual([u.get('sv', 0) for u in migrated['users']], [7, 4, 1])
        self.assertEqual(migrated['secret'], original['secret'])
        migrated['users'][1]['role'] = 'admin'  # A later explicit promotion must survive restart.
        again, changed = userroles.migration(migrated)
        self.assertIs(again, migrated)
        self.assertEqual(changed, 0)
        self.assertEqual(again['users'][1]['role'], 'admin')

    def test_invalid_or_future_migration_never_changes_input(self):
        for source in ({'users': []}, {'users': [{'username': 'admin'}, {'username': 'admin'}]},
                       {'role_policy_version': 3, 'users': [{'username': 'admin'}]}):
            before = copy.deepcopy(source)
            with self.assertRaises(ValueError):
                userroles.migration(source)
            self.assertEqual(before, source)

    def test_malformed_username_and_session_version_fail_closed(self):
        for extra in ({'username': None}, {'username': ''}, {'username': 'alice', 'sv': -1},
                      {'username': 'alice', 'sv': True}, {'username': 'alice', 'sv': 'corrupt'}):
            source = {'users': [{'username': 'admin'}, extra]}
            before = copy.deepcopy(source)
            with self.assertRaises(ValueError): userroles.migration(source)
            self.assertEqual(source, before)


class HumanRolesHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_conn = db._conn
        db._conn = None
        self.addCleanup(self.close_db)
        for name, value in {'DATA_DIR': Path(self.tmp.name), 'DB_PATH': Path(self.tmp.name) / 'manager.db',
                            'USERS_FILE': Path(self.tmp.name) / 'users.json'}.items():
            patch = mock.patch.object(config, name, value)
            patch.start(); self.addCleanup(patch.stop)
        security.save_users({'secret': 'synthetic-role-session-only', 'role_policy_version': 2, 'users': [
            {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('Synthetic-password!')},
            {'username': 'alice', 'role': 'account_admin', 'pwd_hash': security.make_hash('Synthetic-password!')}],
            'api_keys': []})
        app = FastAPI()
        for router in (auth.router, accounts.router, settings.router):
            app.include_router(router)
        self.client = TestClient(app)
        self.signin('alice')

    def close_db(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = self.old_conn

    def signin(self, username):
        self.client.cookies.set(config.COOKIE_NAME, security.issue_token(username, 'forged-snapshot'))

    def test_account_admin_cannot_read_other_surfaces_or_mutate_privileges(self):
        self.assertEqual(self.client.get('/api/me').json()['role'], 'account_admin')
        for method, path, body in [
            ('GET', '/api/accounts', None), ('GET', '/api/users', None),
            ('GET', '/api/settings/panel', None), ('GET', '/api/settings/upstream', None),
            ('GET', '/api/audit-logs', None), ('GET', '/api/accounts/export', None),
            ('POST', '/api/users', {'username': 'evil', 'role': 'admin', 'password': '6842097531'}),
            ('PATCH', '/api/users/alice', {'role': 'admin'}),
            ('DELETE', '/api/users/admin', None), ('POST', '/api/accounts/checkin-all', {}),
            ('POST', '/api/accounts/upload', {}), ('DELETE', '/api/accounts/fixture.json', None),
            ('POST', '/api/settings/upstream', {}), ('POST', '/api/sessions/revoke', {})]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.client.request(method, path, json=body).status_code, 403)

    def test_account_admin_can_start_default_authorization_but_not_choose_routes_or_groups(self):
        with mock.patch.object(accounts, '_group', return_value={'id': 1}), \
             mock.patch.object(accounts.tencent, 'start_login', new=mock.AsyncMock(return_value={
                 'state': '', 'realm': 'cn', 'authUrl': 'https://copilot.tencent.com/'})) as provider:
            response = self.client.post('/api/auth/start', json={'realm': 'cn', 'platform': 'workbuddy'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.client.post('/api/auth/start?upstream_id=8', json={'realm': 'cn'}).status_code, 403)
            self.assertEqual(self.client.post('/api/auth/start', json={'realm': 'cn', 'proxy': 'private'}).status_code, 403)
            provider.assert_awaited_once()

    def test_only_two_roles_can_be_created_and_root_cannot_be_demoted_or_deleted(self):
        self.signin('admin')
        self.assertEqual(self.client.get('/api/users').status_code, 200)
        for role in ('account_admin', 'admin'):
            response = self.client.post('/api/users', json={
                'username': 'synthetic-' + role, 'role': role, 'password': '6842097531'})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.post('/api/users', json={
            'username': 'old-viewer', 'role': 'viewer', 'password': '6842097531'}).status_code, 422)
        self.assertEqual(self.client.patch('/api/users/admin', json={'role': 'account_admin'}).status_code, 400)
        self.assertEqual(self.client.delete('/api/users/admin').status_code, 400)

    def test_super_admin_promotion_revokes_old_session_and_does_not_reset_on_migration(self):
        old = self.client.cookies.get(config.COOKIE_NAME)
        self.signin('admin')
        response = self.client.patch('/api/users/alice', json={'role': 'admin'})
        self.assertEqual(response.status_code, 200, response.text)
        self.client.cookies.set(config.COOKIE_NAME, old)
        self.assertEqual(self.client.get('/api/me').status_code, 401)
        security.migrate_user_roles()
        self.signin('alice')
        self.assertEqual(self.client.get('/api/users').status_code, 200)

    def test_root_changes_existing_roles_both_directions_and_revokes_each_old_session(self):
        def stored(username):
            return next(row for row in security.load_users()['users'] if row['username'] == username)

        self.signin('admin')
        created = self.client.post('/api/users', json={
            'username': 'second-super', 'role': 'admin', 'password': '6842097531'})
        self.assertEqual(created.status_code, 200, created.text)
        root_before = copy.deepcopy(stored('admin'))

        for username, role in (('alice', 'admin'), ('alice', 'account_admin'),
                               ('second-super', 'account_admin')):
            with self.subTest(username=username, role=role):
                before = copy.deepcopy(stored(username))
                previous_cookie = security.issue_token(username, before['role'])
                self.signin('admin')
                response = self.client.patch('/api/users/' + username, json={'role': role})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['role'], role)
                self.assertTrue(response.json()['sessions_revoked'])
                after = stored(username)
                self.assertEqual(after['role'], role)
                self.assertEqual(after['sv'], int(before.get('sv', 0)) + 1)
                self.assertEqual(after['pwd_hash'], before['pwd_hash'])
                self.client.cookies.set(config.COOKIE_NAME, previous_cookie)
                self.assertEqual(self.client.get('/api/me').status_code, 401)
                self.signin(username)
                self.assertEqual(self.client.get('/api/me').json()['role'], role)
                self.assertEqual(self.client.get('/api/users').status_code, 200 if role == 'admin' else 403)
                self.assertEqual(stored('admin'), root_before)

        security.migrate_user_roles()
        self.assertEqual(stored('alice')['role'], 'account_admin')
        self.assertEqual(stored('second-super')['role'], 'account_admin')
        self.assertEqual(stored('admin'), root_before)

    def test_unchanged_role_is_idempotent_and_does_not_revoke_target_session(self):
        self.signin('admin')
        for username, role in (('alice', 'account_admin'), ('admin', 'admin')):
            with self.subTest(username=username):
                before = copy.deepcopy(security.load_users())
                previous_cookie = security.issue_token(username, role)
                response = self.client.patch('/api/users/' + username, json={'role': role})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()['sessions_revoked'])
                self.assertFalse(response.json()['relogin_required'])
                self.assertEqual(security.load_users(), before)
                self.client.cookies.set(config.COOKIE_NAME, previous_cookie)
                self.assertEqual(self.client.get('/api/me').status_code, 200)
                self.signin('admin')

    def test_role_updates_reject_arbitrary_roles_without_changing_users(self):
        self.signin('admin')
        before = copy.deepcopy(security.load_users())
        for role in ('viewer', 'readonly', 'superadmin', '*', '', 'ADMIN', [], {}):
            with self.subTest(role=role):
                response = self.client.patch('/api/users/alice', json={'role': role})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(security.load_users(), before)

    def test_account_admin_cannot_promote_demote_or_change_root(self):
        before = copy.deepcopy(security.load_users())
        for username, role in (('alice', 'admin'), ('alice', 'account_admin'),
                               ('admin', 'account_admin'), ('admin', 'admin')):
            with self.subTest(username=username, role=role):
                response = self.client.patch('/api/users/' + username, json={'role': role})
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(security.load_users(), before)

    def test_root_privileges_remain_preserved_when_another_super_exists(self):
        self.signin('admin')
        created = self.client.post('/api/users', json={
            'username': 'second-super', 'role': 'admin', 'password': '6842097531'})
        self.assertEqual(created.status_code, 200, created.text)
        before = copy.deepcopy(security.load_users())
        for actor in ('admin', 'second-super'):
            with self.subTest(actor=actor):
                self.signin(actor)
                response = self.client.patch('/api/users/admin', json={'role': 'account_admin'})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(security.load_users(), before)
                self.assertEqual(self.client.delete('/api/users/admin').status_code, 400)
                self.assertEqual(security.load_users(), before)

    def test_managed_users_both_roles_accept_eight_or_more_raw_characters_and_can_log_in(self):
        self.signin('admin')
        cases = (('account_admin', '07392641'), ('admin', 'aB3!xY9?'),
                 ('account_admin', 'nine-1234'), ('admin', '6842097531'),
                 ('account_admin', 'Long mixed password without a ten-character cap!'),
                 ('admin', '智汇用户登录密码'), ('account_admin', '\U0001f680' * 8),
                 ('admin', '  raw  8'))
        for index, (role, password) in enumerate(cases):
            with self.subTest(role=role, length=len(password)):
                username = 'managed-' + str(index)
                response = self.client.post('/api/users', json={
                    'username': username, 'role': role, 'password': password})
                self.assertEqual(response.status_code, 200, response.text)
                row = next(u for u in security.load_users()['users'] if u['username'] == username)
                self.assertNotEqual(row['pwd_hash'], password)
                self.assertTrue(security.verify_pwd(password, row['pwd_hash']))
                self.client.cookies.clear()
                login = self.client.post('/api/login', json={'username': username, 'password': password})
                self.assertEqual(login.status_code, 200, login.text)
                self.assertEqual(self.client.get('/api/me').json()['role'], role)
                self.signin('admin')

    def test_managed_user_create_rejects_fewer_than_eight_characters_without_mutation(self):
        self.signin('admin')
        before = copy.deepcopy(security.load_users())
        invalid = ('seven12', '0739264', '智汇密码七个字', '\U0001f680' * 7, '       ')
        for password in invalid:
            with self.subTest(password=password):
                response = self.client.post('/api/users', json={
                    'username': 'invalid-short', 'role': 'account_admin', 'password': password})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn('8', response.json()['detail'])
                self.assertEqual(security.load_users(), before)

    def test_nonroot_password_reset_uses_minimum_eight_for_both_roles_and_revokes(self):
        for role in ('account_admin', 'admin'):
            with self.subTest(role=role):
                self.signin('admin')
                changed_role = self.client.patch('/api/users/alice', json={'role': role})
                self.assertEqual(changed_role.status_code, 200, changed_role.text)
                previous = copy.deepcopy(security.load_users())
                previous_row = next(u for u in previous['users'] if u['username'] == 'alice')
                old_cookie = security.issue_token('alice', role)
                for password in ('seven12', '0739264', '智汇密码七个字', '\U0001f680' * 7):
                    response = self.client.patch('/api/users/alice', json={'password': password})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(security.load_users(), previous)
                password = '07392641' if role == 'account_admin' else 'Mix!密码八位'
                response = self.client.patch('/api/users/alice', json={'password': password})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()['sessions_revoked'])
                row = next(u for u in security.load_users()['users'] if u['username'] == 'alice')
                self.assertEqual(row['sv'], int(previous_row.get('sv', 0)) + 1)
                self.assertEqual(row['role'], role)
                self.client.cookies.set(config.COOKIE_NAME, old_cookie)
                self.assertEqual(self.client.get('/api/me').status_code, 401)
                self.client.cookies.clear()
                login = self.client.post('/api/login', json={'username': 'alice', 'password': password})
                self.assertEqual(login.status_code, 200, login.text)
                self.assertEqual(self.client.get('/api/me').json()['role'], role)

    def test_managed_password_helper_has_only_raw_minimum_length_constraint(self):
        for password in ('12345678', 'admin123', '        ', '１２３４５６７８',
                         '٠١٢٣٤٥٦٧', '\U0001f680' * 8, 'x' * 128, '1234567\n'):
            with self.subTest(password=password):
                self.assertIsNone(security.managed_user_password_problem(password))
        for password in (None, 12345678, [], {}, '', '1234567', '\U0001f680' * 7):
            with self.subTest(password=password):
                self.assertEqual(security.managed_user_password_problem(password), '用户登录密码至少 8 位')

    def test_root_reset_retains_strong_policy_and_existing_nonroot_login_is_unchanged(self):
        self.client.cookies.clear()
        legacy = self.client.post('/api/login', json={
            'username': 'alice', 'password': 'Synthetic-password!'})
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.signin('admin')
        before = copy.deepcopy(security.load_users())
        for password in ('6842097531', 'short'):
            response = self.client.patch('/api/users/admin', json={'password': password})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(security.load_users(), before)
        self.assertIsNotNone(security.password_problem('6842097531', 'admin'))
        self.assertIsNone(security.managed_user_password_problem('6842097531'))
        response = self.client.patch('/api/users/admin', json={'password': 'Fresh-root-password!'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['relogin_required'])
        self.assertEqual(self.client.get('/api/me').status_code, 401)
        self.client.cookies.clear()
        login = self.client.post('/api/login', json={'username': 'admin', 'password': 'Fresh-root-password!'})
        self.assertEqual(login.status_code, 200, login.text)

    def test_explicit_empty_password_rejects_whole_update_without_mutation(self):
        self.signin('admin')
        before = copy.deepcopy(security.load_users())
        for username, body in (('alice', {'password': ''}),
                                ('alice', {'password': '', 'role': 'admin'}),
                                ('admin', {'password': ''}),
                                ('admin', {'password': '', 'role': 'admin'})):
            with self.subTest(username=username, body=body):
                response = self.client.patch('/api/users/' + username, json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(security.load_users(), before)
                self.assertEqual(self.client.get('/api/me').status_code, 200)

    def test_null_or_omitted_password_preserves_optional_patch_semantics(self):
        self.signin('admin')
        before = copy.deepcopy(security.load_users())
        for body in ({}, {'password': None}, {'role': 'account_admin'},
                     {'password': None, 'role': 'account_admin'}):
            with self.subTest(body=body):
                response = self.client.patch('/api/users/alice', json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()['sessions_revoked'])
                self.assertEqual(security.load_users(), before)
        alice_before = next(u for u in before['users'] if u['username'] == 'alice')
        response = self.client.patch('/api/users/alice', json={'password': None, 'role': 'admin'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['sessions_revoked'])
        alice = next(u for u in security.load_users()['users'] if u['username'] == 'alice')
        self.assertEqual(alice['role'], 'admin')
        self.assertEqual(alice['pwd_hash'], alice_before['pwd_hash'])
        self.assertEqual(alice['sv'], int(alice_before.get('sv', 0)) + 1)

    def test_username_creation_cannot_poison_restart_or_create_unreachable_identity(self):
        self.signin('admin')
        before = security.load_users()
        for username in (' ', '\t', '\u3000', ' alice', 'alice ', 'ali ce',
                         'a/b', 'a\\b', 'a?b', 'a#b', 'a%b', 'a\x00b', '.', '..'):
            with self.subTest(username=repr(username)):
                response = self.client.post('/api/users', json={
                    'username': username, 'role': 'account_admin', 'password': '6842097531'})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(security.load_users(), before)
        response = self.client.post('/api/users', json={
            'username': 'synthetic.valid-用户', 'role': 'account_admin', 'password': '6842097531'})
        self.assertEqual(response.status_code, 200, response.text)
        security.migrate_user_roles()
        self.signin('synthetic.valid-用户')
        self.assertEqual(self.client.get('/api/me').status_code, 200)

    def test_role_boundary_is_deny_by_default_even_for_unlisted_gets(self):
        from starlette.requests import Request
        for path in ('/api/keys', '/api/logs', '/api/stats/summary', '/api/v1/accounts',
                     '/__qtg/api/accounts', '/api/auth/poll-extra', '/api/future-read-endpoint'):
            req = Request({'type': 'http', 'method': 'GET', 'path': path, 'headers': [],
                           'query_string': b'', 'scheme': 'http', 'server': ('testserver', 80)})
            with mock.patch.object(security, '_current_user', return_value={'username': 'alice', 'role': 'account_admin'}):
                with self.assertRaises(Exception) as error:
                    security.current_user(req)
            self.assertEqual(error.exception.status_code, 403)

    def test_parallel_revocations_cannot_restore_an_older_session_version(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            versions = list(pool.map(lambda _: security.revoke_sessions('alice'), range(16)))
        self.assertEqual(sorted(versions), list(range(1, 17)))
        ctx = multiprocessing.get_context('spawn')
        go = ctx.Event()
        ready = [ctx.Event(), ctx.Event()]
        workers = [ctx.Process(target=_revoke_worker, args=(str(config.USERS_FILE), signal, go)) for signal in ready]
        try:
            for worker in workers: worker.start()
            for signal in ready: self.assertTrue(signal.wait(20))
            go.set()
            for worker in workers:
                worker.join(25)
                self.assertEqual(worker.exitcode, 0)
        finally:
            for worker in workers:
                if worker.is_alive(): worker.terminate(); worker.join(5)
        alice = next(u for u in security.load_users()['users'] if u['username'] == 'alice')
        self.assertEqual(alice['sv'], 32)
