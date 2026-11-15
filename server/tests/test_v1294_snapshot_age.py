"""v1.2.94：备份报告说 ok，加密快照却一整天没产出。

## 事故

`recovery_v3.py` 调基线备份时写的超时是 300 秒，而实际需要 4 分钟以上。2026-11-15
那一版起，它每次都在 300 秒处放弃，于是：

| 产物 | 状态 |
| --- | --- |
| tar 基线备份 | **照常产出**（子进程成了孤儿，继续跑完） |
| 加密快照 | **一份都没有** |

**而 `backup-status.json` 仍写着 `restore_check: ok`** —— 那份报告只校验基线 tar
本身。于是「备份正常」这个结论在告警系统里成立，在事实上不成立。

上一版加的 `workbuddy_backup_age_seconds` 测的正是那份报告 —— **它测不到这个**。

## 修法

新增 `workbuddy_snapshot_age_seconds`：直接看加密快照目录里最新一份的时间。

两条指标分别回答两个问题：

| 指标 | 回答 |
| --- | --- |
| `backup_age_seconds` | 基线备份的报告是不是新的 |
| `snapshot_age_seconds` | v2 恢复点还在不在推进 |

**它们可以不一致，而那正是 2026-11-15 的现场。**
"""
from __future__ import annotations

import inspect
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from server import main, operational


class SnapshotAgeTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.object(operational, '_SNAPSHOT_ROOT', self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make(self, name, *, age_seconds, now=1000000):
        import os
        target = self.dir / name
        target.mkdir()
        stamp = now - age_seconds
        os.utime(target, (stamp, stamp))

    def test_newest_snapshot_wins(self):
        self._make('20261113-095451', age_seconds=90000)
        self._make('20261114-043512', age_seconds=30000)
        self.assertEqual(operational.snapshot_age_seconds(now=1000000), 30000)

    def test_empty_directory_is_none(self):
        """一份都没有是「没做」，不是「很旧」—— 后者会被读成备份在变慢。"""
        self.assertIsNone(operational.snapshot_age_seconds(now=1000000))

    def test_missing_root_is_none(self):
        with mock.patch.object(operational, '_SNAPSHOT_ROOT',
                               pathlib.Path('/nonexistent/snapshots')):
            self.assertIsNone(operational.snapshot_age_seconds(now=1000000))

    def test_files_are_not_counted(self):
        """只认目录：同名的散文件不该被当成快照。"""
        (self.dir / 'stray.txt').write_text('x')
        self.assertIsNone(operational.snapshot_age_seconds(now=1000000))

    def test_future_snapshot_clamps_to_zero(self):
        self._make('20261115-999999', age_seconds=-600)
        self.assertEqual(operational.snapshot_age_seconds(now=1000000), 0)

    def test_it_never_raises(self):
        with mock.patch.object(operational, '_SNAPSHOT_ROOT',
                               pathlib.Path('/root/definitely-forbidden')):
            try:
                operational.snapshot_age_seconds(now=1)
            except Exception as exc:      # noqa: BLE001
                self.fail(f'不该抛异常：{exc!r}')


class ItIsADifferentQuestionTests(unittest.TestCase):
    """两条指标的差别就是这次事故本身。"""

    def test_the_two_helpers_are_separate(self):
        self.assertTrue(callable(operational.backup_age_seconds))
        self.assertTrue(callable(operational.snapshot_age_seconds))
        self.assertIsNot(operational.backup_age_seconds,
                         operational.snapshot_age_seconds)

    def test_snapshot_does_not_read_the_status_file(self):
        source = inspect.getsource(operational.snapshot_age_seconds)
        self.assertNotIn('BACKUP_STATUS_FILE', source,
                         '它必须独立于基线报告，否则重复了同一个盲区')

    def test_backup_age_does_not_read_the_snapshot_root(self):
        source = inspect.getsource(operational.backup_age_seconds)
        self.assertNotIn('_SNAPSHOT_ROOT', source)


class TheMetricIsHonestTests(unittest.TestCase):
    SOURCE = inspect.getsource(main.prometheus_metrics)

    def test_the_metric_exists(self):
        self.assertIn('workbuddy_snapshot_age_seconds', self.SOURCE)

    def test_unknown_is_reported_as_minus_one(self):
        """拿不到年龄时要报 -1，不能让整条序列消失。

        「从来没有过快照」正是最该报警的状态；
        序列不存在时告警会安静地不触发。
        """
        start = self.SOURCE.index('workbuddy_snapshot_age_seconds')
        window = self.SOURCE[max(0, start - 500):start + 900]
        self.assertIn('-1 if _snapshot_age is None', window,
                      '未知要报 -1，不是不报')

    def test_help_records_the_incident(self):
        start = self.SOURCE.index('workbuddy_snapshot_age_seconds')
        window = self.SOURCE[start:start + 600]
        self.assertIn('2026-11-15', window,
                      '要写出那天两条指标分叉的事实，否则后来人不懂为何要两条')


class TheReasonIsRecordedTests(unittest.TestCase):
    def test_module_records_what_happened(self):
        source = inspect.getsource(operational)
        start = source.index('_SNAPSHOT_ROOT')
        window = source[max(0, start - 1400):start]
        self.assertIn('restore_check', window)
        self.assertIn('300', window, '要写出那个不够用的超时值')


if __name__ == '__main__':
    unittest.main()
