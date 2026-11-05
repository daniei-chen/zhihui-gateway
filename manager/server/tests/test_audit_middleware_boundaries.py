"""Route rewriting must preserve privacy and never stall the event loop."""
import asyncio
import threading
import unittest
from unittest import mock

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from server import config, main, security


def request(path, headers=()):
    return Request({'type': 'http', 'method': 'GET', 'scheme': 'http',
                    'path': path, 'raw_path': path.encode(), 'query_string': b'',
                    'server': ('testserver', 80), 'client': ('127.0.0.1', 1),
                    'headers': [(b'host', b'testserver'), *headers]})


class MiddlewareBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_rewritten_qtg_response_remains_no_store(self):
        req = request('/api/accounts')

        async def rewrite(received):
            received.scope['path'] = '/__qtg/api/accounts'
            return JSONResponse({'items': []})

        response = await main.cache_headers(req, rewrite)
        self.assertEqual(response.headers['cache-control'], 'no-store, no-cache, must-revalidate')

    async def test_responses_alias_and_readiness_are_no_store(self):
        async def next_response(_):
            return JSONResponse({'ok': False}, status_code=503)

        for path in ('/responses', '/responses/', '/readyz'):
            with self.subTest(path=path):
                response = await main.cache_headers(request(path), next_response)
                self.assertIn('no-store', response.headers['cache-control'])

    async def test_qtg_identity_lock_does_not_block_other_async_work(self):
        released = threading.Event()

        def blocking_identity(_):
            if not released.wait(0.5):
                raise RuntimeError('event loop could not release the identity lock')
            return {'username': 'fixture', 'role': 'admin'}

        async def next_response(req):
            self.assertEqual(req.scope['path'], '/__qtg/api/accounts')
            return Response(status_code=204)

        handle = asyncio.get_running_loop().call_later(0.01, released.set)
        try:
            with mock.patch.object(security, 'current_user', side_effect=blocking_identity):
                response = await main._qtg_platform_route(
                    request('/api/accounts', [(b'x-qtg-platform', b'qoder')]), next_response)
            self.assertEqual(response.status_code, 204)
        finally:
            released.set()
            handle.cancel()

    async def test_cookie_renewal_lock_does_not_block_stream_progress(self):
        released = threading.Event()
        req = request('/api/me', [(b'cookie', (config.COOKIE_NAME + '=old').encode())])
        req.state.session_renew = True

        def blocking_users():
            if not released.wait(0.5):
                raise RuntimeError('event loop could not release the user-file lock')
            return {'secret': 'fixture'}

        async def next_response(_):
            return JSONResponse({'ok': True})

        handle = asyncio.get_running_loop().call_later(0.01, released.set)
        try:
            with (mock.patch.object(security, 'load_users', side_effect=blocking_users),
                  mock.patch.object(security, '_unsign', return_value={'username': 'fixture', 'orig': 123, 'sv': 7}),
                  mock.patch.object(security, 'issue_token', return_value='renewed') as issue):
                response = await main.cache_headers(req, next_response)
            self.assertIn(config.COOKIE_NAME + '=renewed', response.headers['set-cookie'])
            issue.assert_called_once_with('fixture', '', orig=123, expected_sv=7)
        finally:
            released.set()
            handle.cancel()
