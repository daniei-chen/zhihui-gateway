"""G-09 / D-12：恢复包必须真的覆盖每个数据根，而且覆盖不完整时不许报成功。

上一版的行为：`backup_application.py` 只硬编码了 5 个状态文件，
`manager/data` / `gateway/data` / `gateway/auths` 三个数据根**只写一行目录条目**
（`add_state_directory()`），内容是空的。于是 `tar -tzf` 里能看到 `manager/data`
却一个文件都没有，而备份照样报 `ok` —— 清单看起来覆盖了，实际没覆盖。

这里验的是新不变量：**根下每个普通文件要么进包、要么在排除表里带理由**，
新增文件默认进包；空壳目录算失败；覆盖不完整时状态与告警必须说出来。
"""
import json
import os
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from server import backupstatus, recoverystate


def _write(path, text='{}'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


class StateRootInvariantTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = Path(self.directory.name) / 'manager'
        self.gateway = Path(self.directory.name) / 'gateway'
        _write(self.manager / 'data' / 'users.json')
        _write(self.manager / 'data' / 'manager.db')

    def collect(self, paired=False):
        return recoverystate.collect_state_entries(self.manager, self.gateway, paired=paired)

    def test_new_state_file_is_archived_without_touching_any_list(self):
        """**默认进包**：新增一个状态文件，不需要回来改清单。"""
        _write(self.manager / 'data' / 'brand-new-state.json',
               '{"installed": "2026-11-28"}')
        entries, excluded, unexpected = self.collect()
        self.assertEqual(unexpected, [])
        archived = {name for name, _path in entries}
        self.assertIn('manager/data/brand-new-state.json', archived)
        # 同一次里，硬编码时代会漏掉的那些「有状态但不在 5 个名字里」的文件也在。
        _write(self.manager / 'data' / 'admission-cohort.db', 'x')
        _write(self.manager / 'data' / 'notify' / 'undelivered.jsonl', '{}')
        entries, _excluded, _unexpected = self.collect()
        archived = {name for name, _path in entries}
        self.assertIn('manager/data/admission-cohort.db', archived)
        self.assertIn('manager/data/notify/undelivered.jsonl', archived)

    def test_every_exclusion_carries_a_reason(self):
        """没有理由的排除等于没排除 —— 这条让排除表不能当垃圾桶。"""
        for owner, pattern, reason in recoverystate.STATE_EXCLUSIONS + recoverystate.PAIRED_STATE:
            self.assertIn(owner, ('manager', 'gateway'), pattern)
            self.assertTrue(pattern.strip(), reason)
            self.assertGreaterEqual(len(reason.strip()), 8,
                                    f'{pattern} 的排除理由太短，等于没写')

    def test_the_whole_root_is_accounted_for(self):
        """不变量本体：根下每个文件都落在「进包」或「带理由排除」里。"""
        _write(self.manager / 'data' / 'update.lock', '')
        _write(self.gateway / 'data' / 'state.json', '{}')
        _write(self.gateway / 'auths' / 'a@example.com.json', '{}')
        entries, excluded, unexpected = self.collect()
        self.assertEqual(unexpected, [])
        archived = {name for name, _path in entries}
        skipped = {item['path'] for item in excluded}
        on_disk = set()
        for family, directory in recoverystate.STATE_ROOTS:
            root = (self.manager if family == 'manager' else self.gateway) / directory
            if not root.is_dir():
                continue
            for path in root.rglob('*'):
                if path.is_file():
                    on_disk.add(family + '/' + directory + '/' + path.relative_to(root).as_posix())
        self.assertEqual(on_disk, archived | skipped,
                         '有文件既不进包也没被带理由排除')
        self.assertIn('manager/data/users.json', archived)
        self.assertIn('manager/data/manager.db', skipped)
        self.assertIn('manager/data/update.lock', skipped)

    def test_paired_state_is_only_excluded_when_the_pair_was_taken(self):
        _write(self.manager / 'data' / 'user-portal.sqlite3', 'db')
        _write(self.manager / 'data' / 'ledger-spool' / 'a.json', '{}')
        entries, excluded, _unexpected = self.collect(paired=False)
        archived = {name for name, _path in entries}
        self.assertIn('manager/data/user-portal.sqlite3', archived,
                      '没打 paired snapshot 时必须按默认路径进包，否则账本会静默丢掉')
        self.assertIn('manager/data/ledger-spool/a.json', archived)
        entries, excluded, _unexpected = self.collect(paired=True)
        archived = {name for name, _path in entries}
        skipped = {item['path'] for item in excluded}
        self.assertNotIn('manager/data/user-portal.sqlite3', archived)
        self.assertIn('manager/data/user-portal.sqlite3', skipped)
        self.assertIn('manager/data/ledger-spool/a.json', skipped)

    def test_symlink_inside_a_root_fails_instead_of_being_skipped(self):
        target = _write(self.manager / 'outside.json')
        link = self.manager / 'data' / 'linked.json'
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows 无权限时
            self.skipTest('本机不能创建符号链接')
        _entries, _excluded, unexpected = self.collect()
        self.assertEqual([item['path'] for item in unexpected], ['manager/data/linked.json'])

    def test_empty_directory_entry_is_treated_as_a_failure(self):
        """`tar -tzf` 里有一行 `manager/data` 不等于里面备份了东西。"""
        archive_path = Path(self.directory.name) / 'probe.tar'
        with tarfile.open(archive_path, 'w') as archive:
            info = tarfile.TarInfo('manager/data')
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
            self.assertEqual(recoverystate.empty_archive_directories(archive),
                             ['manager/data'])
        with tarfile.open(archive_path, 'w') as archive:
            info = tarfile.TarInfo('manager/data')
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
            payload = b'{}'
            member = tarfile.TarInfo('manager/data/users.json')
            member.size = len(payload)
            import io
            archive.addfile(member, io.BytesIO(payload))
            self.assertEqual(recoverystate.empty_archive_directories(archive), [])

    def test_uncovered_roots_are_declared_until_they_are_archived(self):
        """qtg 引擎根在 PR2 之前必须自报未覆盖；它落地后这条会红，提醒删掉声明。"""
        uncovered = recoverystate.uncovered_roots()
        self.assertTrue(uncovered, '未覆盖的根必须显式声明，不能默默漏掉')
        for item in uncovered:
            self.assertTrue(item['root'])
            self.assertGreaterEqual(len(item['reason']), 8)


class CoverageStatusTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.status_path = Path(self.directory.name) / 'backup-status.json'
        self.patch = patch('server.config.BACKUP_STATUS_FILE', self.status_path)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def result(self, complete):
        return {'bytes': 4096, 'restore_check': 'ok', 'sha256': 'a' * 64,
                'verified_at': int(time.time()),
                'state_coverage_complete': complete,
                'state_coverage': {'roots': {'manager/data': 3},
                                   'excluded': [], 'complete': complete,
                                   'uncovered': [] if complete else recoverystate.uncovered_roots()}}

    def test_incomplete_coverage_is_published_as_degraded(self):
        public = backupstatus.publish_verified_status(self.result(False), self.status_path)
        self.assertFalse(public['state_coverage_complete'])
        self.assertEqual(public['uncovered_state_roots'], ['qtg-engine-var'])
        on_disk = json.loads(self.status_path.read_text())
        self.assertFalse(on_disk['state_coverage_complete'],
                         '落盘的那份也要带着这个结论，否则下游读不到')

    def test_complete_coverage_publishes_clean(self):
        public = backupstatus.publish_verified_status(self.result(True), self.status_path)
        self.assertTrue(public['state_coverage_complete'])
        self.assertEqual(public['uncovered_state_roots'], [])


class MissingCoverageFieldIsNotHealthyTest(unittest.TestCase):
    """旧格式报告（没有覆盖结论）不能被当成「覆盖完整」。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'backup-status.json'
        self.now = time.time()
        patcher = patch('server.config.BACKUP_STATUS_FILE', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        offsite = Path(self.directory.name) / 'offsite.json'
        offsite.write_text('{}')
        patcher = patch('server.offsitebackup.CONFIG', offsite)
        patcher.start()
        self.addCleanup(patcher.stop)
        from server import operational
        patcher = patch.object(operational, 'alerts', {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.operational = operational

    def test_report_without_coverage_field_cannot_clear_the_alert(self):
        from server import offsitebackup
        self.path.write_text(json.dumps({
            'schema': 2, 'created_at': int(self.now), 'restore_check': 'ok',
            'bytes': 4096, 'offsite': True, 'archive_sha256': 'a' * 64,
            'offsite_readback': {'method': offsitebackup.READBACK_METHOD,
                                 'verified_at': int(self.now), 'commit': 'b' * 40,
                                 'cipher_sha256': 'c' * 64, 'cipher_bytes': 4096,
                                 'source_archive_sha256': 'a' * 64}}))
        os.utime(self.path, (self.now, self.now))
        self.operational.evaluate_backup_health(self.now)
        self.assertIn('backup_state_coverage', self.operational.alerts,
                      '旧格式报告没有覆盖结论，不能当成「覆盖完整」')
        self.assertNotIn('backup_stale', self.operational.alerts)


if __name__ == '__main__':
    unittest.main()
