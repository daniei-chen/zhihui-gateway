"""G-07（审计 D-08）：管理员点「重置用量」后，同一天必须真的能再发请求。

## 问题

`budget.prepare` 判日额度读的是 `dailyquota.snapshot(key_id, day)`，也就是
`key_daily_usage` 这张独立计数表（`server/budget.py:291`、`:312-319`），它不看
`api_keys.used_tokens`。而 `keysvc.reset_usage` 原来只清 `api_keys`
（`server/keysvc.py:390`）。

于是管理端的「重置用量」变成一句谎话：列表上的已用变成 0、`used_tokens` 归零，
但 `key_daily_usage` 里今天的计数还在，下一个请求照样
`daily_token_quota_exhausted`（429）。复现见本目录测试的
`test_reset_clears_today_daily_counters_so_same_day_request_passes`。

## 这条测试为什么必须用真实 `budget.prepare`

如果只断言「`key_daily_usage` 里那行变成 0」，把清表删掉照样能靠别的方式凑绿；
真正要证明的是**调用方不再被挡**。所以这里走 `budget.prepare` —— 与网关同一条
路径、同一份拒绝码。

## 第二个坑：删行不等于清零

`dailyquota.snapshot` 在 `key_daily_usage` 没有对应行时会**回退**去汇总
`usage_daily`（`server/dailyquota.py:29-39`，为的是把功能上线前的存量流量算进去）。
所以「删掉今天这行」会把刚重置掉的用量重新数回来。今天的行必须**显式写 0**，
`test_reset_masks_the_usage_daily_fallback` 守的就是这一点。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import budget, budgetowner, config, dailyquota, db, keysvc, observability


class ResetDailyUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_conn = db._conn
        db._conn = None
        self.addCleanup(self._cleanup_db)
        for name, value in {'DATA_DIR': Path(self.tmp.name),
                            'DB_PATH': Path(self.tmp.name) / 'reset.db',
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

    def _prepare(self, key, request_id='g07-request'):
        """走网关同一条路径：`budget.prepare` 判的就是日额度。"""
        state = observability.RequestAudit(request_id, '/v1/chat/completions', 'POST',
                                           'openai', time.monotonic())
        observability.current.set(state)
        # 估算输入 ≈ body 字节 + 2048 头寸；额度取 6000 才留得住「重置前拒绝、
        # 重置后通过」这个区间（额度小于 2048 的头寸时，重置完也过不去）。
        budget.prepare(key, {'model': 'synthetic-model',
                             'messages': [{'role': 'user', 'content': 'hi'}]})
        return state

    def test_reset_clears_today_daily_counters_so_same_day_request_passes(self):
        """重置 → 同一天同一把密钥必须能再发请求，不是「界面清零、请求 429」。"""
        key = keysvc.create_key('g07-reset', daily_token_quota=6000)
        keysvc.touch(key, '127.0.0.1', 5000, 0)
        dailyquota.charge(key['id'], 5000, 0, int(time.time()))
        self.assertGreaterEqual(dailyquota.snapshot(key['id'], self.day)['tokens'], 5000)
        with self.assertRaises(budget.BudgetRejected) as error:
            self._prepare(key, 'g07-before-reset')
        self.assertEqual(error.exception.code, 'daily_token_quota_exhausted')

        self.assertTrue(keysvc.reset_usage(key['id'], actor='g07-test'))

        self.assertEqual(dailyquota.snapshot(key['id'], self.day)['tokens'], 0,
                         '重置后当天的日配额计数必须归零')
        self._prepare(key, 'g07-after-reset')
        self.assertGreater(
            db.query_one('SELECT COUNT(*) FROM budget_reservations WHERE key_id=?', (key['id'],))[0], 0,
            '重置后 prepare 必须真的走到预留，否则「通过」是假的')

    def test_reset_masks_the_usage_daily_fallback(self):
        """今天的行必须显式写 0：删行会让 snapshot 回退去汇总 usage_daily。"""
        key = keysvc.create_key('g07-fallback', daily_token_quota=6000)
        db.execute("INSERT INTO usage_daily(day,key_id,model,requests,prompt_tokens,completion_tokens,credit,realm) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (self.day, key['id'], 'synthetic-model', 1, 3000, 2000, 0, 'cn'))
        self.assertEqual(db.query_one('SELECT COUNT(*) FROM key_daily_usage WHERE key_id=? AND day=?',
                                      (key['id'], self.day))[0], 0, '前提：今天还没有独立计数行')
        self.assertEqual(dailyquota.snapshot(key['id'], self.day)['tokens'], 5000,
                         '前提：没有独立行时会回退汇总 usage_daily')

        self.assertTrue(keysvc.reset_usage(key['id'], actor='g07-test'))

        self.assertEqual(dailyquota.snapshot(key['id'], self.day)['tokens'], 0,
                         '重置后必须挡住 usage_daily 回退，否则今天的用量又数回来')

    def test_reset_leaves_other_days_untouched(self):
        """历史日的计数是报表口径：只清今天，不许一把清空整张表。"""
        key = keysvc.create_key('g07-history', daily_token_quota=6000)
        yesterday = db.day_of(int(time.time()) - 86400)
        db.execute('INSERT INTO key_daily_usage(key_id,day,tokens,credit_units) VALUES(?,?,?,?)',
                   (key['id'], yesterday, 4321, 0))
        dailyquota.charge(key['id'], 5000, 0, int(time.time()))

        self.assertTrue(keysvc.reset_usage(key['id'], actor='g07-test'))

        self.assertEqual(dailyquota.snapshot(key['id'], self.day)['tokens'], 0)
        self.assertEqual(db.query_one('SELECT tokens FROM key_daily_usage WHERE key_id=? AND day=?',
                                      (key['id'], yesterday))['tokens'], 4321,
                         '昨天那一行被顺手清掉了（重置只该管今天）')


if __name__ == '__main__':
    unittest.main()
