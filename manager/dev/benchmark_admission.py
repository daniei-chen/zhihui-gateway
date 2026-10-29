#!/usr/bin/env python3
"""Bounded real-HTTP load against an isolated Manager and private fake provider."""
from __future__ import annotations
import asyncio
import base64
from collections import Counter
import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
import httpx
from run_isolated_tests import Provider


def percentile(values, p):
    ordered=sorted(values)
    return round(ordered[max(0, min(len(ordered)-1, int(len(ordered)*p)-1))], 2) if values else None


def png_fixture():
    """Deterministic RGB PNG with actual pixel data, not a malformed base64 blob."""
    width,height=1024,512
    pixels=hashlib.shake_256(b'zhihui-admission-image-fixture').digest(width*height*3)
    raw=b''.join(b'\0'+pixels[y*width*3:(y+1)*width*3] for y in range(height))
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    png=(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+
         chunk(b'IDAT',zlib.compress(raw,1))+chunk(b'IEND',b''))
    return 'data:image/png;base64,'+base64.b64encode(png).decode()


class Runtime:
    def __init__(self, root, folder, provider, budget):
        self.folder=folder
        self.stop=threading.Event()
        self.samples=[]
        listener=socket.socket()
        listener.bind(('127.0.0.1',0))
        listener.listen(128)
        self.url=f'http://127.0.0.1:{listener.getsockname()[1]}'
        (folder/'auths').mkdir()
        (folder/'home').mkdir()
        (folder/'config.json').write_text('{"server":{"listen":":7863"}}\n')
        env=dict(os.environ, HOME=str(folder/'home'), WB_DATA_DIR=str(folder),
                 WB_AUTH_DIR=str(folder/'auths'), WB_UPSTREAM_CONFIG=str(folder/'config.json'),
                 WB2API_BASE=f'http://127.0.0.1:{provider.server_port}', WB2API_KEY='isolated-upstream-test',
                 WB_BACKGROUND_JOBS='0', WB_MANAGER_HOST='127.0.0.1', WB_SECURE_COOKIE='false',
                 WB_GATEWAY_MAX_CONCURRENCY='60', WB_GATEWAY_LARGE_MAX_CONCURRENCY='50',
                 WB_GATEWAY_MAX_INFLIGHT_BODY_BYTES=str(budget), PYTHONDONTWRITEBYTECODE='1',
                 WB_ADMIN_PASSWORD='AuditFixture-20261028-Sandbox!', PYTHONPATH=str(root),
                 QTG_DATA_DIR=str(folder/'qtg'), QTG_STATIC_DIR=str(folder/'static'), QTG_WB_ENGINE='off',
                 QTG_CHECKIN_SCHEDULER_ENABLED='0', QTG_QODER_ENGINE='qoder2', QTG_TRAE_ENGINE='aiwork',
                 QTG_QODER_BASE=f'http://127.0.0.1:{provider.server_port}',
                 QTG_QODER2_BASE=f'http://127.0.0.1:{provider.server_port}',
                 QTG_TRAE_BASE=f'http://127.0.0.1:{provider.server_port}',
                 QTG_AIWORK_BASE=f'http://127.0.0.1:{provider.server_port}',
                 QTG_QODER_DATA_DIR=str(folder/'qoder'), QTG_TRAE_DATA_DIR=str(folder/'trae'))
        self.log=(folder/'runtime.log').open('wb')
        self.process=subprocess.Popen([sys.executable,'-m','uvicorn','server.main:app',
            '--fd',str(listener.fileno()),'--workers','1','--no-proxy-headers','--log-level','warning'],
            cwd=root, env=env, pass_fds=(listener.fileno(),), stdout=self.log, stderr=subprocess.STDOUT)
        listener.close()
        self.thread=threading.Thread(target=self.sample,daemon=True)
        self.thread.start()

    def sample(self):
        while not self.stop.wait(0.02):
            try:
                data=Path(f'/proc/{self.process.pid}/status').read_text()
                values={line.split(':',1)[0]:int(line.split()[1])*1024 for line in data.splitlines()
                        if line.startswith(('VmRSS:','VmSwap:'))}
                self.samples.append(values)
            except (FileNotFoundError, ProcessLookupError):
                return

    async def connect(self):
        self.client=httpx.AsyncClient(base_url=self.url,timeout=40,trust_env=False)
        for _ in range(150):
            if self.process.poll() is not None:
                raise RuntimeError('Isolated Manager exited during startup')
            try:
                response=await self.client.get('/healthz')
                if response.status_code==200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError('Isolated Manager readiness failed')
        response=await self.client.post('/api/login',json={'username':'admin','password':'AuditFixture-20261028-Sandbox!'})
        if response.status_code!=200:
            raise RuntimeError(f'Fixture administrator login failed: status={response.status_code}')
        self.keys=[]
        for index in range(12):
            response=await self.client.post('/api/keys',json={'name':f'load-{index}','platforms':['workbuddy']})
            if response.status_code!=200:
                raise RuntimeError(f'Fixture key creation failed: status={response.status_code}')
            self.keys.append(response.json()['key'])

    async def close(self):
        if hasattr(self,'client'):
            await self.client.aclose()
        self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.stop.set()
        self.thread.join(timeout=3)
        self.log.close()


