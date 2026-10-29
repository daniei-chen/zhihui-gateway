"""U20 资产清理工具的快照分组回归测试（2026-10-28）。

## 真实缺陷（先于修复复现）

`deploy/asset_lifecycle.py --audit` 把 `data/` 下每个快照**文件**当独立个体按
mtime 排序，用 `index < KEEP_SNAPSHOTS` 挑「保留最近 2 个」。但 `-wal`/`-shm`
的 mtime 永远晚于 `.db` 主体，于是现场输出是：

    keep   32.0K  pre-switch-20261028-005502.db-shm   最近 2 个快照之一
    keep    0.0B  pre-switch-20261028-005502.db-wal   最近 2 个快照之一
    PRUNE 201.0M  pre-switch-20261028-005502.db       旧快照（保留最近 2 个）

即：最新那个**唯一完整**的快照，主体被判可删、孤立的 `-wal`/`-shm` 留下。
一旦 `--apply`，等于用垃圾换掉完整快照。四个 `pre-switch-*.db` 全部落在这个坑里。

## 修复后的行为

按「快照身份」分组（`snapshot_key()`），整组一起保留、整组一起清理。

运行：`python -m pytest server/tests/test_asset_lifecycle.py -q`
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'deploy'))

import asset_lifecycle as demo  # noqa: E402


class SnapshotKeyTest(unittest.TestCase):
    """文件名 → 快照身份。"""

    def test_all_members_share_one_identity(self):
        cases = {
            'pre-switch-20261028-005502.db': 'pre-switch-20261028-005502',
            'pre-switch-20261028-005502.db-wal': 'pre-switch-20261028-005502',
            'pre-switch-20261028-005502.db-shm': 'pre-switch-20261028-005502',
            'pre-switch-20261028-005502.db.gz': 'pre-switch-20261028-005502',
            'pre-switch-20261026-144713.db.gz': 'pre-switch-20261026-144713',
            'manager.db.bak-20261027-093537.gz': 'manager.db.bak-20261027-093537',
            'manager.db.bak-budget-1009.gz': 'manager.db.bak-budget-1009',
            'users.json.bak-20260919-215149': 'users.json.bak-20260919-215149',
        }
        for name, expected in cases.items():
            self.assertEqual(demo.snapshot_key(name), expected, name)

    def test_identity_is_idempotent(self):
        key = demo.snapshot_key('pre-switch-20261028-005502.db-wal')
        self.assertEqual(demo.snapshot_key(key), key)

    def test_unrelated_name_untouched(self):
        self.assertEqual(demo.snapshot_key('pre-switch-x'), 'pre-switch-x')

    def test_bare_suffix_is_not_eaten_to_nothing(self):
        # `len(stem) > len(suffix)` 守卫：整个名字就是后缀时不至于变成空串
        self.assertEqual(demo.snapshot_key('.gz'), '.gz')


class AuditSnapshotGroupTest(unittest.TestCase):
    """端到端：audit() 必须整组保留 / 整组清理。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.prod = root / 'prod'
        self.data = self.prod / 'data'
        self.data.mkdir(parents=True)
        (self.prod / 'releases').mkdir()
        self.backups = root / 'backups'
        self.backups.mkdir()
        patcher = mock.patch.multiple(
            demo,
            PROD=self.prod,
            RELEASES=self.prod / 'releases',
            DATA=self.data,
            BACKUPS=self.backups,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _touch(self, name: str, size: int, mtime: float) -> pathlib.Path:
        path = self.data / name
        path.write_bytes(b'x' * size)
        os.utime(path, (mtime, mtime))
        return path

    def _actions(self, group_name: str) -> dict:
        report = demo.audit()
        group = next(g for g in report['groups'] if g['name'] == group_name)
        return {pathlib.Path(i['path']).name: i['action'] for i in group['items']}

    def test_newest_snapshot_body_survives_even_though_its_wal_is_newer(self):
        """核心回归：主体的 mtime 早于自己的 -wal/-shm，仍必须保留整组。"""
        base = 1_700_000_000.0
        # 三个快照，保留最近 2 个（KEEP_SNAPSHOTS = 2）
        self._touch('pre-switch-20261027-185408.db', 120, base)
        self._touch('pre-switch-20261027-185408.db-wal', 0, base + 1)
        self._touch('pre-switch-20261027-194023.db', 240, base + 100)
        self._touch('pre-switch-20261027-194023.db-wal', 0, base + 101)
        # 最新：主体最早、-shm/-wal 最晚（正是现场形态）
        self._touch('pre-switch-20261028-005502.db', 480, base + 200)
        self._touch('pre-switch-20261028-005502.db-shm', 32, base + 300)
        self._touch('pre-switch-20261028-005502.db-wal', 0, base + 301)

        actions = self._actions('data-snapshots')

        for name in ('pre-switch-20261028-005502.db',
                     'pre-switch-20261028-005502.db-shm',
                     'pre-switch-20261028-005502.db-wal'):
            self.assertEqual(actions[name], 'keep', '%s 应整组保留' % name)
        for name in ('pre-switch-20261027-194023.db',
                     'pre-switch-20261027-194023.db-wal'):
            self.assertEqual(actions[name], 'keep', '%s 应整组保留' % name)
        for name in ('pre-switch-20261027-185408.db',
                     'pre-switch-20261027-185408.db-wal'):
            self.assertEqual(actions[name], 'prune', '%s 应整组清理' % name)

    def test_snapshot_never_split_across_actions(self):
        """任何快照的成员必须动作一致 —— 不允许「删主体留 -wal」。"""
        base = 1_700_000_000.0
        for index, stamp in enumerate(('20261027-185408', '20261027-194023', '20261028-005502')):
            self._touch('pre-switch-%s.db' % stamp, 100, base + index * 100)
            self._touch('pre-switch-%s.db-shm' % stamp, 32, base + index * 100 + 50)
            self._touch('pre-switch-%s.db-wal' % stamp, 0, base + index * 100 + 51)

        actions = self._actions('data-snapshots')
        by_key: dict = {}
        for name, action in actions.items():
            by_key.setdefault(demo.snapshot_key(name), set()).add(action)
        for key, actions_for_key in by_key.items():
            self.assertEqual(len(actions_for_key), 1,
                             '%s 被拆成了 %s' % (key, actions_for_key))

    def test_budget_owner_locks_are_never_touch(self):
        owners = self.data / '.budget-owners'
        owners.mkdir()
        (owners / 'VM-1_abc').write_bytes(b'')
        report = demo.audit()
        group = next(g for g in report['groups'] if g['name'] == 'never-touch')
        entry = next(i for i in group['items'] if i['path'].endswith('.budget-owners'))
        self.assertEqual(entry['action'], 'keep')

    def test_wrong_token_deletes_nothing(self):
        self._touch('pre-switch-20261027-185408.db', 10, 1_700_000_000.0)
        self._touch('pre-switch-20261028-005502.db', 10, 1_700_000_100.0)
        report = demo.audit()
        before = sorted(p.name for p in self.data.iterdir())
        self.assertEqual(demo.apply_prune(report, 'not-the-token'), 2)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), before)


