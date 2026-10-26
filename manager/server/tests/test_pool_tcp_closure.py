"""Real TCP keepalive and cancellation; fixtures never contact a provider."""
import asyncio
import json
import os
import unittest
from pathlib import Path
from server import config

class RealTCPPoolContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await config.close_clients()
        self.connections=0;self.requests=0;self.writers=set();self.handlers=set()
        self.server=await asyncio.start_server(self.handle,'127.0.0.1',0)
        self.url='http://127.0.0.1:'+str(self.server.sockets[0].getsockname()[1])
    async def handle(self,reader,writer):
        self.connections+=1;self.writers.add(writer);self.handlers.add(asyncio.current_task())
        try:
            while True:
                header=await reader.readuntil(b'\r\n\r\n')
                self.requests+=1
                if b'/slow ' in header:
                    await asyncio.sleep(0.04)
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok')
                await writer.drain()
        except (asyncio.IncompleteReadError,ConnectionError,asyncio.CancelledError):pass
        finally:
            self.writers.discard(writer);writer.close()
            try:await writer.wait_closed()
            except ConnectionError:pass
            self.handlers.discard(asyncio.current_task())
    async def asyncTearDown(self):
        await config.close_clients()
        self.server.close();await self.server.wait_closed()
        for writer in list(self.writers):writer.close()
        if self.handlers:
            await asyncio.wait_for(asyncio.gather(*list(self.handlers),return_exceptions=True),10)
    async def settle_connections(self,limit):
        async with asyncio.timeout(10):
            # handle() removes writers before await writer.wait_closed(); its
            # task remains registered until transport shutdown has completed.
            while len(self.writers)>limit or len(self.handlers)>limit:
                await asyncio.sleep(0.005)
        self.assertLessEqual(len(self.writers),limit)
        self.assertLessEqual(len(self.handlers),limit)
    async def test_real_keepalive_across_lease_clients(self):
        for _ in range(100):
            async with config.http_client(2,proxy='',identity='fixture-tcp') as client:
                response=await client.get(self.url+'/ok')
                self.assertEqual(response.content,b'ok')
        self.assertEqual(self.requests,100);self.assertEqual(self.connections,1)
        self.assertTrue(all(v.references==0 for v in config._CLIENTS.values()))
    async def test_bounded_load_cancel_and_file_descriptors(self):
        fd=Path('/proc/self/fd')
        def fd_snapshot():
            if not fd.is_dir():return {}
            out={}
            for item in fd.iterdir():
                try:out[item.name]=os.readlink(item)
                except FileNotFoundError:pass
            return out
        details={'before':fd_snapshot()}
        before=len(list(fd.iterdir())) if fd.is_dir() else None
        async def request(index,slow=False):
            async with config.http_client(2,proxy='',identity='fixture-load') as client:
                response=await client.get(self.url+('/slow' if slow else '/ok'))
                self.assertEqual(response.content,b'ok')
        samples=[]
        for batch in range(100):
            await asyncio.gather(*(request(i) for i in range(8)))
            # Measure completed keepalive cleanup, not a scheduler-dependent instant
            # while the peer is still processing EOF from excess idle connections.
            await self.settle_connections(config.HTTP_POOL_KEEPALIVE)
            if batch%20==0 and fd.is_dir():
                samples.append(len(list(fd.iterdir())))
                details[str(batch)]={'fds':fd_snapshot(),'writers':len(self.writers),'handlers':len(self.handlers)}
        for batch in range(20):
            tasks=[asyncio.create_task(request(i,True)) for i in range(8)]
            await asyncio.sleep(0.005)
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        await request(1)
        self.assertTrue(all(v.references==0 for v in config._CLIENTS.values()))
        await config.close_clients();await self.settle_connections(0)
        after=len(list(fd.iterdir())) if fd.is_dir() else None
        details['after']=fd_snapshot()
        print(json.dumps({'tcp_requests':self.requests,'connections':self.connections,'fd_samples':samples,'fd_before':before,'fd_after':after,'fd_details':details}))
        if samples:self.assertLessEqual(max(samples)-min(samples),4)
        if before is not None:self.assertLessEqual(after,before+2)
