"""S1: actual body-bearing safe-method requests share the buffer capacity limit."""
import asyncio
import unittest

from server.bodyguard import APIBodyGuard


class BodyRepairs(unittest.IsolatedAsyncioTestCase):
    async def run_batch(self,method,body,headers=()):
        release=asyncio.Event(); entered=asyncio.Event(); statuses=[]; payloads=[]
        async def app(scope,receive,send):
            payloads.append((await receive())['body'])
            entered.set()
            await release.wait()
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'ok'})
        guard=APIBodyGuard(app,concurrency=1)
        async def receive(): return {'type':'http.request','body':body,'more_body':False}
        async def send(event):
            if event['type']=='http.response.start':statuses.append(event['status'])
        tasks=[asyncio.create_task(guard({'type':'http','method':method,'path':'/api/synthetic','headers':list(headers)},receive,send)) for _ in range(3)]
        await asyncio.wait_for(entered.wait(),1)
        await asyncio.sleep(0.01)
        active=guard.active
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(guard.active,0)
        return sorted(statuses),active,payloads

    async def test_get_head_options_with_body_use_capacity(self):
        for method in ('GET','HEAD','OPTIONS','POST'):
            with self.subTest(method=method):
                statuses,active,payloads=await self.run_batch(method,b'synthetic')
                self.assertEqual(statuses,[200,503,503])
                self.assertEqual(active,1)
                self.assertEqual(payloads,[b'synthetic'])

    async def test_empty_safe_requests_do_not_contend_for_body_capacity(self):
        for method in ('GET','HEAD','OPTIONS'):
            for headers in ((),((b'content-length',b'0'),)):
                with self.subTest(method=method,headers=headers):
                    statuses,active,payloads=await self.run_batch(method,b'',headers)
                    self.assertEqual(statuses,[200,200,200])
                    self.assertEqual(active,0)

    async def test_cancel_timeout_disconnect_and_overflow_release_slots(self):
        async def send(_event):pass
        async def blocked(scope,receive,send):
            await asyncio.Event().wait()
        guard=APIBodyGuard(blocked,concurrency=1,maximum=3,timeout=0.01)
        scope={'type':'http','method':'GET','path':'/api/synthetic','headers':[]}
        async def body():return {'type':'http.request','body':b'x','more_body':False}
        task=asyncio.create_task(guard(scope,body,send))
        await asyncio.sleep(0.01)
        self.assertEqual(guard.active,1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(guard.active,0)
        async def slow():await asyncio.sleep(1)
        await guard(scope,slow,send)
        self.assertEqual(guard.active,0)
        async def disconnect():return {'type':'http.disconnect'}
        await guard(scope,disconnect,send)
        self.assertEqual(guard.active,0)
        async def large():return {'type':'http.request','body':b'xxxx','more_body':False}
        await guard(scope,large,send)
        self.assertEqual(guard.active,0)
