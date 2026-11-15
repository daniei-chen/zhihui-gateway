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


class ItFindsTheManifestItselfTests(unittest.TestCase):
    """不再依赖某个固定路径的临时文件。"""

    def test_reads_from_the_release(self):
        self.assertIn('PREFLIGHT-MANIFEST.txt', SCRIPT)

    def test_keeps_a_fallback(self):
        """旧布局的发布目录没有这个文件，仍要能用。"""
        self.assertIn('/tmp/preflight-manifest.txt', SCRIPT)


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


class TheMemoryCheckIsHonestAboutItsLimitsTests(unittest.TestCase):
    """这一项不判断泄漏 —— 泄漏要看趋势，而一次采样看不出趋势。

    如果把它当成「泄漏检测」，它会给出一份**虚假的安全感**：
    每次都是 PASS，而真正在缓慢上涨的东西没人在看。
    """

    def test_it_reads_resident_memory(self):
        self.assertIn('VmRSS', SCRIPT)

    def test_it_has_a_configurable_threshold(self):
        self.assertIn('PREFLIGHT_MEM_LIMIT_KB', SCRIPT)

    def test_it_says_it_does_not_detect_leaks(self):
        self.assertIn('不判断泄漏', SCRIPT)
        self.assertIn('虚假的安全感', SCRIPT)

    def test_missing_pid_is_skip_not_fail(self):
        """取不到进程是「没法检查」，不是「检查失败」。"""
        idx = SCRIPT.find('取不到主进程')
        self.assertGreater(idx, 0)
        before = SCRIPT[max(0, idx - 200):idx]
        self.assertIn('skipped=$((skipped + 1))', SCRIPT[idx:idx + 200])


class ItProbesDangerousRoutesTests(unittest.TestCase):
    """鉴权不在每个路由的依赖里，而由中间件统一处理。

    「不在路由里」和「没有保护」是两件事 —— 2026-11-15 逐个实测了七个
    删除/清空/重置类路由，全部返回 401。但「现在都对」不等于「以后都对」，
    新加路由时中间件是否覆盖到它，没有任何声音提醒。
    """

    def test_it_probes_routes(self):
        self.assertIn('DANGEROUS_PROBES', SCRIPT)

    def test_401_and_403_both_count_as_refused(self):
        self.assertIn('401|403)', SCRIPT)

    def test_unreachable_is_not_treated_as_allowed(self):
        """连不上不等于放行，也不算失败。"""
        self.assertIn('000)', SCRIPT)

    def test_it_uses_while_read_not_for(self):
        """`for spec in $LIST` 会按空格分词，把方法和路径拆散。

        第一版就是这么写的：方法和路径变成两个独立单词，
        于是每条探测都拿「路径」当方法去发请求。
        """
        idx = SCRIPT.find('DANGEROUS_PROBES')
        window = SCRIPT[idx:idx + 900]
        self.assertIn('while read -r method path', window)
        self.assertNotIn('for spec in $DANGEROUS_PROBES', window)

    def test_it_explains_why_absence_of_dependency_is_not_absence_of_protection(self):
        self.assertIn('不在路由里', SCRIPT)


class ItChecksForFailedUnitsTests(unittest.TestCase):
    """一次真实的漏网：清理服务连续失败好几天，没有人知道。

    它每天凌晨只失败一次，不影响任何用户请求，状态从 inactive 变成 failed，
    而 inactive 看起来同样「没在跑」。旧快照一直堆积、磁盘慢慢涨。

    **这类失败不影响线上，所以不会被任何面向用户的检查发现。**
    """

    def test_it_lists_failed_units(self):
        self.assertIn('--state=failed', SCRIPT)

    def test_it_covers_both_prefixes(self):
        """第一版只查 `zhihui-*`，而主服务叫 `workbuddy-web` —— 不带那个前缀。

        也就是说**主服务失败时这一项不会报**，而那正是最该报的一个。
        这台机器上的命名并不统一，两个都得查。
        """
        self.assertIn("'zhihui-*'", SCRIPT)
        self.assertIn("'workbuddy-*'", SCRIPT)

    def test_it_does_not_cover_other_apps(self):
        """别的应用的失败不该阻断这个网关的发布。"""
        self.assertNotIn("'kaogong-*'", SCRIPT)

    def test_it_excludes_the_onfailure_template(self):
        """`wb-*` 是告警模板实例，正常就有非活动实例，算进来是噪音。"""
        self.assertIn('不查 `wb-*`', SCRIPT)

    def test_failure_is_reported(self):
        self.assertIn('处于 failed', SCRIPT)

    def test_it_records_the_incident(self):
        self.assertIn('没有人知道', SCRIPT)

    def test_missing_systemctl_is_skip(self):
        self.assertIn('没有 systemctl', SCRIPT)


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