async def scenario(runtime, name, messages, rounds=5):
    results=[]
    payload={'model':'fake-model:slow','messages':messages,'max_tokens':8,'stream':True}
    encoded=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
    async def one(index):
        started=time.monotonic()
        first=None
        collected=bytearray()
        async with runtime.client.stream('POST','/v1/chat/completions',content=encoded,
            headers={'Content-Type':'application/json','Authorization':'Bearer '+runtime.keys[index],
                     'User-Agent':'ZhihuiAdmissionLoadTest/20261028'}) as response:
            async for chunk in response.aiter_bytes():
                if first is None and chunk:
                    first=(time.monotonic()-started)*1000
                collected.extend(chunk[:max(0,65536-len(collected))])
            elapsed=(time.monotonic()-started)*1000
            complete=response.status_code!=200 or b'data: [DONE]' in collected
            return {'status':response.status_code,'latency_ms':elapsed,'first_data_ms':first,'complete':complete}
    for _ in range(rounds):
        results.extend(await asyncio.gather(*(one(index) for index in range(12))))
    successes=[row for row in results if row['status']==200]
    return {'name':name,'request_bytes':len(encoded),'concurrency':12,'requests':len(results),
            'statuses':dict(Counter(row['status'] for row in results)),
            'p95_ms':percentile([row['latency_ms'] for row in successes],0.95),
            'first_data_p95_ms':percentile([row['first_data_ms'] for row in successes if row['first_data_ms'] is not None],0.95),
            'all_success_streams_complete':all(row['complete'] for row in successes)}


async def main():
    os.umask(0o077)
    root=Path(__file__).resolve().parent.parent
    output=Path(sys.argv[1] if len(sys.argv)>1 else '/tmp/zh-admission-benchmark.json')
    provider=Provider()
    thread=threading.Thread(target=provider.serve_forever,daemon=True)
    thread.start()
    report={'cst':datetime.datetime.now().astimezone().isoformat(),'environment':'isolated-loopback-http',
            'manager_uid':os.getuid(),'observability_sha256':hashlib.sha256((root/'server/observability.py').read_bytes()).hexdigest(),
            'synthetic_provider_delay_ms':150,'queue_policy':'reject before app entry; no admission queue',
            'image_fixture':{'format':'PNG','width':1024,'height':512,'pixel_data':'deterministic RGB noise'},
            'production_traffic_sent':False,'runs':[]}
    try:
        with tempfile.TemporaryDirectory(prefix='zh-admission-load-') as name:
            folder=Path(name)
            for budget in (64*1024**2,6*1024**2):
                state=folder/str(budget)
                state.mkdir()
                runtime=Runtime(root,state,provider,budget)
                try:
                    await runtime.connect()
                    workloads=[('short',[{'role':'user','content':'Reply OK.'}]),
                        ('long_context',[{'role':'user','content':'x'*(1536*1024)}]),
                        ('image',[{'role':'user','content':[{'type':'text','text':'Describe this image.'},
                            {'type':'image_url','image_url':{'url':png_fixture()}}]}]),
                        ('tool',[{'role':'user','content':'Read the fixture.'},
                            {'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function',
                             'function':{'name':'read','arguments':'{}'}}]},
                            {'role':'tool','tool_call_id':'call_1','content':'x'*(64*1024)}])]
                    if budget==6*1024**2:
                        workloads=[workloads[2]]
                    scenarios=[]
                    for label,messages in workloads:
                        scenarios.append(await scenario(runtime,label,messages))
                    metrics=(await runtime.client.get('/metrics')).text
                    admission={}
                    for line in metrics.splitlines():
                        if line.startswith(('workbuddy_gateway_inflight ','workbuddy_gateway_inflight_body_bytes ',
                                            'workbuddy_gateway_inflight_body_bytes_max ',
                                            'workbuddy_gateway_capacity_rejections_total ')):
                            key,value=line.split(None,1)
                            admission[key]=float(value)
                    report['runs'].append({'raw_body_budget_bytes':budget,'scenarios':scenarios,
                        'rss_peak_bytes':max((v.get('VmRSS',0) for v in runtime.samples),default=0),
                        'swap_peak_bytes':max((v.get('VmSwap',0) for v in runtime.samples),default=0),
                        'alive_after_load':runtime.process.poll() is None,'final_admission':admission})
                    assert admission.get('workbuddy_gateway_inflight')==0
                    assert admission.get('workbuddy_gateway_inflight_body_bytes')==0
                    assert all(s['all_success_streams_complete'] for s in scenarios)
                    if budget==64*1024**2:
                        assert all(set(s['statuses'])=={200} for s in scenarios)
                    else:
                        assert all(set(s['statuses'])<={200,503} and s['statuses'].get(503,0)>0 for s in scenarios)
                except BaseException:
                    runtime.log.flush()
                    output.with_suffix('.fixture.log').write_bytes((state/'runtime.log').read_bytes())
                    raise
                finally:
                    await runtime.close()
        report['status']='verified'
    except BaseException as exc:
        report['status']='failed'
        report['error_type']=type(exc).__name__
        raise
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    asyncio.run(main())
