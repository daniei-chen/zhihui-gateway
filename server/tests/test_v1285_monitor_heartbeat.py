"""v1.2.85：监控循环的偶发失败，从外部完全看不见。

## 起因

2026-11-15 那次事故里，加了一个统计桶却忘了初始化，某一轮健康评估在汇总时抛键错误。

**那一轮崩溃点之后的告警全都没评估** —— 磁盘水位、账号池、备份检查都在后面。

而从外部完全看不出来：

| 外部信号 | 当时的值 |
| --- | --- |
| 请求处理 | 正常 |
| `/healthz` | 200 |
| `/readyz` | 200（读的是「最近一次结果」） |
| 完整性闭包 | ok |

**因为它只崩了部分轮次**，其余轮次成功，`_cached_readiness` 一直有值。
闪断看不见 —— 这是「此刻健康」与「一直在健康」的差别。

## 加了什么

三个累计量，由 `/metrics` 暴露：

| 指标 | 作用 |
| --- | --- |
| `workbuddy_monitor_successes_total` | 成功轮次 |
| `workbuddy_monitor_failures_total` | **失败轮次 —— 非零就说明有整轮被跳过** |
| `workbuddy_monitor_last_success_age_seconds` | 距上次成功多久；停滞时会无限增长 |

三个键**全部显式初始化** —— 上一版的教训正是「漏一个键会抛」。
"""
from __future__ import annotations

import inspect
import unittest

from server import main, operational


class MonitorStatsTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(operational._MONITOR_STATS)
        operational._MONITOR_STATS.update(
            {'successes': 0, 'failures': 0, 'last_success': 0.0})

    def tearDown(self):
        operational._MONITOR_STATS.clear()
        operational._MONITOR_STATS.update(self._saved)

    def test_initial_state_has_every_key(self):
        """三个键一个都不能少 —— v1.2.83 就是漏了一个键才炸的。"""
        stats = operational.monitor_stats()
        self.assertEqual(set(stats), {'successes', 'failures', 'last_success'})

    def test_success_increments_and_stamps(self):
        operational.note_monitor_result(True, now=500.0)
        stats = operational.monitor_stats()
        self.assertEqual(stats['successes'], 1)
        self.assertEqual(stats['failures'], 0)
        self.assertEqual(stats['last_success'], 500.0)

    def test_failure_increments_but_keeps_the_last_success(self):
        """失败不能把「上次成功时间」抹掉 —— 否则分不清「从没成功」和「刚失败」。"""
        operational.note_monitor_result(True, now=100.0)
        operational.note_monitor_result(False)
        stats = operational.monitor_stats()
        self.assertEqual(stats['failures'], 1)
        self.assertEqual(stats['last_success'], 100.0)

    def test_counting_is_additive(self):
        for _ in range(3):
            operational.note_monitor_result(True, now=1.0)
        for _ in range(2):
            operational.note_monitor_result(False)
        stats = operational.monitor_stats()
        self.assertEqual((stats['successes'], stats['failures']), (3, 2))

    def test_the_returned_dict_is_a_copy(self):
        """调用方改返回值不该影响内部状态。"""
        stats = operational.monitor_stats()
        stats['successes'] = 999
        self.assertNotEqual(operational.monitor_stats()['successes'], 999)


class ItIsWiredIntoTheLoopTests(unittest.TestCase):
    SOURCE = inspect.getsource(operational.monitor)

    def test_success_path_records_it(self):
        self.assertIn('note_monitor_result(True)', self.SOURCE)

    def test_failure_path_records_it(self):
        self.assertIn('note_monitor_result(False)', self.SOURCE)

    def test_the_failure_path_still_clears_readiness(self):
        """原有行为不能因为加统计而丢。"""
        self.assertIn('_cached_readiness = None', self.SOURCE)


class TheMetricsExposeItTests(unittest.TestCase):
    SOURCE = inspect.getsource(main.prometheus_metrics)

    def test_all_three_are_exposed(self):
        for name in ('workbuddy_monitor_successes_total',
                     'workbuddy_monitor_failures_total',
                     'workbuddy_monitor_last_success_age_seconds'):
            self.assertIn(name, self.SOURCE)

    def test_the_failure_help_says_what_it_means(self):
        start = self.SOURCE.index('workbuddy_monitor_failures_total')
        window = self.SOURCE[start:start + 400]
        self.assertIn('un-evaluated', window,
                      '要说明非零意味着「有整轮告警没评估」，否则看不出严重性')

    def test_the_age_gauge_is_skipped_before_first_success(self):
        """从没成功过时不输出，避免把「0 秒前成功过」这个假信号发出去。"""
        start = self.SOURCE.index('workbuddy_monitor_last_success_age_seconds')
        window = self.SOURCE[max(0, start - 300):start + 100]
        self.assertIn('if _monitor', window)

    def test_metric_errors_do_not_break_metrics(self):
        """取统计失败不该让整个 /metrics 挂掉。"""
        start = self.SOURCE.index('_operational.monitor_stats()')
        window = self.SOURCE[max(0, start - 600):start + 1200]
        self.assertIn('except Exception', window)

    def test_route_still_points_at_the_handler(self):
        from fastapi.routing import APIRoute
        hits = [r for r in main.app.routes
                if isinstance(r, APIRoute) and getattr(r, 'path', '') == '/metrics']
        self.assertEqual(hits[0].endpoint.__name__, 'prometheus_metrics')


class TheHelpersAreNotRoutesTests(unittest.TestCase):
    def test_note_and_stats_are_plain_functions(self):
        from fastapi.routing import APIRoute
        endpoints = {r.endpoint.__name__ for r in main.app.routes
                     if isinstance(r, APIRoute)}
        self.assertNotIn('monitor_stats', endpoints)
        self.assertNotIn('note_monitor_result', endpoints)


if __name__ == '__main__':
    unittest.main()
