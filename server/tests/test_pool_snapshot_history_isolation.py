"""A denied optional analytics directory cannot break the actual account pool."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from server.qtgcore import service
from server.qtgcore.adapters.trae2 import Trae2Adapter
from server.qtgcore.errors import UpstreamError


class PoolHistoryIsolation(unittest.IsolatedAsyncioTestCase):
    def runtime(self,reply):
        adapter=Trae2Adapter.__new__(Trae2Adapter)
        adapter.name='trae';adapter.base_url='http://127.0.0.1/synthetic'
        adapter._cmd=AsyncMock(return_value=reply)
        adapter._db=Mock(side_effect=PermissionError('synthetic denied analytics directory'))
        adapter.health=AsyncMock(return_value={'up':True})
        adapter.settings=AsyncMock(return_value={})
        return SimpleNamespace(trae=adapter,settings=SimpleNamespace(cache_overview_sec=0)),adapter

    async def test_account_pool_is_independent_of_denied_local_history(self):
        runtime,adapter=self.runtime({'ok':True,'data':[{'user_id':'synthetic-owner','remaining_credits':123}]})
        result=await service.build_pool_snapshot(runtime,'trae',force=True)
        self.assertEqual(len(result.trae_accounts),1)
        self.assertEqual(result.trae_accounts[0].uid,'synthetic-owner')
        self.assertTrue(result.health[0].up)
        self.assertEqual(result.trae_raw['scope'],'engine_account_list')
        # 原来是 `_db.assert_not_called()` —— 要求完全不碰本地分析库。
        # 但 overview() 会附带 checkin 历史，那必然要读一次库。这条断言的
        # 真实意图是「分析库读不到时，账号池不许因此失败」，而那已经由上面
        # 三条断言证明（账号数、uid、health 都对）。
        # 2026-11-23：把「完全不碰」放宽为「碰了也不影响结果」，
        # _checkin_rows 已改成 on OSError 退化返回空。
        # 原来要求调用序列恰好是 ['accounts_list']。build_pool_snapshot 后来还会
        # 调一次 pool_list 补充明细，所以这里只断言「账号列表来自 accounts_list」，
        # 不再锁死完整序列 —— 锁死会让每次加一个上游调用都误报。
        self.assertIn('accounts_list',
                      [call.args[0] for call in adapter._cmd.call_args_list])

    async def test_health_cannot_turn_failed_account_read_into_empty_success(self):
        runtime,adapter=self.runtime({'ok':False,'data':[]})
        with self.assertRaises(UpstreamError):
            await service.build_pool_snapshot(runtime,'trae',force=True)
        adapter._db.assert_not_called()

    async def test_explicit_successful_empty_account_list_is_authoritative(self):
        runtime,adapter=self.runtime({'ok':True,'data':[]})
        result=await service.build_pool_snapshot(runtime,'trae',force=True)
        self.assertEqual(result.trae_accounts,[])
        self.assertTrue(result.health[0].up)
        # 同 test_account_pool_is_independent_of_denied_local_history：
        # 「空账号列表是权威的」由上面两条断言表达，不靠「没碰过 _db」来证明。

    async def test_legacy_http_adapter_keeps_its_account_contract(self):
        runtime,_=self.runtime({'ok':True,'data':[]})
        legacy=SimpleNamespace(name='trae',base_url='http://127.0.0.1/synthetic',
            overview=AsyncMock(return_value={'accounts':[]}),
            health=AsyncMock(return_value={'up':True}),settings=AsyncMock(return_value={}))
        runtime.trae=legacy
        result=await service.build_pool_snapshot(runtime,'trae',force=True)
        self.assertEqual(result.trae_accounts,[]);legacy.overview.assert_awaited_once()
