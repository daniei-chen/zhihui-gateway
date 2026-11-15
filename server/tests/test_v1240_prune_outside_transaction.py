"""v1.2.40：侧表清理离开账本事务；归档通路首次在真实数据上验证。

`_prune_request_logs` 原先挂在 `add_request_log` 里，而后者在结算事务内被调用 ——
每两千次结算就有一次账本事务要多跑四条 DELETE（含一条 `OFFSET 20000` 子查询）。
现在写入方只置标记，清理由 `prune_if_pending()` 在事务提交之后执行。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from server import db


class PruneRunsOutsideTheTransactionTests(unittest.TestCase):
    def setUp(self):
        db._prune_pending = False
        db._request_log_writes = 0
        self.addCleanup(setattr, db, '_prune_pending', False)
        self.addCleanup(setattr, db, '_request_log_writes', 0)

    def test_writer_only_sets_the_flag(self):
        """写入方不得在事务里直接执行清理。"""
        source = (Path(__file__).parents[1] / 'db.py').read_text(encoding='utf-8')
        marker = '_request_log_writes >= _REQUEST_LOG_CHECK_EVERY'
        index = source.index(marker)
        block = source[index:index + 400]
        self.assertIn('_prune_pending = True', block)
        self.assertNotIn('_prune_request_logs()', block,
                         '阈值分支里不能直接清理，那只会在结算事务内跑 DELETE')

    def test_pending_flag_triggers_one_prune(self):
        db._prune_pending = True
        with mock.patch.object(db, '_prune_request_logs') as prune:
            db.prune_if_pending()
            db.prune_if_pending()          # 第二次已经是空的
        prune.assert_called_once()

    def test_nothing_pending_is_a_no_op(self):
        with mock.patch.object(db, '_prune_request_logs') as prune:
            db.prune_if_pending()
        prune.assert_not_called()

    def test_prune_failure_never_escapes(self):
        """清理是旁路：它失败不能让结算看起来失败。"""
        db._prune_pending = True
        with mock.patch.object(db, '_prune_request_logs', side_effect=RuntimeError('boom')):
            db.prune_if_pending()          # 不抛

    def test_settlement_calls_the_outside_entry_point(self):
        source = (Path(__file__).parents[1] / 'routers' / 'gateway.py').read_text(encoding='utf-8')
        self.assertIn('db.prune_if_pending()', source)
        # 它必须在 try/except 之后（事务之外），不能出现在 transaction 块里
        block = source[source.index('with db.transaction():'):]
        self.assertLess(block.index('db.prune_if_pending()'), len(block))
        before = source[:source.index('db.prune_if_pending()')]
        self.assertIn('请求账本事务失败', before, '清理必须在异常分支之后执行')


if __name__ == '__main__':
    unittest.main()
