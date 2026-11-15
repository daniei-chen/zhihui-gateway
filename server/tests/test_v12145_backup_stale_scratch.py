"""v1.2.145：备份被中断时留下的临时工作区，现在会在下次启动时自己清掉。

## 怎么发现的

2026-11-15 磁盘涨到 94%，排查时看到 `/root/backups/workbuddy-daily` 下有
两个 `snapshot-*` 目录，合计 2.6 GB —— 每个里面躺着一份完整的 `manager.db`。

它们来自 `tempfile.TemporaryDirectory(prefix='snapshot-', dir=BASE)`。

## 为什么它没被自动删掉

`TemporaryDirectory` 靠 `__exit__` 清理。正常结束、抛异常都会走到那里，
**但 SIGKILL、OOM、宿主重启不会** —— 进程直接消失，`__exit__` 从来没跑过。

而这是**一个装着完整数据库副本的目录**，一份一点几 GB。中断两次就是 2.6 GB，
**直到磁盘写满才会有人注意到**。

## 改法

在拿到独占锁之后、创建本次工作区之前，清掉所有 `snapshot-*`。

**为什么必须在锁之后**：`flock` 成功意味着此刻没有别的备份在跑，
所以磁盘上那些 `snapshot-*` 必然是死掉的残留。**在锁之前删**有可能
删掉另一个正在跑的进程的工作区 —— 那会毁掉一次进行中的备份。

**删不掉不报错**：一次清理失败不该阻断一次正常备份，下次启动再试。
这比「为了清理干净而让备份失败」安全。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 启动时清理 `snapshot-*` | 中断一次泄漏 1–2 GB，直到磁盘满 |
| 清理发生在拿到锁之后 | 可能删掉正在进行的备份的工作区 |
| 只删目录、不跟随符号链接 | 误删目录之外的东西 |
| 删除失败不阻断备份 | 一次权限问题让备份整体失败 |
"""
from __future__ import annotations

import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / 'backup_application.py').read_text(
    encoding='utf-8')


class TheCleanupExistsTests(unittest.TestCase):
    def test_it_globs_the_staging_prefix(self):
        self.assertIn("BASE.glob('snapshot-*')", SOURCE,
                      '没有清理残留工作区 —— 中断一次就泄漏一份数据库副本')

    def test_it_removes_the_tree(self):
        idx = SOURCE.find("BASE.glob('snapshot-*')")
        window = SOURCE[idx:idx + 500]
        self.assertIn('shutil.rmtree', window)


class ItRunsAfterTheLockTests(unittest.TestCase):
    """必须在 flock 成功之后 —— 否则可能删掉正在运行的备份的工作区。"""

    def test_cleanup_follows_the_flock(self):
        lock_at = SOURCE.find('LOCK_EX')
        clean_at = SOURCE.find("BASE.glob('snapshot-*')")
        self.assertGreater(lock_at, 0)
        self.assertGreater(clean_at, 0)
        self.assertLess(lock_at, clean_at,
                        '清理在加锁之前 —— 可能删掉另一个正在跑的备份的工作区')


class ItIsConservativeTests(unittest.TestCase):
    def setUp(self):
        idx = SOURCE.find("BASE.glob('snapshot-*')")
        self.window = SOURCE[idx:idx + 600]

    def test_it_only_touches_directories(self):
        self.assertIn('is_dir()', self.window)

    def test_it_refuses_symlinks(self):
        self.assertIn('is_symlink()', self.window)

    def test_a_failed_removal_does_not_abort_the_backup(self):
        self.assertIn('except OSError', self.window)
        self.assertNotIn('raise', self.window.split('except OSError')[1][:200])


class TheReasonIsWrittenDownTests(unittest.TestCase):
    def test_the_comment_records_the_measured_leak(self):
        idx = SOURCE.find("BASE.glob('snapshot-*')")
        window = SOURCE[max(0, idx - 1400):idx]
        self.assertIn('2.6 GB', window, '没写出实测到的泄漏量，后来人不会当回事')
        self.assertIn('SIGKILL', window, '没写清为什么不靠 TemporaryDirectory 自己清理')


class TheImportIsAtModuleScopeTests(unittest.TestCase):
    """`shutil` 必须在模块级导入。

    这个文件里另有一处 `import shutil` 藏在**另一个函数内部**
    （`verify_paired_archive` 的 `shutil.copyfileobj`）。如果只靠那一处，
    `main()` 里的 `shutil.rmtree` 会在运行时抛 `NameError` ——
    而它只在**真有残留要清**的时候才执行，平时看不出来。
    """

    def test_shutil_is_imported_at_module_level(self):
        header = SOURCE.split('\n\n\n')[0]
        self.assertRegex(header, r'(?m)^import .*\bshutil\b',
                         'shutil 不在模块级导入 —— main() 里用它会在运行时 NameError')

    def test_the_cleanup_is_not_inside_the_other_function(self):
        """`BASE.glob('snapshot-*')` 必须落在 main() 里，不是别处。"""
        main_at = SOURCE.find('def main():')
        clean_at = SOURCE.find("BASE.glob('snapshot-*')")
        self.assertGreater(main_at, 0)
        self.assertGreater(clean_at, main_at,
                           '清理代码不在 main() 之后 —— 可能落进了别的函数')


if __name__ == '__main__':
    unittest.main()
