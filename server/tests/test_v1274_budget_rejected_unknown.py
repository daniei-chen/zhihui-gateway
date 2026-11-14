"""v1.2.74：给「被拒却留着未决预留」加专门告警。

## 为什么

`budget_unknown` 的判据是「积压超过处理期限」，它把各种成因汇总在一起
（客户端断开、上游报错、传输中断……）—— 那些是机制固有的不确定性，可以接受。

但有一类**不该存在**：请求被拒绝（400/429），预算却留在未决状态。
拒绝意味着什么都没发生，预留应当归还。

实测 2026-11-15：库里有 156 条这种残留，形态都是「prompt 太长」的 400 却预留了
上千万 token。集中在 11-03（112 条）、11-10、11-12，**11-13 之后为 0** ——
链路已经修好，但**没有告警盯着它**，将来回归只能靠人工翻数据发现。

所以新增这条，只看**最近 24 小时**：历史残留不该让它长期 firing，
新出现一条就说明有回归。
"""
from __future__ import annotations

import inspect
import pathlib
import unittest

from server import operational


class BudgetRejectedUnknownAlertTests(unittest.TestCase):
    def test_zero_does_not_fire(self):
        active, _ = operational.budget_rejected_unknown_alert(0)
        self.assertFalse(active)

    def test_any_new_occurrence_fires(self):
        """一条就算 —— 这种行本身就不该存在，不需要比例判断。"""
        for count in (1, 3, 156):
            active, _ = operational.budget_rejected_unknown_alert(count)
            self.assertTrue(active, f'{count} 条应当告警')

    def test_none_is_treated_as_zero(self):
        active, _ = operational.budget_rejected_unknown_alert(None)
        self.assertFalse(active)

    def test_detail_carries_the_count(self):
        _, detail = operational.budget_rejected_unknown_alert(7)
        self.assertIn(': 7', detail)

    def test_detail_says_what_the_rule_is(self):
        _, detail = operational.budget_rejected_unknown_alert(1)
        self.assertIn('a rejection must release its reservation', detail,
                      '要说明为什么这算异常，否则看的人不知道严重性')

    def test_detail_points_at_the_report(self):
        _, detail = operational.budget_rejected_unknown_alert(1)
        self.assertIn('LEDGER-RECONCILIATION.md', detail)

    def test_detail_stays_ascii(self):
        for count in (0, 1, 156):
            _, detail = operational.budget_rejected_unknown_alert(count)
            detail.encode('ascii')


class ItLooksAtTheLastDayOnlyTests(unittest.TestCase):
    """历史残留不能让它长期 firing —— 那会把告警变成背景噪音。"""

    def test_the_window_is_24_hours(self):
        source = inspect.getsource(operational._monitor_tick)
        start = source.index('budget_rejected_unknown_alert')
        window = source[max(0, start - 800):start]
        self.assertIn('86400', window, '窗口应当是一天，否则历史残留会让它常亮')

    def test_it_joins_on_request_logs_to_read_the_terminal_state(self):
        source = inspect.getsource(operational._monitor_tick)
        start = source.index('budget_rejected_unknown_alert')
        window = source[max(0, start - 800):start]
        self.assertIn('terminal_state', window)
        self.assertIn("state='unknown'", window)

    def test_it_is_wired_into_the_monitor(self):
        source = inspect.getsource(operational._monitor_tick)
        self.assertIn("transition('budget_rejected_unknown'", source)


class TheQueryUsesIndexesTests(unittest.TestCase):
    """这条查询在定期任务里跑，不能全表扫。

    `budget_reservations` 上有个 `(state, created_at, request_id)` 的索引
    （v1.2.60 加的），这里正好用得上 —— 用真库验证一次路径没有退化。
    """

    def test_plan_uses_an_index_on_budget_reservations(self):
        import sqlite3
        conn = sqlite3.connect(':memory:')
        conn.executescript(
            'CREATE TABLE budget_reservations (request_id TEXT, created_at INTEGER, '
            'state TEXT);'
            'CREATE INDEX idx_budget_state_scan ON budget_reservations(state, created_at, request_id);'
            'CREATE TABLE request_logs (request_id TEXT, terminal_state TEXT);'
            'CREATE INDEX idx_request_logs_request_id ON request_logs(request_id);')
        plan = ' '.join(
            row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT count(*) FROM budget_reservations b "
                "JOIN request_logs l ON l.request_id=b.request_id "
                "WHERE b.state='unknown' AND l.terminal_state='rejected' "
                "AND b.created_at>=1"))
        conn.close()
        self.assertIn('idx_budget_state_scan', plan,
                      '预算侧应当走 (state, created_at) 索引，否则定期任务会全表扫')
        self.assertNotIn('SCAN b', plan)


if __name__ == '__main__':
    unittest.main()
