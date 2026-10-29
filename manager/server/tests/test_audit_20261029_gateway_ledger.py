"""Regression coverage for the six 2026-10-29 gateway/ledger findings.

All state is temporary. Every outbound request uses MockTransport; the default
client factory raises if a test forgets to install its mock.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import budget, budgetowner, config, db, deadline, keysvc, ledgerreplay
from server import observability, operational, platformpolicy
from server.routers import anthropic, gateway, responses


PROTOCOLS = ('chat/completions', 'messages', 'responses')
GOOD = {
    'id': 'mock-completion', 'object': 'chat.completion', 'model': 'm',
    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                 'finish_reason': 'stop'}],
    'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'credit': 0.01},
}


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0, started=None):
        self.chunks, self.delay, self.started = chunks, delay, started
        self.closed = False

    async def __aiter__(self):
        if self.started is not None:
            self.started.set()
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        await asyncio.sleep(0)
        self.closed = True


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)
        self.original_connection = db._conn
        budgetowner.close()
        db._conn = None
        self.stack.enter_context(mock.patch.multiple(
            config, DB_PATH=self.root / 'test.db', DATA_DIR=self.root,
            USERS_FILE=self.root / 'users.json', AUTH_DIR=self.root / 'auth',
            UPSTREAM_CONFIG=self.root / 'upstream.json',
            WB2API_BASE='http://workbuddy.mock', WB2API_KEY='mock-key', BASE_PATH=''))
        self.stack.enter_context(mock.patch.dict(os.environ, {
            'WB_LEDGER_SPOOL_DIR': str(self.root / 'spool'),
            'WB_KEY_BUDGET_PENDING': '50', 'WB_KEY_BUDGET_PENDING_QUOTA': '1',
            'WB_TOTAL_TIMEOUT_S': '3',
        }))
        self.stack.enter_context(mock.patch.object(
            config, 'http_client', side_effect=AssertionError('unmocked upstream I/O')))
        fake_key = self.root / 'fake-platform-key'
        fake_key.write_text('mock-platform-key', encoding='utf-8')
        self.stack.enter_context(mock.patch.object(gateway, '_THREEWAY_SPEC', {
            p: (f'http://{p}.mock', str(fake_key)) for p in ('qoder', 'trae')}))
        self.stack.enter_context(mock.patch.object(gateway, '_REJECTION_RETRY_DELAY_S', 0))
        self.stack.enter_context(mock.patch.object(gateway, '_rejection_retry_limit', return_value=2))
        self.stack.enter_context(mock.patch.object(gateway, 'RATE_MAX_PER_MIN', 0))
        db.connect()
        budget.initialize()
        app = FastAPI()
        for router in (gateway.router, anthropic.router, responses.router):
            app.include_router(router)
        app.add_middleware(observability.RequestAuditMiddleware)
        app.add_middleware(platformpolicy.PlatformRoutingMiddleware)
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        budgetowner.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = self.original_connection
        self.stack.close()

    def key(self, **kwargs):
        return keysvc.create_key(name='audit-regression', **kwargs)

    def payload(self, protocol, *, stream=False, model='m'):
        result = {'model': model, 'stream': stream}
        if protocol == 'responses':
            result.update(input='hi', max_output_tokens=8)
        else:
            result.update(messages=[{'role': 'user', 'content': 'hi'}], max_tokens=8)
        return result

    def post(self, protocol, *, key=None, prefix='', headers=None, **kwargs):
        key = key or self.key()
        auth = {'Authorization': 'Bearer ' + key['key'],
                'anthropic-version': '2023-06-01', **(headers or {})}
        return self.client.post(prefix + '/v1/' + protocol,
                                json=self.payload(protocol, **kwargs), headers=auth)

    def mock_transport(self, handler, clients=None):
        def factory(*args, **kwargs):
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            if clients is not None:
                clients.append(client)
            return client
        return mock.patch.object(config, 'http_client', factory)


class RouteIsolationTest(_Fixture):
    def test_three_protocols_three_platforms_and_selection_sources(self):
        hosts = []
        def respond(request):
            hosts.append(request.url.host)
            return httpx.Response(200, json=GOOD)
        with self.mock_transport(respond):
            for platform in ('workbuddy', 'qoder', 'trae'):
                for protocol in PROTOCOLS:
                    for source in ('path', 'header', 'model'):
                        with self.subTest(platform=platform, protocol=protocol, source=source):
                            key = self.key(platforms=[platform])
                            prefix = '/' + platform if source == 'path' else ''
                            headers = {'X-QTG-Platform': platform} if source == 'header' else {}
                            model = platform + '/m' if source == 'model' else 'm'
                            result = self.post(protocol, key=key, prefix=prefix,
                                               headers=headers, model=model)
                            self.assertEqual(result.status_code, 200, result.text)
                            self.assertEqual(hosts[-1], platform + '.mock')
                            row = db.query_one('SELECT actual_platform,upstream_ref FROM request_logs '
                                               'WHERE key_id=?', (key['id'],))
                            self.assertEqual(row['actual_platform'], platform)
                            self.assertIn(platform + '.mock', row['upstream_ref'])

    def test_platform_and_model_scope_conflicts_fail_before_io(self):
        for protocol in PROTOCOLS:
            key = self.key(platforms=['qoder'], models=['m'])
            for params, status in (({}, 403),
                                   ({'prefix': '/qoder', 'headers': {'X-QTG-Platform': 'trae'}}, 400),
                                   ({'prefix': '/qoder', 'model': 'trae/m'}, 400),
                                   ({'prefix': '/qoder', 'model': 'forbidden'}, 400)):
                with self.subTest(protocol=protocol, params=params):
                    result = self.post(protocol, key=key, **params)
                    self.assertEqual(result.status_code, status, result.text)

    def test_missing_or_empty_platform_credentials_never_fall_back(self):
        empty = self.root / 'empty-key'
        empty.write_text('', encoding='utf-8')
        for keyfile in (self.root / 'missing-key', empty):
            with mock.patch.object(gateway, '_THREEWAY_SPEC', {
                    'qoder': ('http://qoder.mock', str(keyfile))}):
                for protocol in PROTOCOLS:
                    result = self.post(protocol, key=self.key(platforms=['qoder']), prefix='/qoder')
                    self.assertEqual(result.status_code, 503, result.text)


class FeeAndValidationTest(_Fixture):
    def test_usage_cache_evidence_survives_success_and_error_paths(self):
        reported = {**GOOD['usage'], 'prompt_cache_hit_tokens': 2,
                    'prompt_cache_miss_tokens': 1, 'prompt_cache_write_tokens': 0}
        for protocol in PROTOCOLS:
            for scenario in ('success', 'invalid_success', 'error', 'stream_error', 'stream_success'):
                with self.subTest(protocol=protocol, scenario=scenario):
                    key = self.key()
                    status = 503 if 'error' in scenario else 200
                    payload = ({'error': {'code': 'no_healthy_account'}, 'usage': reported}
                               if status == 503 else {'usage': reported}
                               if scenario == 'invalid_success' else {**GOOD, 'usage': reported})
                    if scenario == 'stream_success':
                        frames = [
                            {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'ok'},
                                          'finish_reason': None}]},
                            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                             'usage': reported},
                        ]
                        raw = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
                        raw += 'data: [DONE]\n\n'
                        def respond(request):
                            return httpx.Response(200, content=raw,
                                                  headers={'content-type': 'text/event-stream'})
                    else:
                        def respond(request):
                            return httpx.Response(status, json=payload)
                    with self.mock_transport(respond):
                        response = self.post(protocol, key=key, stream=scenario.startswith('stream_'))
                    expected = 502 if scenario == 'invalid_success' else status
                    self.assertEqual(response.status_code, expected, response.text)
                    log = db.query_one('SELECT http_status,cache_hit_tokens,cache_miss_tokens,'
                                       'cache_write_tokens,credit,usage_source FROM request_logs '
                                       'WHERE key_id=?', (key['id'],))
                    self.assertEqual(tuple(log), (expected, 2, 1, 0, 0.01, 'upstream'))
                    self.assertEqual(db.query_one('SELECT used_credit_units FROM api_keys WHERE id=?',
                                                   (key['id'],))[0], 10000)

    def test_reported_error_usage_is_not_retried_and_is_kept_for_every_protocol(self):
        calls = []
        body = {'error': {'code': 'no_healthy_account'},
                'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'credit': 1.25}}
        def respond(request):
            calls.append(request)
            return httpx.Response(503, json=body)
        with self.mock_transport(respond):
            for protocol in PROTOCOLS:
                for stream in (False, True):
                    with self.subTest(protocol=protocol, stream=stream):
                        key = self.key()
                        before = len(calls)
                        response = self.post(protocol, key=key, stream=stream)
                        self.assertEqual(response.status_code, 503, response.text)
                        self.assertEqual(len(calls) - before, 1)
                        row = db.query_one('SELECT used_tokens,used_credit_units FROM api_keys WHERE id=?',
                                           (key['id'],))
                        self.assertEqual(tuple(row), (110, 1250000))
                        log = db.query_one('SELECT credit,usage_source FROM request_logs WHERE key_id=?',
                                           (key['id'],))
                        self.assertEqual(tuple(log), (1.25, 'upstream'))
                        reservation = db.query_one('SELECT state,charged_credit FROM budget_reservations '
                                                   'WHERE key_id=?', (key['id'],))
                        self.assertEqual(tuple(reservation), ('unknown', 1.25))

    def test_classifier_requires_structured_rejection_and_no_processing_evidence(self):
        base = {'error': {'code': 'no_healthy_account'}}
        evidence = ({'usage': {}}, {'usage': {'credit': 0}}, {'usage': {'prompt_tokens': 1}},
                    {'choices': []}, {'partial_output': 'x'}, {'processed': True},
                    {'credit': 1}, {'attempts': [{'code': 'accepted'}]},
                    {'diagnostic': {'usage': {'credit': 1}}},
                    {'diagnostic': json.dumps({'usage': {'credit': 1}})})
        for addition in evidence:
            self.assertFalse(gateway._is_not_processed_rejection(503, json.dumps({**base, **addition})))
        self.assertFalse(gateway._is_not_processed_rejection(503, json.dumps({
            'error': {'code': 'connection_lost'}, 'model': 'no_healthy_account_demo'})))
        self.assertFalse(gateway._is_not_processed_rejection(503, '<html>no_healthy_account</html>'))
        self.assertTrue(gateway._is_not_processed_rejection(503, json.dumps(base)))

    def test_invalid_success_is_502_in_response_log_and_outcome(self):
        invalid = (b'<html>bad proxy</html>', b'null', b'[]', b'{}', b'{"choices":[]}',
                   b'{"choices":[{"message":{}}]}')
        for protocol in PROTOCOLS:
            for raw in invalid:
                with self.subTest(protocol=protocol, raw=raw), self.mock_transport(
                        lambda request: httpx.Response(200, content=raw)):
                    key = self.key()
                    response = self.post(protocol, key=key)
                    self.assertEqual(response.status_code, 502, response.text)
                    log = db.query_one('SELECT request_id,status,terminal_state,credit FROM request_logs '
                                       'WHERE key_id=?', (key['id'],))
                    self.assertEqual((log['status'], log['terminal_state'], log['credit']),
                                     (502, 'protocol_incomplete', None))
                    outcome = db.query_one('SELECT http_status,terminal_state FROM request_outcomes '
                                           'WHERE request_id=?', (log['request_id'],))
                    self.assertEqual(tuple(outcome), (502, 'protocol_incomplete'))
                    self.assertEqual(budget.unresolved(key['id'])[0]['state'], 'unknown')

    def test_invalid_success_and_converter_failure_preserve_reported_usage(self):
        for protocol in PROTOCOLS:
            key = self.key()
            with self.mock_transport(lambda request: httpx.Response(200, json={'usage': GOOD['usage']})):
                response = self.post(protocol, key=key)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(db.query_one('SELECT used_credit_units FROM api_keys WHERE id=?',
                                          (key['id'],))[0], 10000)
        for protocol, module, name in (('messages', anthropic, 'to_anthropic_response'),
                                       ('responses', responses, 'to_responses_object')):
            key = self.key()
            with self.mock_transport(lambda request: httpx.Response(200, json=GOOD)), \
                    mock.patch.object(module, name, side_effect=ValueError('malformed tool arguments')):
                response = self.post(protocol, key=key)
            self.assertEqual(response.status_code, 502)
            row = db.query_one('SELECT status,terminal_state,credit FROM request_logs WHERE key_id=?',
                               (key['id'],))
            self.assertEqual(tuple(row), (502, 'protocol_incomplete', 0.01))

    def test_valid_tool_only_and_empty_text_responses_stay_supported(self):
        for message in ({'role': 'assistant', 'content': ''},
                        {'role': 'assistant', 'content': None, 'tool_calls': [
                            {'id': 'c1', 'type': 'function',
                             'function': {'name': 'f', 'arguments': '{}'}}]}):
            body = {**GOOD, 'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop'}]}
            with self.mock_transport(lambda request: httpx.Response(200, json=body)):
                for protocol in PROTOCOLS:
                    response = self.post(protocol)
                    self.assertEqual(response.status_code, 200, response.text)


class ErrorLifetimeTest(_Fixture):
    def test_slow_error_bodies_share_total_deadline_and_close(self):
        for protocol in PROTOCOLS:
            clients, streams = [], []
            def respond(request):
                stream = _Stream([b'x'] * 100, delay=0.01)
                streams.append(stream)
                return httpx.Response(503, stream=stream)
            with self.mock_transport(respond, clients), mock.patch.dict(os.environ, {'WB_TOTAL_TIMEOUT_S': '0.06'}):
                started = time.monotonic()
                result = self.post(protocol, stream=True)
            self.assertEqual(result.status_code, 502, result.text)
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(all(c.is_closed for c in clients))
            self.assertTrue(all(s.closed for s in streams))
            row = db.query_one('SELECT terminal_state FROM request_logs ORDER BY id DESC LIMIT 1')
            self.assertEqual(row[0], 'transport_error')

    def test_oversized_error_body_is_bounded_and_never_retried(self):
        for protocol in PROTOCOLS:
            streams, clients = [], []
            def respond(request):
                stream = _Stream([b'x' * 65536, b'y'])
                streams.append(stream)
                return httpx.Response(503, stream=stream)
            with self.mock_transport(respond, clients):
                result = self.post(protocol, stream=True)
            self.assertEqual(result.status_code, 502, result.text)
            self.assertEqual(len(streams), 1)
            self.assertTrue(streams[0].closed)
            self.assertTrue(clients[0].is_closed)

    def test_cancellation_during_headers_and_error_body_closes_every_protocol(self):
        async def scenario(protocol, phase):
            started = asyncio.Event()
            clients, streams = [], []
            async def respond(request):
                if phase == 'headers':
                    started.set()
                    await asyncio.sleep(10)
                stream = _Stream([b'x'], delay=10, started=started)
                streams.append(stream)
                return httpx.Response(503, stream=stream)
            key = self.key()
            with self.mock_transport(respond, clients):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                              base_url='http://testserver') as client:
                    task = asyncio.create_task(client.post('/v1/' + protocol,
                        json=self.payload(protocol, stream=True),
                        headers={'Authorization': 'Bearer ' + key['key'],
                                 'anthropic-version': '2023-06-01'}))
                    await asyncio.wait_for(started.wait(), 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            self.assertTrue(clients[0].is_closed)
            self.assertTrue(all(s.closed for s in streams))
            row = db.query_one('SELECT terminal_state FROM request_logs WHERE key_id=?', (key['id'],))
            self.assertEqual(row[0], 'client_cancelled')
            self.assertEqual(budget.unresolved(key['id'])[0]['state'], 'unknown')
        for protocol in PROTOCOLS:
            for phase in ('headers', 'body'):
                with self.subTest(protocol=protocol, phase=phase):
                    asyncio.run(scenario(protocol, phase))

    def test_cleanup_is_bounded_when_upstream_close_hangs(self):
        closed = []
        class Slow:
            async def aclose(self):
                closed.append('response')
                await asyncio.sleep(10)
        class Client:
            async def aclose(self):
                closed.append('client')
        started = time.monotonic()
        asyncio.run(gateway._close_pending_response(Slow(), Client(), deadline.Budgets(client_cancel_s=0.01)))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(closed, ['response', 'client'])

    def test_error_body_read_failure_is_not_retried_and_still_closes(self):
        for protocol in PROTOCOLS:
            clients, streams = [], []
            class Broken(_Stream):
                async def __aiter__(self):
                    yield b'{"error":'
                    raise httpx.ReadError('peer disconnected')
            def respond(request):
                stream = Broken([])
                streams.append(stream)
                return httpx.Response(503, stream=stream)
            with self.mock_transport(respond, clients):
                result = self.post(protocol, stream=True)
            self.assertGreaterEqual(result.status_code, 400)
            self.assertEqual(len(streams), 1)
            self.assertTrue(streams[0].closed)
            self.assertTrue(clients[0].is_closed)

    def test_safe_retry_stays_bounded_and_leaves_success_sse_unchanged(self):
        frames = (b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
                  b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                  b'"usage":{"prompt_tokens":1,"completion_tokens":1,"credit":0.01}}\n\n'
                  b'data: [DONE]\n\n')
        clients, streams = [], []
        def respond(request):
            if not streams:
                content = json.dumps({'error': {'code': 'no_healthy_account'}}).encode()
                stream = _Stream([content])
                status = 503
            else:
                stream, status = _Stream([frames]), 200
            streams.append(stream)
            return httpx.Response(status, stream=stream, headers={'content-type': 'text/event-stream'})
        with self.mock_transport(respond, clients):
            result = self.post('chat/completions', stream=True)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content, frames)
        self.assertEqual(len(streams), 2)
        self.assertTrue(all(s.closed for s in streams))
        self.assertTrue(all(c.is_closed for c in clients))

    def test_safe_retry_sleep_cannot_extend_request_total_budget(self):
        clients, calls = [], []
        def respond(request):
            calls.append(request)
            return httpx.Response(503, json={'error': {'code': 'no_healthy_account'}})
        for stream in (False, True):
            calls.clear()
            with self.mock_transport(respond, clients), \
                    mock.patch.object(gateway, '_REJECTION_RETRY_DELAY_S', 10), \
                    mock.patch.dict(os.environ, {'WB_TOTAL_TIMEOUT_S': '0.03'}):
                started = time.monotonic()
                result = self.post('chat/completions', stream=stream)
            self.assertEqual(result.status_code, 502)
            self.assertEqual(len(calls), 1)
            self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(all(c.is_closed for c in clients))


class AggregationAndUnknownScanTest(_Fixture):
    def test_operational_zero_reconcile_scans_all_rows_without_erasing_usage(self):
        now, key = int(time.time()), self.key()
        fields = ('reserved_tokens', 'reserved_credit', 'charged_tokens', 'charged_credit')
        self._seed(key, 1005, now)
        for index, field in enumerate(fields):
            db.execute('UPDATE budget_reservations SET reserved_tokens=0,' + field + '=1 '
                       'WHERE request_id=?', (f'{key["id"]}-r{index:05d}',))
        zero_id = f'{key["id"]}-r01004'
        db.execute('UPDATE budget_reservations SET reserved_tokens=0 WHERE request_id=?', (zero_id,))
        db.execute('UPDATE api_keys SET used_tokens=77,used_credit_units=1234000,used_credit=1.234 '
                   'WHERE id=?', (key['id'],))
        with mock.patch.object(budget, 'resolve', wraps=budget.resolve) as resolve:
            self.assertEqual(operational.reconcile_zero_reservation_unknowns(now), 1)
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual(resolve.call_args.args[0], zero_id)
            self.assertIs(resolve.call_args.kwargs['zero_only'], True)
        self.assertEqual(operational.reconcile_zero_reservation_unknowns(now), 0)
        for index, field in enumerate(fields):
            row = db.query_one('SELECT state,' + field + ' FROM budget_reservations WHERE request_id=?',
                               (f'{key["id"]}-r{index:05d}',))
            self.assertEqual(tuple(row), ('unknown', 1))
        self.assertEqual(db.query_one('SELECT count(*) FROM budget_reservations '
                                      'WHERE state=\'unknown\'')[0], 1004)
        self.assertEqual(tuple(db.query_one('SELECT used_tokens,used_credit_units FROM api_keys '
                                            'WHERE id=?', (key['id'],))), (77, 1234000))
        self.assertEqual(tuple(db.query_one('SELECT token_delta,credit_delta FROM budget_adjustments '
                                            'WHERE request_id=?', (zero_id,))), (0, 0))

    def test_replayed_receipt_uses_event_bucket_without_moving_last_use_backwards(self):
        key = self.key()
        event_ts = 946684800
        latest = int(time.time())
        db.execute('UPDATE api_keys SET last_used_at=? WHERE id=?', (latest, key['id']))
        facts = dict(ts=event_ts, key_id=key['id'], ip='127.0.0.1', model='m', mapped_model='m',
                     status=200, prompt_tokens=3, completion_tokens=2, latency_ms=1,
                     ua='audit-test', error=None, stream=0, credit=0.01, realm='cn',
                     request_id='old-receipt', terminal_state='completed', usage_source='upstream',
                     usage_complete=1)
        entry = ledgerreplay.build_entry(facts)
        self.assertEqual(ledgerreplay.apply(entry, 'old-receipt'), 'applied')
        self.assertEqual(ledgerreplay.apply(entry, 'old-receipt'), 'duplicate')
        daily = db.query_one('SELECT day,requests,credit_units FROM usage_daily')
        hourly = db.query_one('SELECT day,hour,requests,credit_units FROM usage_hourly')
        self.assertEqual(tuple(daily), (db.day_of(event_ts), 1, 10000))
        self.assertEqual(tuple(hourly), (db.day_of(event_ts), db.hour_of(event_ts), 1, 10000))
        usage = db.query_one('SELECT used_tokens,used_credit_units,last_used_at FROM api_keys WHERE id=?',
                             (key['id'],))
        self.assertEqual(tuple(usage), (5, 10000, latest))

    def test_aggregate_samples_clock_once_and_uses_same_hour_for_integer_credit(self):
        key = self.key()
        with mock.patch.object(db.time, 'time', side_effect=[946684800, 946688400]) as clock, \
                mock.patch.object(db, 'hour_of', wraps=db.hour_of) as hour:
            db.bump_usage(key['id'], 'm', 1, 2, 0.25)
        self.assertEqual(clock.call_count, 1)
        self.assertEqual(hour.call_count, 1)
        self.assertEqual(db.query_one('SELECT credit_units FROM usage_hourly')[0], 250000)

    def _seed(self, key, count, now, *, archived=False):
        with db.transaction() as connection:
            connection.executemany(
                'INSERT INTO budget_reservations(request_id,key_id,created_at,state,reserved_tokens,'
                'updated_at,resolution) VALUES(?,?,?,\'unknown\',10,?,?)',
                [(f'{key["id"]}-r{i:05d}', key['id'], now - 9 * 86400, now - 9 * 86400,
                  budget.ARCHIVE_RESOLUTION_PREFIX + 'test' if archived else None) for i in range(count)])

    def test_summary_counts_beyond_first_page_but_keeps_payload_bounded(self):
        now, key = int(time.time()), self.key()
        self._seed(key, 2501, now, archived=True)
        last_id = f'{key["id"]}-r02500'
        db.execute('UPDATE budget_reservations SET resolution=NULL WHERE request_id=?', (last_id,))
        result = budget.unresolved_summary(key['id'], now=now)
        self.assertEqual(len(result['items']), 1000)
        self.assertTrue(result['items_truncated'])
        self.assertEqual(result['summary']['total'], 2501)
        self.assertEqual(result['summary']['archived_pending'], 2500)
        self.assertEqual(result['summary']['needs_human_old'], 1)
        self.assertEqual(result['summary']['token_lower_bound'], 25010)
        self.assertEqual(budget.archive_overdue_unknowns(now), 1)
        self.assertEqual(budget.archive_overdue_unknowns(now), 0)
        self.assertEqual(db.query_one('SELECT count(*) FROM budget_reservations WHERE state=\'unknown\'')[0], 2501)

    def test_keyset_does_not_skip_when_rows_settle_and_holds_no_transaction_between_batches(self):
        now, key = int(time.time()), self.key()
        self._seed(key, 1001, now)
        seen = []
        for rows in budget.unresolved_batches(key['id'], batch_size=137):
            self.assertFalse(db.connect().in_transaction)
            seen.extend(row['request_id'] for row in rows)
            with db.transaction():
                for row in rows:
                    db.execute('UPDATE budget_reservations SET state=\'settled\' WHERE request_id=?',
                               (row['request_id'],))
        self.assertEqual(len(seen), 1001)
        self.assertEqual(len(set(seen)), 1001)

    def test_batch_scan_finishes_at_initial_ceiling_and_filters_key(self):
        now, key, other = int(time.time()), self.key(), self.key()
        self._seed(key, 5, now)
        self._seed(other, 2, now)
        batches = budget.unresolved_batches(key['id'], batch_size=2)
        seen = next(batches)
        db.execute('INSERT INTO budget_reservations(request_id,key_id,created_at,state,updated_at) '
                   'VALUES(?,?,?,\'unknown\',?)', ('new-after-scan', key['id'], now, now))
        for rows in batches:
            seen.extend(rows)
        self.assertEqual(len(seen), 5)
        self.assertEqual({r['key_id'] for r in seen}, {key['id']})
        self.assertEqual(budget.unresolved_summary(key['id'])['summary']['total'], 6)

    def test_evidenced_reconciliation_reaches_after_1000_archived_rows(self):
        now, key = int(time.time()), self.key()
        self._seed(key, 1001, now, archived=True)
        request_id = f'{key["id"]}-r01000'
        db.add_request_log(ts=now, key_id=key['id'], ip='127.0.0.1', model='m', mapped_model='m',
                           status=503, prompt_tokens=0, completion_tokens=0, latency_ms=1, ua='test',
                           error='{"error":{"code":"no_healthy_account"}}', stream=0,
                           request_id=request_id, usage_source='unknown', terminal_state='upstream_error')
        self.assertEqual(budget.reconcile_evidenced_unknowns(now), 1)
        self.assertEqual(db.query_one('SELECT state FROM budget_reservations WHERE request_id=?',
                                     (request_id,))[0], 'settled')
        self.assertEqual(db.query_one('SELECT used_credit_units FROM api_keys WHERE id=?', (key['id'],))[0], 0)


if __name__ == '__main__':
    unittest.main()
