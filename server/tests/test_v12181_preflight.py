"""v1.2.181：把三个检查统一成一个发布前入口。

## 为什么

仓库里有三个检查脚本，要记住三个名字、三个参数顺序、三个退出码含义。
**记不住就会跳过其中一个** —— 而跳过的那一个恰好可能是这次会失败的那个。

## 核心判断：跳过不等于通过

脚本用退出码 2 表示「没法检查」（缺清单、缺前端产物、缺脚本），
与「检查失败」（退出码 1）分开。

**但只分开还不够**：如果三项全部跳过，那是环境问题，不该报成功。
所以最后有一道判断——一项都没跑成时，退出码是 2，不是 0。
"""
from __future__ import annotations

import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2] / 'deploy' / 'preflight.sh').read_text(encoding='utf-8')


class ItRunsTheThreeChecksTests(unittest.TestCase):
    def test_it_invokes_staged_files(self):
        self.assertIn('verify-staged-files.sh', SCRIPT)

    def test_it_invokes_route_coverage(self):
        self.assertIn('check-route-coverage.sh', SCRIPT)

    def test_it_checks_version_consistency(self):
        self.assertIn('.version', SCRIPT)
        self.assertIn('release_id', SCRIPT)

    def test_no_end_of_line_anchor_in_version_extraction(self):
        """第一个过滤的输出末尾还有引号，`v[0-9.]+$` 匹配不到。

        第一版就是这么误报的，而它报出来的是「版本不一致」这种严重结论。
        """
        self.assertNotIn("grep -oE 'v[0-9.]+$'", SCRIPT)
        self.assertIn("grep -oE 'v[0-9.]+' | head -1", SCRIPT)


class SkippedIsNotPassedTests(unittest.TestCase):
    """最容易写错的一条。"""

    def test_skip_is_tracked_separately(self):
        self.assertIn('skipped=', SCRIPT)
        self.assertIn('skipped=$((skipped + 1))', SCRIPT)

    def test_exit_two_means_cannot_check(self):
        self.assertIn('elif [ "$rc" -eq 2 ]', SCRIPT)

    def test_all_skipped_is_an_error(self):
        self.assertIn('[ "$passed" -eq 0 ]', SCRIPT)
        idx = SCRIPT.find('[ "$passed" -eq 0 ]')
        window = SCRIPT[idx:idx + 200]
        self.assertIn('exit 2', window, '一项都没跑成时不该报成功')

    def test_failure_wins_over_skip(self):
        idx = SCRIPT.find('[ "$failed" -gt 0 ]')
        self.assertGreater(idx, 0)
        window = SCRIPT[idx:idx + 120]
        self.assertIn('exit 1', window)


class ItIsReadOnlyTests(unittest.TestCase):
    """所有检查都必须只读 —— 发布前跑的东西不能改变被检查的对象。"""

    def test_no_rm(self):
        self.assertNotIn('rm -rf', SCRIPT)

    def test_no_writes_to_target(self):
        self.assertNotIn('> "$TARGET', SCRIPT)

    def test_documents_that_it_does_not_deploy(self):
        self.assertIn('不部署', SCRIPT)
        self.assertIn('不改任何东西', SCRIPT)


class ItIsSafeUnderStrictModeTests(unittest.TestCase):
    def test_strict_mode(self):
        self.assertIn('set -uo pipefail', SCRIPT)

    def test_missing_target_is_an_error(self):
        self.assertIn('目录不存在', SCRIPT)


if __name__ == '__main__':
    unittest.main()
