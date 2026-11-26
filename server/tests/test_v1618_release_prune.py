"""旧发布目录要能清理，但不能把回滚路径清掉。

## 为什么需要

`releases/` 每发一版就多一份完整产物（约 41 MB）。2026-11-26 实测 65 个版本占
2.6 GB，磁盘 74%，而每轮迭代都要发一版 —— 其中绝大多数永远不会再被 activate。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 默认只预演 | 一条命令手滑删掉几十个版本 |
| `current` 与 `.previous` 保留 | 回滚目标消失，故障时无处可退 |
| 最近 N 个保留 | 连刚发的版本都被删 |
| 版本号按数字排序 | `v1.6.10` 排到 `v1.6.9` 前面，删错 |
| 符号链接与非版本目录不碰 | 删掉 `current` 或其他无关目录 |
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy'))

import release  # noqa: E402
from release import LABEL  # noqa: E402


def _symlinks_available() -> bool:
    """Windows 上创建目录符号链接需要特权；没有就跳过那两个用例。"""
    try:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            target.mkdir()
            os.symlink(target, Path(directory) / 'link', target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


SYMLINKS = _symlinks_available()


class ReleasePruneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'releases'
        self.root.mkdir()

    def _make(self, version: str, size: int = 8) -> Path:
        path = self.root / version
        path.mkdir()
        (path / 'payload.bin').write_bytes(b'x' * size)
        return path

    def _run(self, *argv: str) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = release.main(['--root', str(self.root), 'prune', *argv])
        self.assertEqual(code, 0, buffer.getvalue())
        return buffer.getvalue()

    def _versions(self) -> set[str]:
        return {p.name for p in self.root.iterdir() if p.is_dir() and LABEL.fullmatch(p.name)}

    def test_dry_run_keeps_everything(self) -> None:
        for version in ('v1.0.1', 'v1.0.2', 'v1.0.3', 'v1.0.4'):
            self._make(version)
        output = self._run('--keep', '2')
        self.assertIn('预演', output)
        self.assertEqual(len(self._versions()), 4, '没加 --apply 就不许删')

    def test_apply_keeps_the_recent_window(self) -> None:
        for version in ('v1.0.1', 'v1.0.2', 'v1.0.3', 'v1.0.4'):
            self._make(version)
        self._run('--keep', '2', '--apply')
        self.assertEqual(self._versions(), {'v1.0.3', 'v1.0.4'})

    def test_current_and_previous_are_never_pruned(self) -> None:
        for version in ('v1.0.1', 'v1.0.2', 'v1.0.3', 'v1.0.4'):
            self._make(version)
        # current 指向最老的、.previous 指向次老的 —— 正好都在「最近 1 个」之外。
        # 用 patch 而不是真建符号链接：Windows 上那需要特权，而这个用例要验的是
        # 保留集合的算法，不是 readlink 能不能跑。
        (self.root / '.previous').write_text('v1.0.2\n', encoding='utf-8')
        with patch.object(release, '_previous', return_value='v1.0.1'):
            self._run('--keep', '1', '--apply')
        kept = self._versions()
        self.assertIn('v1.0.1', kept, 'current 指向的版本不能删')
        self.assertIn('v1.0.2', kept, '.previous 指向的版本不能删（否则回滚无处可去）')
        self.assertIn('v1.0.4', kept)

    def test_versions_sort_numerically_not_lexically(self) -> None:
        for version in ('v1.6.2', 'v1.6.9', 'v1.6.10', 'v1.6.11'):
            self._make(version)
        self._run('--keep', '2', '--apply')
        # 字符串排序会把 v1.6.9 排在 v1.6.10 之后，那样 v1.6.10/1.6.11 会被当成旧版删掉。
        self.assertEqual(self._versions(), {'v1.6.10', 'v1.6.11'})

    def test_foreign_directories_are_left_alone(self) -> None:
        for version in ('v1.0.1', 'v1.0.2', 'v1.0.3'):
            self._make(version)
        (self.root / 'staging').mkdir()
        (self.root / '.previous').write_text('v1.0.1\n', encoding='utf-8')
        self._run('--keep', '1', '--apply')
        self.assertTrue((self.root / 'staging').is_dir(), '不是版本号的目录一概不碰')
        self.assertTrue((self.root / '.previous').is_file())

    @unittest.skipUnless(SYMLINKS, '需要符号链接权限（Windows 上默认没有；Linux CI 覆盖）')
    def test_symlinks_are_left_alone(self) -> None:
        for version in ('v1.0.1', 'v1.0.2', 'v1.0.3'):
            self._make(version)
        (self.root / 'current').symlink_to(self.root / 'v1.0.1', target_is_directory=True)
        self._run('--keep', '1', '--apply')
        self.assertTrue((self.root / 'current').is_symlink(), 'current 是符号链接，删不得')

    def test_nothing_to_do_is_not_an_error(self) -> None:
        for version in ('v1.0.1', 'v1.0.2'):
            self._make(version)
        output = self._run('--keep', '5', '--apply')
        self.assertIn('没有可清理', output)
        self.assertEqual(len(self._versions()), 2)


if __name__ == '__main__':
    unittest.main()