class ReleaseOrderTest(unittest.TestCase):
    """release 排序必须按版本语义，不能按 mtime。"""

    def test_version_beats_mtime_order(self):
        names = ['v10.7.5+u17fix+n01n03.20261028.1',
                 'v10.7.7+n03n05+u28.20261028.1',
                 'v10.7.8+n04.20261028.1',
                 'v10.7.9+u11v.20261028.1',
                 'v10.7.10+u20fix.20261028.1']
        ordered = sorted(names, key=demo.release_order_key, reverse=True)
        self.assertEqual(ordered[0], 'v10.7.10+u20fix.20261028.1')
        self.assertEqual(ordered[1], 'v10.7.9+u11v.20261028.1')
        self.assertEqual(ordered[-1], 'v10.7.5+u17fix+n01n03.20261028.1')

    def test_multi_digit_patch_sorts_numerically(self):
        # 字符串比较会把 v10.7.9 排在 v10.7.10 前面
        self.assertGreater(demo.release_order_key('v10.7.10+x.20261028.1'),
                           demo.release_order_key('v10.7.9+x.20261028.1'))
        self.assertGreater(demo.release_order_key('v10.7.9+x.20261028.1'),
                           demo.release_order_key('v10.7.5+x.20261028.1'))

    def test_unversioned_names_sort_lowest(self):
        self.assertGreater(demo.release_order_key('v1.0.79+closure.20261005.7'),
                           demo.release_order_key('baseline-audit-20261005-5'))

    def test_numeric_version_across_major(self):
        self.assertGreater(demo.release_order_key('v10.0.0+merge.20261027.1'),
                           demo.release_order_key('v9.5.0+u3.20261027.6'))


