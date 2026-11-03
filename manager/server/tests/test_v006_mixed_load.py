"""ASGI capacity exercise with synthetic bodies; never contacts a supplier."""
import asyncio
import contextlib
import gzip
import hashlib
import os
import unittest
from unittest import mock

from server import observability as obs


class MixedLoad(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_gzip_large_small_and_queued_cancellation(self):
        release = asyncio.Event()
        seen, reads, responses = {}, {}, {}
        peak_active, peak_bytes = 0, 0

        def authorize(request, **kwargs):
            state = obs.current.get()
            state.key_id = int(request.headers['x-test-key'])
            return None

        async def app(scope, receive, send):
            nonlocal peak_active, peak_bytes
            index = int(dict(scope['headers'])[b'x-test-index'])
            data = bytearray()
            while True:
                message = await receive()
                data.extend(message.get('body', b''))
                if not message.get('more_body'):
                    break
            seen[index] = hashlib.sha256(data).hexdigest()
            peak_active = max(peak_active, middleware.active)
            peak_bytes = max(peak_bytes, middleware.inflight_body_bytes)
            await release.wait()
            await asyncio.sleep(.001)
            await send({'type':'http.response.start', 'status':200, 'headers':[]})
            await send({'type':'http.response.body', 'body':b'data: [DONE]\n\n'})

        async def drive(index, wire, compressed):
            reads[index], responses[index] = 0, []
            async def receive():
                reads[index] += 1
                return {'type':'http.request', 'body':wire, 'more_body':False}
            async def send(message):
                responses[index].append(message)
            headers = [(b'content-length',str(len(wire)).encode()),
                       (b'x-test-index',str(index).encode()), (b'x-test-key',str(index % 4 + 1).encode())]
            if compressed:
                headers.append((b'content-encoding',b'gzip'))
            await middleware({'type':'http','path':'/v1/chat/completions','method':'POST','headers':headers}, receive, send)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {
                'WB_GATEWAY_MAX_CONCURRENCY':'3', 'WB_GATEWAY_LARGE_MAX_CONCURRENCY':'1',
                'WB_GATEWAY_MAX_INFLIGHT_BODY_BYTES':str(4 * 1024**2),
                'WB_GATEWAY_QUEUE_WAIT_MS':'5000'}))
            for name, value in [('server.routers.gateway.preauthorize_body', authorize),
                                ('server.observability.ensure_outcome_schema', lambda:True),
                                ('server.db.execute', lambda *a, **k:None),
                                ('server.budget.finish', lambda *a:None),
                                ('server.requestperf.record', lambda *a:None)]:
                stack.enter_context(mock.patch(name, side_effect=value))
            middleware = obs.RequestAuditMiddleware(app)
            tasks, expected = [], {}
            try:
                for i in range(14):
                    body = b'{"pad":"' + (b'x' * (1100000 if i % 4 == 0 else 100)) + b'"}'
                    expected[i] = hashlib.sha256(body).hexdigest()
                    compressed = i % 3 == 0
                    tasks.append(asyncio.create_task(drive(i, gzip.compress(body) if compressed else body, compressed)))
                    # Finish each bounded preauthorization before launching another;
                    # this exercise targets body/capacity scheduling, not auth bursts.
                    async with asyncio.timeout(2):
                        while middleware.active + middleware.fair_gate.pending < i+1:
                            if tasks[-1].done():
                                await tasks[-1]
                                self.fail('unexpected pre-admission rejection')
                            await asyncio.sleep(0)
                self.assertEqual(middleware.active, 3)
                self.assertEqual(middleware.fair_gate.pending, 11)
                self.assertEqual(reads[8], 0)
                self.assertEqual(reads[9], 0)
                for i in (8,9):
                    tasks[i].cancel()
                await asyncio.gather(tasks[8], tasks[9], return_exceptions=True)
                release.set()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, result in enumerate(results):
                    if i in (8,9):
                        self.assertIsInstance(result, asyncio.CancelledError)
                    else:
                        if isinstance(result, BaseException):
                            raise result
                        self.assertEqual(responses[i][0]['status'], 200)
                        self.assertEqual(seen[i], expected[i])
                self.assertLessEqual(peak_active, 3)
                self.assertLessEqual(peak_bytes, 4 * 1024**2)
                self.assertEqual((middleware.active, middleware.large, middleware.inflight_body_bytes, middleware.fair_gate.pending), (0,0,0,0))
                self.assertEqual(len(seen), 12)
            finally:
                release.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
