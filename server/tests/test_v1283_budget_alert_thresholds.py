"""v1.2.83：一条永远为真的告警，等价于没有告警。

## 怎么发现的

审计告警阈值时注意到 `budget_unknown` 的判据是 `needs_human_old > 0` ——
**只要有一条超期就永久 firing**。

实测（2026-11-15）：

| 项 | 数值 |
| --- | --- |
| 已超期未决预算 | **916 条** |
| 最近 24 小时新增 | 约 16 条 |
| 处理方式 | **只有单条核销**，无批量接口 |

九百多条逐条核销不现实，于是这条告警从上线起一直亮着 —— 亮到没人再看它。

## 拆成两件不同的事

| 关注点 | 判据 | 理由 |
| --- | --- | --- |
| 积压是否失控 | 超期 >= 2000 | 已知积压不该反复叫人；涨到失控才叫 |
| 新增是否异常 | 最近 24h 新增 > 100 | 真正的「又出问题了」信号 |

当前 916 / 16，两条都安静 —— 与实际相符。积压规模仍完整写在文案里，
**不报不等于看不见**。

## 顺带修一个会读错的桶名

`new_1h` 其实是「还没到处理期限」，`older_1h` 是「已超期」，**与一小时无关**
（期限由 `WB_BUDGET_REVIEW_DEADLINE_DAYS` 决定）。新增真正的 `new_24h`。
"""
from __future__ import annotations

import inspect
import unittest

from server import operational


def _summary(backlog=916, new_24h=16):
    return {
        'needs_human_old': backlog,
        'review_buckets': {'new_24h': new_24h, 'new_1h': new_24h, 'older_7d': backlog},
        'auto_reconcilable': 0,
        'archived_pending': 0,
        'token_lower_bound': 0,
    }


class BacklogAlertTests(unittest.TestCase):
    def test_the_current_backlog_stays_quiet(self):
        """916 条超期是历史集中事件造成的，不该让告警常亮。"""
        active, _ = operational.budget_unknown_alert(_summary(backlog=916))
        self.assertFalse(active)

    def test_runaway_backlog_fires(self):
        active, detail = operational.budget_unknown_alert(_summary(backlog=2500))
        self.assertTrue(active)
        self.assertIn('2500', detail)

    def test_the_boundary_is_inclusive(self):
        self.assertTrue(operational.budget_unknown_alert(
            _summary(backlog=operational.BUDGET_BACKLOG_LIMIT))[0])
        self.assertFalse(operational.budget_unknown_alert(
            _summary(backlog=operational.BUDGET_BACKLOG_LIMIT - 1))[0])

    def test_zero_backlog_is_quiet(self):
        active, _ = operational.budget_unknown_alert(_summary(backlog=0, new_24h=0))
        self.assertFalse(active)

    def test_detail_still_reports_the_full_breakdown(self):
        """不报不等于看不见 —— 分布必须在文案里。"""
        _, detail = operational.budget_unknown_alert(_summary())
        for field in ('new-24h', 'older-7d', 'token bound'):
            self.assertIn(field, detail)

    def test_detail_says_where_to_settle(self):
        _, detail = operational.budget_unknown_alert(_summary())
        self.assertIn('/api/budgets/review', detail)

    def test_details_stay_ascii(self):
        for summary in (_summary(0, 0), _summary(), _summary(2500, 500)):
            operational.budget_unknown_alert(summary)[1].encode('ascii')


class InflowAlertTests(unittest.TestCase):
    def test_the_current_rate_stays_quiet(self):
        active, _ = operational.budget_unknown_inflow_alert(_summary(new_24h=16))
        self.assertFalse(active)

    def test_a_spike_fires(self):
        active, detail = operational.budget_unknown_inflow_alert(_summary(new_24h=150))
        self.assertTrue(active)
        self.assertIn('150', detail)

    def test_missing_bucket_is_treated_as_zero(self):
        """升级后第一次 tick 可能拿不到新桶 —— 不能因此误报。"""
        active, _ = operational.budget_unknown_inflow_alert({})
        self.assertFalse(active)

    def test_detail_explains_what_a_spike_means(self):
        _, detail = operational.budget_unknown_inflow_alert(_summary(new_24h=150))
        self.assertIn('stopped returning usage', detail,
                      '要说出尖峰通常意味着什么，否则看到的人不知道该查哪儿')

    def test_it_is_a_separate_alert_name(self):
        """共用名字会让「积压安静但新增异常」不产生新通报。"""
        source = inspect.getsource(operational._monitor_tick)
        self.assertIn("transition('budget_unknown'", source)
        self.assertIn("transition('budget_unknown_inflow'", source)


class TheNewBucketIsWiredTests(unittest.TestCase):
    def test_budget_module_counts_new_24h(self):
        from server import budget
        source = inspect.getsource(budget)
        self.assertIn("review_buckets['new_24h']", source)

    def test_it_uses_a_real_24_hour_window(self):
        from server import budget
        source = inspect.getsource(budget)
        start = source.index("review_buckets['new_24h']")
        window = source[max(0, start - 300):start + 100]
        self.assertIn('86400', window,
                      '必须按真正的 24 小时切，不能复用那个名叫 1h 实则看期限的桶')

    def test_the_monitor_calls_both(self):
        source = inspect.getsource(operational._monitor_tick)
        self.assertIn('budget_unknown_alert', source)
        self.assertIn('budget_unknown_inflow_alert', source)


class ThresholdsAreDocumentedTests(unittest.TestCase):
    def test_both_limits_carry_a_comment_with_the_measured_numbers(self):
        source = inspect.getsource(operational)
        for name in ('BUDGET_BACKLOG_LIMIT', 'BUDGET_INFLOW_LIMIT'):
            start = source.index(name)
            window = source[max(0, start - 700):start]
            self.assertIn('916', window + source[start:start + 2000],
                          f'{name} 的取值要有实测依据')

    def test_backlog_limit_exceeds_current_measured_backlog(self):
        self.assertGreater(operational.BUDGET_BACKLOG_LIMIT, 916,
                           '阈值必须高于当前已知积压，否则等于没改')

    def test_inflow_limit_leaves_room_above_current_rate(self):
        self.assertGreater(operational.BUDGET_INFLOW_LIMIT, 16 * 3,
                           '至少要有三倍余量，避免正常波动就报')


if __name__ == '__main__':
    unittest.main()
