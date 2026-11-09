"""Cancellation, admission ownership, and narrow read paths under slow work."""
import asyncio
from contextlib import ExitStack
import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from server import config, db, observability, requestperf
from server.routers import gateway, accounts
from server.services import credits


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = observability.RequestAudit('speed-fixture', '/v1/chat/completions', 'POST',
                                                'openai', time.monotonic())
        self.state.performance = requestperf.Trace(self.state.started)
        self.state.actual_platform = 'workbuddy'
        self.context = observability.current.set(self.state)
        self.addCleanup(observability.current.reset, self.context)

    def patches(self):
        stack = ExitStack()
        for name in ('server.budget.prepare', 'server.budget.forwarded', 'server.ledger.forwarding'):
            stack.enter_context(patch(name))
        stack.enter_context(patch('server.services.wb2api.load_upstream_config', return_value={}))
        return stack

    async def test_slow_encoder_does_not_block_loop_and_reservation_runs_on_loop(self):
        loop_thread = threading.get_ident()
        worker_threads, reservations = [], []
        entered, release = threading.Event(), threading.Event()
        from server import wirebody
        original = wirebody.encode
        def slow(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture worker wait expired')
            return original(*args, **kwargs)
        self.state.reserve_wire_body = lambda size: reservations.append((size, threading.get_ident()))
        with self.patches(), patch.object(wirebody, 'encode', side_effect=slow):
            task = asyncio.create_task(gateway._prepare_forward_async({'id': 1}, {'model': 'm', 'text': 'x' * 90000}))
            try:
                await asyncio.wait_for(asyncio.to_thread(entered.wait), 1)
                # A live worker cannot prevent the loop from serving a cheap task.
                self.assertFalse(task.done())
                await asyncio.wait_for(asyncio.sleep(.01), .2)
            finally:
                release.set()
            wire = await asyncio.wait_for(task, 2)
        self.assertEqual(json.loads(wire)['model'], 'm')
        self.assertNotEqual(worker_threads[0], loop_thread)
        self.assertTrue(reservations)
        self.assertTrue(all(thread == loop_thread for _, thread in reservations))
        self.assertIn('forward_prepare', self.state.performance.spans)

    async def test_cancel_joins_encoder_and_never_advances_budget(self):
        entered, release = threading.Event(), threading.Event()
        from server import wirebody
        original = wirebody.encode
        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture worker wait expired')
            return original(*args, **kwargs)
        with self.patches(), patch.object(wirebody, 'encode', side_effect=slow), \
                patch('server.budget.prepare') as prepare, patch('server.budget.forwarded') as forwarded:
            task = asyncio.create_task(gateway._prepare_forward_async({'id': 1}, {'model': 'm'}))
            await asyncio.wait_for(asyncio.to_thread(entered.wait), 1)
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done(), 'body lease must remain held until worker ends')
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            prepare.assert_not_called()
            forwarded.assert_not_called()

    async def test_capacity_refusal_precedes_budget(self):
        def reject(size):
            raise observability.CapacityError()
        self.state.reserve_wire_body = reject
        with self.patches(), patch('server.budget.prepare') as prepare:
            result = await gateway._prepare_forward_async({'id': 1}, {'model': 'm'})
        self.assertEqual(result.status_code, 503)
        prepare.assert_not_called()

    async def test_revoked_key_during_preparation_cannot_dispatch(self):
        from starlette.requests import Request
        request = Request({'type': 'http', 'path': '/v1/chat/completions',
            'headers': [(b'authorization', b'Bearer synthetic')],
            'server': ('testserver', 80), 'client': ('127.0.0.1', 1), 'scheme': 'http',
            'query_string': b''})
        with self.patches(), patch.object(gateway.keysvc, 'resolve', return_value=None):
            result = await gateway._prepare_forward_async({'id': 1}, {'model': 'm'}, request=request)
        self.assertEqual(result.status_code, 401)
        self.assertEqual(json.loads(result.body)['error']['code'], 'invalid_api_key')

    async def test_exact_capacity_is_not_rounded_up(self):
        body = {'model': 'm'}
        from server import wirebody
        wire = wirebody.encode(body, 100)
        sizes = []
        self.state.reserve_wire_body = lambda size: sizes.append(size)
        with self.patches():
            result = await gateway._prepare_forward_async({'id': 1}, body)
        self.assertEqual(result, wire)
        self.assertEqual(sizes, [len(wire)])


class CreditCoalescingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        credits.invalidate()
        self.addCleanup(credits.invalidate)

    async def test_concurrent_refreshes_join_one_provider_read_and_one_record(self):
        release = asyncio.Event()
        calls = 0
        async def fetch(auth):
            nonlocal calls
            calls += 1
            await release.wait()
            return True, 123, 'ok', []
        with patch.object(credits.tencent, 'fetch_credits', side_effect=fetch), \
                patch.object(credits, 'record_balance') as record:
            jobs = [asyncio.create_task(credits.get_credits({'uid': 'same', 'access_token': 'fixture'}, force=True))
                    for _ in range(8)]
            await asyncio.sleep(.03)
            self.assertEqual(calls, 1)
            release.set()
            values = await asyncio.gather(*jobs)
        self.assertTrue(all(row[0] and row[1] == 123 for row in values))
        record.assert_called_once()

    async def test_invalidation_fences_result_including_persistent_snapshot(self):
        release = asyncio.Event()
        async def fetch(auth):
            await release.wait()
            return True, 100, 'ok', []
        with patch.object(credits.tencent, 'fetch_credits', side_effect=fetch), \
                patch.object(credits, 'record_balance') as record:
            task = asyncio.create_task(credits.get_credits({'uid': 'same'}))
            await asyncio.sleep(.02)
            credits.invalidate('same')
            release.set()
            result = await task
        self.assertFalse(result[0])
        self.assertIsNone(credits.cached_credits('same'))
        record.assert_not_called()

    async def test_changed_credentials_cannot_hit_previous_cache(self):
        with patch.object(credits.tencent, 'fetch_credits', new=AsyncMock(return_value=(True, 10, 'ok', []))) as fetch, \
                patch.object(credits, 'record_balance'):
            await credits.get_credits({'uid': 'same', 'access_token': 'one'})
            await credits.get_credits({'uid': 'same', 'access_token': 'two'})
        self.assertEqual(fetch.await_count, 2)

    async def test_new_credentials_fence_late_old_balance(self):
        release = asyncio.Event()
        async def fetch(auth):
            if auth['access_token'] == 'one':
                await release.wait()
                return True, 1, 'old', []
            return True, 2, 'new', []
        with patch.object(credits.tencent, 'fetch_credits', side_effect=fetch), \
                patch.object(credits, 'record_balance') as record:
            old = asyncio.create_task(credits.get_credits({'uid': 'same', 'access_token': 'one'}))
            await asyncio.sleep(.01)
            new = await credits.get_credits({'uid': 'same', 'access_token': 'two'})
            release.set()
            late = await old
        self.assertEqual(new[1], 2)
        self.assertFalse(late[0])
        self.assertEqual(credits.cached_credits('same')[1], 2)
        record.assert_called_once()


class PoolIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_catalog_does_not_fetch_unselected_platform(self):
        from server.qtgcore.routers import compat_fixes
        rt = SimpleNamespace(qoder=SimpleNamespace(models=AsyncMock(return_value={'data': []})))
        result = await compat_fixes._load_models(rt, 'qoder')
        self.assertEqual(result['errors'], [])
        rt.qoder.models.assert_awaited_once()

    async def test_qoder_pool_does_not_wait_for_other_platforms_or_usage(self):
        from server.qtgcore import service
        from server.qtgcore.cache import TTLCache
        q = SimpleNamespace(accounts=AsyncMock(return_value={'accounts': []}),
                            health=AsyncMock(return_value={'up': True}), base_url='http://fixture')
        rt = SimpleNamespace(qoder=q, settings=SimpleNamespace(cache_overview_sec=30))
        with patch.object(service, 'cache', TTLCache()):
            first = await service.build_pool_snapshot(rt, 'qoder')
            second = await service.build_pool_snapshot(rt, 'qoder')
        self.assertIs(first, second)
        self.assertEqual(first.qoder_accounts, [])
        q.accounts.assert_awaited_once()

    async def test_failed_list_never_becomes_empty_healthy_pool(self):
        from server.qtgcore import service
        from server.qtgcore.cache import TTLCache
        from server.qtgcore.errors import UpstreamError
        rt = SimpleNamespace(qoder=SimpleNamespace(accounts=AsyncMock(return_value={}),
            health=AsyncMock(return_value={'up': True})), settings=SimpleNamespace(cache_overview_sec=30))
        with patch.object(service, 'cache', TTLCache()), self.assertRaises(UpstreamError):
            await service.build_pool_snapshot(rt, 'qoder')

    async def test_status_resolves_header_before_selecting_pool(self):
        from server.qtgcore.routers import compat_fixes
        from starlette.requests import Request
        request = Request({'type': 'http', 'path': '/api/status', 'headers': [(b'x-qtg-platform', b'qoder')]})
        agg = SimpleNamespace(qoder_accounts=[], trae_accounts=[object()], wb_accounts=[object()],
                              health=[SimpleNamespace(up=True)])
        with patch.object(compat_fixes, '_rt'), \
                patch.object(compat_fixes, 'build_pool_snapshot', new=AsyncMock(return_value=agg)) as pool, \
                patch.object(compat_fixes, 'build_aggregate', side_effect=AssertionError('all platforms fetched')):
            result = await compat_fixes.fixed_status(request)
        self.assertEqual(result['total'], 0)
        self.assertEqual(pool.call_args.args[1], 'qoder')


