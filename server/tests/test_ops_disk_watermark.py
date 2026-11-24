"""`ops/wb-gateway-alert.py` 的磁盘水位规则（R11）。

这是运维脚本，不在应用进程里跑，所以只能这样用 importlib 按路径加载来测。
值得测的理由：它的失败方式是**要么不响、要么一直响**。
* 不响：2026-11-15 盘到 94% 才有人发现，账本已经在静默丢行；
* 一直响：阈值算错（比如把"已用"写成"空闲"）会让每 5 分钟一次的定时任务
  天天喊狼来了，几次之后没人再看告警。
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'ops' / 'wb-gateway-alert.py'


def _load():
    spec = importlib.util.spec_from_file_location('wb_gateway_alert', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metrics(res: dict) -> dict:
    """一份「其它规则都不触发」的基线指标，只留资源部分可控。"""
    return {
        'service': 'active', 'health': 'healthy', 'probe': '200 OK',
        'timeout_n': 0, 'cap_n': 0, 'pool_n': 0, 's503_n': 0,
        'soft_rate_n': 0, 'risk_1128_n': 0,
        'client': {'total': 0}, 'backup': {}, 'res': res,
    }


class DiskWatermarkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_healthy_disk_does_not_alert(self) -> None:
        fired = self.mod.evaluate(_metrics({'disk_pct': 65.0, 'disk_free_gb': 20.0,
                                            'db_mb': 703.0}))
        self.assertNotIn('R11', fired)

    def test_warn_threshold(self) -> None:
        fired = self.mod.evaluate(_metrics({'disk_pct': 85.0, 'disk_free_gb': 8.0}))
        self.assertIn('R11', fired)
        self.assertEqual(fired['R11'][0], 'WARN')

    def test_critical_threshold(self) -> None:
        fired = self.mod.evaluate(_metrics({'disk_pct': 92.0, 'disk_free_gb': 3.0}))
        self.assertEqual(fired['R11'][0], 'CRITICAL')

    def test_just_below_warn_is_silent(self) -> None:
        fired = self.mod.evaluate(_metrics({'disk_pct': 84.9}))
        self.assertNotIn('R11', fired)

    def test_unknown_disk_reading_is_silent(self) -> None:
        # 拿不到读数时不能报警：报警内容会是"None%"，等于噪声。
        fired = self.mod.evaluate(_metrics({'disk_pct': None}))
        self.assertNotIn('R11', fired)

    def test_message_carries_actionable_numbers(self) -> None:
        fired = self.mod.evaluate(_metrics({'disk_pct': 90.5, 'disk_free_gb': 4.2,
                                            'db_mb': 900.0}))
        text = fired['R11'][1]
        self.assertIn('90.5', text)
        self.assertIn('4.2', text)
        self.assertIn('900', text)

    def test_thresholds_are_ordered_and_sane(self) -> None:
        self.assertLess(self.mod.DISK_WARN_PCT, self.mod.DISK_CRIT_PCT)
        self.assertLess(self.mod.DISK_CRIT_PCT, 100)


if __name__ == '__main__':
    unittest.main()
