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
import signal
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


SOURCE = (Path(__file__).resolve().parents[1] / 'backup_application.py').read_text(
    encoding='utf-8')


class MainWiringIsPresentTest(unittest.TestCase):
    """`main()` 的两处接线存在性。

    **这证的是接线存在，不是接线跑通。** 本仓没有任何用例真正执行过
    `backup_application.main()`：该模块 `import fcntl`，非 POSIX 上连导入都做不到，
    现有证据全是源码文本断言（`test_v12144` / `test_v12145` / `test_v12157`
    也是同一形态）。所以下面两条只能证明「`if …: raise` 还在」，
    **不能**当端到端回归依据 —— `collect_state_entries` 的返回值对，
    不等于备份真的会失败。真跑一次 `main()` 的 POSIX 门控用例见 SWO-88。
    """

    def anchor(self, start, length=400):
        index = SOURCE.find(start)
        self.assertNotEqual(index, -1, f'源码里找不到接线锚点：{start}')
        return SOURCE[index:index + length]

    def test_main_raises_on_unaccounted_state_entries(self):
        """未归类的条目必须让 `main()` 失败，不能只算进覆盖清单。"""
        window = self.anchor('collect_state_entries(MANAGER, GATEWAY')
        self.assertIn('if unexpected:', window,
                      '`unexpected` 算出来了却没有判据 —— 无法归类的条目会被静默放行')
        self.assertIn("raise RuntimeError('state root has unaccounted entries",
                      window, '`unexpected` 非空时必须抛出，而不是继续打包')

    def test_main_raises_on_empty_archive_directories(self):
        """空壳目录条目必须让 `main()` 失败 —— 清单里有目录不等于备份了内容。"""
        window = self.anchor('empty = empty_archive_directories(archive)')
        self.assertIn('if empty:', window,
                      '`empty` 算出来了却没有判据 —— 空壳目录会照样通过验证')
        self.assertIn("raise RuntimeError('recovery archive has directory entries without files",
                      window, '有空壳目录条目时必须抛出，而不是继续发布这份包')


@unittest.skipUnless(hasattr(os, 'mkfifo'), '本机没有 os.mkfifo（Windows 无法造 FIFO）')
class NonRegularStateEntryTest(unittest.TestCase):
    """FIFO 之类不是普通文件，**不能当文件收进包**：`open('rb')` 在 FIFO 上是阻塞，
    不是异常 —— 当晚备份会挂住，没有异常、没有退出码，只能靠人第二天发现。
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = Path(self.directory.name) / 'manager'
        self.gateway = Path(self.directory.name) / 'gateway'
        self.pipe = self.manager / 'data' / 'logpipe'
        self.pipe.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self.pipe)  # 真的 FIFO，不是 mock

    def timed(self, call):
        """把「挂住」变成失败：Windows 没有 SIGALRM，但那里没有 mkfifo，整类已 skip。"""
        if not hasattr(signal, 'SIGALRM'):
            call()
            return
        def handler(signum, frame):
            raise AssertionError('在 FIFO 上挂住了 —— 判据失效，这一夜备份不会结束')
        previous = signal.signal(signal.SIGALRM, handler)
        signal.alarm(5)
        try:
            call()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def test_a_fifo_in_a_state_root_is_reported_instead_of_blocking(self):
        entries, _excluded, unexpected = recoverystate.collect_state_entries(
            self.manager, self.gateway, paired=False)
        self.assertEqual([item['path'] for item in unexpected], ['manager/data/logpipe'])
        self.assertNotIn('manager/data/logpipe', {name for name, _path in entries},
                         'FIFO 被当成普通文件收进包，备份就会卡在 open 上')

    def test_writing_a_fifo_fails_instead_of_hanging(self):
        """枚举之后、打开之前被换成 FIFO 的窗口：必须报错，不能挂住。"""
        archive_path = Path(self.directory.name) / 'probe.tar'

        def call():
            with tarfile.open(archive_path, 'w') as archive:
                with self.assertRaises(RuntimeError):
                    recoverystate.add_state_file(
                        archive, 'manager/data/logpipe', self.pipe)

        self.timed(call)


class DisabledAuthFileKeepsRawBytesTest(unittest.TestCase):
    """`.json.disabled` 现在**原样入包**，不再经过 `stable_json` 归一化。

    旧版显式 `state.extend((GATEWAY/'auths').glob('*.json.disabled'))`，随后统一
    `add_bytes(archive, name, stable_json(path), ...)`；新版 `main()` 只对 suffix
    恰好是 `.json` 的成员做归一化。原样更对 —— 禁用文件不是运行时配置，而且源文件
    本身不是合法 JSON 时不再把整次备份拖垮 —— 但它**改了包内字节**，所以这里钉住
    两件事：归一化的选择条件，以及原样写法确实逐字节保真。

    边界：钉的是「选择条件 + 原样写法」，不是端到端跑一次 `main()`（那件事在 SWO-88）。
    """

    def test_only_exact_json_suffix_is_normalized(self):
        self.assertEqual(Path('account.json').suffix, '.json')
        self.assertEqual(Path('account.json.disabled').suffix, '.disabled')
        index = SOURCE.find("if path.suffix=='.json':")
        self.assertNotEqual(index, -1, '归一化的选择条件不见了，先确认它挪到哪了')
        self.assertIn('add_bytes', SOURCE[index:index + 200])

    def test_raw_state_files_survive_byte_for_byte(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name) / 'account.json.disabled'
        source.write_text('{"b": 1,   "a": 2}\n', encoding='utf-8')
        archive_path = Path(directory.name) / 'probe.tar'
        with tarfile.open(archive_path, 'w') as archive:
            recoverystate.add_state_file(
                archive, 'gateway/auths/account.json.disabled', source)
        with tarfile.open(archive_path) as archive:
            got = archive.extractfile('gateway/auths/account.json.disabled').read()
        self.assertEqual(got, source.read_bytes(),
                         '`.json.disabled` 必须原样入包；被重排说明它走了 .json 那条路')


if __name__ == '__main__':
    unittest.main()
