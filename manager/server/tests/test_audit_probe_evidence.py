"""A credential probe needs bounded protocol evidence, not just HTTP bytes."""
from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest import mock

import httpx

from server import config
from server.services import tencent


def event(value):
    return ('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode()


SUCCESS = event({'choices': [{'delta': {'content': '好'}}]})
AUTH = {'access_token': 'synthetic-only', 'uid': 'synthetic-user', 'realm': 'cn'}


class ProbeStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, block=False, reject_tail=False):
        self.chunks = chunks
        self.block = block
        self.reject_tail = reject_tail
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.reject_tail:
            raise AssertionError('probe read beyond valid success evidence')
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class ProbeEvidenceTest(unittest.IsolatedAsyncioTestCase):
    async def probe(self, chunks, *, content_type='text/event-stream', status=200,
                    content_encoding=None, block=False, reject_tail=False, timeout=1):
        stream = ProbeStream(chunks, block=block, reject_tail=reject_tail)
        headers = {'content-type': content_type}
        if content_encoding:
            headers['content-encoding'] = content_encoding
        transport = httpx.MockTransport(lambda request: httpx.Response(
            status, stream=stream, headers=headers))

        @asynccontextmanager
        async def client(*args, **kwargs):
            async with httpx.AsyncClient(transport=transport) as value:
                yield value

        with mock.patch.object(tencent, '_account_http_client', client), \
                mock.patch.object(tencent, 'device_token_for', return_value=''), \
                mock.patch.object(tencent, 'attribution_headers', return_value={}), \
                mock.patch.object(config, 'TENCENT_TIMEOUT', timeout):
            result = await tencent.probe_account(AUTH)
        self.assertTrue(stream.closed, 'every outcome must release the provider response')
        return result, stream

    async def test_http_200_nested_error_is_failure_with_provider_code(self):
        (ok, message), _ = await self.probe([event({'error': {
            'code': 12153, 'message': 'synthetic token expired'}})])
        self.assertFalse(ok)
        self.assertIn('12153', message)
        self.assertIn('synthetic token expired', message)

    async def test_http_200_business_error_retains_credit_semantics(self):
        (ok, message), _ = await self.probe([event({'code': 14018, 'msg': 'no credits'})])
        self.assertFalse(ok)
        self.assertIn('14018', message)
        self.assertIn('积分耗尽', message)

    async def test_heartbeat_eof_is_not_health(self):
        (ok, _), _ = await self.probe([b': heartbeat\n\n'])
        self.assertFalse(ok)

    async def test_done_or_role_only_is_not_health(self):
        for wire in (b'data: [DONE]\n\n', event({'choices': [{'delta': {'role': 'assistant'}}]})):
            with self.subTest(wire=wire):
                (ok, _), _ = await self.probe([wire])
                self.assertFalse(ok)

    async def test_content_evidence_stops_without_reading_tail(self):
        (ok, _), stream = await self.probe([SUCCESS], reject_tail=True)
        self.assertTrue(ok)
        self.assertEqual(stream.reads, 1)

    async def test_utf8_and_crlf_fragments_are_not_network_chunk_boundaries(self):
        wire = b': ping\r\n\r\n' + SUCCESS.replace(b'\n', b'\r\n')
        (ok, _), stream = await self.probe([bytes([value]) for value in wire])
        self.assertTrue(ok)
        self.assertGreater(stream.reads, 10)

    async def test_valid_finish_is_success_evidence(self):
        (ok, _), _ = await self.probe([event({'choices': [{'delta': {}, 'finish_reason': 'length'}]})])
        self.assertTrue(ok)

    async def test_non_sse_or_non_protocol_payload_is_not_health(self):
        for wire in (b'<html>upstream error</html>', b'{"choices":[{"delta":{"content":"x"}}]}',
                     event([]), event({'pong': True}), event({'choices': [{'delta': {'content': {'bad': True}}}]}),
                     event({'choices': [{'delta': {'content': {'bad': True}}, 'finish_reason': 'length'}]}),
                     b'data: {invalid-json}\n\n', b'event: error\ndata: {"message":"failed"}\n\n'):
            with self.subTest(wire=wire):
                (ok, _), _ = await self.probe([wire])
                self.assertFalse(ok)

    async def test_unexpected_compression_is_rejected_before_body_decoding(self):
        (ok, message), stream = await self.probe([b'not-read'], content_encoding='gzip')
        self.assertFalse(ok)
        self.assertIn('identity', message)
        self.assertEqual(stream.reads, 0)

    async def test_coalesced_error_wins_over_earlier_content(self):
        (ok, message), _ = await self.probe([SUCCESS + event({'error': {'code': 14018, 'message': 'exhausted'}})])
        self.assertFalse(ok)
        self.assertIn('14018', message)

    async def test_json_http_200_error_retains_code_without_claiming_sse_success(self):
        (ok, message), _ = await self.probe([b'{"error":{"code":12153,"message":"expired"}}'], content_type='application/json')
        self.assertFalse(ok)
        self.assertIn('12153', message)

    async def test_unterminated_and_cumulative_budgets_are_bounded(self):
        for chunks in ([b'data: ' + b'x' * (70 * 1024)], [b': ping\n\n' * 1024] * 40,
                       [b'data:\n' * 2048]):
            with self.subTest(size=sum(map(len, chunks))):
                (ok, _), _ = await self.probe(chunks)
                self.assertFalse(ok)

    async def test_heartbeat_cannot_extend_total_probe_deadline(self):
        (ok, message), _ = await self.probe([b': ping\n\n'], block=True, timeout=0.02)
        self.assertFalse(ok)
        self.assertIn('超时', message)

    async def test_http_error_body_retains_nested_provider_code(self):
        (ok, message), _ = await self.probe([b'{"error":{"code":14018,"message":"exhausted"}}'], status=429)
        self.assertFalse(ok)
        self.assertIn('14018', message)

    async def test_channel_error_in_sse_tries_second_path(self):
        paths = []
        first = event({'code': '11-128', 'msg': 'channel verification failed'})

        def respond(request):
            paths.append(request.url.path)
            return httpx.Response(200, content=first if len(paths) == 1 else SUCCESS,
                                  headers={'content-type': 'text/event-stream'})

        @asynccontextmanager
        async def client(*args, **kwargs):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as value:
                yield value

        with mock.patch.object(tencent, '_account_http_client', client), \
                mock.patch.object(tencent, 'device_token_for', return_value=''), \
                mock.patch.object(tencent, 'attribution_headers', return_value={}):
            ok, message = await tencent.probe_account(AUTH)
        self.assertTrue(ok, message)
        self.assertEqual(len(paths), 2)


if __name__ == '__main__':
    unittest.main()
