"""Concurrent expired control-plane sessions share one credential refresh."""
import asyncio
import unittest

import httpx

from server.qtgcore.adapters.base import BaseAdapter
from server.qtgcore.config import Settings


class Adapter(BaseAdapter):
    async def authenticate(self):
        self.logins += 1
        await asyncio.sleep(0)
        self.client.headers['x-test-session'] = f'session-{self.logins}'


class AdapterRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_401_responses_refresh_the_same_generation_once(self):
        arrived = 0
        ready = asyncio.Event()
        calls = []

        async def handler(request):
            nonlocal arrived
            session = request.headers.get('x-test-session')
            calls.append(session)
            if session == 'expired':
                arrived += 1
                if arrived == 8:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 2)
                return httpx.Response(401)
            return httpx.Response(200, json={'ok': True})

        adapter = Adapter('http://127.0.0.1', Settings(upstream_retries=1))
        adapter.logins = 0
        adapter._authenticated = True
        adapter._client = httpx.AsyncClient(base_url='http://127.0.0.1',
            headers={'x-test-session': 'expired'}, transport=httpx.MockTransport(handler))
        try:
            responses = await asyncio.gather(*(adapter._request('GET', '/accounts') for _ in range(8)))
            self.assertTrue(all(response.status_code == 200 for response in responses))
            self.assertEqual(adapter.logins, 1)
            self.assertEqual(len(calls), 16)
            self.assertEqual(set(calls), {'expired', 'session-1'})
            await adapter.ensure_auth(force=True)
            self.assertEqual(adapter.logins, 2, 'a later explicit refresh remains available')
        finally:
            await adapter.close()

    async def test_zero_retry_budget_preserves_real_401_without_futile_login(self):
        adapter = Adapter('http://127.0.0.1', Settings(upstream_retries=0))
        adapter.logins = 0
        adapter._authenticated = True
        adapter._client = httpx.AsyncClient(base_url='http://127.0.0.1',
            transport=httpx.MockTransport(lambda _: httpx.Response(401)))
        try:
            response = await adapter._request('GET', '/accounts')
            self.assertEqual(response.status_code, 401)
            self.assertEqual(adapter.logins, 0)
        finally:
            await adapter.close()

    async def test_ambiguous_write_is_not_replayed_or_reauthenticated(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(401)

        adapter = Adapter('http://127.0.0.1', Settings(upstream_retries=2))
        adapter.logins = 0
        adapter._authenticated = True
        adapter._client = httpx.AsyncClient(base_url='http://127.0.0.1', transport=httpx.MockTransport(handler))
        try:
            response = await adapter._request('POST', '/accounts/delete', json_body={'uid': 'synthetic'})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(calls, ['/accounts/delete'])
            self.assertEqual(adapter.logins, 0)
        finally:
            await adapter.close()
