"""v1.2.71：磁盘水位加一档提前量，并成文数据增长预算。

## 起因

审计数据增长时算清了两件事：

1. 数据库 614 MB，其中 `forwarding_events` 是唯一「永不删除」又高速写入的表
   （9,581 行/天，约 855 MB/年）；
2. **备份把它放大约 11 倍** —— 数据库 614 MB，`/root/backups` 是 6.9 GB。

所以这条卷的年增长约 **9 GB**，而当时可用只有 9.7 GB。

**当前磁盘告警只有「低于 2 GiB」一档** —— 按这个速度，触发时离写满只剩几周。

## 改法

分两档，用**两个不同的告警名**：

| 告警名 | 阈值 | 含义 |
| --- | --- | --- |
| `disk_free` | < 2 GiB | 紧急 |
| `disk_space_trend` | < 5 GiB | 趋势，该做决定了 |

**为什么不能共用一个名字**：状态机只在**变化**时通报。共用的话，从 5 GiB 掉到
2 GiB 不会产生新通报 —— 而那一刻才是最该被看见的。
"""
from __future__ import annotations

import inspect
import unittest

from server import operational

GIB = 1024 ** 3


class DiskSpaceAlertTests(unittest.TestCase):
    def _alerts(self, free):
        return {name: (active, detail)
                for name, active, detail in operational.disk_space_alerts(free)}

    def test_plenty_of_space_fires_nothing(self):
        alerts = self._alerts(20 * GIB)
        self.assertFalse(alerts['disk_free'][0])
        self.assertFalse(alerts['disk_space_trend'][0])

    def test_the_trend_band_fires_on_its_own(self):
        """4 GiB：趋势告警响，紧急不响 —— 这就是新增的提前量。"""
        alerts = self._alerts(4 * GIB)
        self.assertFalse(alerts['disk_free'][0])
        self.assertTrue(alerts['disk_space_trend'][0])

    def test_both_fire_when_critical(self):
        alerts = self._alerts(1 * GIB)
        self.assertTrue(alerts['disk_free'][0])
        self.assertTrue(alerts['disk_space_trend'][0])

    def test_they_are_separate_names(self):
        """共用名字会让「从 5 GiB 掉到 2 GiB」不产生新通报。"""
        names = [name for name, _, _ in operational.disk_space_alerts(1 * GIB)]
        self.assertEqual(len(names), len(set(names)), '告警名重复了')
        self.assertIn('disk_free', names)
        self.assertIn('disk_space_trend', names)

    def test_boundaries_are_exclusive(self):
        self.assertFalse(self._alerts(5 * GIB)['disk_space_trend'][0],
                         '正好 5 GiB 不算低于 5 GiB')
        self.assertFalse(self._alerts(2 * GIB)['disk_free'][0],
                         '正好 2 GiB 不算低于 2 GiB')
        self.assertTrue(self._alerts(5 * GIB - 1)['disk_space_trend'][0])

    def test_unknown_free_space_does_not_guess(self):
        """取不到时不猜 —— 猜成「充足」会掩盖问题，猜成「紧急」会误报。"""
        alerts = self._alerts(None)
        self.assertFalse(alerts['disk_free'][0])
        self.assertFalse(alerts['disk_space_trend'][0])
        self.assertIn('unknown', alerts['disk_free'][1])

    def test_detail_carries_the_actual_free_space(self):
        _, detail = self._alerts(3 * GIB)['disk_space_trend']
        self.assertIn('3.0 GiB', detail)

    def test_the_trend_detail_points_at_the_budget_doc(self):
        _, detail = self._alerts(3 * GIB)['disk_space_trend']
        self.assertIn('DATA-GROWTH.md', detail,
                      '告警要把人引到能算账的地方')

    def test_details_stay_ascii(self):
        for free in (1 * GIB, 3 * GIB, 20 * GIB, None):
            for _, _, detail in operational.disk_space_alerts(free):
                detail.encode('ascii')


class MonitorUsesItTests(unittest.TestCase):
    def test_monitor_tick_iterates_the_alerts(self):
        source = inspect.getsource(operational._monitor_tick)
        self.assertIn('disk_space_alerts', source)

    def test_the_old_single_threshold_is_gone(self):
        source = inspect.getsource(operational._monitor_tick)
        self.assertNotIn("free<2*1024**3", source,
                         '旧的内联阈值如果还在，两处判据会漂移')


if __name__ == '__main__':
    unittest.main()
