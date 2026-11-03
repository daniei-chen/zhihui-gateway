"""Approved shared-pool calls charge the caller, never a contributor or future grants."""
import json
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from server import budget, db, keysvc, observability, portalbilling, portalstore as store, portalwallet as wallet
from server.tests.test_portal_public_pool import PublicPoolTests
from server.tests.test_user_portal import PortalTests


class CappedBillingTests(PortalTests):
    pooled = PublicPoolTests.pooled
    make_key = PublicPoolTests.make_key
    state = PublicPoolTests.state
    enabled = PublicPoolTests.enabled
    test_approved_main_commit_failure_replays_capped_receipt_once = PublicPoolTests.test_gateway_failed_main_commit_spools_then_replays_wallet_once
    test_approved_unknown_fee_never_auto_releases = PublicPoolTests.test_unknown_missing_credit_incomplete_cancel_never_auto_frees
    test_approved_expiry_during_stream_preserves_original_expiry = PublicPoolTests.test_settlement_returns_only_unexpired_leftovers
    test_approved_unsent_or_main_rollback_keeps_conservative_hold = PublicPoolTests.test_unsent_release_expiry_and_main_db_rollback

    def setUp(self):
        super().setUp()
        from server import budgetowner
        self.addCleanup(budgetowner.close)
        with store.transaction() as conn:
            conn.execute('INSERT INTO portal_settings VALUES(?,?)', ('billing_policy', 'wallet-cap-platform-absorbs-v1'))

    async def settled(self, who='alice', amount=10, actual=2, request='synthetic-capped'):
        await self.pooled(who, 'synthetic-supplier-' + who, amount)
        made = await self.make_key(who)
        with self.enabled():
            key = keysvc.resolve(made['key'])
            state = self.state(request, key)
            token = observability.current.set(state)
            try:
                db.execute(wallet.RECEIPTS)
                wallet.reserve(key)
                wallet.forwarded(state)
                state.terminal_state = 'completed'
                with db.transaction():
                    wallet.receipt(state, {'prompt_tokens': 1, 'completion_tokens': 2}, actual)
                wallet.finish(state)
            finally:
                observability.current.reset(token)
        return made, state

    async def test_explicit_approval_opens_admission_but_missing_or_changed_policy_stays_closed(self):
        self.assertTrue(portalbilling.dispatch_ready())
        for value in ('true', 'wallet-cap', ''):
            with store.transaction() as conn:
                conn.execute("UPDATE portal_settings SET value=? WHERE key='billing_policy'", (value,))
            self.assertFalse(portalbilling.dispatch_ready())

    async def test_known_cost_charges_once_and_keeps_full_supplier_receipt(self):
        made, state = await self.settled(actual='2.123456')
        wallet.finish(state)
        wallet.recover()
        self.assertEqual(store.overview('alice')['balance_units'], store.units('7.876544'))
        with store.transaction() as conn:
            row = conn.execute('SELECT * FROM portal_settlements WHERE request_id=?', (state.request_id,)).fetchone()
            self.assertEqual((row['supplier_units'], row['charged_units'], row['platform_units']),
                             (store.units('2.123456'), store.units('2.123456'), 0))
            self.assertEqual(row['key_id'], made['id'])

    async def test_overrun_caps_user_at_held_balance_never_debt_or_contributor_spend(self):
        await self.pooled('bob', 'synthetic-independent-supplier', 50)
        _, state = await self.settled(actual=13)
        self.assertEqual(store.overview('bob')['balance_units'], store.units(50))
        view = store.overview('alice')
        self.assertEqual((view['balance_units'], view['held_units'], view['debt_units']), (0, 0, 0))
        self.assertEqual(next(e['amount_units'] for e in view['events'] if e['kind'] == 'usage'), -store.units(10))
        with store.transaction() as conn:
            row = conn.execute('SELECT * FROM portal_settlements WHERE request_id=?', (state.request_id,)).fetchone()
            self.assertEqual((row['supplier_units'], row['charged_units'], row['platform_units']),
                             (store.units(13), store.units(10), store.units(3)))

    async def test_policy_revoke_before_send_refuses_and_returns_unsent_hold(self):
        await self.pooled(amount=10)
        made = await self.make_key()
        with self.enabled():
            key = keysvc.resolve(made['key'])
            state = self.state('synthetic-revoked-policy', key)
            token = observability.current.set(state)
            try:
                db.execute(wallet.RECEIPTS)
                wallet.reserve(key)
                with store.transaction() as conn:
                    conn.execute("DELETE FROM portal_settings WHERE key='billing_policy'")
                with self.assertRaises(budget.BudgetRejected):
                    wallet.forwarded(state)
                wallet.finish(state)
            finally:
                observability.current.reset(token)
        self.assertEqual(store.overview('alice')['balance_units'], store.units(10))

    async def test_user_history_and_key_usage_show_user_charge_not_supplier_overrun(self):
        made, state = await self.settled(actual=15)
        db.execute('INSERT INTO request_logs(ts,key_id,request_id,model,status,credit) VALUES(?,?,?,?,?,?)',
                   (store.now(), made['id'], state.request_id, 'deepseek-v4.1-flash', 200, 15))
        response = await self.call('GET', '/api/portal/requests')
        row = response.json()['items'][0]
        self.assertEqual(row['credit'], 10)
        self.assertEqual(row['billing_state'], 'settled')
        self.app.include_router(__import__('server.routers.gateway', fromlist=['router']).router)
        with self.enabled():
            result = await self.call('GET', '/v1/key', headers={'authorization': 'Bearer ' + made['key']})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['credits']['used'], 10)

    async def test_later_bonus_cannot_pay_for_an_earlier_supplier_overrun(self):
        await self.pooled(amount=5)
        made = await self.make_key()
        with self.enabled():
            key = keysvc.resolve(made['key'])
            state = self.state('synthetic-future-grant', key)
            token = observability.current.set(state)
            try:
                db.execute(wallet.RECEIPTS)
                wallet.reserve(key); wallet.forwarded(state)
                wallet.grant_bonus('alice', 'd' * 32, 20, 30, 'admin', 'synthetic later bonus reason')
                state.terminal_state = 'completed'
                with db.transaction():
                    wallet.receipt(state, {'prompt_tokens': 1, 'completion_tokens': 1}, 8)
                wallet.finish(state)
            finally:
                observability.current.reset(token)
        self.assertEqual(store.overview('alice')['balance_units'], store.units(20))
        self.assertEqual(wallet.platform_costs_page()['platform_units'], store.units(3))

    async def test_unknown_fee_stays_held_and_manual_resolution_uses_same_cap(self):
        from server import budgetowner
        await self.pooled(amount=5)
        made = await self.make_key()
        with self.enabled():
            key = keysvc.resolve(made['key'])
            state = self.state('synthetic-unknown-capped', key)
            token = observability.current.set(state)
            try:
                db.execute(wallet.RECEIPTS)
                wallet.reserve(key); wallet.forwarded(state)
                state.terminal_state = 'protocol_incomplete'
                with db.transaction():
                    wallet.receipt(state, {'prompt_tokens': 1, 'completion_tokens': 1}, None)
                wallet.finish(state)
                self.assertEqual(store.overview('alice')['held_units'], store.units(5))
                with self.assertRaises(budget.BudgetRejected):
                    wallet.reserve(key)
                with mock.patch.object(budgetowner, 'alive', return_value=False):
                    wallet.resolve_unknown(state.request_id, 8, 'admin', 'synthetic verified reason', 'synthetic provider evidence')
            finally:
                observability.current.reset(token)
        self.assertEqual(store.overview('alice')['debt_units'], 0)
        self.assertEqual(wallet.platform_costs_page()['platform_units'], store.units(3))

    async def test_two_keys_cannot_hold_or_charge_the_same_balance_twice(self):
        await self.pooled(amount=10)
        made = [await self.make_key(), await self.make_key()]
        with self.enabled():
            keys = [keysvc.resolve(row['key']) for row in made]
            def run(index):
                token = observability.current.set(self.state('synthetic-concurrent-cap-' + str(index), keys[index]))
                try:
                    wallet.reserve(keys[index]); return True
                except budget.BudgetRejected:
                    return False
                finally:
                    observability.current.reset(token)
            with ThreadPoolExecutor(max_workers=2) as workers:
                self.assertEqual(sum(workers.map(run, (0, 1))), 1)
        self.assertEqual(store.overview('alice')['held_units'], store.units(10))

    async def test_failed_settlement_write_rolls_back_all_money_and_recovery_is_once(self):
        await self.pooled(amount=5)
        made = await self.make_key()
        with self.enabled():
            key = keysvc.resolve(made['key'])
            state = self.state('synthetic-settlement-crash', key)
            token = observability.current.set(state)
            try:
                db.execute(wallet.RECEIPTS)
                wallet.reserve(key); wallet.forwarded(state)
                state.terminal_state = 'completed'
                with db.transaction():
                    wallet.receipt(state, {'prompt_tokens': 1, 'completion_tokens': 1}, 8)
                with store.transaction() as conn:
                    conn.execute("CREATE TRIGGER synthetic_reject BEFORE INSERT ON portal_settlements BEGIN SELECT RAISE(ABORT,'synthetic storage failure'); END")
                wallet.finish(state)
                self.assertEqual(store.overview('alice')['held_units'], store.units(5))
                self.assertEqual(wallet.platform_costs_page()['total'], 0)
                with store.transaction() as conn:
                    conn.execute('DROP TRIGGER synthetic_reject')
                wallet.recover(); wallet.recover()
            finally:
                observability.current.reset(token)
        self.assertEqual(store.overview('alice')['held_units'], 0)
        self.assertEqual(wallet.platform_costs_page()['total'], 1)
        with store.transaction() as conn:
            for sql in ('UPDATE portal_settlements SET platform_units=0', 'DELETE FROM portal_settlements'):
                with self.assertRaises(Exception):
                    conn.execute(sql)

    async def test_approved_gateway_all_protocols_charge_caller_and_empty_wallet_never_sends(self):
        import httpx
        from fastapi import FastAPI
        from server import config, platformpolicy, portalpolicy
        from server.routers import gateway, anthropic, responses
        from server.tests.test_audit_20261029_gateway_ledger import GOOD, _Stream
        await self.pooled('alice', 'synthetic-caller', 1)
        await self.pooled('bob', 'synthetic-contributor', 50)
        made = await self.make_key()
        app = FastAPI()
        for router in (gateway.router, anthropic.router, responses.router):
            app.include_router(router)
        app.add_middleware(observability.RequestAuditMiddleware)
        app.add_middleware(platformpolicy.PlatformRoutingMiddleware)
        calls = []
        def respond(request):
            calls.append(request)
            if json.loads(request.content).get('stream'):
                chunks = [b'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n',
                    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
                    b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"credit":0.01}}\n\n', b'data: [DONE]\n\n']
                return httpx.Response(200, headers={'content-type': 'text/event-stream', 'x-wb-account-uid': 'synthetic-contributor'}, stream=_Stream(chunks))
            return httpx.Response(200, json=GOOD, headers={'x-wb-account-uid': 'synthetic-contributor'})
        def factory(*args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with self.enabled(), mock.patch.object(config, 'http_client', side_effect=factory), \
                mock.patch.object(gateway, 'RATE_MAX_PER_MIN', 0), \
                mock.patch('server.services.wb2api.load_upstream_config', return_value={}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                for protocol in ('chat/completions', 'messages', 'responses'):
                    for stream in (False, True):
                        payload = {'model': portalpolicy.MODELS[1], 'stream': stream}
                        payload.update({'input': 'hi', 'max_output_tokens': 8} if protocol == 'responses' else
                            {'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 8})
                        response = await client.post('/v1/' + protocol, json=payload, headers={'authorization': 'Bearer ' + made['key']})
                        self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(calls), 6)
                self.assertEqual(store.overview('alice')['balance_units'], store.units('.94'))
                self.assertEqual(store.overview('bob')['balance_units'], store.units(50))
                with store.transaction() as conn:
                    conn.execute('UPDATE portal_grants SET remaining_units=0 WHERE owner_id=?', ('alice',))
                for protocol in ('chat/completions', 'messages', 'responses'):
                    payload = {'model': portalpolicy.MODELS[1], 'input': 'hi'} if protocol == 'responses' else {
                        'model': portalpolicy.MODELS[1], 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 8}
                    response = await client.post('/v1/' + protocol, json=payload, headers={'authorization': 'Bearer ' + made['key']})
                    self.assertEqual(response.status_code, 429, response.text)
                    self.assertIn('portal_wallet_empty', response.text)
                self.assertEqual(len(calls), 6)

    async def test_platform_costs_are_admin_only_and_visible_in_history(self):
        await self.settled(actual=13)
        self.assertEqual((await self.call('GET', '/api/portal-admin/wallet-platform-costs')).status_code, 401)
        response = await self.call('GET', '/api/portal-admin/wallet-platform-costs', headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['platform_units'], store.units(3))
        response = await self.call('GET', '/api/portal-admin/wallet-history', headers=self.admin)
        self.assertEqual(response.json()['items'][0]['kind'], 'platform_overrun')

    async def test_restored_snapshot_cannot_reuse_billing_approval(self):
        from server import portalrecovery, config, budgetowner
        from server.tests.test_portal_closeout import PortalCloseoutTests
        args = await PortalCloseoutTests.prepare_snapshot(self)
        budgetowner.close()
        receipt = portalrecovery.create_snapshot(**args)
        restored = self.root / 'synthetic-capped-restore'
        portalrecovery.restore_quarantine(args['destination'], restored, receipt['manifest_sha256'])
        with mock.patch.object(config, 'DATA_DIR', restored):
            self.assertFalse(portalbilling.dispatch_ready())
            with store.transaction() as conn:
                self.assertIsNone(conn.execute("SELECT value FROM portal_settings WHERE key='billing_policy'").fetchone())


for name in tuple(vars(PortalTests)):
    if name.startswith('test_') and name not in vars(CappedBillingTests):
        setattr(CappedBillingTests, name, None)
del PublicPoolTests, PortalTests
