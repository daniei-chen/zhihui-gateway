"""Local release readiness: user recovery, isolation and complete request paging."""
from unittest import mock

from server import config, db, keysvc, portalstore as store, portalwallet as wallet, security
from server.tests import test_user_portal as fixtures


class PortalReleaseTests(fixtures.PortalTests):
    async def test_portal_credential_shape_is_rejected_by_signed_predecessor(self):
        import re
        from server import portalpolicy
        made = (await self.call('POST', '/api/portal/keys', json={'label': 'rollback proof'})).json()
        token = made['key']
        self.assertRegex(token, r'^sk-portal-[0-9a-f]{32}$')
        # Exact accepted shapes in the retained signed v0.0.3 resolver. Hash
        # matching cannot be reached with this new credential, even if enabled.
        self.assertFalse(re.fullmatch(r'sk-[0-9a-f]{32}', token))
        self.assertFalse(token.startswith(('wbk_', 'sk-qtg-')))
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}):
            self.assertEqual(keysvc.resolve(token)['source'], portalpolicy.SOURCE)
            db.execute("UPDATE api_keys SET source='manual' WHERE id=?", (made['id'],))
            self.assertIsNone(keysvc.resolve(token))
            db.execute('UPDATE api_keys SET source=? WHERE id=?', (portalpolicy.SOURCE, made['id']))
            self.assertIsNotNone(keysvc.resolve(token))

    async def test_admin_password_reset_revokes_sessions_keys_and_clears_lock_not_balance(self):
        await self.bind()
        made = (await self.call('POST', '/api/portal/keys', json={'label': 'synthetic reset key'})).json()
        old_cookie = self.headers()
        with store.transaction() as conn:
            conn.execute('UPDATE portal_users SET failed_logins=5,blocked_until=? WHERE id=?', (fixtures.NOW + 600, 'alice'))
        response = await self.call('PATCH', '/api/portal-admin/users/alice', headers=self.admin,
            json={'password': 'New-Synthetic-Password-2026'})
        self.assertEqual(response.status_code, 200, response.text)
        user = store.user('alice')
        self.assertEqual((user['failed_logins'], user['blocked_until']), (0, 0))
        self.assertEqual((await self.call('GET', '/api/portal/me', headers=old_cookie)).status_code, 401)
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}):
            self.assertIsNone(keysvc.resolve(made['key']))
        login = await self.call('POST', '/api/portal/login', headers={'origin': 'http://testserver'},
            json={'username': 'alice', 'password': 'New-Synthetic-Password-2026'})
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(store.overview('alice')['balance_units'], store.units(260))
        self.assertEqual(store.overview('alice')['account_count'], 1)
        self.assertNotIn('New-Synthetic', (config.DATA_DIR / 'user-portal.sqlite3').read_bytes().decode('ascii', 'ignore'))

    async def test_reset_rejects_username_password_and_non_superadmin(self):
        before = store.user('alice')
        for password in ('alice', 'alice12345678', '        ', 'aaaaaaaa'):
            response = await self.call('PATCH', '/api/portal-admin/users/alice', headers=self.admin, json={'password': password})
            self.assertEqual(response.status_code, 400, response.text)
        reader = {'cookie': f'{config.COOKIE_NAME}={security.issue_token("reader", "account_admin")}', 'origin': 'http://testserver'}
        for headers, expected in ((reader, 403), (self.headers(), 401), ({**self.admin, 'origin': 'https://example.invalid'}, 403)):
            response = await self.call('PATCH', '/api/portal-admin/users/alice', headers=headers, json={'password': 'Synthetic-Not-Applied'})
            self.assertEqual(response.status_code, expected, response.text)
        after = store.user('alice')
        self.assertEqual((before['sv'], before['pwd_hash']), (after['sv'], after['pwd_hash']))

    async def test_quarantined_copy_rejects_user_creation_reset_and_automatic_enable(self):
        with store.transaction() as conn:
            conn.execute("INSERT INTO portal_settings VALUES('restore_quarantine','true')")
        before = store.user('alice')
        operations = [('POST', '/api/portal-admin/users', {'username': 'new-user', 'password': 'Synthetic-Not-Created'}),
            ('PATCH', '/api/portal-admin/users/alice', {'password': 'Synthetic-Not-Applied'}),
            ('PUT', '/api/portal-admin/automatic-checkin', {'enabled': True})]
        for method, path, payload in operations:
            response = await self.call(method, path, headers=self.admin, json=payload)
            self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(store.user('alice')['sv'], before['sv'])
        self.assertFalse(store.automatic_enabled())
        with store.transaction() as conn:
            self.assertFalse(conn.execute('SELECT 1 FROM portal_users WHERE username=?', ('new-user',)).fetchone())

    async def test_request_paging_contains_only_current_owned_hash_and_preserves_unknown_fee(self):
        alice = (await self.call('POST', '/api/portal/keys', json={'label': 'synthetic alice'})).json()
        bob = (await self.call('POST', '/api/portal/keys', headers=self.headers('bob'), json={'label': 'synthetic bob'})).json()
        for index in range(65):
            db.execute('INSERT INTO request_logs(ts,key_id,model,status,credit) VALUES(?,?,?,?,?)',
                (fixtures.NOW + index, alice['id'], 'deepseek-v4-pro', 200, None if index == 64 else 1))
        db.execute('INSERT INTO request_logs(ts,key_id,model,status,credit) VALUES(?,?,?,?,?)',
            (fixtures.NOW + 1000, bob['id'], 'private-bob-model', 200, 999))
        pages = []
        for page, count in ((1, 30), (2, 30), (3, 5)):
            response = await self.call('GET', '/api/portal/requests?page=' + str(page))
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual((data['page'], data['total'], len(data['items'])), (page, 65, count))
            self.assertNotIn('private-bob', response.text)
            pages.extend(data['items'])
        self.assertEqual(len({row['ts'] for row in pages}), 65)
        self.assertIsNone(pages[0]['credit'])


for name in tuple(vars(fixtures.PortalTests)):
    if name.startswith('test_'):
        setattr(PortalReleaseTests, name, None)
del name
