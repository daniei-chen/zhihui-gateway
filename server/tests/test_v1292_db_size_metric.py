"""v1.2.92：数据库有多大，此前只能靠人工 du。

## 为什么值得加

2026-11-15 审计容量时发现：库 614 MB、备份 6.9 GB、磁盘余量 9.7 GB —— 一年后就会顶到边。

但这些数字是**人工跑 `du`** 得到的。运行期没有任何关于「库现在多大」的指标。

## 为什么必须把 WAL 一起算

用的是 WAL 模式，「数据库有多大」不是主库一个文件的事：

| 文件 | 内容 |
| --- | --- |
| `manager.db` | 已写回的主体 |
| `manager.db-wal` | 尚未 checkpoint 的增量 |
| `manager.db-shm` | 共享内存索引 |

**只看主库会低估**：一次大批量写入之后、checkpoint 之前，WAL 可能有几十 MB ——
而那正是磁盘水位最该看到的部分。

WAL 长期偏大还有第二层含义：checkpoint 跟不上写入速度，读性能会开始付代价。
所以它单独一条指标。
"""
from __future__ import annotations

import inspect
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from server import config, main, operational


class SizeTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / 'manager.db'
        patcher = mock.patch.object(config, 'DB_PATH', self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_three_are_counted(self):
        self.db.write_bytes(b'x' * 1000)
        pathlib.Path(str(self.db) + '-wal').write_bytes(b'y' * 400)
        pathlib.Path(str(self.db) + '-shm').write_bytes(b'z' * 32)
        size = operational.database_size_bytes()
        self.assertEqual(size['main'], 1000)
        self.assertEqual(size['wal'], 400)
        self.assertEqual(size['shm'], 32)
        self.assertEqual(size['total'], 1432)

    def test_total_includes_the_wal(self):
        """只看主库会低估 —— 这是这个函数存在的理由。"""
        self.db.write_bytes(b'x' * 100)
        pathlib.Path(str(self.db) + '-wal').write_bytes(b'y' * 900)
        size = operational.database_size_bytes()
        self.assertEqual(size['total'], 1000,
                         '总大小必须含 WAL，否则磁盘水位会低估')
        self.assertGreater(size['total'], size['main'])

    def test_missing_sidecars_count_as_zero(self):
        """checkpoint 之后 -wal 可能不存在，那不是错误。"""
        self.db.write_bytes(b'x' * 500)
        size = operational.database_size_bytes()
        self.assertEqual(size['wal'], 0)
        self.assertEqual(size['shm'], 0)
        self.assertEqual(size['total'], 500)

    def test_all_missing_returns_none(self):
        """一个都读不到是「不知道」，不是「零字节」。"""
        self.assertIsNone(operational.database_size_bytes())

    def test_empty_config_returns_none(self):
        with mock.patch.object(config, 'DB_PATH', ''):
            self.assertIsNone(operational.database_size_bytes())

    def test_it_never_raises(self):
        with mock.patch.object(config, 'DB_PATH', pathlib.Path('/root/forbidden/x.db')):
            try:
                operational.database_size_bytes()
            except Exception as exc:      # noqa: BLE001
                self.fail(f'不该抛异常：{exc!r}')


class TheMetricsExposeThemTests(unittest.TestCase):
    SOURCE = inspect.getsource(main.prometheus_metrics)

    def test_both_metrics_exist(self):
        for name in ('workbuddy_db_size_bytes', 'workbuddy_db_wal_size_bytes'):
            self.assertIn(name, self.SOURCE)

    def test_size_help_mentions_the_wal_inclusion(self):
        start = self.SOURCE.index('workbuddy_db_size_bytes')
        window = self.SOURCE[start:start + 500]
        self.assertIn('WAL', window,
                      '要说明含 WAL，否则读者会以为这是主库单文件大小')

    def test_wal_help_explains_the_second_meaning(self):
        start = self.SOURCE.index('workbuddy_db_wal_size_bytes')
        window = self.SOURCE[start:start + 500]
        self.assertIn('checkpoint', window,
                      'WAL 长期偏大还意味着 checkpoint 跟不上')

    def test_errors_do_not_break_metrics(self):
        start = self.SOURCE.index('database_size_bytes')
        window = self.SOURCE[max(0, start - 300):start + 900]
        self.assertIn('except Exception', window)

    def test_route_is_intact(self):
        from fastapi.routing import APIRoute
        hits = [r for r in main.app.routes
                if isinstance(r, APIRoute) and getattr(r, 'path', '') == '/metrics']
        self.assertEqual(hits[0].endpoint.__name__, 'prometheus_metrics')


class TheReasonIsRecordedTests(unittest.TestCase):
    def test_module_records_the_measured_baseline(self):
        source = inspect.getsource(operational)
        start = source.index('_DB_SIDECAR_SUFFIXES')
        window = source[max(0, start - 1400):start]
        self.assertIn('614', window, '要写出实测基线，否则看不出为什么值得加')


if __name__ == '__main__':
    unittest.main()
