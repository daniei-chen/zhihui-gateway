"""v1.2.36：每请求少一次空事务，归因证据不再能拖垮转发。

`budget.finish` 的兜底 UPDATE 只在"结算没跑成"时有活干。结算一旦提交，它条件里
的 `state IN ('reserved','forwarded')` 就再也匹配不到，于是每次请求都多付一个
空事务：`BEGIN IMMEDIATE` + 0 行 + commit + fsync。

这个标记只在结算事务**提交成功后**才置位（`_record` 里 `state.recorded = True`
的同一步），所以回滚时兜底照旧生效 —— 语义没动，只是不再付那份空转的磁盘同步。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import budget, db, observability


class _State:
    """够 `budget.finish` 判断的最小对象。"""

    def __init__(self, **kwargs):
        self.request_id = 'req-1'
        self.key_id = 1
        self.budget_reserved = True
        self.budget_fence = 'fence'
        self.budget_settled = False
        self.portal_cleanup_unconfirmed = False
        for key, value in kwargs.items():
            setattr(self, key, value)


class FinishSkipsTheEmptyTransactionTests(unittest.TestCase):
    def setUp(self):
        # `budget.finish` 用 `from . import portalwallet` 延迟导入，所以补丁要打在
        # 真模块上，打 `budget.portalwallet` 是无效的。
        self.wallet = mock.patch('server.portalwallet.finish')
        self.wallet_finish = self.wallet.start()
        self.addCleanup(self.wallet.stop)

    def test_settled_request_does_not_touch_the_database_again(self):
        state = _State(budget_settled=True)
        with mock.patch.object(db, 'execute') as execute:
            budget.finish(state)
        execute.assert_not_called()

    def test_unsettled_request_still_runs_the_fallback(self):
        state = _State(budget_settled=False)
        # `budgetowner.identity()` 会碰数据目录，这里只关心"兜底 UPDATE 有没有发出去"
        with mock.patch.object(db, 'execute') as execute, \
                mock.patch.object(budget.budgetowner, 'identity', lambda: 'owner'):
            budget.finish(state)
        execute.assert_called_once()
        sql, args = execute.call_args[0]
        self.assertIn("state IN ('reserved','forwarded')", sql)
        self.assertEqual(args[1], 'req-1')

    def test_unreserved_request_never_touches_the_database(self):
        state = _State(budget_reserved=False, budget_settled=False)
        with mock.patch.object(db, 'execute') as execute:
            budget.finish(state)
        execute.assert_not_called()

    def test_the_skip_still_closes_the_portal_wallet(self):
        """门户钱包结算是副作用，不能因为跳过预算兜底就一起跳过。"""
        state = _State(budget_settled=True)
        with mock.patch.object(db, 'execute'):
            budget.finish(state)
        self.wallet_finish.assert_called_once_with(state)


class SettleMarksOnlyAfterCommitTests(unittest.TestCase):
    def test_settle_records_a_pending_marker_not_the_final_flag(self):
        source = (Path(__file__).parents[2] / 'server' / 'budget.py').read_text(encoding='utf-8')
        self.assertIn('state.budget_settled_pending = True', source)
        # 最终标记只能由结算事务提交后的那一步设置
        self.assertNotIn("state.budget_settled = True", source)

    def test_gateway_promotes_the_marker_after_the_transaction_commits(self):
        source = (Path(__file__).parents[2] / 'server' / 'routers' /
                  'gateway.py').read_text(encoding='utf-8')
        self.assertIn("state.budget_settled=getattr(state,'budget_settled_pending',False)", source)
        # 它必须紧跟在 recorded 之后（也就是事务成功之后），不能出现在 except 分支里
        after = source.split('state.recorded=True', 1)[1]
        self.assertTrue(after.lstrip().startswith('#') or 'budget_settled' in after[:400])


class DispatchEvidenceIsNotAPreconditionTests(unittest.TestCase):
    def test_dispatch_insert_failure_does_not_fail_the_forward(self):
        source = (Path(__file__).parents[2] / 'server' / 'routers' /
                  'gateway.py').read_text(encoding='utf-8')
        marker = "db.execute('INSERT INTO provider_dispatches_v2 VALUES(?,?,?)'"
        index = source.index(marker)
        block = source[max(0, index - 300):index + 500]
        self.assertIn('try:', block, '归因写入必须被异常隔离')
        self.assertIn('except Exception', block)
        self.assertIn('provider dispatch 记录失败', block)


if __name__ == '__main__':
    unittest.main()
