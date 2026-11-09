"""Public-pool activation separates authentication, discovery and paid admission."""
import json
from unittest import mock

import httpx

from server import config, db, keysvc, portalpolicy, portalservice, portalstore as store
from server.routers import gateway
from server.tests import test_user_portal as fixtures


class PortalActivationTests(fixtures.PortalTests):
    async def make_key(self):
        result = await self.call('POST', '/api/portal/keys', json={'label': 'synthetic discovery'})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    async def test_pool_disabled_does_not_make_owned_key_invalid(self):
        made = await self.make_key()
        key = keysvc.resolve(made['key'])
        self.assertIsNotNone(key)
        self.assertIsNone(keysvc.validate(key, '127.0.0.1', None, is_model_list=True))
        rejected = keysvc.validate(key, '127.0.0.1', portalpolicy.MODELS[0])
        self.assertEqual((rejected.status, rejected.code), (503, 'portal_pool_disabled'))

    async def test_model_discovery_is_real_filtered_metadata_not_a_paid_request(self):
        made = await self.make_key()
        self.app.include_router(gateway.router)
        calls = []

        def respond(request):
            calls.append(request)
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.path, '/v1/models')
            return httpx.Response(200, json={'object': 'list', 'data': [
                {'id': 'cn:' + portalpolicy.MODELS[0]}, {'id': portalpolicy.MODELS[1]},
                {'id': 'global:' + portalpolicy.MODELS[0]}, {'id': 'gpt-other'}]})

        def factory(*args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(respond))

        with mock.patch.object(config, 'http_client', side_effect=factory), \
                mock.patch.object(gateway, '_drop_models_outside_catalog', side_effect=lambda payload, key: payload), \
                mock.patch.object(gateway, 'RATE_MAX_PER_MIN', 0):
            response = await self.call('GET', '/v1/models', headers={'authorization': 'Bearer ' + made['key']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row['id'] for row in response.json()['data']], list(portalpolicy.MODELS[:2]))
        self.assertEqual(len(calls), 1)
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_spends').fetchone()[0], 0)
        self.assertFalse(store.overview('alice')['dispatch_enabled'])

    async def test_revoked_disabled_foreign_or_quarantined_identity_still_fails_authentication(self):
        made = await self.make_key()
        self.assertIsNone(keysvc.resolve(made['key'][:-1] + ('0' if made['key'][-1] != '0' else '1')))
        for sql in ("UPDATE portal_keys SET revoked=1", "UPDATE portal_users SET enabled=0 WHERE id='alice'",
                    "UPDATE portal_users SET sv=sv+1 WHERE id='alice'", "DELETE FROM portal_keys"):
            with store.transaction() as conn:
                conn.execute('SAVEPOINT synthetic')
                conn.execute(sql)
                conn.execute('RELEASE synthetic')
            self.assertIsNone(keysvc.resolve(made['key']))
            # Create a fresh isolated owner generation for the next case.
            with store.transaction() as conn:
                conn.execute("UPDATE portal_users SET enabled=1,sv=1 WHERE id='alice'")
                conn.execute('INSERT OR REPLACE INTO portal_keys VALUES(?,?,?,?,0)', (made['id'], 1, 'synthetic', fixtures.NOW))
        with store.transaction() as conn:
            conn.execute("INSERT INTO portal_settings VALUES('restore_quarantine','true')")
        self.assertIsNone(keysvc.resolve(made['key']))

    async def test_pool_switch_cannot_expand_platform_or_models_or_key_route(self):
        made = await self.make_key()
        key = keysvc.resolve(made['key'])
        self.assertIsNotNone(key)
        for platform in ('qoder', 'trae'):
            self.assertIsNotNone(keysvc.validate(key, '127.0.0.1', None, is_model_list=True, platform=platform))
        key['upstream_id'] = 99
        self.assertIsNotNone(keysvc.validate(key, '127.0.0.1', None, is_model_list=True))

    async def test_key_page_reports_disabled_dispatch_without_exposing_secrets(self):
        made = await self.make_key()
        response = await self.call('GET', '/api/portal/keys')
        self.assertEqual(response.status_code, 200)
        value = response.json()
        self.assertFalse(value['dispatch_enabled'])
        self.assertFalse(value['pool_enabled'])
        self.assertTrue(value['model_discovery_enabled'])
        self.assertIsInstance(value['dispatch_note'], str)
        self.assertNotIn(made['key'], response.text)

    async def test_key_self_service_reports_wallet_not_unlimited_credit(self):
        made = await self.make_key()
        await self.bind(total=260)
        self.app.include_router(gateway.router)
        response = await self.call('GET', '/v1/key', headers={'authorization': 'Bearer ' + made['key']})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertFalse(value['credits']['unlimited'])
        self.assertTrue(value['credits']['shared_wallet'])
        self.assertEqual(value['credits']['remaining'], 0)
        self.assertEqual(value['portal']['pending_pool_units'], 260 * store.SCALE)
        self.assertFalse(value['portal']['dispatch_enabled'])
        self.assertNotIn(made['key'], response.text)

    async def test_model_listing_rechecks_revocation_after_upstream_await(self):
        made = await self.make_key()
        self.app.include_router(gateway.router)

        def respond(request):
            with store.transaction() as conn:
                conn.execute('UPDATE portal_keys SET revoked=1 WHERE key_id=?', (made['id'],))
            return httpx.Response(200, json={'object': 'list', 'data': [{'id': portalpolicy.MODELS[0]}]})

        def factory(*args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(respond))

        with mock.patch.object(config, 'http_client', side_effect=factory), \
                mock.patch.object(gateway, '_drop_models_outside_catalog', side_effect=lambda payload, key: payload), \
                mock.patch.object(gateway, 'RATE_MAX_PER_MIN', 0):
            response = await self.call('GET', '/v1/models', headers={'authorization': 'Bearer ' + made['key']})
        self.assertEqual(response.status_code, 401, response.text)

    async def test_upstream_catalog_errors_are_not_disguised_as_empty_models(self):
        made = await self.make_key()
        self.app.include_router(gateway.router)
        headers = {'authorization': 'Bearer ' + made['key']}
        for status, value in ((401, {'error': {'message': 'synthetic upstream secret'}}),
                              (200, {'error': {'message': 'synthetic upstream secret'}}),
                              (200, {'object': 'list', 'data': None})):
            def factory(*args, **kwargs):
                return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=value)))
            with mock.patch.object(config, 'http_client', side_effect=factory), mock.patch.object(gateway, 'RATE_MAX_PER_MIN', 0):
                response = await self.call('GET', '/v1/models', headers=headers)
            self.assertEqual(response.status_code, 502, response.text)
            self.assertEqual(response.json()['error']['code'], 'portal_model_catalog_unavailable')
            self.assertNotIn('synthetic upstream secret', response.text)

    async def test_pool_publication_rejects_replaced_local_identity_or_expired_credentials(self):
        out, result = await self.bind()
        with store.transaction() as conn:
            account = dict(store.require_account(conn, out['id']))
        for changes in ({'uid': 'synthetic-other'}, {'realm': 'global'}, {'enterprise_id': 'synthetic-company'},
                        {'access_token': ''}, {'expires_at': fixtures.NOW - 1}):
            portalservice.save_credentials(account, {**result, **changes})
            with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}), \
                    mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}):
                with self.assertRaises(ValueError):
                    portalservice.promote(account['id'])
            self.assertFalse(list(config.AUTH_DIR.glob('*.json')))
        self.assertEqual(store.overview('alice')['balance_units'], 260 * store.SCALE)

    async def test_pool_credential_link_cannot_escape_auth_directory(self):
        out, _ = await self.bind()
        private = config.AUTH_DIR.parent / 'synthetic-private.json'
        private.write_text(json.dumps({'account': {'uid': 'synthetic-uid'}, 'auth': {'realm': 'cn', 'accessToken': 'synthetic-private'}}))
        with store.transaction() as conn:
            conn.execute('INSERT INTO portal_pool_links VALUES(?,?,?)', (out['id'], '../synthetic-private.json', fixtures.NOW))
        with self.assertRaises(ValueError):
            portalservice.load_credentials(out['id'])

    async def test_confirmed_publication_is_idempotent_without_duplicating_initial_credit(self):
        first, _ = await self.bind(uid='synthetic-first')
        second, _ = await self.bind(uid='synthetic-second')
        before = store.overview('alice')['balance_units']
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}), \
                mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}):
            self.assertEqual(portalservice.publish_confirmed_accounts(), {'published': 2, 'pending': 0})
            original = {p.name: p.read_bytes() for p in config.AUTH_DIR.glob('*.json')}
            self.assertEqual(portalservice.publish_confirmed_accounts(), {'published': 0, 'pending': 0})
        self.assertEqual(original, {p.name: p.read_bytes() for p in config.AUTH_DIR.glob('*.json')})
        self.assertEqual(store.overview('alice')['balance_units'], before)
        with store.transaction() as conn:
            self.assertEqual({row[0] for row in conn.execute('SELECT account_id FROM portal_pool_links')}, {first['id'], second['id']})

    async def test_inactive_account_cannot_be_published_or_recovered(self):
        out, _ = await self.bind()
        with store.transaction() as conn:
            conn.execute("UPDATE portal_accounts SET status='disabled' WHERE id=?", (out['id'],))
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}), \
                mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}):
            with self.assertRaises(ValueError):
                portalservice.promote(out['id'])
        self.assertFalse(list(config.AUTH_DIR.glob('*.json')))

    async def test_publication_rechecks_quarantine_and_account_state_after_intent_commit(self):
        from contextlib import contextmanager
        for change in ('quarantine', 'disable', 'unconfirm'):
            with self.subTest(change=change):
                with store.transaction() as conn:
                    conn.execute("DELETE FROM portal_settings WHERE key='restore_quarantine'")
                out, _ = await self.bind(uid='synthetic-race-' + change)
                original = store.transaction
                calls = 0

                @contextmanager
                def intervened():
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        with original() as conn:
                            if change == 'quarantine':
                                conn.execute("INSERT OR REPLACE INTO portal_settings VALUES('restore_quarantine','true')")
                            else:
                                field, value = ('status', 'disabled') if change == 'disable' else ('initial_state', 'pending')
                                conn.execute(f'UPDATE portal_accounts SET {field}=? WHERE id=?', (value, out['id']))
                    with original() as conn:
                        yield conn

                with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}), \
                        mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}), \
                        mock.patch.object(store, 'transaction', side_effect=intervened):
                    with self.assertRaises(ValueError):
                        portalservice.promote(out['id'])
                self.assertFalse(list(config.AUTH_DIR.glob('*.json')))
                with original() as conn:
                    conn.execute("DELETE FROM portal_settings WHERE key='restore_quarantine'")

    async def test_engine_checkin_cannot_be_reenabled_while_customer_accounts_are_pooled(self):
        from server.services import wb2api
        out, _ = await self.bind()
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}), \
                mock.patch.object(wb2api, 'load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}):
            portalservice.promote(out['id'])
        path = self.root / 'synthetic-upstream.json'
        original = json.dumps({'api_key': 'synthetic-config-secret', 'schedule': {'checkin_enabled': False}})
        path.write_text(original)
        with mock.patch.object(config, 'UPSTREAM_CONFIG', path):
            with self.assertRaises(ValueError):
                wb2api.save_upstream_config({'schedule': {'checkin_enabled': True}})
        self.assertEqual(path.read_text(), original)

    async def test_corrupt_publication_intent_cannot_write_outside_pool_or_recover_foreign_realm(self):
        out, credentials = await self.bind()
        with store.transaction() as conn:
            conn.execute('INSERT INTO portal_pool_intents VALUES(?,?,?,?)',
                (out['id'], '../synthetic-outside.json', 'synthetic-marker', fixtures.NOW))
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}), \
                mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}):
            with self.assertRaises(ValueError):
                portalservice.promote(out['id'])
        self.assertFalse((config.AUTH_DIR.parent / 'synthetic-outside.json').exists())

    async def test_missing_or_changed_published_file_does_not_report_success_or_overwrite(self):
        out, _ = await self.bind()
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}), \
                mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}):
            portalservice.promote(out['id'])
            path = next(config.AUTH_DIR.glob('*.json'))
            raw = json.loads(path.read_text())
            raw['auth']['realm'] = 'global'
            changed = json.dumps(raw)
            path.write_text(changed)
            with self.assertRaises(ValueError):
                portalservice.promote(out['id'])
        self.assertEqual(path.read_text(), changed)

    async def test_background_publication_recovers_without_enabling_or_repeating_checkin(self):
        await self.bind()
        before = store.overview('alice')['balance_units']
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}), \
                mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}), \
                mock.patch.object(portalservice, 'checkin', side_effect=AssertionError('automatic checkin stays off')):
            await portalservice.automatic_once()
            await portalservice.automatic_once()
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_pool_links').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_checkins').fetchone()[0], 0)
        self.assertFalse(store.automatic_enabled())
        self.assertEqual(store.overview('alice')['balance_units'], before)

    async def test_publication_recovery_failure_does_not_skip_wallet_recovery(self):
        from server import portalwallet
        with mock.patch.object(portalservice, 'publish_confirmed_accounts', side_effect=OSError('synthetic-unavailable')), \
                mock.patch.object(portalwallet, 'recover') as recovered:
            self.assertEqual(await portalservice.automatic_once(), 0)
        recovered.assert_called_once_with()

    async def test_background_cancellation_waits_for_owned_publication_worker(self):
        import asyncio
        import threading
        entered, release = threading.Event(), threading.Event()

        def publish():
            entered.set()
            release.wait(5)
            return {'published': 0, 'pending': 0}

        with mock.patch.object(portalservice, 'publish_confirmed_accounts', side_effect=publish):
            task = asyncio.create_task(portalservice.automatic_once())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                task.cancel()
                await asyncio.sleep(.05)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task


for name in tuple(vars(fixtures.PortalTests)):
    if name.startswith('test_'):
        setattr(PortalActivationTests, name, None)
del name
