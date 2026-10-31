"""Upload progress, absolute deadlines and all three protocol admission paths."""
import asyncio
import json
import unittest
from unittest.mock import patch

from starlette.requests import Request

from server import config, observability, requestbody
from server.routers import gateway
from server.tests.authorization_test_support import isolated_data
from server.configcontract import ConfigurationError, validate_environment


class Clock:
    now = 0

    def __call__(self):
        return self.now


class WindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_progressing_upload_may_exceed_old_thirty_second_deadline(self):
        clock = Clock()
        window = requestbody.BodyReadWindow(clock=clock)
        for at in (20, 40, 80, 120, 160):
            async def receive():
                clock.now = at
                return {'type': 'http.request', 'body': b'part', 'more_body': True}
            self.assertEqual((await window.read(receive))['body'], b'part')
        self.assertEqual(window.progress, 160)

    async def test_continuous_progress_cannot_renew_absolute_deadline(self):
        clock = Clock()
        window = requestbody.BodyReadWindow(clock=clock)
        for at in (40, 80, 120, 160, 181):
            async def receive():
                clock.now = at
                return b'part'
            if at < 180:
                await window.read(receive)
            else:
                with self.assertRaises(requestbody.BodyReadTimeout) as caught:
                    await window.read(receive)
                self.assertEqual(caught.exception.reason, 'total')

    async def test_empty_frames_do_not_reset_idle_deadline(self):
        clock = Clock()
        window = requestbody.BodyReadWindow(clock=clock)
        for at in (20, 40, 61):
            async def receive():
                clock.now = at
                return {'type': 'http.request', 'body': b'', 'more_body': True}
            if at < 60:
                await window.read(receive)
                self.assertEqual(window.progress, 0)
            else:
                with self.assertRaises(requestbody.BodyReadTimeout) as caught:
                    await window.read(receive)
                self.assertEqual(caught.exception.reason, 'idle')

    async def test_expired_budget_never_starts_another_receive(self):
        clock = Clock()
        window = requestbody.BodyReadWindow(clock=clock)
        clock.now = 181
        def receive():
            self.fail('expired read must not start')
        with self.assertRaises(requestbody.BodyReadTimeout):
            await window.read(receive)

    async def test_real_wait_timeout_cancels_and_joins_receive(self):
        finished = asyncio.Event()
        async def receive():
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        with patch.object(config, 'GATEWAY_BODY_IDLE_TIMEOUT_S', .03):
            window = requestbody.BodyReadWindow()
            with self.assertRaises(requestbody.BodyReadTimeout) as caught:
                await window.read(receive)
        self.assertTrue(finished.is_set())
        self.assertEqual(caught.exception.reason, 'idle')

    async def test_cancellation_propagates_instead_of_becoming_408(self):
        started, finished = asyncio.Event(), asyncio.Event()
        async def receive():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                finished.set()
        task = asyncio.create_task(requestbody.BodyReadWindow().read(receive))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())


class AdmissionTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(isolated_data())
        self.enterContext(patch('server.routers.gateway.preauthorize_body', return_value=None))
        self.enterContext(patch('server.db.execute'))
        self.enterContext(patch('server.budget.finish'))
        self.enterContext(patch('server.observability.ensure_outcome_schema', return_value=False))

    async def run_upload(self, path, times, *, keep_open=False, declared=None):
        clock = Clock()
        class Window(requestbody.BodyReadWindow):
            def __init__(self):
                super().__init__(clock=clock)
        messages, calls = [], []
        parts = [b'{', b'"messages":', b'[]}']
        async def receive():
            index = len(calls)
            calls.append(index)
            if index >= len(times):
                await asyncio.Event().wait()
            clock.now = times[index]
            return {'type': 'http.request', 'body': parts[index],
                    'more_body': keep_open or index < len(times) - 1}
        entered = []
        async def app(scope, receive, send):
            entered.append(True)
            body, error = await gateway._read_json_body(Request(scope, receive))
            self.assertIsNone(error)
            self.assertEqual(body, {'messages': []})
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'fixture-only'})
        async def send(message):
            messages.append(message)
        scope = {'type': 'http', 'path': path, 'method': 'POST', 'headers': []}
        if declared is not None:
            scope['headers'] = [(b'content-length', str(declared).encode())]
        with patch.object(observability, 'BodyReadWindow', Window), patch.object(gateway, 'BodyReadWindow', Window):
            middleware = observability.RequestAuditMiddleware(app)
            await middleware(scope, receive, send)
        self.assertEqual((middleware.active, middleware.large, middleware.inflight_body_bytes), (0, 0, 0))
        return messages, entered, calls

    async def test_three_protocols_and_platform_paths_accept_progress_after_thirty_seconds(self):
        for path in ('/v1/chat/completions', '/v1/responses', '/v1/messages',
                     '/qoder/v1/chat/completions', '/trae/v1/messages'):
            with self.subTest(path=path):
                messages, entered, calls = await self.run_upload(path, (16, 32, 48))
                self.assertEqual(messages[0]['status'], 200)
                self.assertEqual(len(calls), 3)
                self.assertEqual(len(entered), 1)

    async def test_idle_timeout_has_stable_code_no_protocol_or_provider_dispatch(self):
        messages, entered, _ = await self.run_upload('/v1/messages', (61,))
        self.assertEqual(messages[0]['status'], 408)
        error = json.loads(messages[1]['body'])['error']
        self.assertEqual(error['code'], 'body_read_timeout')
        self.assertIn('60', error['message'])
        self.assertEqual(entered, [])

    async def test_total_timeout_and_forged_length_still_release_reservations(self):
        messages, entered, _ = await self.run_upload('/v1/chat/completions', (50, 100, 181), declared=1)
        self.assertEqual(messages[0]['status'], 408)
        self.assertIn('180', json.loads(messages[1]['body'])['error']['message'])
        self.assertEqual(entered, [])

    async def test_direct_protocol_reader_obeys_same_idle_setting(self):
        async def receive():
            await asyncio.Event().wait()
        with patch.object(config, 'GATEWAY_BODY_IDLE_TIMEOUT_S', .03):
            _, error = await gateway._read_json_body(Request(
                {'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions', 'headers': []}, receive))
        self.assertEqual(error.status_code, 408)
        self.assertEqual(json.loads(error.body)['error']['code'], 'body_read_timeout')


class TimeoutConfigTests(unittest.TestCase):
    def test_limits_registered_with_units_and_finite_positive_hard_bounds(self):
        names = ('WB_GATEWAY_BODY_READ_TIMEOUT_S', 'WB_GATEWAY_BODY_IDLE_TIMEOUT_S')
        rows = validate_environment({})['settings']
        self.assertEqual([rows[name]['value'] for name in names], [180, 60])
        for name, maximum in zip(names, (600, 120)):
            self.assertEqual(rows[name]['unit'], 'seconds')
            validate_environment({name: str(maximum)})
            for bad in ('0', '-1', 'nan', 'inf', '30s', '1.5', str(maximum + 1)):
                with self.subTest(name=name, bad=bad), self.assertRaises(ConfigurationError):
                    validate_environment({name: bad})


if __name__ == '__main__':
    unittest.main()
