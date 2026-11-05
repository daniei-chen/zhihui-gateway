"""Wire and decoded response budgets apply before buffering, including SSE."""
import asyncio
import gzip
import json
import tracemalloc
import unittest
import zlib
from unittest.mock import patch

import httpx

from server import config
from server.upstreambody import BoundedTransport, ResponseTooLarge


class Pieces(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = 0
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed += 1


class ResponseBodyTests(unittest.IsolatedAsyncioTestCase):
    def client(self, stream, *, maximum=1024, headers=None):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, stream=stream, headers=headers))
        return httpx.AsyncClient(transport=BoundedTransport(transport, maximum),
                                 base_url='http://fixture')

    async def test_declared_oversize_body_is_rejected_before_read(self):
        stream = Pieces([b'x'])
        async with self.client(stream, headers={'content-length': '1025'}) as client:
            with self.assertRaises(ResponseTooLarge):
                await client.get('/')
        self.assertEqual(stream.reads, 0)
        self.assertEqual(stream.closed, 1)

    async def test_chunked_body_and_sse_have_a_cumulative_budget(self):
        for content_type in ('application/json', 'text/event-stream'):
            stream = Pieces([b'x' * 64] * 30)
            async with self.client(stream, headers={'content-type': content_type}) as client:
                with self.assertRaises(ResponseTooLarge):
                    async with client.stream('GET', '/') as response:
                        async for _ in response.aiter_bytes():
                            pass
            self.assertEqual(stream.reads, 17)
            self.assertEqual(stream.closed, 1)

    async def test_gzip_expansion_is_bounded_before_allocation(self):
        wire = gzip.compress(b'a' * (8 * 1024**2))
        stream = Pieces([wire])
        async with self.client(stream, maximum=65536,
                               headers={'content-encoding': 'gzip'}) as client:
            tracemalloc.start()
            try:
                with self.assertRaises(ResponseTooLarge):
                    await client.get('/')
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertLess(peak, 1024**2)
        self.assertEqual(stream.closed, 1)

    async def test_valid_compression_split_headers_and_identity_preserve_bytes(self):
        original = '中文🙂 sample'.encode() * 10
        raw_deflate = zlib.compress(original)[2:-4]
        for coding, wire in (('', original), ('gzip', gzip.compress(original)),
                             ('deflate', zlib.compress(original)), ('deflate', raw_deflate),
                             ('gzip, deflate', zlib.compress(gzip.compress(original)))):
            with self.subTest(coding=coding):
                stream = Pieces([wire[i:i+1] for i in range(len(wire))])
                async with self.client(stream, headers={'content-encoding': coding,
                                      'content-length': str(len(wire))}) as client:
                    response = await client.get('/')
                self.assertEqual(response.content, original)
                self.assertEqual(stream.closed, 1)

    async def test_incomplete_or_unsupported_compression_is_not_success(self):
        for coding, wire in (('gzip', gzip.compress(b'hello')[:-3]),
                             ('gzip', b'invalid'), ('br', b'unexpected')):
            with self.subTest(coding=coding):
                stream = Pieces([wire])
                async with self.client(stream, headers={'content-encoding': coding}) as client:
                    with self.assertRaises(httpx.DecodingError):
                        await client.get('/')
                self.assertEqual(stream.closed, 1)

    async def test_shared_config_client_uses_guard_and_requests_identity(self):
        stream = Pieces([b'x' * 100000] * 11)
        seen = []
        def handle(request):
            seen.append(request.headers['accept-encoding'])
            return httpx.Response(200, stream=stream)
        with patch.object(config, '_build_transport', return_value=httpx.MockTransport(handle)), \
                patch.object(config, 'GATEWAY_MAX_RESPONSE_MB', 1):
            async with config.http_client(5, proxy='', lane='audit-response', bounded_response=True) as client:
                with self.assertRaises(ResponseTooLarge):
                    await client.get('http://fixture')
        self.assertEqual(seen, ['identity'])
        self.assertEqual(stream.closed, 1)
        await config.close_clients()

    async def test_consumer_cancel_closes_owned_stream(self):
        waiting = asyncio.Event()
        class Waiting(Pieces):
            async def __aiter__(self):
                waiting.set()
                await asyncio.Event().wait()
                yield b''
        stream = Waiting([])
        async with self.client(stream) as client:
            task = asyncio.create_task(client.get('/'))
            await waiting.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(stream.closed, 1)

    async def test_immediately_ready_empty_frames_do_not_starve_cancellation(self):
        stream = Pieces([b''] * 100000)
        async with self.client(stream) as client:
            task = asyncio.create_task(client.get('/'))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertLess(stream.reads, 100000)
        self.assertEqual(stream.closed, 1)

    async def test_management_downloads_keep_their_independent_response_policy(self):
        stream = Pieces([b'x' * 100000] * 11)
        seen = []
        def handle(request):
            seen.append(request.headers['accept-encoding'])
            return httpx.Response(200, stream=stream)
        with patch.object(config, '_build_transport', return_value=httpx.MockTransport(handle)), \
                patch.object(config, 'GATEWAY_MAX_RESPONSE_MB', 1):
            async with config.http_client(5, proxy='', lane='audit-management') as client:
                self.assertEqual(len((await client.get('http://fixture')).content), 1100000)
        self.assertNotEqual(seen, ['identity'])
        self.assertEqual(stream.closed, 1)
        await config.close_clients()

    async def test_concurrent_requests_share_transport_but_not_byte_counters(self):
        streams = []
        def handle(request):
            stream = Pieces([b'x' * 800])
            streams.append(stream)
            return httpx.Response(200, stream=stream)
        async with httpx.AsyncClient(transport=BoundedTransport(
                httpx.MockTransport(handle), 1024)) as client:
            replies = await asyncio.gather(*(client.get('http://fixture') for _ in range(10)))
        self.assertTrue(all(response.content == b'x' * 800 for response in replies))
        self.assertTrue(all(stream.closed == 1 for stream in streams))

    async def test_repeated_encoding_headers_and_exact_decoded_limit(self):
        body = b'x' * 65536
        wire = gzip.compress(gzip.compress(body))
        stream = Pieces([wire])
        async with self.client(stream, maximum=len(body), headers=[
                ('content-encoding', 'gzip'), ('content-encoding', 'gzip')]) as client:
            self.assertEqual((await client.get('/')).content, body)
        self.assertEqual(stream.closed, 1)

    async def test_trailing_compressed_bytes_are_rejected_not_ignored(self):
        for tail in (b'junk', gzip.compress(b'second member')):
            stream = Pieces([gzip.compress(b'first member') + tail])
            async with self.client(stream, headers={'content-encoding': 'gzip'}) as client:
                with self.assertRaises(httpx.DecodingError):
                    await client.get('/')
            self.assertEqual(stream.closed, 1)


