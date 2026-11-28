"""重复源的结构守卫：打包产物里的副本必须与唯一源逐字节一致，历史文件不能被悄悄接回去。

为什么需要这一条：`deploy/update.py` 与 `server/deploy/update.py` 是同一个 10 万字节
脚本的两份拷贝（`.github/workflows/release.yml:156` 在打包时 `cp` 生成后者，理由见
那里：一键更新会整体替换 `server/`，更新器得能跟着换新，否则「要先更新一次才能拿到
新更新器」）。仓库里先把副本签进去了，于是**两份都可能被单独修改**，改错那一份不会
报错：真正跑的是 `deploy/update.py`，而用户在更新途中用的是更新器自己复制出来的那份。

同理，`docs/history/` 下的两份早期日志是历史归档。它们长得和当前 `CHANGELOG.md` 一样，
一旦有人把它们接回界面，用户会看到两年前的版本列表；`server/services/changelog.py`
的候选路径里只有根目录与 `server/` 两份，接错不会被别的测试发现。

这里的断言都是「看一眼文件」级别的，成本极低，但挡住的是静默分叉。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / 'server/tests'

# 历史日志：任何代码/脚本引用它们都算回归（要么该删，要么该说明为什么）。
HISTORICAL_LOGS = (
    'docs/history/CHANGELOG.manager-legacy.md',
    'docs/history/CHANGELOG.manager-server-legacy.md',
)

# 参与「谁读了历史日志」扫描的后缀：源码、脚本、打包与前端。
_SCAN_SUFFIXES = ('.py', '.sh', '.ps1', '.yml', '.yaml', '.ts', '.tsx', '.js', '.mjs', '.json')
_SKIP_DIRS = {'node_modules', '.git', 'out', '.next', 'docs/history'}


def _iter_repo_files():
    for path in ROOT.rglob('*'):
        if not path.is_file() or path.suffix not in _SCAN_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


class UpdateWorkerMirrorTests(unittest.TestCase):
    """`server/deploy/update.py` 是 `deploy/update.py` 的打包副本，不许分叉。"""

    def test_the_shipped_copy_is_byte_identical(self) -> None:
        source = ROOT / 'deploy' / 'update.py'
        mirror = ROOT / 'server' / 'deploy' / 'update.py'
        self.assertTrue(source.is_file(), '唯一源不见了：deploy/update.py')
        self.assertTrue(mirror.is_file(), '打包副本不见了：server/deploy/update.py')
        if source.read_bytes() != mirror.read_bytes():
            raise AssertionError(
                'deploy/update.py 与 server/deploy/update.py 不一致。'
                '只改 deploy/update.py（唯一源），然后按 release.yml:156 的同一动作同步副本。')

    def test_both_say_which_one_is_the_source(self) -> None:
        for name in ('deploy/update.py', 'server/deploy/update.py'):
            head = (ROOT / name).read_text(encoding='utf-8')[:600]
            self.assertIn('唯一源', head, f'{name} 没写明哪份是唯一源，下一个人还会改错')


class HistoricalChangelogTests(unittest.TestCase):
    """历史日志必须自带「归档」标记，且不被任何代码/脚本引用。"""

    def test_historical_files_are_marked_as_archive(self) -> None:
        for name in HISTORICAL_LOGS:
            head = (ROOT / name).read_text(encoding='utf-8')[:400]
            self.assertIn('历史归档', head, f'{name} 缺少归档标记')
            self.assertIn('CHANGELOG.md', head, f'{name} 没指向当前日志')

    def test_nothing_reads_the_historical_logs(self) -> None:
        offenders = []
        for path in _iter_repo_files():
            if path.name in ('test_duplicate_sources.py', 'changelog-archive.json'):
                continue      # 守卫自己、以及描述归档清单的索引文件
            try:
                text = path.read_text(encoding='utf-8', errors='strict')
            except (OSError, UnicodeDecodeError):
                continue
            for name in HISTORICAL_LOGS:
                if name in text or Path(name).name in text:
                    offenders.append(f'{path.relative_to(ROOT)} → {name}')
        self.assertEqual(offenders, [],
                         '有代码/脚本/页面读了历史日志；界面会显示两年前的版本列表')

    def test_the_current_log_is_the_one_the_ui_reads(self) -> None:
        from server.services import changelog
        paths = [Path(p).name for p in changelog._candidate_paths()]
        self.assertEqual(paths, ['CHANGELOG.md', 'CHANGELOG.md'],
                         '候选路径变了：确认界面读的仍是根目录与 server/ 这两份当前日志')


if __name__ == '__main__':
    unittest.main()
