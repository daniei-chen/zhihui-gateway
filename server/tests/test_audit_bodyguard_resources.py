"""Actual ASGI bodies, not method names or frame counts, bound ingress memory."""
import asyncio
import tracemalloc
import unittest

from server.bodyguard import APIBodyGuard


class BodyGuardResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_body_on_read_methods_cannot_bypass_capacity(self):
        for method in ('GET', 'HEAD', 'OPTIONS'):
            with self.subTest(method=method):
                entered, release = asyncio.Event(), asyncio.Event()

                async def app(scope, receive, send):
                    body = (await receive())['body']
                    if scope.get('hold'):
                        entered.set()
                        await release.wait()
                    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                    await send({'type': 'http.response.body', 'body': body})

                guard = APIBodyGuard(app, concurrency=1)

                async def request(body, *, hold=False):
                    messages = []

                    async def receive():
                        return {'type': 'http.request', 'body': body, 'more_body': False}

                    async def send(message):
                        messages.append(message)

                    await guard({'type': 'http', 'path': '/api/me', 'method': method,
                                 'headers': [], 'hold': hold}, receive, send)
                    return messages[0]['status']

                first = asyncio.create_task(request(b'body without content-length', hold=True))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertEqual(await request(b'another body'), 503)
                    self.assertEqual(await request(b''), 200,
                                     'A bodyless dashboard read must remain available')
                finally:
                    release.set()
                    self.assertEqual(await first, 200)
                self.assertEqual(guard.active, 0)

    async def test_read_body_cancellation_releases_capacity(self):
        waiting = asyncio.Event()
        calls = 0

        async def receive():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {'type': 'http.request', 'body': b'part', 'more_body': True}
            waiting.set()
            await asyncio.Event().wait()

        async def unreachable(*args):
            self.fail('Incomplete upload must never reach the application or send a response')

        guard = APIBodyGuard(unreachable, concurrency=1)
        task = asyncio.create_task(guard({'type': 'http', 'path': '/api/me',
                                          'method': 'GET', 'headers': []}, receive, unreachable))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            self.assertEqual(guard.active, 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(guard.active, 0)

    async def test_fragmented_body_memory_scales_with_bytes(self):
        loop=asyncio.get_running_loop();debug=loop.get_debug()
        loop.set_debug(False);self.addCleanup(loop.set_debug,debug)
        size = 128 * 1024
        received = 0
        captured = None

        async def receive():
            nonlocal received
            received += 1
            return {'type': 'http.request', 'body': b'x', 'more_body': received < size}

        async def app(scope, receive, send):
            nonlocal captured
            captured = (await receive())['body']

        async def send(message):
            pass

        tracing = tracemalloc.is_tracing()
        if tracing:
            self.skipTest('Independent peak memory measurement requires an idle tracer')
        tracemalloc.start()
        try:
            await APIBodyGuard(app, maximum=size)({'type': 'http', 'path': '/api/upload',
                                                   'method': 'POST', 'headers': []}, receive, send)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(captured, b'x' * size)
        self.assertLess(peak, size * 4,
                        'Tiny ASGI frames must not create an unbounded per-frame object list')

    async def test_immediately_ready_empty_frames_cannot_starve_timeout(self):
        received = 0
        statuses = []

        async def receive():
            nonlocal received
            received += 1
            return {'type': 'http.request', 'body': b'', 'more_body': received < 100000}

        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})

        async def send(message):
            if message['type'] == 'http.response.start':
                statuses.append(message['status'])

        guard = APIBodyGuard(app, timeout=0.001)
        await guard({'type': 'http', 'path': '/api/upload', 'method': 'POST',
                     'headers': []}, receive, send)
        self.assertEqual(statuses, [408])
        self.assertLess(received, 100000)
        self.assertEqual(guard.active, 0)
