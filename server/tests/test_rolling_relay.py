import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from deploy.cohort_rollout import atomic_json
from server.rollingrelay import StreamingRelay, RouteError


class BytesBody(httpx.AsyncByteStream):
    def __init__(self, content): self.content = content
    async def __aiter__(self): yield self.content


class RelayTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'route.json'
        atomic_json(self.path, {'epoch': 1, 'target': 'old'})
        self.calls = []
        async def provider(request):
            self.calls.append((request.url, request.headers, await request.aread()))
            return httpx.Response(200, stream=BytesBody(b'data: [DONE]\n\n'),
                headers={'content-type': 'text/event-stream'})
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        self.relay = StreamingRelay({'old': 'http://127.0.0.1:10001', 'new': 'http://127.0.0.1:10002'},
                                     self.path, client=self.client)
        self.addAsyncCleanup(self.relay.close)

    async def request(self, path='/v1/chat/completions', body=b'{}', headers=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.relay), base_url='http://ingress') as client:
            return await client.post(path, content=body, headers=headers)

    async def test_route_switch_changes_only_subsequent_requests(self):
        self.assertEqual((await self.request()).status_code, 200)
        atomic_json(self.path, {'epoch': 2, 'target': 'new'})
        self.assertEqual((await self.request()).status_code, 200)
        self.assertEqual([call[0].port for call in self.calls], [10001, 10002])
        self.assertEqual(self.relay.snapshot()['active'], {'old': 0, 'new': 0})

    async def test_payload_credentials_and_correlation_pass_through_exactly(self):
        body = b'{"port":11128,"content":"fixture\\n"}'
        response = await self.request(body=body, headers={'Authorization': 'Bearer synthetic',
                                                       'X-Request-ID': 'fixture-correlation'})
        self.assertEqual(response.content, b'data: [DONE]\n\n')
        self.assertEqual(self.calls[0][2], body)
        self.assertEqual(self.calls[0][1]['authorization'], 'Bearer synthetic')
        self.assertEqual(self.calls[0][1]['x-request-id'], 'fixture-correlation')

    async def test_supplier_refusal_is_never_rewritten_or_retried(self):
        async def rejection(request):
            self.calls.append(request.url)
            return httpx.Response(400, stream=BytesBody(b'{"code":11128}'), headers={'content-type': 'application/json'})
        await self.client.aclose()
        self.relay.client = httpx.AsyncClient(transport=httpx.MockTransport(rejection))
        result = await self.request()
        self.assertEqual((result.status_code, result.content), (400, b'{"code":11128}'))
        self.assertEqual(len(self.calls), 1)

    async def test_uncertain_transport_is_one_send_and_one_502(self):
        async def fail(request):
            self.calls.append(request.url)
            raise httpx.ReadError('synthetic accepted disconnect')
        await self.client.aclose()
        self.relay.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        result = await self.request()
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()['error']['code'], 'relay_upstream_transport_uncertain')
        self.assertEqual(len(self.calls), 1)

    async def test_unknown_target_missing_or_stale_route_never_uses_fallback(self):
        for route in ({'epoch': 2, 'target': 'external'}, {'epoch': 0, 'target': 'old'},
                      {'epoch': True, 'target': 'old'}):
            atomic_json(self.path, route)
            self.assertEqual((await self.request()).status_code, 503)
        self.path.unlink()
        self.assertEqual((await self.request()).status_code, 503)
        self.assertEqual(self.calls, [])

    async def test_same_epoch_cannot_select_another_target(self):
        await self.request()
        atomic_json(self.path, {'epoch': 1, 'target': 'new'})
        self.assertEqual((await self.request()).status_code, 503)
        self.assertEqual(len(self.calls), 1)

    async def test_public_port_has_no_control_or_management_api(self):
        for path in ('/snapshot', '/api/users', '/api/system/update', '/__relay/route'):
            self.assertEqual((await self.request(path)).status_code, 404)
        self.assertEqual(self.calls, [])

    async def test_declared_backend_cannot_be_external_url_or_credentials(self):
        for address in ('https://127.0.0.1:1', 'http://example.com:80',
                        'http://secret@127.0.0.1:80', 'http://127.0.0.1:1/path'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                StreamingRelay({'bad': address}, self.path)

    async def test_long_stream_keeps_its_original_backend_through_switch(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: first\n\n'
                started.set()
                await release.wait()
                yield b'data: [DONE]\n\n'
        async def slow(request):
            self.calls.append(request.url)
            return httpx.Response(200, stream=Body())
        await self.client.aclose()
        self.relay.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        first = asyncio.create_task(self.request())
        await started.wait()
        self.assertEqual(self.relay.snapshot()['active']['old'], 1)
        atomic_json(self.path, {'epoch': 2, 'target': 'new'})
        self.assertEqual(self.relay.route(), 'new')
        self.assertEqual(self.calls[0].port, 10001)
        with self.assertRaisesRegex(RouteError, 'still_owns'):
            await self.relay.close()
        release.set()
        self.assertIn(b'[DONE]', (await first).content)
        self.assertEqual(self.relay.snapshot()['active']['old'], 0)

    async def test_repeated_cancel_joins_socket_close_before_releasing_owner(self):
        began, closing, close_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class SlowClose(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: first\n\n'
                began.set()
                await asyncio.Event().wait()
            async def aclose(self):
                closing.set()
                await close_done.wait()
        async def provider(request):
            self.calls.append(request.url)
            return httpx.Response(200, stream=SlowClose())
        await self.client.aclose()
        self.relay.client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        events = asyncio.Queue()
        await events.put({'type': 'http.request', 'body': b'{}', 'more_body': False})
        async def receive(): return await events.get()
        async def send(message): pass
        operation = asyncio.create_task(self.relay({'type': 'http', 'path': '/v1/chat/completions',
            'method': 'POST', 'headers': []}, receive, send))
        await began.wait()
        await events.put({'type': 'http.disconnect'})
        await closing.wait()
        operation.cancel()
        await asyncio.sleep(0.02)
        self.assertEqual(self.relay.snapshot()['active']['old'], 1)
        self.assertFalse(operation.done())
        close_done.set()
        await asyncio.gather(operation, return_exceptions=True)
        self.assertEqual(self.relay.snapshot()['active']['old'], 0)
        self.assertEqual(len(self.calls), 1)