class ReleaseRetentionTest(unittest.TestCase):
    """端到端：mtime 被批量改写后，最近两个版本仍必须保留。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.prod = root / 'prod'
        self.releases = self.prod / 'releases'
        self.releases.mkdir(parents=True)
        self.data = self.prod / 'data'
        self.data.mkdir()
        self.backups = root / 'backups'
        self.backups.mkdir()
        patcher = mock.patch.multiple(
            demo, PROD=self.prod, RELEASES=self.releases,
            DATA=self.data, BACKUPS=self.backups)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _release(self, name: str, mtime: float) -> pathlib.Path:
        path = self.releases / name
        path.mkdir()
        (path / '.version').write_text(name.split('+')[0] + '\n')
        os.utime(path, (mtime, mtime))
        return path

    def test_recent_versions_survive_bulk_mtime_rewrite(self):
        base = 1_700_000_000.0
        # 现场形态：低版本被批量改写后 mtime 反而最新
        self._release('v10.7.9+u11v.20261028.1', base)
        self._release('v10.7.8+n04.20261028.1', base + 1)
        self._release('v10.7.5+u17fix+n01n03.20261028.1', base + 500)
        self._release('v10.7.7+n03n05+u28.20261028.1', base + 501)

        report = demo.audit()
        group = next(g for g in report['groups'] if g['name'] == 'releases')
        actions = {pathlib.Path(i['path']).name: i['action'] for i in group['items']}

        self.assertEqual(actions['v10.7.9+u11v.20261028.1'], 'keep')
        self.assertEqual(actions['v10.7.8+n04.20261028.1'], 'keep')
        self.assertEqual(actions['v10.7.5+u17fix+n01n03.20261028.1'], 'prune')


class GroupFilterTest(unittest.TestCase):
    """--only / --exclude：高风险分组必须能单独排除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.prod = root / 'prod'
        self.releases = self.prod / 'releases'
        self.releases.mkdir(parents=True)
        self.data = self.prod / 'data'
        self.data.mkdir()
        self.backups = root / 'backups'
        self.backups.mkdir()
        patcher = mock.patch.multiple(
            demo, PROD=self.prod, RELEASES=self.releases,
            DATA=self.data, BACKUPS=self.backups)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _release(self, name: str) -> pathlib.Path:
        path = self.releases / name
        path.mkdir()
        (path / '.version').write_text(name.split('+')[0] + '\n')
        return path

    def test_unknown_group_is_rejected(self):
        report = demo.audit()
        self.assertEqual(demo.apply_prune(report, report['confirm_token'], only='nope'), 2)

    def test_only_leaves_other_groups_untouched(self):
        for index in range(5):
            self._release('v9.%d.0+x.20261027.%d' % (index, index))
        # 两个老快照：保留窗口是 2，故 0 个可删 → 再补一个更老的
        for index in range(3):
            self.data.joinpath('pre-switch-2020010%d-000000.db' % index).write_bytes(b'x')
            os.utime(self.data / ('pre-switch-2020010%d-000000.db' % index),
                     (1_600_000_000 + index, 1_600_000_000 + index))

        report = demo.audit()
        release_group = next(g for g in report['groups'] if g['name'] == 'releases')
        self.assertTrue(any(i['action'] == 'prune' for i in release_group['items']),
                        '前置条件：releases 组里应有可删项，否则测不到隔离效果')

        rc = demo.apply_prune(report, report['confirm_token'], only='data-snapshots')
        self.assertEqual(rc, 0)

        # data-snapshots 被处理：只剩保留窗口内的 2 个
        self.assertEqual(len(sorted(p.name for p in self.data.iterdir())), 2)
        # releases 一个都没动
        self.assertEqual(len([p for p in self.releases.iterdir() if p.is_dir()]), 5)


if __name__ == '__main__':
    unittest.main()
