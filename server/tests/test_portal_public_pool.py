"""Offline public-pool customer policy and wallet regressions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time
from unittest import mock

from server import budget, budgetowner, db, keysvc, observability
from server import portalpolicy as policy, portalstore as store, portalwallet as wallet
from server.routers import gateway
from server.tests import test_user_portal as fixtures

NOW = fixtures.NOW


class PublicPoolTests(fixtures.PortalTests):
    # This class reuses the isolated setup without duplicating the base tests.
    def setUp(self):
        super().setUp()
        self.addCleanup(budgetowner.close)
        # Accounting fixtures bypass only operator approval. Real policy and
        # gateway admission are tested separately without this mock.
        self.stack.enter_context(mock.patch('server.portalbilling.require_approved_policy'))

    async def make_key(self, who='alice'):
        response = await self.call('POST', '/api/portal/keys', headers=self.headers(who), json={'label': 'synthetic key'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def make_legacy_key(self, who='alice'):
        """Preserve multi-key wallet tests as pre-upgrade data, not new issuance."""
        with db.transaction() as conn:
            made=keysvc.create_key('synthetic legacy',models=list(policy.MODELS),realm='cn',platforms=['workbuddy'],_conn=conn,_portal=True)
            conn.execute('UPDATE api_keys SET source=? WHERE id=?',(policy.SOURCE,made['id']))
        with store.transaction() as conn:
            owner=conn.execute('SELECT sv FROM portal_users WHERE id=?',(who,)).fetchone()
            conn.execute('INSERT INTO portal_key_links VALUES(?,?,?)',(made['id'],who,keysvc._hash(made['key'])))
            conn.execute('INSERT INTO portal_keys VALUES(?,?,?,?,0)',(made['id'],owner['sv'],'synthetic legacy',NOW))
        return made

    def enabled(self):
        return mock.patch.dict(os.environ, {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'})

    async def pooled(self, who='alice', uid='pool-alice', amount=100, expiry=None):
        out, _ = await self.bind(store.user(who), uid, total=amount, expiry=expiry)
        with self.enabled(), mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}):
            self.assertTrue(__import__('server.portalservice', fromlist=['promote']).promote(out['id']))
        return out

    def state(self, rid, key):
        state = observability.RequestAudit(rid, '/v1/chat/completions', 'POST', 'openai', time.monotonic())
        state.key_id = key['id']
        return state

    async def test_publication_no_overwrite_and_one_credential_authority(self):
        from server import config, portalservice as service
        out = await self.pooled()
        path = config.AUTH_DIR / 'workbuddy-pool-alice.json'
        raw = json.loads(path.read_text())
        raw['auth']['accessToken'] = 'synthetic-engine-renewed'
        path.write_text(json.dumps(raw))
        self.assertEqual(service.load_credentials(out['id'])['access_token'], 'synthetic-engine-renewed')
        with self.enabled(), mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}):
            service.promote(out['id'])
        self.assertEqual(json.loads(path.read_text())['auth']['accessToken'], 'synthetic-engine-renewed')

    async def test_publication_rejects_existing_file_without_changing_it(self):
        from server import config, portalservice as service
        out, _ = await self.bind(uid='collision')
        config.AUTH_DIR.mkdir()
        path = config.AUTH_DIR / 'workbuddy-collision.json'
        original = json.dumps({'account': {'uid': 'collision'}, 'auth': {'accessToken': 'synthetic-admin-owned'}})
        path.write_text(original)
        with self.enabled(), mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}):
            with self.assertRaises(ValueError):
                service.promote(out['id'])
        self.assertEqual(path.read_text(), original)
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_pool_links').fetchone()[0], 0)

    async def test_engine_independent_checkin_must_be_explicitly_off(self):
        from server import portalservice as service
        out, _ = await self.bind()
        for schedule in ({}, {'checkin_enabled': True}, {'checkin_enabled': 0}):
            with self.enabled(), mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': schedule}):
                with self.assertRaises(ValueError):
                    service.promote(out['id'])

    async def test_original_manager_checkin_uses_portal_claim_no_double_credit(self):
        import httpx
        from server import portalservice as service
        from server.services import tencent
        out, result = await self.bind(total=0)
        def client(*args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'code': 0, 'data': {}})))
        with mock.patch.object(tencent, '_account_http_client', side_effect=client) as http, \
                mock.patch.object(tencent, 'fetch_credits', side_effect=[(True, 0, 'ok', []), (True, 100, 'ok', [{'at': NOW + 30 * 86400, 'amount': 100}])]):
            first = await tencent.checkin(result, 'cn')
            self.assertEqual(first[0], 0)
            second = await service.checkin(out['id'], 'user', 'alice')
            self.assertTrue(second['already'])
            self.assertEqual(http.call_count, 1)
        self.assertEqual(store.overview('alice')['today_checkin_units'], 100 * store.SCALE)

    async def test_existing_uid_under_alternate_filename_cannot_mint_initial(self):
        from server import config
        config.AUTH_DIR.mkdir()
        (config.AUTH_DIR / 'workbuddy_old.json').write_text(json.dumps({'account': {'uid': 'synthetic-uid'}, 'auth': {}}))
        with self.assertRaises(ValueError):
            await self.bind()
        self.assertEqual(store.overview('alice')['balance_units'], 0)

    def test_startup_contract_accepts_only_explicit_candidate_switch(self):
        from server.configcontract import validate_environment, ConfigurationError
        for value in ('0', '1'):
            validate_environment({'WB_PORTAL_PUBLIC_POOL_ENABLED': value})
        with self.assertRaises(ConfigurationError):
            validate_environment({'WB_PORTAL_PUBLIC_POOL_ENABLED': 'yes'})

    async def test_key_limit_rejects_before_leaving_disabled_orphan(self):
        first=await self.make_key()
        before = db.query_one('SELECT COUNT(*) FROM api_keys')[0]
        for _ in range(2):
            response = await self.call('POST', '/api/portal/keys', json={'label': 'limit'})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['existing'])
            self.assertEqual(response.json()['id'],first['id'])
            self.assertIsNone(response.json()['key'])
        self.assertEqual(db.query_one('SELECT COUNT(*) FROM api_keys')[0], before)

    async def test_publication_after_sql_failure_recovers_own_file_without_overwrite(self):
        from contextlib import contextmanager
        from server import config, portalservice as service
        out, _ = await self.bind()
        original = store.transaction
        @contextmanager
        def fail_pool_link():
            with original() as conn:
                yield conn
                if conn.execute('SELECT 1 FROM portal_pool_links WHERE account_id=?', (out['id'],)).fetchone():
                    raise OSError('synthetic SQL commit failure after publication')
        with self.enabled(), mock.patch('server.services.wb2api.load_upstream_config', return_value={'schedule': {'checkin_enabled': False}}):
            with mock.patch.object(store, 'transaction', fail_pool_link), self.assertRaises(OSError):
                service.promote(out['id'])
            path = config.AUTH_DIR / 'workbuddy-synthetic-uid.json'
            before = path.read_bytes()
            with original() as conn:
                self.assertIsNone(conn.execute('SELECT 1 FROM portal_pool_links').fetchone())
            self.assertTrue(service.promote(out['id']))
            self.assertEqual(path.read_bytes(), before)

    async def test_gateway_failed_main_commit_spools_then_replays_wallet_once(self):
        from server import ledgerreplay, ledger, operational
        await self.pooled(); made = await self.make_key(); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key']); state = self.state('portal-spooled', key)
            token = observability.current.set(state)
            try:
                budget.prepare(key, {'model': policy.MODELS[0], 'max_tokens': 8}); budget.forwarded()
                with mock.patch.object(ledger, 'record', side_effect=OSError('synthetic SQL failure')):
                    gateway._record(key, '127.0.0.1', policy.MODELS[0], policy.MODELS[0], 200, 3, 2, 10, None, None, False,
                        usage={'prompt_tokens': 3, 'completion_tokens': 2, 'credit': 2})
                budget.finish(state)
                self.assertIsNone(db.query_one('SELECT * FROM portal_spend_receipts WHERE request_id=?', (state.request_id,)))
                self.assertEqual(store.overview('alice')['held_units'], 100 * store.SCALE)
                report = ledgerreplay.replay_batch()
                self.assertEqual(report['applied'], 1)
                wallet.recover(); wallet.recover()
                self.assertEqual(store.overview('alice')['balance_units'], 98 * store.SCALE)
                self.assertEqual(sum(e['kind'] == 'usage' for e in store.overview('alice')['events']), 1)
            finally:
                operational.ledger_write_healthy = True
                observability.current.reset(token)

    async def test_manual_unknown_resolution_requires_dead_worker_and_evidence(self):
        await self.pooled(); made = await self.make_key(); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key']); state = self.state('manual-resolution', key)
            token = observability.current.set(state)
            try:
                wallet.reserve(key); wallet.forwarded(state)
                with store.transaction() as conn:
                    conn.execute("UPDATE portal_spends SET state='unknown' WHERE request_id=?", (state.request_id,))
                with mock.patch.object(budgetowner, 'alive', return_value=True), self.assertRaises(ValueError):
                    wallet.resolve_unknown(state.request_id, 2, 'admin', 'synthetic verified reason', 'synthetic provider receipt')
                with mock.patch.object(budgetowner, 'alive', return_value=False):
                    wallet.resolve_unknown(state.request_id, 2, 'admin', 'synthetic verified reason', 'synthetic provider receipt')
                    with self.assertRaises(ValueError):
                        wallet.resolve_unknown(state.request_id, 2, 'admin', 'synthetic verified reason', 'synthetic provider receipt')
                self.assertEqual(store.overview('alice')['balance_units'], 98 * store.SCALE)
            finally:
                observability.current.reset(token)

    async def test_keys_plaintext_once_ownership_revoke_and_no_ip_restriction(self):
        made = await self.make_key()
        discovered = keysvc.resolve(made['key'])
        self.assertIsNotNone(discovered)
        self.assertIsNone(keysvc.validate(discovered, '127.0.0.1', None, is_model_list=True))
        self.assertEqual(keysvc.validate(discovered, '127.0.0.1', policy.MODELS[0]).code, 'portal_pool_disabled')
        with self.enabled():
            key = keysvc.resolve(made['key'])
            self.assertEqual(key['max_ips'], 0)
            self.assertEqual(key['ip_allowlist'], [])
            self.assertEqual(key['upstream_id'], None)
            listing = await self.call('GET', '/api/portal/keys')
            self.assertNotIn(made['key'], listing.text)
            self.assertNotIn('key_hash', listing.text)
            denied = await self.call('DELETE', f'/api/portal/keys/{made["id"]}', headers=self.headers('bob'))
            self.assertEqual(denied.status_code, 404)
            response = await self.call('DELETE', f'/api/portal/keys/{made["id"]}')
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(keysvc.resolve(made['key']))

    async def test_non_deepseek_alias_global_platform_and_route_are_rejected(self):
        made = await self.make_key()
        with self.enabled():
            key = keysvc.resolve(made['key'])
            model = policy.MODELS[0]
            self.assertIsNone(keysvc.validate(key, '203.0.113.2', model, mapped_model=model))
            for name, target, platform in [('gpt-5', 'gpt-5', 'workbuddy'), ('friendly-alias', model, 'workbuddy'),
                    (model, 'gpt-5', 'workbuddy'), (model, 'global:' + model, 'workbuddy'),
                    ('global:' + model, model, 'workbuddy'), (model, model, 'qoder'), (model, model, 'trae')]:
                self.assertIsNotNone(keysvc.validate(key, '203.0.113.2', name, mapped_model=target, platform=platform))
            keysvc.update_key(key['id'], {'models': [], 'realm': '', 'platforms': ['workbuddy', 'qoder']})
            fresh = keysvc.resolve(made['key'])
            self.assertIsNotNone(keysvc.validate(fresh, '203.0.113.2', model, mapped_model='gpt-5'))
            with mock.patch.object(gateway, '_map_model', side_effect=lambda m: 'gpt-5' if m == model else m):
                listing = gateway._scope_models({'data': [{'id': name} for name in [model, policy.MODELS[1], 'gpt-5', 'global:' + model]]}, fresh)
            self.assertEqual([item['id'] for item in listing['data']], [policy.MODELS[1]])

    async def test_orphan_key_fails_closed_even_if_manager_enabled_it(self):
        made = await self.make_key()
        with store.transaction() as conn:
            conn.execute('DELETE FROM portal_keys WHERE key_id=?', (made['id'],))
        with self.enabled():
            self.assertIsNone(keysvc.resolve(made['key']))

    async def test_wallet_two_keys_share_one_lock_and_earliest_expiry(self):
        out = await self.pooled(amount=10, expiry=NOW + 100)
        with store.transaction() as conn:
            account = store.require_account(conn, out['id'])
            store._grant(conn, account, 'checkin', 'later', 20 * store.SCALE, NOW, NOW + 200)
        first, second = await self.make_key(), await self.make_legacy_key()
        db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(first['key'])
            state = self.state('wallet-first', key)
            context = observability.current.set(state)
            try:
                wallet.reserve(key)
                self.assertEqual(store.overview('alice')['balance_units'], 0)
                self.assertEqual(store.overview('alice')['held_units'], 30 * store.SCALE)
                another = self.state('wallet-other', key)
                token = observability.current.set(another)
                try:
                    with self.assertRaises(budget.BudgetRejected):
                        wallet.reserve(keysvc.resolve(second['key']))
                finally:
                    observability.current.reset(token)
                wallet.forwarded(state)
                state.terminal_state = 'completed'
                with db.transaction():
                    wallet.receipt(state, {'prompt_tokens': 2, 'completion_tokens': 3}, 12)
                wallet.finish(state); wallet.finish(state)
                view = store.overview('alice')
                self.assertEqual(view['balance_units'], 18 * store.SCALE)
                self.assertEqual(view['held_units'], 0)
                self.assertEqual(sum(e['kind'] == 'usage' for e in view['events']), 1)
                self.assertEqual(view['grants'][0]['expires_at'], NOW + 200)
            finally:
                observability.current.reset(context)

    async def test_unknown_missing_credit_incomplete_cancel_never_auto_frees(self):
        await self.pooled(amount=50)
        made = await self.make_key()
        db.execute(wallet.RECEIPTS)
        for terminal, credit, usage in [('completed', None, {'prompt_tokens': 1, 'completion_tokens': 1}),
                                      ('client_cancelled', 1, {'prompt_tokens': 1, 'completion_tokens': 1}),
                                      ('completed', float('inf'), {'prompt_tokens': 1, 'completion_tokens': 1}),
                                      ('protocol_incomplete', 1, {'prompt_tokens': 1, 'completion_tokens': 1})]:
            with self.enabled(), store.transaction() as conn:
                conn.execute('DELETE FROM portal_request_ends')
                conn.execute('DELETE FROM portal_allocations'); conn.execute('DELETE FROM portal_spend_policies'); conn.execute('DELETE FROM portal_spends')
                conn.execute('UPDATE portal_grants SET remaining_units=amount_units')
            db.execute('DELETE FROM portal_spend_receipts')
            with self.enabled():
                key = keysvc.resolve(made['key']); state = self.state('unknown', key)
                token = observability.current.set(state)
                try:
                    wallet.reserve(key); wallet.forwarded(state); state.terminal_state = terminal
                    with db.transaction():
                        wallet.receipt(state, usage, credit)
                    wallet.finish(state)
                    with mock.patch.object(store, 'now', return_value=NOW + 40 * 86400):
                        wallet.finish(state)
                        self.assertEqual(store.overview('alice')['held_units'], 50 * store.SCALE)
                        with self.assertRaises(budget.BudgetRejected):
                            wallet.reserve(key)
                finally:
                    observability.current.reset(token)

    async def test_unsent_release_expiry_and_main_db_rollback(self):
        await self.pooled(amount=40, expiry=NOW + 100)
        made = await self.make_key(); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key']); state = self.state('unsent', key)
            token = observability.current.set(state)
            try:
                wallet.reserve(key)
                wallet.finish(state)
                self.assertEqual(store.overview('alice')['balance_units'], 40 * store.SCALE)
                state = self.state('rolled-back', key); observability.current.set(state)
                wallet.reserve(key); wallet.forwarded(state); state.terminal_state = 'completed'
                with self.assertRaises(RuntimeError):
                    with db.transaction():
                        wallet.receipt(state, {'prompt_tokens': 1, 'completion_tokens': 1}, 3)
                        raise RuntimeError('synthetic main commit failure')
                wallet.finish(state)
                self.assertEqual(store.overview('alice')['held_units'], 40 * store.SCALE)
            finally:
                observability.current.reset(token)

    async def test_approved_overrun_caps_charge_blocks_empty_wallet_and_does_not_touch_contributor(self):
        await self.pooled('alice', 'supplier-alice', 10)
        await self.pooled('bob', 'supplier-bob', 5)
        made = await self.make_key('bob'); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key']); state = self.state('bob-uses-shared-pool', key)
            token = observability.current.set(state)
            try:
                wallet.reserve(key); wallet.forwarded(state); state.terminal_state = 'completed'
                with db.transaction():
                    wallet.receipt(state, {'prompt_tokens': 1, 'completion_tokens': 1}, 7)
                wallet.finish(state)
                self.assertEqual(store.overview('alice')['balance_units'], 10 * store.SCALE)
                self.assertEqual(store.overview('bob')['debt_units'], 0)
                self.assertEqual(wallet.platform_costs_page()['platform_units'], 2 * store.SCALE)
                with self.assertRaises(budget.BudgetRejected):
                    wallet.reserve(key)
            finally:
                observability.current.reset(token)

    async def test_disable_or_revoke_during_prepare_stops_send(self):
        await self.pooled(); made = await self.make_key(); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key']); state = self.state('revoked-before-send', key)
            token = observability.current.set(state)
            try:
                wallet.reserve(key)
                with store.transaction() as conn:
                    conn.execute('UPDATE portal_users SET sv=sv+1 WHERE id=?', ('alice',))
                with self.assertRaises(budget.BudgetRejected):
                    wallet.forwarded(state)
                wallet.finish(state)
                self.assertEqual(store.overview('alice')['balance_units'], 100 * store.SCALE)
            finally:
                observability.current.reset(token)

    async def test_dead_worker_only_releases_demonstrably_unsent(self):
        await self.pooled(); made = await self.make_key(); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key']); state = self.state('dead-unsent', key)
            token = observability.current.set(state)
            try:
                wallet.reserve(key)
                with mock.patch.object(budgetowner, 'alive', return_value=False):
                    wallet.recover()
                self.assertEqual(store.overview('alice')['balance_units'], 100 * store.SCALE)
                state = self.state('dead-sent', key); observability.current.set(state)
                wallet.reserve(key); wallet.forwarded(state)
                with mock.patch.object(budgetowner, 'alive', return_value=False):
                    wallet.recover()
                self.assertEqual(store.overview('alice')['held_units'], 100 * store.SCALE)
            finally:
                observability.current.reset(token)

    async def test_concurrent_keys_cannot_reserve_twice(self):
        await self.pooled(); first, second = await self.make_key(), await self.make_legacy_key()
        with self.enabled():
            keys = [keysvc.resolve(value['key']) for value in (first, second)]
            def attempt(index):
                token = observability.current.set(self.state('concurrent-' + str(index), keys[index]))
                try:
                    wallet.reserve(keys[index]); return True
                except budget.BudgetRejected:
                    return False
                finally:
                    observability.current.reset(token)
            with ThreadPoolExecutor(max_workers=2) as workers:
                self.assertEqual(sum(workers.map(attempt, (0, 1))), 1)
            self.assertEqual(store.overview('alice')['held_units'], 100 * store.SCALE)

    async def test_settlement_returns_only_unexpired_leftovers(self):
        await self.pooled(amount=100, expiry=NOW + 10)
        made = await self.make_key(); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key']); state = self.state('expiry-during-stream', key)
            token = observability.current.set(state)
            try:
                wallet.reserve(key); wallet.forwarded(state); state.terminal_state = 'completed'
                with db.transaction():
                    wallet.receipt(state, {'prompt_tokens': 1, 'completion_tokens': 1}, 30)
                with mock.patch.object(store, 'now', return_value=NOW + 11):
                    wallet.finish(state)
                    self.assertEqual(store.overview('alice')['balance_units'], 0)
                    self.assertEqual(store.overview('alice')['held_units'], 0)
                    self.assertEqual(next(e['amount_units'] for e in store.overview('alice')['events'] if e['kind'] == 'expired'), -70 * store.SCALE)
            finally:
                observability.current.reset(token)

    async def test_committed_outbox_recovers_after_wallet_write_failure(self):
        await self.pooled(); made = await self.make_key(); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key']); state = self.state('wallet-write-failed', key)
            token = observability.current.set(state)
            try:
                wallet.reserve(key); wallet.forwarded(state); state.terminal_state = 'completed'
                with db.transaction():
                    wallet.receipt(state, {'prompt_tokens': 1, 'completion_tokens': 1}, 2)
                with mock.patch.object(wallet, 'apply', side_effect=OSError('synthetic disk failure')):
                    wallet.finish(state)
                self.assertEqual(store.overview('alice')['held_units'], 100 * store.SCALE)
                wallet.recover(); wallet.recover()
                self.assertEqual(store.overview('alice')['balance_units'], 98 * store.SCALE)
            finally:
                observability.current.reset(token)

    async def test_gateway_all_protocols_shared_route_actual_wallet_and_sse(self):
        import httpx
        from fastapi import FastAPI
        from server import config, platformpolicy
        from server.routers import anthropic, responses
        from server.tests.test_audit_20261029_gateway_ledger import GOOD, _Stream
        await self.pooled('alice', 'contributor-alice', 10)
        await self.pooled('bob', 'contributor-bob', 10)
        made = await self.make_key('bob')
        app = FastAPI()
        for router in (gateway.router, anthropic.router, responses.router):
            app.include_router(router)
        app.add_middleware(observability.RequestAuditMiddleware)
        app.add_middleware(platformpolicy.PlatformRoutingMiddleware)
        transport = httpx.ASGITransport(app=app)
        calls = []
        def respond(request):
            calls.append(request)
            body = json.loads(request.content)
            self.assertEqual(body['model'], policy.MODELS[0])
            if body.get('stream'):
                chunks = [b'data: {"id":"fixture","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n',
                    b'data: {"id":"fixture","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
                    b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"credit":0.01}}\n\n', b'data: [DONE]\n\n']
                return httpx.Response(200, headers={'content-type': 'text/event-stream', 'x-wb-account-uid': 'contributor-alice'}, stream=_Stream(chunks))
            return httpx.Response(200, json=GOOD, headers={'x-wb-account-uid': 'contributor-alice'})
        def factory(*args, **kw):
            return httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with self.enabled(), mock.patch.object(config, 'http_client', side_effect=factory), \
                mock.patch.object(gateway, 'RATE_MAX_PER_MIN', 0), \
                mock.patch('server.services.wb2api.load_upstream_config', return_value={}):
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
                for stream in (False, True):
                    for protocol in ('chat/completions', 'messages', 'responses'):
                        payload = {'model': policy.MODELS[0], 'stream': stream}
                        payload.update({'input': 'hi', 'max_output_tokens': 8} if protocol == 'responses'
                            else {'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 8})
                        result = await client.post('/v1/' + protocol, json=payload,
                            headers={'Authorization': 'Bearer ' + made['key'], 'anthropic-version': '2023-06-01'})
                        self.assertEqual(result.status_code, 200, result.text)
                for model, header in [('gpt-5', {}), (policy.MODELS[0], {'x-qtg-platform': 'qoder'})]:
                    denied = await client.post('/v1/chat/completions', json={'model': model, 'messages': []},
                        headers={'Authorization': 'Bearer ' + made['key'], **header})
                    self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(len(calls), 6)
        self.assertEqual(len({str(req.url) for req in calls}), 1)
        self.assertEqual(store.overview('alice')['balance_units'], 10 * store.SCALE)
        self.assertEqual(store.overview('bob')['balance_units'], store.units('9.94'))
        self.assertEqual(store.overview('bob')['held_units'], 0)
        logs = await self.call('GET', '/api/portal/requests', headers=self.headers('bob'))
        self.assertGreaterEqual(logs.json()['total'], 6)
        self.assertEqual((await self.call('GET', '/api/portal/requests')).json()['total'], 0)


# Reuse only helpers/setup; unittest otherwise also inherits every first-portal test.
for _name in tuple(vars(fixtures.PortalTests)):
    if _name.startswith('test_') and _name not in vars(PublicPoolTests):
        setattr(PublicPoolTests, _name, None)
