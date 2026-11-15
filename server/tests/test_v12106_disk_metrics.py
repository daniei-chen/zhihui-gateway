"""v1.2.106：磁盘水位有告警、没指标。

## 发现经过

排查时注意到根分区从 95% 降到 93%，于是想画一条「空间还剩多少」的趋势 ——
**翻遍 `/metrics` 没有这一项**。

告警是有的：`disk_space_trend`（低于 5 GiB）与 `disk_free`（低于 2 GiB），
而且**确实在触发** —— 六小时内触发了 19 次，日志里能读到空间从 3.8 GiB 一路降到 3.4 GiB。

**但那个「一路下降」的经过，只有把日志按时间排序才看得出来。**
指标本身就能画出来，不需要 grep。

## 本版做什么

两条 gauge：

| 指标 | 含义 |
| --- | --- |
| `workbuddy_disk_free_bytes` | 应用卷剩余字节 |
| `workbuddy_disk_used_ratio` | 应用卷已用比例（0-1） |

**口径与告警保持一致**：都读 `DATA_DIR` 所在卷的 `statvfs`，
5 GiB / 2 GiB 两条线不变。

**为什么两个都要**：绝对字节数能直接对着阈值判断，比例更适合画图和跨机器比较。
只给一个，另一个总要心算。

## 顺带记下的一个事实

本轮回收了 626 MB（删掉两个旧的部署快照），95% → 93%。

**但这不是修复**：每次部署仍会产生约 328 MB 的新快照，而保留策略写的是
「等异地备份策略」——两件事放在一起，磁盘必然被填满。

**指标不会让它变慢，只会让它可见。** 真正的解法需要设计决策。
"""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

from server import main


class TheMetricsExistTests(unittest.TestCase):
    SOURCE = inspect.getsource(main.prometheus_metrics)

    def test_free_bytes_gauge_is_emitted(self):
        self.assertIn('workbuddy_disk_free_bytes', self.SOURCE)

    def test_used_ratio_gauge_is_emitted(self):
        self.assertIn('workbuddy_disk_used_ratio', self.SOURCE)

    def test_thresholds_are_documented_on_the_metric(self):
        """阈值不写在指标上，后来人就得去翻告警代码才知道 5 GiB / 2 GiB 从哪来。"""
        start = self.SOURCE.index('workbuddy_disk_free_bytes')
        window = self.SOURCE[start:start + 400]
        self.assertIn('5 GiB', window)
        self.assertIn('2 GiB', window)

    def test_why_both_are_needed_is_documented(self):
        start = self.SOURCE.index('workbuddy_disk_used_ratio')
        window = self.SOURCE[start:start + 300]
        self.assertIn('0-1', window)

    def test_the_gap_that_motivated_it_is_recorded(self):
        """指标存在的理由要写清，否则会被当成冗余删掉。"""
        start = self.SOURCE.index('workbuddy_disk_free_bytes')
        window = self.SOURCE[max(0, start - 1200):start]
        self.assertIn('19', window, '要写明当时告警触发了 19 次却没有指标')

    def test_platform_without_statvfs_does_not_break_metrics(self):
        """Windows 没有 statvfs；缺了它不能让整个 /metrics 挂掉。"""
        import os
        with mock.patch.object(os, 'statvfs', create=True,
                               side_effect=AttributeError('no statvfs'), ):
            source = inspect.getsource(main.prometheus_metrics)
            self.assertIn('hasattr', source, '必须先用 hasattr 判断')


class TheAlertSideIsUnchangedTests(unittest.TestCase):
    """这一版只加指标，不动告警口径。"""

    def test_alert_thresholds_are_the_same_constants(self):
        from server import operational
        urgent = (2 * 1024**3, 'disk_free')
        trending = (5 * 1024**3, 'disk_space_trend')
        for limit, name in (urgent, trending):
            alerts = operational.disk_space_alerts(limit - 1)
            hit = [a for a in alerts if a[0] == name]
            self.assertTrue(hit, name)
            self.assertTrue(hit[0][1], '%s 应在阈值下触发' % name)

    def test_above_both_thresholds_is_quiet(self):
        from server import operational
        alerts = operational.disk_space_alerts(6 * 1024**3)
        self.assertFalse(any(active for _name, active, _detail in alerts))

    def test_unknown_free_space_is_not_an_alarm(self):
        """拿不到水位时不该报警 —— 那不是磁盘满了，是读不到。"""
        from server import operational
        alerts = operational.disk_space_alerts(None)
        self.assertFalse(any(active for _name, active, _detail in alerts))


if __name__ == '__main__':
    unittest.main()
