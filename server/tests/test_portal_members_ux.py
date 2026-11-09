"""Portal credential errors describe the real policy without changing it."""
from unittest import mock

from server import security, portalstore as store
from server.tests import test_user_portal as fixtures


class PortalMembersUXTests(fixtures.PortalTests):
    async def test_create_reports_actual_password_reason_without_writing_user(self):
        cases = [('short', '用户登录密码至少 8 位'),
                 ('12345678', '密码过于常见，请换一个'),
                 ('aaaaaaaa', '密码过于常见，请换一个'),
                 ('alice1234', '密码不能是用户名加数字'),
                 ('x' * 129, '用户登录密码最多 128 位'),
                 (None, '用户登录密码必须是字符串')]
        for password, reason in cases:
            with self.subTest(reason=reason), mock.patch.object(security, 'make_hash',
                    side_effect=AssertionError('invalid password must not be hashed')):
                response = await self.call('POST', '/api/portal-admin/users', headers=self.admin,
                    json={'username': 'alice', 'password': password})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()['detail'], reason)
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_users').fetchone()[0], 2)

    async def test_eight_character_numeric_and_unicode_passwords_can_create_and_login(self):
        for index, password in enumerate(('07392641', 'D7x!p9Q2', '智汇测试用户登录', '\U0001f680' * 7 + 'X')):
            name = 'valid-user-' + str(index)
            response = await self.call('POST', '/api/portal-admin/users', headers=self.admin,
                json={'username': name, 'password': password})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn('password', response.json())
            login = await self.call('POST', '/api/portal/login', headers={'origin': 'http://testserver'},
                json={'username': name, 'password': password})
            self.assertEqual(login.status_code, 200, login.text)
            self.assertNotIn(name, [row['username'] for row in security.load_users()['users']])

    async def test_reset_reports_actual_reason_and_preserves_credentials_and_sessions(self):
        before = store.user('alice')
        for password, reason in ((None, '用户登录密码必须是字符串'),
                ('12345678', '密码过于常见，请换一个'), ('alice1234', '密码不能是用户名加数字'),
                ('x' * 129, '用户登录密码最多 128 位')):
            response = await self.call('PATCH', '/api/portal-admin/users/alice', headers=self.admin,
                json={'password': password})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()['detail'], reason)
            after = store.user('alice')
            self.assertEqual((before['pwd_hash'], before['sv']), (after['pwd_hash'], after['sv']))

    async def test_username_error_and_duplicate_remain_distinct_from_password_errors(self):
        for name, status, message in (('bad name', 400, '用户名需为3至40位字母、数字、下划线或短横线'),
                                      ('alice', 409, '用户名已存在')):
            response = await self.call('POST', '/api/portal-admin/users', headers=self.admin,
                json={'username': name, 'password': 'D7x!p9Q2'})
            self.assertEqual(response.status_code, status, response.text)
            self.assertEqual(response.json()['detail'], message)


for name in tuple(vars(fixtures.PortalTests)):
    if name.startswith('test_'):
        setattr(PortalMembersUXTests, name, None)
del name
