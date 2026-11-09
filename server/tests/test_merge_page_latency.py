"""Selected pages must not wait for unrelated provider I/O."""
import asyncio
from types import SimpleNamespace

from server.qtgcore import service
from server.services.scope import fixed


class FakeAdapter:
    def __init__(self,name,*,release=None,entered=None,forbidden=False):
        self.name=name;self.base_url='http://127.0.0.1/fixture'
        self.release=release;self.entered=entered;self.forbidden=forbidden;self.calls=[]

    def __getattr__(self,method):
        async def call(*args,**kwargs):
            if self.forbidden:raise AssertionError('unselected provider was called: '+self.name+'.'+method)
            self.calls.append(method)
            if self.entered:self.entered.set()
            if self.release:await self.release.wait()
            return {'up':True} if method=='health' else {}
        return call


def runtime(q,t,w):
    return SimpleNamespace(qoder=q,trae=t,wb=w,settings=SimpleNamespace(
        cache_overview_sec=0,qoder_base='http://127.0.0.1/q',trae_base='http://127.0.0.1/t',wb_api_base='http://127.0.0.1/w'))


def test_selected_workbuddy_reads_make_no_calls_to_other_platforms():
    async def run():
        q,t=FakeAdapter('qoder',forbidden=True),FakeAdapter('trae',forbidden=True)
        w=FakeAdapter('workbuddy')
        result=await service.build_aggregate(runtime(q,t,w),scope=fixed('workbuddy',authority='upstream'),force=True)
        assert q.calls==t.calls==[]
        assert set(w.calls)=={'overview','usage','settings','health'}
        assert len(result.health)==1 and result.health[0].name=='workbuddy' and result.health[0].up
        assert result.errors==[]
    asyncio.run(run())


def test_all_platform_reads_start_workbuddy_before_slow_qoder_and_trae_complete():
    async def run():
        release,started=asyncio.Event(),asyncio.Event()
        q,t=FakeAdapter('qoder',release=release),FakeAdapter('trae',release=release)
        w=FakeAdapter('workbuddy',entered=started)
        task=asyncio.create_task(service.build_aggregate(runtime(q,t,w),force=True))
        try:
            await asyncio.wait_for(started.wait(),3)
            assert not task.done() and not release.is_set()
        finally:
            release.set()
            result=await task
        assert len(result.health)==3 and all(row.up for row in result.health)
    asyncio.run(run())
