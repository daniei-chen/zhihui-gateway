"""Slow writers, repeated cancellation and byte pressure use real ownership paths."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from starlette.requests import Request

from deploy.migrate_admission_policy import Refused, migrate
from server import budget, budgetowner, db, deadline, drain, keysvc, memorybudget, observability as obs
from server.outputlimits import OutputLimitExceeded
from server.routers import gateway
from server.services import persistence
from server.sharedadmission import Coordinator, Unavailable
from server.tests._db_fixture import isolate_database


async def until(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.002)
    raise AssertionError('fixture did not reach its synchronization point')


async def drive(middleware, body=b'{}', *, path='/v1/chat/completions', method='POST'):
    messages = []
    consumed = False

    async def receive():
        nonlocal consumed
        if consumed:
            await asyncio.Event().wait()
        consumed = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    async def send(message):
        messages.append(message)

    scope = {'type': 'http', 'path': path, 'method': method,
             'headers': [(b'content-length', str(len(body)).encode())],
             'scheme': 'http', 'server': ('fixture', 80), 'client': ('127.0.0.1', 1),
             'query_string': b''}
    await middleware(scope, receive, send)
    return messages


class PersistenceOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate_database(self)

    async def test_executor_capacity_is_finite_and_wait_does_not_block_loop(self):
        pool = persistence.PersistencePool(workers=1, pending=1)
        gate = threading.Event()
        entered = threading.Event()
        def slow():
            entered.set()
            gate.wait(5)
        first = pool.submit(slow)
        try:
            await until(entered.is_set)
            second = pool.submit(lambda: None)
            self.assertIsNotNone(second)
            self.assertIsNone(pool.submit(lambda: self.fail('overflow must not submit')))
            self.assertEqual(pool.accepted, 2)
            waiter = asyncio.create_task(pool.run(lambda: 'after saturation'))
            for _ in range(5):
                await asyncio.sleep(0.002)
            self.assertFalse(waiter.done())
            self.assertEqual(pool.accepted, 2)
            gate.set()
            self.assertEqual(await waiter, 'after saturation')
            await asyncio.wrap_future(first)
            await asyncio.wrap_future(second)
            self.assertEqual(pool.accepted, 0)
        finally:
            gate.set()
            pool.close()

    async def test_cancellation_while_waiting_for_writer_does_not_drop_known_facts(self):
        pool = persistence.PersistencePool(workers=1, pending=0)
        gate = threading.Event()
        first = pool.submit(lambda: gate.wait(5))
        facts = []
        waiter = asyncio.create_task(pool.run(facts.append, 'known provider receipt'))
        try:
            await asyncio.sleep(0.02)
            waiter.cancel()
            await asyncio.sleep(0.01)
            waiter.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(waiter.done())
            self.assertEqual(facts, [])
            gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            await asyncio.wrap_future(first)
            self.assertEqual(facts, ['known provider receipt'])
            self.assertEqual(pool.accepted, 0)
        finally:
            gate.set()
            pool.close()

    async def test_real_receipt_commits_before_cancelled_caller_releases_ownership(self):
        key = keysvc.resolve(keysvc.create_key(name='owned receipt', quota=10000)['key'])
        state = obs.RequestAudit('owned-receipt', '/v1/chat/completions', 'POST', 'openai',
                                 time.monotonic())
        token = obs.current.set(state)
        entered, release = threading.Event(), threading.Event()
        real_record = gateway._record

        def slow_record(*args, **kwargs):
            self.assertIs(obs.current.get(), state)
            entered.set()
            release.wait(5)
            real_record(*args, **kwargs)

        try:
            budget.prepare(key, {'model': 'fixture', 'messages': [], 'max_tokens': 32})
            budget.forwarded()
            with patch.object(gateway, '_record', slow_record):
                task = asyncio.create_task(gateway._record_async(
                    key, '127.0.0.1', 'fixture', 'fixture', 200, 9, 3, 10,
                    'synthetic', None, False,
                    usage={'prompt_tokens': 9, 'completion_tokens': 3}))
                await until(entered.is_set)
                task.cancel()
                await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertEqual(db.query_one('SELECT state FROM budget_reservations '
                    'WHERE request_id=?', (state.request_id,))['state'], 'forwarded')
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(state.recorded)
            self.assertTrue(state.budget_settled)
            self.assertEqual(db.query_one('SELECT state,charged_tokens FROM budget_reservations '
                'WHERE request_id=?', (state.request_id,))['state'], 'settled')
            self.assertEqual(db.query_one('SELECT prompt_tokens+completion_tokens n FROM request_logs '
                'WHERE request_id=?', (state.request_id,))['n'], 12)
            self.assertEqual(db.query_one('SELECT used_tokens FROM api_keys WHERE id=?',
                                        (key['id'],))['used_tokens'], 12)
        finally:
            release.set()
            obs.current.reset(token)

    async def test_middleware_keeps_byte_and_active_leases_during_slow_cancelled_write(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(5)
        async def app(scope, receive, send):
            await receive()
            await gateway._record_async()
        with patch.object(gateway, 'preauthorize_body', return_value=None), \
                patch.object(gateway, '_record', slow), \
                patch.dict(os.environ, {'WB_GATEWAY_QUEUE_WAIT_MS': '0'}):
            middleware = obs.RequestAuditMiddleware(app)
            task = asyncio.create_task(drive(middleware))
            try:
                await until(entered.is_set)
                held = middleware.inflight_body_bytes
                task.cancel()
                await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertEqual(middleware.active, 1)
                self.assertEqual(middleware.inflight_body_bytes, held)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual((middleware.active, middleware.inflight_body_bytes), (0, 0))
            finally:
                release.set()

    async def test_outcome_and_budget_finalization_run_off_loop(self):
        main = threading.get_ident()
        seen = []
        original = budget.finish
        def finish(state):
            seen.append(threading.get_ident())
            time.sleep(0.03)
            original(state)
        async def app(scope, receive, send):
            await receive()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        with patch.object(gateway, 'preauthorize_body', return_value=None), \
                patch.object(budget, 'finish', finish):
            task = asyncio.create_task(drive(obs.RequestAuditMiddleware(app)))
            await until(lambda: bool(seen))
            await asyncio.sleep(0.002)
            self.assertFalse(task.done())
            self.assertEqual((await task)[0]['status'], 200)
        self.assertTrue(all(thread != main for thread in seen))

    async def test_drain_timeout_reports_owned_writer_and_keeps_lifetime_lock(self):
        owner = budgetowner.identity()
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(5)
        task = asyncio.create_task(persistence.run(slow))
        try:
            await until(entered.is_set)
            started = time.monotonic()
            result = await drain.wait_until_drained(0.01, interval=0.002)
            self.assertFalse(result['drained'])
            self.assertGreaterEqual(result['remaining']['persistence'], 1)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertFalse(budgetowner.close_after_drain(result['drained']))
            self.assertIs(budgetowner.alive(owner), True)
            self.assertFalse(budgetowner.close_after_drain(True),
                             'a stale drained flag cannot release an active writer')
            from server import main, operational
            with patch.object(operational, 'readiness', return_value={'ok': False}):
                detail = main.readiness_detail({'username': 'fixture'})
            self.assertGreaterEqual(detail['persistence']['owned_calls'], 1)
            self.assertGreaterEqual(detail['drain_in_flight']['persistence'], 1)
            release.set()
            await task
            result = await drain.wait_until_drained(0.01)
            self.assertTrue(result['drained'])
            self.assertTrue(budgetowner.close_after_drain(result['drained']))
            self.assertIs(budgetowner.alive(owner), False)
        finally:
            release.set()
            await task

    async def test_slow_writer_and_rejection_flood_have_bounded_request_owners(self):
        release = threading.Event()
        handled = []
        original = budget.finish

        def slow_finish(state):
            release.wait(5)
            original(state)

        async def app(scope, receive, send):
            handled.append(scope['path'])
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        with patch.dict(os.environ, {'WB_GATEWAY_MAX_CONCURRENCY': '1',
                                     'WB_GATEWAY_QUEUE_DEPTH': '1'}), \
                patch.object(budget, 'finish', slow_finish):
            middleware = obs.RequestAuditMiddleware(app)
            self.assertEqual(middleware.lifetime_maximum, 16)
            owned = [asyncio.create_task(drive(middleware, path='/api/fixture', method='GET'))
                     for _ in range(middleware.lifetime_maximum)]
            try:
                await until(lambda: persistence.snapshot()['owned_calls'] == 16)
                self.assertEqual(persistence.snapshot()['submitted'], 12)
                self.assertEqual(persistence.snapshot()['waiting_for_slot'], 4)
                started = time.monotonic()
                replies = await asyncio.wait_for(asyncio.gather(*[
                    drive(middleware, path=('/v1/chat/completions', '/v1/messages',
                                            '/v1/responses')[i % 3]) for i in range(300)
                ]), timeout=1)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(middleware.lifetime_active, 16)
                self.assertEqual(persistence.snapshot()['owned_calls'], 16)
                self.assertEqual(len(handled), 16)
                self.assertEqual(middleware.lifetime_rejections, 300)
                for index, messages in enumerate(replies):
                    self.assertEqual(messages[0]['status'], 503)
                    self.assertIn((b'retry-after', b'1'), messages[0]['headers'])
                    payload = json.loads(messages[1]['body'])
                    self.assertEqual(payload['error']['retry_after_seconds'], 1)
                    self.assertEqual(payload['error']['code'], 'gateway_capacity_exceeded')
                    if index % 3 == 1:
                        self.assertEqual(payload['type'], 'error')
                        self.assertEqual(payload['error']['type'], 'api_error')
                # A public probe has no business fact to queue behind slow writes.
                probe = await asyncio.wait_for(drive(middleware, path='/readyz', method='GET'), 0.2)
                self.assertEqual(probe[0]['status'], 200)
                self.assertEqual(persistence.snapshot()['owned_calls'], 16)
            finally:
                release.set()
                await asyncio.gather(*owned)
            self.assertEqual(middleware.lifetime_active, 0)
            self.assertEqual(persistence.snapshot()['owned_calls'], 0)
            self.assertEqual(db.query_one('SELECT COUNT(*) n FROM request_outcomes')['n'], 16)

    async def test_cancellation_during_complete_response_parse_keeps_usage_in_all_protocols(self):
        from server import config
        from server.routers import anthropic, responses
        real_parse = httpx.Response.json
        for path, payload in (
            ('/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hello'}]}),
            ('/v1/messages', {'messages': [{'role': 'user', 'content': 'hello'}]}),
            ('/v1/responses', {'input': 'hello'}),
        ):
            with self.subTest(path=path):
                entered, release = threading.Event(), threading.Event()
                calls = []
                def slow_parse(response, *args, **kwargs):
                    if b'owned-parser-fixture' in response.content:
                        entered.set()
                        release.wait(5)
                    return real_parse(response, *args, **kwargs)
                async def provider(request):
                    calls.append(True)
                    return httpx.Response(200, json={
                        'id': 'owned-parser-fixture',
                        'choices': [{'message': {'role': 'assistant', 'content': 'hello'},
                                     'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 9, 'completion_tokens': 3}})
                key = keysvc.create_key('parse cancellation ' + path, quota=10000)
                app = FastAPI()
                for router in (gateway.router, anthropic.router, responses.router):
                    app.include_router(router)
                middleware = obs.RequestAuditMiddleware(app)
                with patch.object(config, 'http_client', lambda *args, **kwargs:
                        httpx.AsyncClient(transport=httpx.MockTransport(provider))), \
                        patch.object(httpx.Response, 'json', slow_parse):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware),
                                                 base_url='http://fixture') as client:
                        task = asyncio.create_task(client.post(path,
                            headers={'authorization': 'Bearer ' + key['key']},
                            json={'model': 'm', ('max_output_tokens' if path.endswith('responses')
                                                else 'max_tokens'): 32, **payload}))
                        try:
                            await until(lambda: entered.is_set() or task.done())
                            if not entered.is_set():
                                response = await task
                                self.fail(f'fixture bypassed response parse: {response.status_code} {response.text}')
                            task.cancel()
                            await asyncio.sleep(0.01)
                            task.cancel()
                            await asyncio.sleep(0.01)
                            self.assertFalse(task.done())
                            self.assertEqual(middleware.active, 1)
                            self.assertGreater(middleware.inflight_body_bytes, 0)
                            release.set()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        finally:
                            release.set()
                            if not task.done():
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(calls, [True], 'a completed response is never replayed')
                record = db.query_one('SELECT prompt_tokens,completion_tokens,terminal_state,status '
                                      'FROM request_logs WHERE key_id=?', (key['id'],))
                self.assertEqual(tuple(record), (9, 3, 'client_cancelled', 499))
                self.assertEqual(db.query_one('SELECT used_tokens FROM api_keys WHERE id=?',
                                             (key['id'],))['used_tokens'], 12)
                # DAN-23：取消发生在完整回执解析之后 —— 上游 usage 已把费用定死
                # （9+3 token 已经记进 used_tokens），预留随之结清，不再落成
                # 「待核对」。断言比原来更严：连 charged_tokens 一起钉住，
                # 既证明用量事实留住了，也证明它被当成确定费用而不是悬案。
                row = db.query_one('SELECT state,charged_tokens FROM budget_reservations '
                                   'WHERE key_id=?', (key['id'],))
                self.assertEqual((row['state'], row['charged_tokens']), ('settled', 12))
                self.assertEqual((middleware.active, middleware.inflight_body_bytes,
                                  middleware.lifetime_active), (0, 0, 0))

    async def test_incomplete_application_shutdown_retains_clients_coordinator_and_instance(self):
        from server import config, main, security
        drain.reset_for_tests()
        self.addCleanup(drain.reset_for_tests)
        runtime = SimpleNamespace(startup=AsyncMock(), shutdown=AsyncMock())
        steward = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        entered, release = threading.Event(), threading.Event()
        def slow_write():
            entered.set()
            release.wait(5)
        with patch.object(config, 'BACKGROUND_JOBS', False), \
                patch.object(config, 'SHARED_ADMISSION', False), \
                patch.object(config, 'DRAIN_TIMEOUT_SEC', 0.01), \
                patch.object(security, 'load_users', return_value={'role_policy_version': 2,
                    'users': [{'username': 'admin', 'role': 'admin'}]}), \
                patch('server.qtgcore.runtime.Runtime', return_value=runtime), \
                patch('server.steward_runtime.StewardRuntime', return_value=steward), \
                patch.object(config, 'close_clients', new_callable=AsyncMock) as clients:
            lifetime = main.lifespan(main.app)
            await lifetime.__aenter__()
            instance = main.app.state.instance_runtime
            owner = budgetowner.identity()
            writer = asyncio.create_task(persistence.run(slow_write))
            await until(entered.is_set)
            try:
                await asyncio.wait_for(lifetime.__aexit__(None, None, None), 1)
                self.assertFalse(main.app.state.shutdown_drain['drained'])
                self.assertGreaterEqual(main.app.state.shutdown_drain['remaining']['persistence'], 1)
                runtime.shutdown.assert_not_awaited()
                clients.assert_not_awaited()
                self.assertTrue(instance.held)
                self.assertIsNone(instance.release_ok)
                self.assertTrue(budgetowner.alive(owner))
                row = db.query_one('SELECT role,owner FROM instance_leases WHERE instance_id=?',
                                   (instance.identity['instance_id'],))
                self.assertNotEqual(row['role'], 'stopped')
                self.assertTrue(row['owner'])
                with tempfile.TemporaryDirectory() as directory:
                    policy = dict(active=8, large=2, bytes=1000000, preauth=8, queue=8,
                                  queue_per_key=4, key_default=4, key_quota=1)
                    coordinator = Coordinator(Path(directory) / 'admission.db', policy,
                                              owner=lambda: 'fixture', alive=lambda _: None)
                    async def ended(scope, receive, send):
                        return None
                    wrapper = obs.RequestAuditMiddleware(ended, shared=coordinator)
                    await wrapper({'type': 'lifespan'}, None, None)
                    self.assertTrue(coordinator.connections,
                                    'lifespan exit must not close a still-owned coordinator')
                    release.set()
                    await writer
                    await wrapper({'type': 'lifespan'}, None, None)
                    self.assertFalse(coordinator.connections)
            finally:
                release.set()
                await writer
                await instance.close()
                await runtime.shutdown()


class MemoryBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate_database(self)

    async def test_preparse_bound_covers_dense_and_wide_unicode_objects(self):
        values = [{'a': [1] * 10000}, {'a': [{'x': i} for i in range(3000)]},
                  {'a': 'x' * 10000 + '🙂'}, {'a': ['汉字', '🙂', '\\u0100', 'é'] * 1000}]
        for value in values:
            for ascii_only in (False, True):
                raw = json.dumps(value, ensure_ascii=ascii_only, separators=(',', ':')).encode()
                pieces = [raw[i:i + 7] for i in range(0, len(raw), 7)]
                self.assertGreaterEqual(memorybudget.json_allocation_bound(pieces),
                                        memorybudget.retained_json_bytes(json.loads(raw)))

    async def test_adapter_retains_source_and_transformed_graph_charges(self):
        source = {'parts': ['one' * 10000, 'two' * 10000]}
        adapted = {'model': 'm', 'text': ''.join(source['parts'])}
        state = obs.RequestAudit('adapter-graphs', '/v1/responses', 'POST', 'responses',
                                 time.monotonic())
        state.input_json_identity = id(source)
        state.input_json_bytes = memorybudget.retained_json_bytes(source)
        reservations = []
        state.reserve_decoded_body = reservations.append
        token = obs.current.set(state)
        try:
            with patch.object(gateway, '_prepare_forward', return_value=b'{}'), \
                    patch.object(budget, 'forwarded'):
                await gateway._prepare_forward_async({'id': 1}, adapted)
            self.assertGreaterEqual(reservations[0],
                memorybudget.retained_json_bytes(source) + memorybudget.retained_json_bytes(adapted))
            self.assertGreaterEqual(reservations[-1], reservations[0])
        finally:
            obs.current.reset(token)

    async def test_dense_request_is_rejected_before_object_graph_allocation(self):
        raw = b'{"a":[' + b'1,' * 20000 + b'1]}'
        parsed = []
        real_loads = json.loads
        def loads(value, *args, **kwargs):
            if value == raw:
                parsed.append(True)
            return real_loads(value, *args, **kwargs)
        async def app(scope, receive, send):
            body, error = await gateway._read_json_body(Request(scope, receive))
            self.assertIsNone(body)
            await error(scope, receive, send)
        with patch.dict(os.environ, {'WB_GATEWAY_MAX_INFLIGHT_BODY_BYTES': '262144',
                                     'WB_GATEWAY_BODY_PHYSICAL_FACTOR': '1'}), \
                patch.object(gateway, 'preauthorize_body', return_value=None), \
                patch.object(json, 'loads', loads):
            middleware = obs.RequestAuditMiddleware(app)
            messages = await drive(middleware, raw)
        self.assertEqual(messages[0]['status'], 503)
        self.assertEqual(parsed, [])
        self.assertEqual(middleware.inflight_body_bytes, 0)

    async def test_dense_provider_json_is_rejected_before_parse(self):
        response = httpx.Response(200, content=b'{"a":[' + b'1,' * 20000 + b'1]}')
        state = obs.RequestAudit('response-objects', '/', 'POST', 'openai', time.monotonic())
        def reserve(size):
            if size > 262144:
                raise obs.CapacityError('fixture memory ceiling')
        state.reserve_output_body = reserve
        token = obs.current.set(state)
        try:
            with patch.object(response, 'json', wraps=response.json) as parse:
                with self.assertRaises(OutputLimitExceeded):
                    await gateway._response_json_async(response)
                parse.assert_not_called()
        finally:
            obs.current.reset(token)

    async def test_actual_output_growth_overrides_token_hint_before_next_chunk(self):
        state = obs.RequestAudit('actual-output', '/', 'POST', 'openai', time.monotonic())
        reservations, delivered = [], []
        def reserve(size):
            reservations.append(size)
            if size > 6000:
                raise obs.CapacityError('fixture memory ceiling')
        state.reserve_output_body = reserve
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'a' * 500
                yield b'b' * 2000
        response = httpx.Response(200, stream=Stream())
        token = obs.current.set(state)
        try:
            with self.assertRaises(OutputLimitExceeded):
                async for chunk in deadline.decoded_bytes(response):
                    delivered.append(chunk)
        finally:
            await response.aclose()
            obs.current.reset(token)
        self.assertEqual(delivered, [b'a' * 500])
        self.assertEqual(reservations, [5096, 9096])

    async def test_shared_budget_charges_same_physical_bytes_as_local_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = dict(active=8, large=8, bytes=300000, preauth=8, queue=64,
                          queue_per_key=4, key_default=4, key_quota=1)
            a = Coordinator(Path(directory) / 'a.db', policy, owner=lambda: 'a', alive=lambda _: None)
            b = Coordinator(Path(directory) / 'a.db', policy, owner=lambda: 'b', alive=lambda _: None)
            started, release = asyncio.Event(), asyncio.Event()
            async def app(scope, receive, send):
                await receive()
                started.set()
                await release.wait()
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'ok'})
            with patch.dict(os.environ, {'WB_GATEWAY_MAX_INFLIGHT_BODY_BYTES': '300000',
                                         'WB_GATEWAY_BODY_PHYSICAL_FACTOR': '3',
                                         'WB_GATEWAY_QUEUE_WAIT_MS': '0'}), \
                    patch.object(gateway, 'preauthorize_body', return_value=None):
                first, second = obs.RequestAuditMiddleware(app, shared=a), obs.RequestAuditMiddleware(app, shared=b)
                task = asyncio.create_task(drive(first))
                try:
                    await started.wait()
                    self.assertEqual(a.snapshot()['body_bytes'], first.inflight_body_bytes)
                    self.assertEqual(a.snapshot()['body_bytes'], 3 * 65536)
                    self.assertEqual((await drive(second))[0]['status'], 503)
                finally:
                    release.set()
                    await task
                    a.close()
                    b.close()


@unittest.skipUnless(os.name == 'posix', 'resource migration verifies POSIX lifetime locks')
class AdmissionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / 'admission.db'
        self.owners = self.root / '.budget-owners'
        self.owners.mkdir()
        self.owner = socket.gethostname() + ':' + uuid.uuid4().hex
        self.lock = self.owners / self.owner.replace(':', '_')
        self.lock.write_bytes(b'1')
        self.policy = dict(active=2, large=1, bytes=500000, preauth=2, queue=4,
                           queue_per_key=1, key_default=2, key_quota=1)
        with patch('server.sharedadmission.RESPONSE_BUDGET_PROTOCOL', 1):
            self.old = Coordinator(self.database, self.policy, owner=lambda: self.owner,
                                   alive=lambda _: None)
        self.addCleanup(self.old.close)
        self.old.reserve('stale', 100, False)

    def test_candidate_fails_before_joining_old_cohort_and_inspection_is_readonly(self):
        with self.assertRaisesRegex(Unavailable, 'candidate cannot serve'):
            Coordinator(self.database, self.policy, owner=lambda: 'candidate', alive=lambda _: None)
        before = self.database.read_bytes()
        result = migrate(self.database, self.owners)
        self.assertFalse(result['applied'])
        self.assertEqual(result['stale_resource_leases'], 1)
        self.assertEqual(self.database.read_bytes(), before)

    def test_live_owner_blocks_migration_even_when_its_leases_are_empty(self):
        import fcntl
        self.old.release('stale')
        with self.lock.open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(Refused, 'live_owner'):
                migrate(self.database, self.owners, apply=True)
        self.assertEqual(json.loads(sqlite3.connect(self.database).execute(
            'SELECT value FROM admission_policy').fetchone()[0])['response_budget_protocol'], 1)

    def test_unknown_or_unreadable_owner_never_counts_as_dead(self):
        self.lock.unlink()
        with self.assertRaisesRegex(Refused, 'no_verifiable_lock'):
            migrate(self.database, self.owners, apply=True)

    def test_wrong_owner_directory_cannot_certify_an_idle_cohort(self):
        self.old.release('stale')
        unrelated = self.root / 'empty'
        unrelated.mkdir()
        with self.assertRaisesRegex(Refused, 'must_match'):
            migrate(self.database, unrelated, apply=True)

    def test_resource_only_migration_preserves_limits_and_fences_old_peer(self):
        result = migrate(self.database, self.owners, apply=True)
        self.assertTrue(result['applied'])
        self.assertFalse(result['business_database_changed'])
        new = Coordinator(self.database, self.policy, owner=lambda: 'new', alive=lambda _: None)
        self.addCleanup(new.close)
        self.assertEqual(new.policy, self.policy)
        self.assertEqual(new.snapshot()['leases'], 0)
        self.assertTrue(new.reserve('new-request', 100, False))
        with self.assertRaisesRegex(Unavailable, 'policy_changed'):
            self.old.reserve('old-request', 100, False)


if __name__ == '__main__':
    unittest.main()
