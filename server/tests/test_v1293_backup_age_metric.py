"""v1.2.93：备份「还差多久过期」此前看不到。

## 为什么值得加

备份过期的告警阈值是 26 小时。**低于它的时候一片安静** —— 安静是对的，但也就意味着
「备份时间正在一天天往后漂」这件事完全不可见。

比如备份从 04:30 漂到 05:20、再漂到 06:30：每一次都还在 26 小时窗口内，告警不响，
而它已经在变慢。等到某一越过线，看到的只是「突然过期了」，看不出这是积累出来的。

一条指标给出趋势本身。

## 什么算「拿不到证据」

| 情况 | 返回 |
| --- | --- |
| 文件不存在 / 不可读 | `None` |
| 报告不是合法 JSON | `None` |
| `restore_check` 不是 `ok` | `None` |
| 大小为 0 或不是整数 | `None` |
| 时间戳在未来超过 5 分钟 | `None` |
| 以上都不成立 | 秒数 |

**前三类必须是 `None` 而不是 0**：0 会被读成「刚刚备份成功」，那是最危险的一种假信号。

有效性判据与告警路径**共用同一套**，避免两处对「什么是有效备份」得出不同结论。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from server import config, main, operational


class AgeTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.status = self.dir / 'backup-status.json'
        patcher = mock.patch.object(config, 'BACKUP_STATUS_FILE', self.status)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, payload, *, age_seconds=3600, now=100000):
        self.status.write_text(json.dumps(payload))
        import os
        stamp = now - age_seconds
        os.utime(self.status, (stamp, stamp))
        return operational.backup_age_seconds(now=now)

    GOOD = {'bytes': 123, 'restore_check': 'ok'}

    def test_a_valid_report_yields_the_age(self):
        self.assertEqual(self._write(self.GOOD, age_seconds=3600), 3600)

    def test_missing_file_is_none(self):
        self.assertIsNone(operational.backup_age_seconds(now=100000))

    def test_invalid_json_is_none(self):
        self.status.write_text('not json')
        self.assertIsNone(operational.backup_age_seconds(now=100000))

    def test_failed_restore_check_is_none(self):
        """恢复校验没过，那份报告不能当证据。"""
        self.assertIsNone(self._write({'bytes': 123, 'restore_check': 'failed'}))

    def test_zero_bytes_is_none(self):
        self.assertIsNone(self._write({'bytes': 0, 'restore_check': 'ok'}))

    def test_boolean_size_is_none(self):
        """`True` 是 int 的子类，直接判断会把它当 1 字节。"""
        self.assertIsNone(self._write({'bytes': True, 'restore_check': 'ok'}))

    def test_future_timestamp_beyond_skew_is_none(self):
        self.assertIsNone(self._write(self.GOOD, age_seconds=-600))

    def test_small_clock_skew_is_tolerated(self):
        """几分钟的时钟偏差是正常的，不该因此判为无效。"""
        self.assertIsNotNone(self._write(self.GOOD, age_seconds=-120))

    def test_it_never_returns_negative(self):
        age = self._write(self.GOOD, age_seconds=-120)
        self.assertGreaterEqual(age, 0)

    def test_it_never_raises(self):
        with mock.patch.object(config, 'BACKUP_STATUS_FILE', pathlib.Path('/root/no/x.json')):
            try:
                operational.backup_age_seconds(now=1)
            except Exception as exc:      # noqa: BLE001
                self.fail(f'不该抛异常：{exc!r}')


class TheMetricIsHonestTests(unittest.TestCase):
    SOURCE = __import__('inspect').getsource(main.prometheus_metrics)

    def test_the_metric_exists(self):
        self.assertIn('workbuddy_backup_age_seconds', self.SOURCE)

    def test_it_is_skipped_when_unknown(self):
        start = self.SOURCE.index('workbuddy_backup_age_seconds')
        window = self.SOURCE[max(0, start - 400):start + 100]
        self.assertIn('if _backup_age is not None', window,
                      '拿不到证据时不能输出 —— 0 会被读成刚备份过')

    def test_help_explains_the_drift_use(self):
        start = self.SOURCE.index('workbuddy_backup_age_seconds')
        window = self.SOURCE[start:start + 500]
        self.assertIn('26', window, '要写出告警阈值，读者才知道这个数离危险有多远')

    def test_errors_do_not_break_metrics(self):
        start = self.SOURCE.index('backup_age_seconds')
        window = self.SOURCE[max(0, start - 300):start + 700]
        self.assertIn('except Exception', window)


class ItSharesTheValidityRuleTests(unittest.TestCase):
    """两处对「什么是有效备份」必须给同一个答案。"""

    def test_the_same_five_checks_appear_in_both(self):
        source = inspect_source(operational)
        health = source[source.index('def evaluate_backup_health'):]
        age = source[source.index('def backup_age_seconds'):]
        age = age[:age.index('def monitor_stats')]
        for needle in ('restore_check', 'bytes', 'isinstance(size, int)', '-300'):
            self.assertIn(needle, health, f'告警路径缺 {needle}')
            self.assertIn(needle, age, f'指标路径缺 {needle}')


def inspect_source(module):
    import inspect
    return inspect.getsource(module)


if __name__ == '__main__':
    unittest.main()
