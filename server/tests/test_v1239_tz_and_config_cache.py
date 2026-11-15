"""v1.2.39：日边界钉死在 UTC+8；上游配置按文件指纹缓存。

两件事都属于「口径不该跟环境走」：
  * 「每日」的定义原来跟随进程 TZ，而用户账本一直硬编码 UTC+8 —— 生产主机恰好
    是 +08:00，所以从没暴露，但换台机器就会让两份账本分家；
  * 上游配置指纹在每个请求上都要算，原来每次都完整读文件、解析 JSON、重建视图。
"""
from __future__ import annotations

import calendar
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import db, retention
from server.services import wb2api


def _utc_ts(*args) -> int:
    return calendar.timegm(tuple(args) + (0, 0, 0))


class DayBoundaryTests(unittest.TestCase):
    def test_day_of_is_utc_plus_8_regardless_of_process_tz(self):
        # 2026-01-01 00:30 (+08) = 2025-12-31 16:30 UTC
        ts = _utc_ts(2025, 12, 31, 16, 30)
        self.assertEqual(db.day_of(ts), '2026-01-01')
        # 2025-12-31 23:30 (+08) = 15:30 UTC
        self.assertEqual(db.day_of(_utc_ts(2025, 12, 31, 15, 30)), '2025-12-31')

    def test_day_of_matches_the_ledger_timezone(self):
        from server import portalstore
        for ts in (_utc_ts(2026, 3, 1, 0, 0), _utc_ts(2026, 3, 1, 15, 59),
                   _utc_ts(2026, 3, 1, 16, 0), _utc_ts(2026, 3, 1, 23, 59)):
            self.assertEqual(db.day_of(ts), portalstore.day(ts))

    def test_day_sql_uses_a_fixed_offset_not_localtime(self):
        self.assertIn("'+8 hours'", db.day_sql())
        self.assertNotIn('localtime', db.day_sql())
        self.assertIn("'+8 hours'", db.hour_sql())
        self.assertNotIn('localtime', db.hour_sql())

    def test_day_start_ts_agrees_with_day_sql(self):
        """「本地日期 ≥ X」与「ts ≥ X 当地零点」必须严格等价。"""
        for day in ('2026-01-01', '2026-03-15', '2026-12-31'):
            start = db.day_start_ts(day)
            self.assertEqual(db.day_of(start), day, '零点必须落在当天')
            self.assertEqual(db.day_of(start + 86399), day, '当天最后一秒仍属当天')
            previous = db.day_of(start - 1)
            self.assertNotEqual(previous, day, '零点前一秒绝不能算进当天')

    def test_hour_start_ts_is_utc_plus_8(self):
        start = db.hour_start_ts('2026-06-01', 9)
        self.assertEqual(db.day_of(start), '2026-06-01')
        self.assertEqual(db.hour_of(start), 9)
        self.assertEqual(start - db.hour_start_ts('2026-06-01', 8), 3600)

    def test_retention_cutoff_uses_the_same_zone(self):
        definition = retention.dataset('usage_hourly') if any(
            d.name == 'usage_hourly' for d in retention.datasets()) else None
        if definition is None:
            self.skipTest('usage_hourly 不在保留期数据集里')
        _, args = retention._retention_where(definition, _utc_ts(2025, 12, 31, 16, 0))
        self.assertEqual(args, ('2026-01-01',))


class UpstreamConfigCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.json'
        self.path.write_text(json.dumps({'listen': ':8080', 'pool': {'size': 4}}), encoding='utf-8')
        patcher = mock.patch.object(wb2api.config, 'UPSTREAM_CONFIG', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        wb2api._CONFIG_CACHE['marker'] = None
        wb2api._CONFIG_CACHE['value'] = None
        self.addCleanup(lambda: wb2api._CONFIG_CACHE.update(marker=None, value=None))

    def test_second_call_does_not_reparse_the_file(self):
        real_read = Path.read_text
        reads = []

        def counting_read(self_path, *args, **kwargs):
            reads.append(self_path)
            return real_read(self_path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', counting_read):
            first = wb2api.load_upstream_config()
            second = wb2api.load_upstream_config()
        self.assertEqual(len(reads), 1, '同一份文件只应解析一次')
        self.assertEqual(first['listen'], second['listen'])

    def test_returned_dicts_are_independent(self):
        first = wb2api.load_upstream_config()
        first['listen'] = 'mutated'
        second = wb2api.load_upstream_config()
        self.assertNotEqual(second['listen'], 'mutated', '缓存不能把可变对象直接发出去')

    def test_replaced_file_invalidates_the_cache(self):
        wb2api.load_upstream_config()
        replacement = Path(self.tmp.name) / 'config.json.new'
        replacement.write_text(json.dumps({'listen': ':9999'}), encoding='utf-8')
        os.replace(replacement, self.path)
        self.assertEqual(wb2api.load_upstream_config()['listen'], ':9999')

    def test_removed_file_clears_the_cache(self):
        wb2api.load_upstream_config()
        self.path.unlink()
        missing = wb2api.load_upstream_config()
        self.assertFalse(missing['available'])
        self.assertIsNone(wb2api._CONFIG_CACHE['marker'])


if __name__ == '__main__':
    unittest.main()
