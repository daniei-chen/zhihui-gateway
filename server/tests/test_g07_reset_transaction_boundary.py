"""SWO-68：`reset_usage` 的事务边界——语句中途失败必须整体回滚。

## 为什么还要一条测试

G-07 那三条（`test_g07_reset_daily_usage.py`）证明的是**终态**：重置之后
`api_keys` 与 `key_daily_usage` 都是 0、同一天能再发请求。它们守不住的是
「一半成功」：如果 `key_daily_usage` 的清理被写到事务外（`db.execute`）、
或者事务包裹被拆开，`api_keys.used_tokens` 会**先被提交成 0**，随后日计数
清理抛错——于是管理员看到「重置失败」，用量却已经被清空，`usage_reset_events`
里也留下一条零头记录。终态断言的测试对这条路径全绿。

所以这里的判据落在**中间态**：让事务里的一条写真的抛错（SQLite
`RAISE(ABORT)` 触发器，走 `reset_usage` 自己的 `conn.execute`），然后在
异常之后立刻读 `api_keys.used_tokens` ——它必须还是原值。

## 为什么用真触发器而不是 mock

`reset_usage` 自己开 `db.transaction()`（`server/db.py` 的 RLock 串行 +
`BEGIN IMMEDIATE` + 外层提交/回滚）。mock 掉 `conn.execute` 等于把被测
的回滚路径一起换掉，测的就不是 `db.transaction` 了。触发器让**真实的一条
SQL**失败，回滚路径原样执行。

## 用 fixture 当对照的边界

这里的「原值」是本测试自己 `touch` 出来的 fixture 行，只证明判据咬得住
（这条路径上事务会回滚），**不证明**生产数据在并发/跨进程写入下不会被破坏。
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import budget, budgetowner, config, dailyquota, db, keysvc, observability


class ResetTransactionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_conn = db._conn
        db._conn = None
        self.addCleanup(self._cleanup_db)
        for name, value in {'DATA_DIR': Path(self.tmp.name),
                            'DB_PATH': Path(self.tmp.name) / 'boundary.db',
                            'USERS_FILE': Path(self.tmp.name) / 'users.json'}.items():
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch('server.ledgerreplay.admission_block', return_value={'blocked': False})
        patch.start()
        self.addCleanup(patch.stop)
        budget.initialize()
        token = observability.current.set(None)
        self.addCleanup(observability.current.reset, token)
        self.day = db.day_of()

    def _cleanup_db(self):
        observability.current.set(None)
        budgetowner.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = self.old_conn

    def _used_key(self, name: str, tokens: int = 5000, credit: float = 2.5) -> dict:
        """一把已经用掉 tokens/credit 的密钥：这是「失败后必须保持」的原值。"""
        key = keysvc.create_key(name, daily_token_quota=6000)
        keysvc.touch(key, '127.0.0.1', tokens, credit)
        self.assertEqual(self._used_tokens(key['id']), tokens, '前提：用量已记到 api_keys')
        return key

    def _used_tokens(self, key_id: int) -> int:
        return db.query_one('SELECT used_tokens FROM api_keys WHERE id=?', (key_id,))['used_tokens']

    def _used_credit_units(self, key_id: int):
        return db.query_one('SELECT used_credit_units FROM api_keys WHERE id=?', (key_id,))['used_credit_units']

    def _daily_tokens(self, key_id: int) -> int:
        return dailyquota.snapshot(key_id, self.day)['tokens']

    def _reset_events(self, key_id: int) -> int:
        return db.query_one('SELECT COUNT(*) FROM usage_reset_events WHERE key_id=?', (key_id,))[0]

    def _fault(self, table: str, verb: str):
        """让事务里打到 `table` 的那条 `verb` 语句真实失败。名字固定，不拼用户输入。"""
        db.execute(f'CREATE TRIGGER g07_fault BEFORE {verb} ON {table} '
                   f"BEGIN SELECT RAISE(ABORT,'synthetic {table} {verb.lower()} failure'); END")

    def _drop_fault(self):
        db.execute('DROP TRIGGER g07_fault')

    def test_daily_counter_insert_failure_rolls_back_the_key_update(self):
        """事务里第 4 条写（`key_daily_usage` 的 INSERT）抛错：前面的清零必须回滚。"""
        key = self._used_key('swo68-daily-insert')
        credit = self._used_credit_units(key['id'])
        self._fault('key_daily_usage', 'INSERT')

        with self.assertRaises(sqlite3.IntegrityError) as error:
            keysvc.reset_usage(key['id'], actor='swo68-test')
        self.assertIn('synthetic key_daily_usage insert failure', str(error.exception),
                      '必须是注入的那条失败，不是别的原因')

        self.assertEqual(self._used_tokens(key['id']), 5000,
                         '事务里后一条写抛错后 api_keys.used_tokens 必须还是原值（否则就是先提交后失败）')
        self.assertEqual(self._used_credit_units(key['id']), credit, '已用积分同理，必须一起回滚')
        self.assertEqual(self._reset_events(key['id']), 0,
                         'usage_reset_events 的审计行也必须跟着回滚（否则留下一条「重置过」的假记录）')
        self.assertEqual(self._daily_tokens(key['id']), 5000, '日计数没被改动')
        self.assertEqual(getattr(db._atomic_state, 'depth', 0), 0, '回滚后不能留下未结束的事务深度')

    def test_daily_counter_update_failure_rolls_back_the_key_update(self):
        """最后一条写（`key_daily_usage` 的 UPDATE）抛错同样是整体回滚。"""
        key = self._used_key('swo68-daily-update')
        self._fault('key_daily_usage', 'UPDATE')

        with self.assertRaises(sqlite3.IntegrityError) as error:
            keysvc.reset_usage(key['id'], actor='swo68-test')
        self.assertIn('synthetic key_daily_usage update failure', str(error.exception))

        self.assertEqual(self._used_tokens(key['id']), 5000, '走到最后一条才失败也必须整体回滚')
        self.assertEqual(self._reset_events(key['id']), 0)
        self.assertEqual(self._daily_tokens(key['id']), 5000)

    def test_reset_event_failure_leaves_key_and_daily_untouched(self):
        """第一条写（审计行）就失败：同样不许有零头写入。"""
        key = self._used_key('swo68-event-insert')
        self._fault('usage_reset_events', 'INSERT')

        with self.assertRaises(sqlite3.IntegrityError) as error:
            keysvc.reset_usage(key['id'], actor='swo68-test')
        self.assertIn('synthetic usage_reset_events insert failure', str(error.exception))

        self.assertEqual(self._used_tokens(key['id']), 5000)
        self.assertEqual(self._reset_events(key['id']), 0)
        self.assertEqual(self._daily_tokens(key['id']), 5000)

    def test_retry_after_fault_removed_resets_cleanly(self):
        """回滚要干净到能立刻重试：故障去掉后第二次必须成功且不留旧痕迹。"""
        key = self._used_key('swo68-retry')
        self._fault('key_daily_usage', 'INSERT')
        with self.assertRaises(sqlite3.IntegrityError):
            keysvc.reset_usage(key['id'], actor='swo68-test')
        self._drop_fault()

        self.assertTrue(keysvc.reset_usage(key['id'], actor='swo68-test'), '回滚后必须还能重试成功')
        self.assertEqual(self._used_tokens(key['id']), 0)
        self.assertEqual(self._reset_events(key['id']), 1, '失败那次不许留审计行，成功这次必须留一条')
        self.assertEqual(self._daily_tokens(key['id']), 0)


if __name__ == '__main__':
    unittest.main()
