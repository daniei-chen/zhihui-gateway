"""v1.2.55：attempt 计费事实的补齐不再空转。

## 问题

`backfill_attempt_billing` 用**内连接**筛请求：

    JOIN attempt_events a ON a.request_id = l.request_id

而 `attempt_events` 在 2026-11-03 之后停止写入 —— 上游事件开始携带 `dispatch_id`，
`ledger.attempt()` 见到这个字段直接返回 False，调用证据改由
`provider_attempt_events_v2` 承载。

内连接一张空表 = 永远空集。于是在生产上：

| 事实 | 值 |
| --- | --- |
| 事实表最后一次写入 | 2026-11-03 19:01 |
| 之后缺事实的请求 | 59,699 |
| 其中有新表证据、本可补齐 | **59,016** |
| 补齐任务的日志 | 照常每轮跑、照常返回 0 |

「任务在跑、返回 0」和「任务失败」看起来完全不同，前者不会引起任何注意 ——
这才是它藏了十一天的原因。

## 修法

两个 EXISTS 取代内连接：任一表有该请求的调用记录即入选。

推导规则本身不动 —— `derive_attempt_billing` 早就认识新表（有 v2 证据时如实给出
unknown，因为缺供应商费用回执）。缺的只是「把这些请求挑出来」这一步。
"""
from __future__ import annotations

import unittest
from pathlib import Path

SOURCE = (Path(__file__).parents[1] / 'ledger.py').read_text(encoding='utf-8')


def backfill_body() -> str:
    start = SOURCE.index('def backfill_attempt_billing(')
    tail = SOURCE[start:]
    end = tail.find('\ndef ')
    return tail if end == -1 else tail[:end]


def backfill_sql() -> str:
    """只取实际执行的 SQL 片段。

    docstring 里为了说明来历，写上了旧的那句 `JOIN attempt_events ...` ——
    整段做字符串匹配会把解释文字当成代码，断言就会误报。这里从 `db.query(`
    起截取，只看真正发给数据库的东西。
    """
    body = backfill_body()
    return body[body.index('db.query('):]


class BackfillSelectsBothTablesTests(unittest.TestCase):
    def test_it_no_longer_inner_joins_the_stopped_table(self):
        sql = backfill_sql()
        self.assertNotIn('JOIN attempt_events', sql,
                         '又回到内连接那张停写的表了')

    def test_it_selects_on_either_table(self):
        sql = backfill_sql()
        self.assertIn('EXISTS (SELECT 1 FROM attempt_events', sql)
        self.assertIn('EXISTS (SELECT 1 FROM provider_attempt_events_v2', sql)

    def test_it_is_an_or_not_an_and(self):
        """两表是「任一存在」，不是「同时存在」。"""
        sql = backfill_sql()
        mark = sql.index('EXISTS (SELECT 1 FROM attempt_events')
        between = sql[mark:sql.index('EXISTS (SELECT 1 FROM provider_attempt_events_v2')]
        self.assertIn('OR', between, '两个 EXISTS 之间必须是 OR')

    def test_the_unfilled_condition_is_kept(self):
        """只补没落过事实的请求 —— 这条不能丢，否则会重复劳动。"""
        self.assertIn('f.request_id IS NULL', backfill_sql())

    def test_the_window_and_limit_are_kept(self):
        sql = backfill_sql()
        self.assertIn('l.ts<=?', sql)
        self.assertIn('LIMIT ?', sql)

    def test_the_reason_is_documented_with_the_numbers(self):
        body = backfill_body()
        self.assertIn('59,016', body, '把实测数字写进注释，便于日后对照')
        self.assertIn('provider_attempt_events_v2', body)


class DerivationStillKnowsTheNewTableTests(unittest.TestCase):
    def test_derive_consults_provider_events_first(self):
        """推导侧原本就认识新表，这次没动它 —— 断言它还在。"""
        start = SOURCE.index('def derive_attempt_billing(')
        body = SOURCE[start:start + 900]
        self.assertIn('providerevents.report', body)

    def test_unknown_is_still_the_answer_for_v2_evidence(self):
        """有调用证据但缺费用回执时给 unknown，不编金额。"""
        start = SOURCE.index('def derive_attempt_billing(')
        body = SOURCE[start:start + 1400]
        self.assertIn('BILLING_UNKNOWN', body)


class LedgerAttemptWriterTests(unittest.TestCase):
    def test_the_old_writer_still_refuses_dispatch_events(self):
        """`attempt()` 见到 dispatch_id 就跳过 —— 这正是老表停写的原因。

        这条不是要改的行为（新表接管了），但要留住，否则有人会以为它坏了。
        """
        start = SOURCE.index('def attempt(value)')
        body = SOURCE[start:start + 600]
        self.assertIn("value.get('dispatch_id')", body)


if __name__ == '__main__':
    unittest.main()
