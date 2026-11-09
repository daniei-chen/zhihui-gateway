"""Local failure recovery must not repeat uncertain provider side effects."""
import asyncio
from unittest import mock

from server import portalservice as service, portalstore as store
from server.routers import portal
from server.services import tencent
from server.tests import test_user_portal as fixtures


class PortalStabilityTests(fixtures.PortalTests):
    async def start_scan(self):
        with mock.patch.object(tencent, 'start_login', return_value={
                'state': 'synthetic-stability-state', 'authUrl': 'https://example.invalid/authorize'}):
            response = await self.call('POST', '/api/portal/auth/start')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['session']

    def scan_row(self, session):
        with store.transaction() as conn:
            return dict(conn.execute('SELECT * FROM portal_auth_sessions WHERE id=?', (session,)).fetchone())

    async def test_parallel_scan_starts_reserve_capacity_before_provider_await(self):
        release, entered = asyncio.Event(), asyncio.Event()
        calls = []

        async def start(*args):
            calls.append(len(calls))
            index = len(calls)
            if len(calls) >= 2:
                entered.set()
            await release.wait()
            return {'state': 'synthetic-state-' + str(index), 'authUrl': 'https://example.invalid/authorize'}

        with mock.patch.object(tencent, 'start_login', side_effect=start):
            tasks = [asyncio.create_task(self.call('POST', '/api/portal/auth/start')) for _ in range(3)]
            try:
                await asyncio.wait_for(entered.wait(), 3)
                await asyncio.sleep(0.05)
            finally:
                release.set()
            responses = await asyncio.gather(*tasks)
        self.assertEqual(sorted(row.status_code for row in responses), [200, 200, 429])
        self.assertEqual(len(calls), 2)

    async def test_failed_scan_start_is_sanitized_and_releases_reserved_capacity(self):
        with mock.patch.object(tencent, 'start_login', side_effect=RuntimeError('synthetic-secret-not-for-response')):
            response = await self.call('POST', '/api/portal/auth/start')
        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotIn('synthetic-secret', response.text)
        with store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM portal_auth_sessions WHERE state IN ('starting','waiting','processing')").fetchone()[0], 0)
        await self.start_scan()

    async def test_cancelled_scan_start_releases_its_reservation(self):
        entered = asyncio.Event()

        async def start(*args):
            entered.set()
            await asyncio.Event().wait()

        with mock.patch.object(tencent, 'start_login', side_effect=start):
            task = asyncio.create_task(portal.auth_start(self.a))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM portal_auth_sessions WHERE state='starting'").fetchone()[0], 0)
        await self.start_scan()

    async def test_scan_start_rejects_malformed_provider_result_without_leaving_live_session(self):
        for result in ({'state': '', 'authUrl': 'https://example.invalid'},
                       {'state': 'synthetic-invalid', 'authUrl': 'http://example.invalid'}, None):
            with self.subTest(result=result), mock.patch.object(tencent, 'start_login', return_value=result), \
                    mock.patch.object(tencent, 'drop_state'):
                response = await self.call('POST', '/api/portal/auth/start')
                self.assertEqual(response.status_code, 502, response.text)
        with store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM portal_auth_sessions WHERE state IN ('starting','waiting','processing')").fetchone()[0], 0)

    async def test_temporary_poll_failure_is_retryable_without_importing_or_leaking_secrets(self):
        session = await self.start_scan()
        with mock.patch.object(tencent, 'poll_login', side_effect=RuntimeError('synthetic-private-token')), \
                mock.patch.object(service, 'import_ready', side_effect=AssertionError('no ready response')):
            response = await self.call('POST', '/api/portal/auth/poll', json={'session': session})
        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotIn('synthetic-private', response.text)
        self.assertEqual(self.scan_row(session)['state'], 'waiting')
        with mock.patch.object(store, 'now', return_value=fixtures.NOW + 3), \
                mock.patch.object(tencent, 'poll_login', return_value={'status': 'waiting'}) as poll:
            response = await self.call('POST', '/api/portal/auth/poll', json={'session': session})
        self.assertEqual(response.json()['state'], 'waiting')
        self.assertEqual(poll.call_count, 1)

    async def test_cancelled_provider_read_releases_only_its_poll_claim(self):
        session = await self.start_scan()
        entered = asyncio.Event()

        async def poll(*args):
            entered.set()
            await asyncio.Event().wait()

        with mock.patch.object(tencent, 'poll_login', side_effect=poll):
            task = asyncio.create_task(portal.auth_poll({'session': session}, self.a))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.scan_row(session)['state'], 'waiting')

    async def test_uncertain_import_is_terminal_and_never_automatically_replayed(self):
        session = await self.start_scan()
        with mock.patch.object(tencent, 'poll_login', return_value={'status': 'ready'}), \
                mock.patch.object(service, 'import_ready', side_effect=OSError('synthetic-private-path')) as imported, \
                mock.patch.object(tencent, 'drop_state'):
            first = await self.call('POST', '/api/portal/auth/poll', json={'session': session})
            second = await self.call('POST', '/api/portal/auth/poll', json={'session': session})
        self.assertEqual(first.status_code, 502, first.text)
        self.assertNotIn('synthetic-private', first.text)
        self.assertEqual(second.json()['state'], 'failed')
        self.assertEqual(imported.call_count, 1)
        self.assertEqual(self.scan_row(session)['state'], 'failed')

    async def test_processing_scan_cannot_be_taken_over_or_cancelled_while_import_is_running(self):
        session = await self.start_scan()
        entered, release = asyncio.Event(), asyncio.Event()

        async def imported(*args):
            entered.set()
            await release.wait()
            return {'id': 'synthetic-imported'}

        with mock.patch.object(tencent, 'poll_login', return_value={'status': 'ready'}), \
                mock.patch.object(service, 'import_ready', side_effect=imported) as run, mock.patch.object(tencent, 'drop_state'):
            task = asyncio.create_task(portal.auth_poll({'session': session}, self.a))
            try:
                await entered.wait()
                with mock.patch.object(store, 'now', return_value=fixtures.NOW + 301):
                    second = await self.call('POST', '/api/portal/auth/poll', json={'session': session})
                    cancelled = await self.call('POST', '/api/portal/auth/cancel', json={'session': session})
                self.assertEqual(second.json()['state'], 'processing')
                self.assertEqual(cancelled.status_code, 409)
            finally:
                release.set()
            result = await task
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(run.call_count, 1)

    async def test_expired_scan_keeps_an_already_committed_completion_receipt(self):
        session = await self.start_scan()
        with store.transaction() as conn:
            conn.execute("UPDATE portal_auth_sessions SET state='complete',account_id='synthetic-receipt',expires_at=? WHERE id=?",
                         (fixtures.NOW - 1, session))
        with mock.patch.object(tencent, 'poll_login', side_effect=AssertionError('committed receipt needs no provider')):
            response = await self.call('POST', '/api/portal/auth/poll', json={'session': session})
        self.assertEqual(response.json()['state'], 'complete')
        response = await self.call('POST', '/api/portal/auth/cancel', json={'session': session})
        self.assertEqual(response.json()['state'], 'complete')
        self.assertEqual(self.scan_row(session)['state'], 'complete')

    async def test_invalid_provider_scan_releases_slot_without_repolling(self):
        session = await self.start_scan()
        with mock.patch.object(tencent, 'poll_login', return_value={'status': 'invalid'}) as poll, \
                mock.patch.object(tencent, 'drop_state'):
            for _ in range(2):
                response = await self.call('POST', '/api/portal/auth/poll', json={'session': session})
                self.assertEqual(response.json()['state'], 'expired')
        self.assertEqual(poll.call_count, 1)
        self.assertEqual(self.scan_row(session)['state'], 'cancelled')

    async def test_scan_rechecks_identity_after_provider_and_drops_only_its_state(self):
        async def start(*args):
            with store.transaction() as conn:
                conn.execute("UPDATE portal_users SET sv=sv+1 WHERE id='alice'")
            return {'state': 'synthetic-revoked-state', 'authUrl': 'https://example.invalid/authorize'}

        with mock.patch.object(tencent, 'start_login', side_effect=start), mock.patch.object(tencent, 'drop_state') as drop:
            response = await self.call('POST', '/api/portal/auth/start')
        self.assertEqual(response.status_code, 401, response.text)
        drop.assert_called_once_with('synthetic-revoked-state')

    async def test_import_cancellation_is_terminal_without_reimporting(self):
        session = await self.start_scan()
        entered = asyncio.Event()

        async def imported(*args):
            entered.set()
            await asyncio.Event().wait()

        with mock.patch.object(tencent, 'poll_login', return_value={'status': 'ready'}), \
                mock.patch.object(service, 'import_ready', side_effect=imported) as run, mock.patch.object(tencent, 'drop_state'):
            task = asyncio.create_task(portal.auth_poll({'session': session}, self.a))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            response = await self.call('POST', '/api/portal/auth/poll', json={'session': session})
        self.assertEqual(response.json()['state'], 'failed')
        self.assertEqual(run.call_count, 1)

    async def make_uncredited(self):
        account, _ = await self.bind(total=0)
        store.claim_checkin(account['id'], 'synthetic-test')
        store.checkin_state(account['id'], store.day(), 'confirmed_uncredited', code=0)
        return account

    async def test_reconcile_crossing_midnight_commits_to_the_original_receipt_day(self):
        account = await self.make_uncredited()
        original_day = store.day()
        clock = [fixtures.NOW]

        async def balance(auth):
            clock[0] += 86400
            return True, 100, 'ok', [{'at': fixtures.NOW + 30 * 86400, 'amount': 100}]

        with mock.patch.object(store, 'now', side_effect=lambda: clock[0]), \
                mock.patch.object(tencent, 'fetch_credits', side_effect=balance), \
                mock.patch.object(tencent, 'checkin', side_effect=AssertionError('reconcile never signs again')):
            await service.reconcile_checkin(account['id'], 'alice')
        with store.transaction() as conn:
            row = conn.execute('SELECT day,state,credited_units FROM portal_checkins WHERE account_id=?', (account['id'],)).fetchone()
        self.assertEqual((row['day'], row['state'], row['credited_units']), (original_day, 'confirmed', 100 * store.SCALE))

    async def test_reconcile_rechecks_authorization_after_balance_await(self):
        account = await self.make_uncredited()

        async def balance(auth):
            with store.transaction() as conn:
                conn.execute("UPDATE portal_users SET sv=sv+1 WHERE id='alice'")
            return True, 100, 'ok', [{'at': fixtures.NOW + 30 * 86400, 'amount': 100}]

        with mock.patch.object(tencent, 'fetch_credits', side_effect=balance):
            response = await self.call('POST', f'/api/portal/accounts/{account["id"]}/reconcile-checkin')
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(store.overview('alice')['balance_units'], 0)

    async def test_one_bad_account_does_not_abort_user_or_admin_batch(self):
        first, _ = await self.bind(uid='synthetic-bad', total=0)
        second, _ = await self.bind(uid='synthetic-good', total=0)

        async def checkin(account_id, *args, **kwargs):
            if account_id == first['id']:
                raise FileNotFoundError('synthetic-private-path')
            return {'state': 'confirmed', 'message': 'synthetic success'}

        for path, headers in (('/api/portal/checkin-all', None), ('/api/portal-admin/checkin-all', self.admin)):
            with mock.patch.object(service, 'checkin', side_effect=checkin) as run:
                response = await self.call('POST', path, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(len(response.json()['results']), 2)
            self.assertNotIn('synthetic-private-path', response.text)
            self.assertEqual({row['account_id'] for row in response.json()['results']}, {first['id'], second['id']})

    async def test_batch_does_not_swallow_revocation_or_cancellation(self):
        from fastapi import HTTPException
        await self.bind(total=0)
        for error in (HTTPException(status_code=401, detail='revoked'), asyncio.CancelledError()):
            with mock.patch.object(service, 'checkin', side_effect=error):
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await portal.checkin_all(self.a)
                else:
                    response = await self.call('POST', '/api/portal/checkin-all')
                    self.assertEqual(response.status_code, 401)

    async def test_user_batch_can_visit_all_one_hundred_owned_accounts(self):
        for index in range(100):
            account = store.reserve_account('alice', f'synthetic-many-{index}', 'synthetic')
            store.initial_credit(account['id'], {})
        with mock.patch.object(service, 'checkin', return_value={'state': 'confirmed', 'already': True}) as run:
            response = await self.call('POST', '/api/portal/checkin-all')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['results']), 100)
        self.assertEqual(run.call_count, 100)

    async def test_automatic_batch_continues_after_unexpected_account_failure(self):
        await self.bind(uid='synthetic-one', total=0)
        await self.bind(uid='synthetic-two', total=0)
        with store.transaction() as conn:
            conn.execute("INSERT INTO portal_settings VALUES('automatic_checkin','true')")
        with mock.patch.object(service, 'checkin', side_effect=[RuntimeError('synthetic-private-error'), {'state': 'confirmed'}]) as run:
            await service.automatic_once()
        self.assertEqual(run.call_count, 2)


for name in tuple(vars(fixtures.PortalTests)):
    if name.startswith('test_'):
        setattr(PortalStabilityTests, name, None)
del name
