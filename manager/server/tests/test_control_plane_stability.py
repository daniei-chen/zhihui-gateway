"""Synthetic-only refresh coalescing and non-replayed control-plane mutations."""
import asyncio
import unittest
from unittest.mock import patch

import httpx

from server.qtgcore.adapters.base import BaseAdapter
from server.qtgcore.cache import TTLCache
from server.qtgcore.config import Settings
from server.qtgcore.errors import UpstreamError


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_simultaneous_force_refreshes_share_one_upstream_build(self):
        store=TTLCache();started=asyncio.Event();release=asyncio.Event();calls=0
        async def build():
            nonlocal calls
            calls+=1;started.set();await release.wait();return {'version':calls}
        first=asyncio.create_task(store.get_or_set('scope',60,build,force=True))
        await asyncio.wait_for(started.wait(),3)
        peers=[asyncio.create_task(store.get_or_set('scope',60,build,force=True))for _ in range(15)]
        await asyncio.sleep(0)
        release.set()
        rows=await asyncio.wait_for(asyncio.gather(first,*peers),3)
        self.assertEqual(calls,1)
        self.assertEqual(rows,[{'version':1}]*16)
        self.assertEqual(store.stats()['coalesced'],15)
        self.assertEqual(await store.get_or_set('scope',60,build,force=True),{'version':2})

    async def test_invalidated_build_and_waiter_cannot_deliver_old_evidence(self):
        store=TTLCache();started=asyncio.Event();release=asyncio.Event();calls=0
        async def build():
            nonlocal calls
            calls+=1;started.set();await release.wait();return 'old'
        first=asyncio.create_task(store.get_or_set('scope',60,build))
        await asyncio.wait_for(started.wait(),3)
        peer=asyncio.create_task(store.get_or_set('scope',60,build,force=True))
        await asyncio.sleep(0)
        store.invalidate('scope');release.set()
        for task in (first,peer):
            with self.assertRaises(UpstreamError)as caught:await task
            self.assertEqual(caught.exception.code,'cache_invalidated')
        self.assertEqual(calls,1)
        self.assertNotIn('scope',store._entries)

    async def test_cancelled_waiter_does_not_cancel_shared_build(self):
        store=TTLCache();started=asyncio.Event();release=asyncio.Event()
        async def build():started.set();await release.wait();return 'current'
        first=asyncio.create_task(store.get_or_set('scope',60,build))
        await asyncio.wait_for(started.wait(),3)
        peer=asyncio.create_task(store.get_or_set('scope',60,build,force=True))
        await asyncio.sleep(0);peer.cancel()
        with self.assertRaises(asyncio.CancelledError):await peer
        release.set();self.assertEqual(await first,'current')


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_mutations_never_retry_timeout_5xx_or_reauthentication(self):
        for path in ('/accounts/refresh','/accounts/import/pat','/settings/save',
                     '/api/cmd/refresh_jwt','/api/cmd/pool_set','/api/cmd/accounts_import',
                     '/unknown-future-write'):
            for status in (401,503,'timeout'):
                with self.subTest(path=path,status=status):
                    calls=[]
                    async def respond(request):
                        calls.append(request.url.path)
                        if status=='timeout':raise httpx.ReadTimeout('synthetic',request=request)
                        return httpx.Response(status,json={'ok':False})
                    adapter=BaseAdapter('http://fixture',Settings(upstream_retries=3,upstream_backoff_sec=0))
                    adapter._client=httpx.AsyncClient(base_url=adapter.base_url,transport=httpx.MockTransport(respond))
                    adapter._authenticated=True
                    try:
                        if status=='timeout':
                            with self.assertRaises(UpstreamError):await adapter._request('POST',path)
                        else:self.assertEqual((await adapter._request('POST',path)).status_code,status)
                        self.assertEqual(calls,[path])
                    finally:await adapter.close()

    async def test_idempotent_reads_recover_without_unbounded_retry(self):
        for method,path in (('GET','/accounts'),('POST','/api/cmd/settings_get')):
            calls=[]
            async def respond(request):
                calls.append(True)
                return httpx.Response(503 if len(calls)==1 else 200,json={'ok':True})
            adapter=BaseAdapter('http://fixture',Settings(upstream_retries=1,upstream_backoff_sec=0))
            adapter._client=httpx.AsyncClient(base_url=adapter.base_url,transport=httpx.MockTransport(respond))
            adapter._authenticated=True
            try:
                self.assertEqual((await adapter._request(method,path)).status_code,200)
                self.assertEqual(len(calls),2)
            finally:await adapter.close()

    async def test_local_control_client_does_not_inherit_environment_proxy(self):
        adapter=BaseAdapter('http://127.0.0.1:8799',Settings())
        with patch('server.qtgcore.adapters.base.httpx.AsyncClient',wraps=httpx.AsyncClient)as factory:
            await adapter.start()
            self.assertIs(factory.call_args.kwargs['trust_env'],False)
        await adapter.close()
