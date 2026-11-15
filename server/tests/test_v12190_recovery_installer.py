"""v1.2.190：让那次修复成为可重复的。

## 由来

2026-11-15：修掉了快照清理任务的崩溃，部署后发现**修复没生效** ——
systemd 执行的是 `/opt/zhihui-release/recovery/` 里那份，而发布目录里那份才是新的。

**两处文件，只有一处被更新。**

更要紧的是：**没有任何部署脚本负责那个目录**，它是手工维护的。
那次修复只要遇到一次重装就会丢失 —— **而丢失之后不会报错**，
只会重新变成「每天失败一次、没有人被通知到」。
"""
from __future__ import annotations

import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / 'deploy' / 'install-recovery-scripts.sh'
SCRIPT = SCRIPT_PATH.read_text(encoding='utf-8')


class ItExistsAndIsSafeTests(unittest.TestCase):
    def test_script_exists(self):
        self.assertTrue(SCRIPT_PATH.is_file())

    def test_strict_mode(self):
        self.assertIn('set -uo pipefail', SCRIPT)

    def test_requires_root(self):
        self.assertIn('需要 root', SCRIPT)

    def test_checks_both_directories(self):
        self.assertIn('源目录不存在', SCRIPT)
        self.assertIn('目标目录不存在', SCRIPT)

    def test_fails_when_nothing_to_install(self):
        """源目录为空时不该「成功地什么也没做」。"""
        self.assertIn('没有 recovery_*.py', SCRIPT)


class ItBacksUpBeforeOverwritingTests(unittest.TestCase):
    def test_backup_with_timestamp(self):
        self.assertIn('bak-$stamp', SCRIPT)

    def test_no_backup_when_identical(self):
        """重复运行不该堆出一堆一样的备份。"""
        self.assertIn('cmp -s "$src" "$dst"', SCRIPT)
        self.assertIn('unchanged', SCRIPT)


class ItPreservesPermissionsTests(unittest.TestCase):
    """这些脚本含备份逻辑，权限必须是 700 root:root。"""

    def test_mode_700(self):
        self.assertIn('-m 0700', SCRIPT)

    def test_owner_root(self):
        self.assertIn('-o 0 -g 0', SCRIPT)


class ItVerifiesAfterInstallingTests(unittest.TestCase):
    """写完不回读，就等于相信「复制成功」等于「内容正确」。"""

    def test_reads_back_hash(self):
        self.assertIn('got=$(sha256sum "$dst"', SCRIPT)

    def test_mismatch_is_a_failure(self):
        self.assertIn('写入后哈希不一致', SCRIPT)

    def test_failure_exits_nonzero(self):
        idx = SCRIPT.find('RECOVERY-INSTALL')
        self.assertIn('exit 1', SCRIPT[idx:idx + 200])


class ItExplainsWhyItExistsTests(unittest.TestCase):
    def test_it_records_the_two_locations_problem(self):
        self.assertIn('两处文件，只有一处被更新', SCRIPT)

    def test_it_says_the_fix_would_be_lost_silently(self):
        self.assertIn('丢失之后不会报错', SCRIPT)


if __name__ == '__main__':
    unittest.main()
