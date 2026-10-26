"""Exercise the actual ASGI lifetime; response headers are not stream completion."""
import asyncio
import unittest

from server import drain


def scope(path='/v1/chat/completions', method='POST'):
    return {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
            'query_string': b'', 'headers': [], 'server': ('127.0.0.1', 8000),
            'client': ('127.0.0.1', 10000)}


async def receive():
    await asyncio.Future()


class StreamingDrainTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        drain.reset_for_tests()

    def tearDown(self):
        drain.reset_for_tests()

    async def test_stream_counted_until_final_body_and_new_admission_rejected(self):
        first = asyncio.Event()
        finish = asyncio.Event()
        messages = []

        async def app(s, recv, send):
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body', 'body': b'data: first\n\n', 'more_body': True})
            first.set()
            await finish.wait()
            await send({'type': 'http.response.body', 'body': b'data: [DONE]\n\n', 'more_body': False})

        async def send(message):
            messages.append(message)

        wrapped = drain.DrainMiddleware(app)
        old_request = asyncio.create_task(wrapped(scope('/qoder/v1/chat/completions'), receive, send))
        await asyncio.wait_for(first.wait(), 2)
        self.assertEqual(drain.in_flight(), {'inference_stream': 1})
        drain.begin()
        rejected = []

        async def reject_send(message):
            rejected.append(message)

        await wrapped(scope('/trae/v1/chat/completions'), receive, reject_send)
        self.assertEqual(rejected[0]['status'], 503)
        self.assertIn(b'gateway_draining', rejected[-1]['body'])
        self.assertEqual(drain.total_in_flight(), 1)
        self.assertFalse((await drain.wait_until_drained(0.02, interval=0.005))['drained'])
        finish.set()
        await asyncio.wait_for(old_request, 2)
        self.assertEqual(drain.total_in_flight(), 0)
        self.assertTrue(messages[-1]['body'].endswith(b'[DONE]\n\n'))

    async def test_cancellation_releases_count(self):
        first = asyncio.Event()

        async def app(s, recv, send):
            first.set()
            await asyncio.Future()

        async def send(message):
            pass

        task = asyncio.create_task(drain.DrainMiddleware(app)(scope(), receive, send))
        await asyncio.wait_for(first.wait(), 2)
        self.assertEqual(drain.total_in_flight(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(drain.total_in_flight(), 0)

    async def test_monitoring_does_not_count_itself_or_get_blocked(self):
        async def app(s, recv, send):
            self.assertEqual(drain.total_in_flight(), 0)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        async def send(message):
            pass

        drain.begin()
        await drain.DrainMiddleware(app)(scope('/api/readyz', 'GET'), receive, send)
        self.assertTrue(drain.resume())
        drain.begin(shutdown=True)
        self.assertFalse(drain.resume())

    async def test_exception_releases_count(self):
        async def app(s, recv, send):
            raise RuntimeError('upstream failed')

        async def send(message):
            pass

        with self.assertRaisesRegex(RuntimeError, 'upstream failed'):
            await drain.DrainMiddleware(app)(scope(), receive, send)
        self.assertEqual(drain.total_in_flight(), 0)


class ManagedShutdownTest(unittest.TestCase):
    def tearDown(self):
        drain.reset_for_tests()

    def test_sigterm_marks_shutdown_before_cleanup(self):
        import signal
        import uvicorn
        from server.serve import DrainServer
        instance = DrainServer(uvicorn.Config('server.main:app'))
        instance.handle_exit(signal.SIGTERM, None)
        self.assertTrue(drain.snapshot()['shutdown_started'])
        self.assertTrue(instance.should_exit)
