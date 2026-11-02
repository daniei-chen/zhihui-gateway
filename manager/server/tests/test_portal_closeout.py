"""Hard send refusal, request closure proof and paired quarantine restore drills."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest import mock

from server import budget, budgetowner, config, db, keysvc, observability, portalbilling
from server import portalstore as store, portalwallet as wallet, portalrecovery as recovery
from server.tests import test_user_portal as fixtures

NOW = fixtures.NOW
REASON = 'synthetic operator reconciliation evidence'


class PortalCloseoutTests(fixtures.PortalTests):
    def setUp(self):
        super().setUp()
        self.addCleanup(budgetowner.close)

    async def key(self):
        response = await self.call('POST', '/api/portal/keys', json={'label': 'synthetic closeout'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def state(self, request, key):
        import time
        state = observability.RequestAudit(request, '/v1/chat/completions', 'POST', 'openai', time.monotonic())
        state.key_id = key['id']
        return state

    async def make_unknown(self, request='synthetic-live-unknown'):
        wallet.grant_bonus('alice', 'f' * 32, 100, 30, 'admin', REASON)
        made = await self.key()
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}):
            key = keysvc.resolve(made['key']); state = self.state(request, key)
            token = observability.current.set(state)
            try:
                db.execute(wallet.RECEIPTS)
                # Synthetic historical in-flight request, not real permission.
                with mock.patch.object(portalbilling, 'require_verified_cap'):
                    wallet.reserve(key); wallet.forwarded(state)
            finally:
                observability.current.reset(token)
        return state

    async def test_real_hard_cap_gate_all_protocols_never_reaches_provider(self):
        import httpx
        from fastapi import FastAPI
        from server import platformpolicy, portalpolicy
        from server.routers import gateway, anthropic, responses
        wallet.grant_bonus('alice', 'a' * 32, 1, 30, 'admin', REASON)
        made = await self.key()
        app = FastAPI()
        for router in (gateway.router, anthropic.router, responses.router):
            app.include_router(router)
        app.add_middleware(observability.RequestAuditMiddleware)
        app.add_middleware(platformpolicy.PlatformRoutingMiddleware)
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}), \
                mock.patch.object(config, 'http_client', side_effect=AssertionError('must refuse before any provider I/O')) as client_factory, \
                mock.patch.object(gateway, 'RATE_MAX_PER_MIN', 0):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                for protocol in ('chat/completions', 'messages', 'responses'):
                    for stream in (False, True):
                        payload = {'model': portalpolicy.MODELS[0], 'stream': stream}
                        payload.update({'input': 'hi', 'max_output_tokens': 1} if protocol == 'responses' else
                            {'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 1})
                        result = await client.post('/v1/' + protocol, json=payload, headers={'authorization': 'Bearer ' + made['key']})
                        self.assertEqual(result.status_code, 503, result.text)
                        self.assertIn(portalbilling.CODE, result.text)
            self.assertEqual(client_factory.call_count, 0)
        self.assertEqual(store.overview('alice')['balance_units'], store.units(1))
        self.assertEqual(store.overview('alice')['held_units'], 0)
        self.assertEqual(store.overview('alice')['debt_units'], 0)
        self.assertFalse(store.overview('alice')['dispatch_enabled'])
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_spends').fetchone()[0], 0)

    async def test_send_boundary_rechecks_hard_cap_and_preserves_unsent_balance(self):
        wallet.grant_bonus('alice', 'a' * 32, 100, 30, 'admin', REASON)
        made = await self.key()
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}):
            key = keysvc.resolve(made['key']); state = self.state('prepared-but-no-cap', key)
            token = observability.current.set(state)
            try:
                db.execute(wallet.RECEIPTS)
                with mock.patch.object(portalbilling, 'require_verified_cap'):
                    wallet.reserve(key)
                with self.assertRaises(budget.BudgetRejected) as raised:
                    wallet.forwarded(state)
                self.assertEqual(raised.exception.code, portalbilling.CODE)
                wallet.finish(state)
                self.assertEqual(store.overview('alice')['balance_units'], store.units(100))
            finally:
                observability.current.reset(token)

    async def test_cancel_during_provider_headers_does_not_mark_end_until_client_cleanup(self):
        import asyncio
        import httpx
        from fastapi import FastAPI
        from server import platformpolicy, portalpolicy
        from server.routers import gateway
        wallet.grant_bonus('alice', 'a' * 32, 10, 30, 'admin', REASON)
        made = await self.key()
        app = FastAPI(); app.include_router(gateway.router)
        app.add_middleware(observability.RequestAuditMiddleware)
        app.add_middleware(platformpolicy.PlatformRoutingMiddleware)
        entered = asyncio.Event(); clients = []
        async def handler(request):
            entered.set()
            await asyncio.sleep(60)
        def factory(*args, **kwargs):
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler)); clients.append(client); return client
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}), \
                mock.patch.object(portalbilling, 'require_verified_cap'), \
                mock.patch.object(config, 'http_client', side_effect=factory), \
                mock.patch.object(gateway, 'RATE_MAX_PER_MIN', 0), \
                mock.patch('server.services.wb2api.load_upstream_config', return_value={}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                task = asyncio.create_task(client.post('/v1/chat/completions', json={'model': portalpolicy.MODELS[0],
                    'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 8, 'stream': True}, headers={'authorization': 'Bearer ' + made['key']}))
                await asyncio.wait_for(entered.wait(), 3)
                with store.transaction() as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_request_ends').fetchone()[0], 0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(all(client.is_closed for client in clients))
        row = wallet.pending_page()['items'][0]
        self.assertTrue(row['end_confirmed'])
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(store.overview('alice')['held_units'], store.units(10))

    async def test_live_worker_request_end_proof_allows_evidenced_resolution_not_free_release(self):
        state = await self.make_unknown()
        wallet.finish(state)
        self.assertEqual(store.overview('alice')['held_units'], store.units(100))
        with mock.patch.object(budgetowner, 'alive', return_value=True):
            row = wallet.pending_page()['items'][0]
            self.assertTrue(row['end_confirmed'])
            self.assertTrue(row['can_resolve'])
            wallet.resolve_unknown(state.request_id, '2.123456', 'admin', REASON, REASON)
            with self.assertRaises(ValueError):
                wallet.resolve_unknown(state.request_id, '2.123456', 'admin', REASON, REASON)
        self.assertEqual(store.overview('alice')['balance_units'], store.units('97.876544'))

    async def test_log_or_wrong_request_end_owner_never_proves_closure(self):
        state = await self.make_unknown()
        with store.transaction() as conn:
            conn.execute("UPDATE portal_spends SET state='unknown' WHERE request_id=?", (state.request_id,))
            conn.execute('INSERT INTO portal_request_ends VALUES(?,?,?,?,?)', (state.request_id, 'bob', state.key_id, budgetowner.identity(), NOW))
        db.execute('INSERT INTO request_logs(ts,key_id,model,status,request_id,terminal_state) VALUES(?,?,?,?,?,?)',
            (NOW, state.key_id, 'deepseek-v4-pro', 502, state.request_id, 'protocol_incomplete'))
        with mock.patch.object(budgetowner, 'alive', return_value=True):
            row = wallet.pending_page()['items'][0]
            self.assertFalse(row['end_confirmed']); self.assertFalse(row['can_resolve'])
            with self.assertRaises(ValueError):
                wallet.resolve_unknown(state.request_id, 0, 'admin', REASON, REASON)

    async def test_failed_request_end_write_keeps_hold_and_blocks_live_manual_resolution(self):
        state = await self.make_unknown()
        with mock.patch.object(wallet, 'mark_request_ended', side_effect=OSError('synthetic disk failure')):
            wallet.finish(state)
        with mock.patch.object(budgetowner, 'alive', return_value=True):
            self.assertFalse(wallet.pending_page()['items'][0]['can_resolve'])
        self.assertEqual(store.overview('alice')['held_units'], store.units(100))

    async def test_stream_cleanup_timeout_never_creates_request_end_proof(self):
        import asyncio
        class Stuck:
            async def aclose(self):
                await asyncio.sleep(60)
        state = await self.make_unknown('cleanup-incomplete')
        token = observability.current.set(state)
        try:
            await observability.close_stream(Stuck(), Stuck(), timeout=0.001)
            self.assertTrue(state.portal_cleanup_unconfirmed)
            wallet.finish(state)
        finally:
            observability.current.reset(token)
        with mock.patch.object(budgetowner, 'alive', return_value=True):
            row = wallet.pending_page()['items'][0]
            self.assertFalse(row['end_confirmed']); self.assertFalse(row['can_resolve'])

    async def test_pending_headers_cleanup_timeout_without_response_keeps_hold(self):
        import asyncio
        from server import deadline
        from server.routers import gateway
        class Stuck:
            async def aclose(self):
                await asyncio.sleep(60)
        state = await self.make_unknown('pending-headers-timeout')
        token = observability.current.set(state)
        try:
            await gateway._close_pending_response(None, Stuck(), deadline.Budgets(client_cancel_s=0.001))
            self.assertTrue(state.portal_cleanup_unconfirmed)
            wallet.finish(state)
        finally:
            observability.current.reset(token)
        with mock.patch.object(budgetowner, 'alive', return_value=True):
            row = wallet.pending_page()['items'][0]
            self.assertFalse(row['end_confirmed']); self.assertFalse(row['can_resolve'])
        self.assertEqual(store.overview('alice')['held_units'], store.units(100))

    async def test_cleanup_exception_is_sticky_after_later_success(self):
        from server import deadline
        from server.routers import gateway
        class Broken:
            async def aclose(self):
                raise OSError('synthetic cleanup failure')
        class Closed:
            async def aclose(self):
                pass
        state = await self.make_unknown('pending-headers-error')
        token = observability.current.set(state)
        try:
            await gateway._close_pending_response(None, Broken(), deadline.Budgets())
            await gateway._close_pending_response(None, Closed(), deadline.Budgets())
            self.assertTrue(state.portal_cleanup_unconfirmed)
            wallet.finish(state)
        finally:
            observability.current.reset(token)
        with mock.patch.object(budgetowner, 'alive', return_value=True):
            self.assertFalse(wallet.pending_page()['items'][0]['can_resolve'])

    async def nonstream_cleanup_failure(self, protocol):
        import httpx
        from fastapi import FastAPI
        from server import platformpolicy, portalpolicy
        from server.routers import gateway, anthropic, responses
        wallet.grant_bonus('alice', 'a' * 32, 10, 30, 'admin', REASON)
        made = await self.key()
        app = FastAPI()
        for router in (gateway.router, anthropic.router, responses.router):
            app.include_router(router)
        app.add_middleware(observability.RequestAuditMiddleware)
        app.add_middleware(platformpolicy.PlatformRoutingMiddleware)
        class ExitBroken(httpx.AsyncClient):
            async def __aexit__(self, *exc_info):
                raise OSError('synthetic client close failure')
        clients = []
        def factory(*args, **kwargs):
            client = ExitBroken(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
                'choices': [{'message': {'role': 'assistant', 'content': 'synthetic'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'credit': 1}})))
            clients.append(client)
            return client
        try:
            with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}), \
                    mock.patch.object(portalbilling, 'require_verified_cap'), \
                    mock.patch.object(config, 'http_client', side_effect=factory), \
                    mock.patch.object(gateway, 'RATE_MAX_PER_MIN', 0), \
                    mock.patch('server.services.wb2api.load_upstream_config', return_value={}):
                payload = {'model': portalpolicy.MODELS[0], 'stream': False}
                payload.update({'input': 'hi', 'max_output_tokens': 8} if protocol == 'responses' else
                    {'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 8})
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                    result = await client.post('/v1/' + protocol, json=payload, headers={'authorization': 'Bearer ' + made['key']})
                self.assertEqual(result.status_code, 502, result.text)
            with mock.patch.object(budgetowner, 'alive', return_value=True):
                row = wallet.pending_page()['items'][0]
                self.assertEqual(row['state'], 'unknown')
                self.assertFalse(row['end_confirmed']); self.assertFalse(row['can_resolve'])
            self.assertEqual(store.overview('alice')['held_units'], store.units(10))
        finally:
            for client in clients:
                await client.aclose()

    async def test_chat_nonstream_client_close_failure_never_proves_end(self):
        await self.nonstream_cleanup_failure('chat/completions')

    async def test_anthropic_nonstream_client_close_failure_never_proves_end(self):
        await self.nonstream_cleanup_failure('messages')

    async def test_responses_nonstream_client_close_failure_never_proves_end(self):
        await self.nonstream_cleanup_failure('responses')

    async def prepare_snapshot(self):
        await self.bind(uid='snapshot-account', total=20)
        wallet.grant_bonus('alice', 'a' * 32, 10, 30, 'admin', REASON)
        self.root.joinpath('pool').mkdir(exist_ok=True)
        self.root.joinpath('spool').mkdir(exist_ok=True)
        # Both linked shared credentials and isolated pending credentials included.
        account = store.overview('alice')['accounts'][0]
        shared = {'account': {'uid': 'snapshot-account', 'enterpriseId': ''}, 'auth': {'accessToken': 'synthetic-shared-secret', 'realm': 'cn'}}
        (config.AUTH_DIR / 'workbuddy-snapshot-account.json').write_text(json.dumps(shared))
        with store.transaction() as conn:
            conn.execute('INSERT INTO portal_pool_links VALUES(?,?,?)', (account['id'], 'workbuddy-snapshot-account.json', NOW))
        return dict(main_db=config.DB_PATH, portal_db=config.DATA_DIR / 'user-portal.sqlite3', users=config.USERS_FILE,
            auths=config.AUTH_DIR, isolated=config.DATA_DIR / 'user-portal-auths', spool=self.root / 'spool',
            destination=self.root / 'snapshot', offline_confirmed=True)

    async def test_paired_snapshot_restore_is_new_directory_quarantined_and_no_double_grants(self):
        args = await self.prepare_snapshot()
        state = await self.make_snapshot_unknown()
        budgetowner.close()
        result = recovery.create_snapshot(**args)
        self.assertTrue(result['complete'])
        manifest = recovery.verify_snapshot(args['destination'], result['manifest_sha256'])
        self.assertIn('auths/workbuddy-snapshot-account.json', manifest['files'])
        self.assertTrue(any(name.startswith('user-portal-auths/') for name in manifest['files']))
        self.assertNotIn('synthetic-shared-secret', (args['destination'] / 'manifest.json').read_text())
        restored = self.root / 'restore'
        self.assertTrue(recovery.restore_quarantine(args['destination'], restored, result['manifest_sha256'])['quarantined'])
        self.assertFalse(list((restored / 'auths').iterdir()))
        self.assertTrue((restored / 'quarantined-credentials/auths/workbuddy-snapshot-account.json').is_file())
        source_hash = recovery._digest(config.DATA_DIR / 'user-portal.sqlite3')
        with mock.patch.object(config, 'DATA_DIR', restored):
            first = store.overview('alice'); second = store.overview('alice')
            self.assertEqual(first['balance_units'], second['balance_units'])
            with store.transaction() as conn:
                self.assertEqual(conn.execute("SELECT value FROM portal_settings WHERE key='restore_quarantine'").fetchone()[0], 'true')
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_request_ends').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT state FROM portal_spends WHERE request_id=?', (state.request_id,)).fetchone()[0], 'unknown')
                self.assertEqual(conn.execute("SELECT value FROM portal_settings WHERE key='automatic_checkin'").fetchone()[0], 'false')
            with self.assertRaises(ValueError):
                wallet.grant_bonus('alice', 'd' * 32, 100, 30, 'admin', REASON)
            with self.assertRaises(ValueError):
                store.claim_checkin(first['accounts'][0]['id'], 'automatic')
        self.assertEqual(source_hash, recovery._digest(config.DATA_DIR / 'user-portal.sqlite3'))
        with closing(sqlite3.connect(restored / 'manager.db')) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM api_keys WHERE enabled=1').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            recovery.restore_quarantine(args['destination'], restored, result['manifest_sha256'])

    async def make_snapshot_unknown(self):
        made = await self.key()
        with mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '1'}):
            key = keysvc.resolve(made['key']); state = self.state('restore-unsettled', key)
            token = observability.current.set(state)
            try:
                db.execute(wallet.RECEIPTS)
                with mock.patch.object(portalbilling, 'require_verified_cap'):
                    wallet.reserve(key); wallet.forwarded(state)
                wallet.finish(state)
            finally:
                observability.current.reset(token)
        return state

    async def test_snapshot_requires_offline_and_refuses_live_worker_existing_or_source_target(self):
        args = await self.prepare_snapshot()
        with self.assertRaises(ValueError):
            recovery.create_snapshot(**{**args, 'offline_confirmed': False})
        budgetowner.identity()
        with self.assertRaises(ValueError):
            recovery.create_snapshot(**args)
        budgetowner.close()
        result = recovery.create_snapshot(**args)
        with self.assertRaises(ValueError):
            recovery.create_snapshot(**args)
        with self.assertRaises(ValueError):
            recovery.restore_quarantine(args['destination'], config.DATA_DIR, result['manifest_sha256'])

    async def test_sqlite_writer_prevents_partial_pair_snapshot(self):
        args = await self.prepare_snapshot()
        with closing(sqlite3.connect(config.DB_PATH)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError):
                recovery.create_snapshot(**args)
        self.assertFalse(args['destination'].exists())

    async def test_independent_manifest_receipt_detects_rewritten_manifest_and_data(self):
        args = await self.prepare_snapshot(); receipt = recovery.create_snapshot(**args)
        root = args['destination']
        path = root / 'users.json'; path.write_text('{}')
        manifest = json.loads((root / 'manifest.json').read_text())
        manifest['files']['users.json'] = {'sha256': recovery._digest(path), 'size': path.stat().st_size}
        (root / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            recovery.verify_snapshot(root, receipt['manifest_sha256'])

    async def test_missing_credential_or_cross_ledger_identity_cannot_form_complete_snapshot(self):
        args = await self.prepare_snapshot()
        (config.AUTH_DIR / 'workbuddy-snapshot-account.json').unlink()
        with self.assertRaises(FileNotFoundError):
            recovery.create_snapshot(**args)
        self.assertFalse((args['destination'] / 'manifest.json').exists())

    async def test_historical_restore_never_turns_post_snapshot_spending_back_into_live_balance(self):
        args = await self.prepare_snapshot(); receipt = recovery.create_snapshot(**args)
        with store.transaction() as conn:
            conn.execute('UPDATE portal_bonus_grants SET remaining_units=0')
        before = store.overview('alice')['balance_units']
        restored = self.root / 'old-quarantine'
        recovery.restore_quarantine(args['destination'], restored, receipt['manifest_sha256'])
        self.assertEqual(store.overview('alice')['balance_units'], before)
        with mock.patch.object(config, 'DATA_DIR', restored):
            with store.transaction() as conn, self.assertRaises(ValueError):
                store.require_not_quarantined(conn)

    async def test_corrupt_missing_extra_and_traversal_snapshot_never_restore(self):
        args = await self.prepare_snapshot(); result = recovery.create_snapshot(**args)
        root = args['destination']
        manifest = (root / 'manifest.json').read_bytes()
        for change in ('corrupt', 'extra', 'traversal'):
            if change == 'corrupt':
                path = root / 'users.json'; saved = path.read_bytes(); path.write_bytes(b'bad')
            elif change == 'extra':
                path = root / 'unexpected.json'; path.write_bytes(b'{}')
            else:
                data = json.loads(manifest); data['files']['../outside'] = {'sha256': '0' * 64, 'size': 0}
                (root / 'manifest.json').write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                recovery.restore_quarantine(root, self.root / ('bad-' + change), result['manifest_sha256'])
            self.assertFalse((self.root / ('bad-' + change)).exists())
            if change == 'corrupt': path.write_bytes(saved)
            elif change == 'extra': path.unlink()
            else: (root / 'manifest.json').write_bytes(manifest)

    async def test_credential_mutation_during_snapshot_leaves_incomplete_unrestorable_copy(self):
        args = await self.prepare_snapshot()
        original = recovery._copy_database
        def mutate(source, destination):
            original(source, destination)
            config.USERS_FILE.write_text(config.USERS_FILE.read_text() + ' ')
        with mock.patch.object(recovery, '_copy_database', side_effect=mutate), self.assertRaises(ValueError):
            recovery.create_snapshot(**args)
        self.assertFalse((args['destination'] / 'manifest.json').exists())
        with self.assertRaises((ValueError, FileNotFoundError)):
            recovery.verify_snapshot(args['destination'], '0' * 64)

    async def test_cross_database_key_hash_mismatch_refuses_complete_snapshot(self):
        args = await self.prepare_snapshot()
        made = await self.key()
        db.execute('UPDATE api_keys SET key_hash=? WHERE id=?', ('0' * 64, made['id']))
        with self.assertRaisesRegex(ValueError, '双库密钥归属不匹配'):
            recovery.create_snapshot(**args)
        self.assertFalse((args['destination'] / 'manifest.json').exists())

    async def test_shared_credential_uid_mismatch_refuses_complete_snapshot(self):
        args = await self.prepare_snapshot()
        path = config.AUTH_DIR / 'workbuddy-snapshot-account.json'
        credential = json.loads(path.read_text())
        credential['account']['uid'] = 'different-synthetic-account'
        path.write_text(json.dumps(credential))
        with self.assertRaisesRegex(ValueError, '账号归属与公共池凭据不一致'):
            recovery.create_snapshot(**args)
        self.assertFalse((args['destination'] / 'manifest.json').exists())

    async def test_spool_pending_dead_processed_dispositions_and_markers_are_preserved(self):
        args = await self.prepare_snapshot()
        files = {
            'pending-request.json': b'{"synthetic":"pending"}',
            'dead/dead-request.json': b'{"synthetic":"dead"}',
            'dead/processed/processed-request.json': b'{"synthetic":"processed"}',
            'dead/dispositions.jsonl': b'{"synthetic":"disposition"}\n',
            '.spool-used': b'1',
        }
        for name, content in files.items():
            path = args['spool'] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (config.DATA_DIR / '.spool-used').write_bytes(b'1')
        receipt = recovery.create_snapshot(**args)
        root = self.root / 'spool-quarantine'
        recovery.restore_quarantine(args['destination'], root, receipt['manifest_sha256'])
        for name, content in files.items():
            self.assertEqual((root / 'spool' / name).read_bytes(), content)
        self.assertEqual((root / '.spool-used').read_bytes(), b'1')

    async def test_failed_restore_copy_never_overwrites_source_or_marks_completion(self):
        args = await self.prepare_snapshot()
        receipt = recovery.create_snapshot(**args)
        source_before = recovery._inventory(args['destination'])
        target = self.root / 'incomplete-restore'
        sibling = self.root / 'existing-data'
        sibling.write_bytes(b'synthetic-existing-data')
        original = recovery._write
        count = 0
        def fail_copy(path, content):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('synthetic restore disk failure')
            return original(path, content)
        with mock.patch.object(recovery, '_write', side_effect=fail_copy), self.assertRaises(OSError):
            recovery.restore_quarantine(args['destination'], target, receipt['manifest_sha256'])
        self.assertTrue(target.exists())
        self.assertFalse((target / 'RESTORED-QUARANTINE.json').exists())
        self.assertEqual(source_before, recovery._inventory(args['destination']))
        self.assertEqual(sibling.read_bytes(), b'synthetic-existing-data')
        with self.assertRaises(ValueError):
            recovery.restore_quarantine(args['destination'], target, receipt['manifest_sha256'])


for name in tuple(vars(fixtures.PortalTests)):
    if name.startswith('test_'):
        setattr(PortalCloseoutTests, name, None)
del name