class ResponseConfigurationTests(unittest.TestCase):
    def test_budget_is_known_bounded_and_cannot_be_disabled(self):
        from server.configcontract import ConfigurationError, validate_environment
        row = validate_environment({'WB_GATEWAY_MAX_RESPONSE_MB': '64'})['settings'][
            'WB_GATEWAY_MAX_RESPONSE_MB']
        self.assertEqual((row['value'], row['unit']), (64, 'MiB'))
        for bad in ('0', '-1', '513', 'infinite'):
            with self.subTest(bad=bad), self.assertRaises(ConfigurationError):
                validate_environment({'WB_GATEWAY_MAX_RESPONSE_MB': bad})


class PlaygroundResponseBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonstream_oversize_returns_502_records_failure_and_closes(self):
        from fastapi import HTTPException
        from starlette.requests import Request
        from server import db
        from server.routers import playground
        from server.tests.authorization_test_support import isolated_data
        with isolated_data():
            stream = Pieces([b'x' * 100000] * 11)
            transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
            request = Request({'type': 'http', 'path': '/api/playground/chat',
                'headers': [], 'client': ('127.0.0.1', 1), 'server': ('fixture', 80),
                'scheme': 'http', 'query_string': b''})
            body = playground.ChatIn(model='fixture', stream=False,
                                     messages=[{'role': 'user', 'content': 'synthetic'}])
            with patch.object(config, '_build_transport', return_value=transport), \
                    patch.object(config, 'GATEWAY_MAX_RESPONSE_MB', 1), \
                    patch.object(playground.upstreamsvc, 'default_upstream',
                                 return_value={'base_url': 'http://fixture', 'name': 'workbuddy'}):
                with self.assertRaises(HTTPException) as caught:
                    await playground.chat(body, request, user={'username': 'admin'})
            self.assertEqual(caught.exception.status_code, 502)
            row = db.query_one('SELECT status,terminal_state,usage_complete FROM request_logs '
                               'ORDER BY id DESC LIMIT 1')
            self.assertEqual(tuple(row), (502, 'upstream_error', 0))
            self.assertEqual(stream.closed, 1)
            await config.close_clients()


from server.tests.test_request_identity import _HttpCase
from server import db


class ResponseBudgetLedgerTests(_HttpCase):
    def test_all_protocols_keep_exceeded_generation_unknown_and_close_transport(self):
        for path in ('/v1/chat/completions', '/v1/messages', '/v1/responses'):
            for streaming in (False, True):
                with self.subTest(path=path, streaming=streaming):
                    key = self.new_key('response-budget', quota=100000)
                    if streaming:
                        chunk = ('data: ' + json.dumps({'choices': [
                            {'delta': {'content': 'x' * 4096}}]}) + '\n\n').encode()
                        chunks = [chunk] * 300 + [b'data: [DONE]\n\n']
                    else:
                        raw = json.dumps({'choices': [{'message': {'role': 'assistant',
                              'content': 'x' * 1100000}, 'finish_reason': 'stop'}],
                              'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}).encode()
                        chunks = [raw[i:i+65536] for i in range(0, len(raw), 65536)]
                    stream = Pieces(chunks)
                    upstream = httpx.MockTransport(lambda request: httpx.Response(200,
                        headers={'content-type': 'text/event-stream' if streaming else 'application/json'},
                        stream=stream))
                    payload = {'model': 'fake-model', 'stream': streaming, 'max_tokens': 32}
                    if path.endswith('/responses'):
                        payload['input'] = 'synthetic'
                        payload['max_output_tokens'] = payload.pop('max_tokens')
                    else:
                        payload['messages'] = [{'role': 'user', 'content': 'synthetic'}]
                    with patch.object(config, '_build_transport', return_value=upstream), \
                            patch.object(config, 'GATEWAY_MAX_RESPONSE_MB', 1):
                        response = self.client.post(path, json=payload,
                            headers={'Authorization': 'Bearer ' + key['key']})
                    self.assertEqual(response.status_code, 200 if streaming else 502, response.text[:300])
                    rid = response.headers['x-request-id']
                    outcome = db.query_one('SELECT terminal_state FROM request_outcomes WHERE request_id=?',
                                           (rid,))
                    budget = db.query_one('SELECT state FROM budget_reservations WHERE request_id=?', (rid,))
                    self.assertNotEqual(outcome['terminal_state'], 'completed')
                    self.assertEqual(budget['state'], 'unknown')
                    self.assertEqual(stream.closed, 1)
                    if streaming:
                        self.assertTrue('error' in response.text)
                        self.assertFalse('response.completed' in response.text)
                        self.assertFalse('event: message_delta' in response.text,
                                         'An error must not invent a successful end_turn/tool_use')
