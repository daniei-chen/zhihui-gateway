"""Concurrent ASGI admission contracts, including chunked bodies and cancellation."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch
from server import observability as obs


class AdmissionBytesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [patch.dict(os.environ, {'WB_GATEWAY_MAX_INFLIGHT_BODY_BYTES': str(1024**2),
            'WB_GATEWAY_MAX_CONCURRENCY': '8', 'WB_GATEWAY_LARGE_MAX_CONCURRENCY': '8'}),
            patch('server.db.execute'), patch('server.budget.finish')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def drive(self, middleware, body=b'{}', declared=None, path='/v1/chat/completions'):
        messages = []
        reads = []
        async def receive():
            reads.append(True)
            return {'type': 'http.request', 'body': body, 'more_body': False}
        async def send(message):
            messages.append(message)
        headers = [] if declared is None else [(b'content-length', str(declared).encode())]
        scope = {'type': 'http', 'path': path, 'method': 'POST', 'headers': headers}
        await middleware(scope, receive, send)
        return messages, reads

    async def test_declared_aggregate_rejection_precedes_body_read(self):
        held = asyncio.Event()
        release = asyncio.Event()
        async def app(scope, receive, send):
            await receive()
            held.set()
            await release.wait()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        middleware = obs.RequestAuditMiddleware(app)
        first = asyncio.create_task(self.drive(middleware, b'a' * 600000, 600000))
        await held.wait()
        try:
            messages, reads = await self.drive(middleware, declared=600000, path='/qoder/v1/chat/completions')
            self.assertEqual(messages[0]['status'], 503)
            self.assertEqual(reads, [])
            self.assertEqual(middleware.inflight_body_bytes, 600000)
            self.assertEqual(middleware.capacity_rejections, 1)
        finally:
            release.set()
            await first
        self.assertEqual((middleware.active, middleware.large, middleware.inflight_body_bytes), (0, 0, 0))

    async def test_chunked_and_understated_bodies_cannot_overcommit(self):
        held = asyncio.Event()
        release = asyncio.Event()
        async def app(scope, receive, send):
            await receive()
            held.set()
            await release.wait()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        middleware = obs.RequestAuditMiddleware(app)
        first = asyncio.create_task(self.drive(middleware, b'a' * 700000))
        await held.wait()
        try:
            for declared in (None, 2):
                messages, _ = await self.drive(middleware, b'b' * 600000, declared, '/trae/v1/chat/completions')
                self.assertEqual(messages[0]['status'], 503)
                self.assertEqual(json.loads(messages[1]['body'])['error']['code'], 'gateway_capacity_exceeded')
                self.assertEqual(middleware.inflight_body_bytes, 700000)
        finally:
            release.set()
            await first
        self.assertEqual(middleware.inflight_body_bytes, 0)

    async def test_reservation_lives_through_sse_and_releases_on_cancel(self):
        started = asyncio.Event()
        async def app(scope, receive, send):
            await receive()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'data: {}\n\n', 'more_body': True})
            started.set()
            await asyncio.Event().wait()
        middleware = obs.RequestAuditMiddleware(app)
        task = asyncio.create_task(self.drive(middleware, b'x' * 800000, 800000))
        await started.wait()
        self.assertEqual(middleware.inflight_body_bytes, 800000)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((middleware.active, middleware.large, middleware.inflight_body_bytes), (0, 0, 0))

    async def test_application_failure_releases_both_budgets(self):
        async def app(scope, receive, send):
            await receive()
            raise RuntimeError('fixture failure')
        middleware = obs.RequestAuditMiddleware(app)
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            await self.drive(middleware, b'x' * 700000)
        self.assertEqual((middleware.active, middleware.large, middleware.inflight_body_bytes), (0, 0, 0))

    async def test_non_inference_upload_is_not_charged_to_generation_budget(self):
        async def app(scope, receive, send):
            await receive()
            self.assertEqual(middleware.inflight_body_bytes, 0)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        middleware = obs.RequestAuditMiddleware(app)
        messages, _ = await self.drive(middleware, b'x' * 1100000, 1100000, '/api/system/update-package')
        self.assertEqual(messages[0]['status'], 200)
        self.assertEqual(middleware.inflight_body_bytes, 0)

    async def test_count_gate_still_rejects_small_requests(self):
        async def app(scope, receive, send):
            self.fail('Count rejection must happen before application entry')
        middleware = obs.RequestAuditMiddleware(app)
        middleware.active = middleware.maximum
        messages, reads = await self.drive(middleware)
        self.assertEqual(messages[0]['status'], 503)
        self.assertEqual(reads, [])
        self.assertEqual(middleware.inflight_body_bytes, 0)

    async def test_fastapi_json_parser_never_turns_capacity_rejection_into_500(self):
        import httpx
        from fastapi import FastAPI
        app=FastAPI()
        entered=asyncio.Event()
        release=asyncio.Event()
        @app.post('/v1/chat/completions')
        async def generation(body: dict):
            entered.set()
            await release.wait()
            return {'ok':True}
        middleware=obs.RequestAuditMiddleware(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware),base_url='http://fixture') as client:
            first=asyncio.create_task(client.post('/v1/chat/completions',json={'text':'x'*700000}))
            await entered.wait()
            try:
                second=await client.post('/v1/chat/completions',json={'text':'y'*600000})
                self.assertEqual(second.status_code,503)
                self.assertEqual(second.json()['error']['code'],'gateway_capacity_exceeded')
            finally:
                release.set()
                self.assertEqual((await first).status_code,200)
        self.assertEqual(middleware.inflight_body_bytes,0)

    async def test_per_request_limit_takes_precedence_and_preserves_messages_error_shape(self):
        async def app(scope,receive,send):
            self.fail('Oversized request must be rejected before parsing')
        middleware=obs.RequestAuditMiddleware(app)
        with patch('server.routers.gateway.max_body_bytes',return_value=500000):
            for declared in (900000,None):
                messages,reads=await self.drive(middleware,b'x'*900000,declared,'/v1/messages')
                self.assertEqual(messages[0]['status'],413)
                self.assertEqual(json.loads(messages[1]['body'])['type'],'error')
                self.assertEqual(len(reads),0 if declared else 1)
        self.assertEqual(middleware.inflight_body_bytes,0)