class CachedEndpointTests(unittest.TestCase):
    def test_cached_balances_are_group_scoped_and_never_query_provider(self):
        with patch.object(accounts, '_group', return_value={}), \
                patch.object(accounts, '_group_dir', return_value=Path('/fixture')), \
                patch.object(accounts.wb2api, 'list_auth_accounts', return_value=[{'uid': 'allowed'}]), \
                patch.object(credits, 'snapshot_entries', return_value={'accounts': [
                    {'uid': 'allowed', 'credits': 10, 'registered_at': 1},
                    {'uid': 'other', 'credits': 500, 'registered_at': 1}]}), \
                patch.object(credits, 'cached_credits', return_value=None), \
                patch.object(credits.tencent, 'fetch_credits', side_effect=AssertionError('provider queried')):
            result = accounts.cached_group_credits(upstream_id=None, user={'role': 'account_admin'})
        self.assertEqual(result['credits'], {'allowed': 10})
        self.assertFalse(result['provider_called'])
        self.assertEqual(result['meta']['allowed']['source'], 'registered')


class TimingTests(unittest.TestCase):
    def test_trace_allowlists_names_and_never_accepts_payload_details(self):
        trace = requestperf.Trace(time.monotonic())
        trace.add('json_parse', .01)
        trace.add('credential-fixture', 2)
        trace.add('json_encode', float('nan'))
        trace.mark('first_useful')
        trace.size('forward_bytes', 100)
        value = trace.payload()
        self.assertEqual(value['spans_ms'], {'json_parse': 10})
        self.assertEqual(value['sizes'], {'forward_bytes': 100})
        self.assertNotIn('credential-fixture', json.dumps(value))


class HttpTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_loopback_transport_records_finite_stages_not_headers(self):
        writers, handlers = set(), set()
        async def serve(reader, writer):
            handlers.add(asyncio.current_task())
            writers.add(writer)
            try:
                await reader.readuntil(b'\r\n\r\n')
                await asyncio.sleep(.02)
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                writers.discard(writer)
                handlers.discard(asyncio.current_task())
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        state = observability.RequestAudit('timing-http', '/v1/chat/completions', 'POST',
                                            'openai', time.monotonic())
        state.performance = requestperf.Trace(state.started)
        token = observability.current.set(state)
        try:
            port = server.sockets[0].getsockname()[1]
            with patch.object(config, 'effective_http_proxy', return_value=''):
                async with config.http_client(2, connect=1, identity='timing-fixture') as client:
                    result = await client.get(f'http://127.0.0.1:{port}/',
                                              headers={'Authorization': 'Bearer private-fixture'})
                    self.assertEqual(result.status_code, 200)
            payload = state.performance.payload()
            for stage in ('transport_slot_wait', 'transport_headers', 'connect', 'headers_wait', 'request_send'):
                self.assertIn(stage, payload['spans_ms'])
            self.assertNotIn('private-fixture', json.dumps(payload))
        finally:
            await config.close_clients()
            observability.current.reset(token)
            server.close()
            await server.wait_closed()
            for writer in list(writers):
                writer.close()
            if handlers:
                await asyncio.gather(*list(handlers), return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
