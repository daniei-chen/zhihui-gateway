"""v1.2.69：模型失败率告警加绝对下限。

## 起因

审计时发现 24 小时里有 57 条 `operations` ERROR，其中一半是 `model_completion`。
查一次具体的：

| 事实 | 值 |
| --- | --- |
| 窗口 | 5 分钟 |
| 请求数 | **25** |
| 失败数 | **2** |
| 比例 | 8% > 1% → 触发 |

那 25 个样本里，**1 个失败就是 4%、2 个就是 8%** —— 一两起偶发就够触发。而这
两个失败还是边界探测产生的（上游回「没这个模型」），不是真故障。

**低流量下，1% 这个比例判据没有统计意义。**

## 改法

比例与绝对失败数**两个都要满足**：

| 场景 | 判断 |
| --- | --- |
| 20 个请求 + 3 个失败（15%） | **告警**，值得看一眼 |
| 1000 个请求 + 3 个失败（0.3%） | 不告警，本来就不该 |
| 1000 个请求 + 30 个失败（3%） | **告警** |

真实故障通常是批量失败（上游整个不可用会成片出错），不会只掉一两个。

## 附带

判据与文案提取成 `model_completion_alert(n, failed)`，与 `budget_unknown_alert`
同一形状 —— 这样能直接测边界值，不必去 mock 整个监控循环。
"""
from __future__ import annotations

import inspect
import unittest

from server import operational


class ModelCompletionAlertTests(unittest.TestCase):
    def test_two_failures_in_a_small_window_no_longer_fires(self):
        """就是这次误报的实际数字：25 个请求 2 个失败。"""
        active, _ = operational.model_completion_alert(25, 2)
        self.assertFalse(active, '低样本下两起偶发不该告警')

    def test_one_failure_in_a_small_window_does_not_fire(self):
        active, _ = operational.model_completion_alert(20, 1)
        self.assertFalse(active)

    def test_three_failures_in_a_small_window_does_fire(self):
        """低流量下三个失败值得看一眼。"""
        active, _ = operational.model_completion_alert(25, 3)
        self.assertTrue(active)

    def test_many_requests_with_a_few_failures_does_not_fire(self):
        """高流量下 0.3% 本来就不该告警。"""
        active, _ = operational.model_completion_alert(1000, 3)
        self.assertFalse(active)

    def test_many_requests_with_many_failures_fires(self):
        active, _ = operational.model_completion_alert(1000, 30)
        self.assertTrue(active)

    def test_below_the_sample_floor_does_not_fire(self):
        active, _ = operational.model_completion_alert(19, 19)
        self.assertFalse(active, '样本不足 20 时比例没有意义')

    def test_exactly_at_both_floors_fires(self):
        """20 个请求 + 3 个失败 = 15%，两个下限都刚好满足。"""
        active, _ = operational.model_completion_alert(20, 3)
        self.assertTrue(active)

    def test_zero_requests_does_not_crash(self):
        for n, failed in ((0, 0), (0, 5), (None, None)):
            active, detail = operational.model_completion_alert(n, failed)
            self.assertFalse(active)
            self.assertIsInstance(detail, str)

    def test_detail_carries_the_actual_numbers(self):
        _, detail = operational.model_completion_alert(1000, 30)
        self.assertIn('30/1000', detail)
        self.assertIn('3.0%', detail)

    def test_detail_stays_ascii_so_logs_do_not_mangle(self):
        """日志里出现非 ASCII 时，某些采集链路会转义成乱码。"""
        _, detail = operational.model_completion_alert(25, 3)
        detail.encode('ascii')      # 抛异常即失败


class TheMonitorUsesItTests(unittest.TestCase):
    def test_monitor_tick_calls_the_extracted_rule(self):
        source = inspect.getsource(operational._monitor_tick)
        self.assertIn('model_completion_alert', source,
                      '判据没接回监控循环，等于改了没生效')

    def test_the_old_inline_comparison_is_gone(self):
        """旧的内联比较如果还在，两处判据会漂移。"""
        source = inspect.getsource(operational._monitor_tick)
        self.assertNotIn("(row['failed'] or 0)/row['n']>0.01", source)


if __name__ == '__main__':
    unittest.main()
