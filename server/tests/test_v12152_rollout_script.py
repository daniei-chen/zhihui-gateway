"""v1.2.152：把「prepare → run → 收尾」三步收成一条命令。

## 为什么

这三步以前分开做，而**第三步经常被忘掉**。

漏掉收尾的后果是静默的：门禁文件留着，对应单元下次**连启动都不会尝试**
（`ConditionPathExists=!` 不满足时 systemd 直接 skip，日志只有一句 skipped）。

**一个必须靠人记住的步骤，等于没有这个步骤。**

## 设计上刻意保留的不对称

三步的失败处理**不一样**，所以不能用 `set -e`：

| 步骤 | 失败后 |
| --- | --- |
| prepare | 中止，事务保留供排查（内部已重试过乐观锁拒绝） |
| run | 中止，**不杀进程**（可能有补偿步骤要跑完） |
| 收尾 | **非零退出，但不自动回滚**（新版本已在跑，失败必须人工核对） |

统一 `set -e` 会把这三种情况压成同一个「退出 1」，
而它们需要的处理完全不同。

## 为什么用 journal 的 phase 等待，而不是猜时间

`run` 耗时从 20 秒到 2 分钟不等（取决于滚动重启）。猜一个固定 sleep，
要么白等要么不够。**判据必须是事务自己报的阶段。**

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 三步都被调用 | 漏掉收尾 → 门禁残留，下次该单元静默不启动 |
| 不用 `set -e` | 三种失败被压成同一种，收尾失败会让成功发布变失败 |
| 收尾失败不改退出码 | 同上 |
| 用 journal phase 等待 | 猜时间 → 要么慢要么误判 |
| 事务失败时提前返回 | 白等满超时，掩盖真失败 |
| 参数缺失时报错 | 空参数会把 `$ROOT` 拼成 releases 目录本身 |
"""
from __future__ import annotations

import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'rollout.sh'
SOURCE = SCRIPT.read_text(encoding='utf-8')


class ItRunsAllThreeStepsTests(unittest.TestCase):
    def test_prepare_is_called(self):
        self.assertIn('prepare-retry.sh', SOURCE)

    def test_run_is_called(self):
        self.assertIn('migration_entry.py" run', SOURCE)

    def test_cleanup_is_called(self):
        self.assertIn('post-migration-cleanup.sh', SOURCE,
                      '漏掉收尾 —— 门禁会残留，而残留是静默的')


class PrepareNeedsTwoLayersOfRetryTests(unittest.TestCase):
    """**一层重试不够。**

    `prepare-retry.sh` 内部重试 4 次，但实测一整轮 4 次全被拒是常态：
    拒绝原因是「多了一个写入者」，而那是看门狗的一轮巡视 ——
    它不会因为你在 4 次内重试就消失。

    **过去的做法是人工再跑一次，第二次通常就过了。** 这个脚本第一版只调一次，
    自举验证时正是这样失败的。
    """

    def test_it_has_an_outer_retry_function(self):
        self.assertIn('prepare_with_outer_retry', SOURCE,
                      '没有外层重试 —— 一整轮 4 连拒就会直接中止')

    def test_it_loops_over_attempts(self):
        idx = SOURCE.find('prepare_with_outer_retry()')
        self.assertGreater(idx, 0)
        body = SOURCE[idx:idx + 900]
        self.assertIn('while', body, '外层重试没有循环')
        self.assertIn('sleep', body, '重试之间没有间隔，等不到写入者退出')

    def test_it_returns_failure_after_exhausting(self):
        idx = SOURCE.find('prepare_with_outer_retry()')
        body = SOURCE[idx:idx + 900]
        self.assertIn('return 1', body, '用尽重试后没有返回失败')

    def test_it_does_not_reference_the_local_max_outside(self):
        """`$max` 是 local，函数外引用它会是空字符串。

        第一版就在 die 的消息里写了 `$max`，实际输出会是「已重试  轮」。
        """
        idx = SOURCE.find('prepare_with_outer_retry; then')
        self.assertGreater(idx, 0)
        window = SOURCE[idx:idx + 400]
        # 只看会真的执行的行 —— 注释里提到 `$max` 是在解释「为什么不引用它」，
        # 第一版测试连注释一起判，被自己的说明绊倒。
        for line in window.splitlines():
            if line.strip().startswith('#'):
                continue
            self.assertNotIn('$max', line,
                             '在函数外引用了 local 变量 $max：' + line.strip())


class ItKeepsTheThreeFailuresDistinctTests(unittest.TestCase):
    def test_no_set_e(self):
        """`set -e` 会把三种失败压成同一个退出码。"""
        self.assertIn('set -uo pipefail', SOURCE)
        self.assertNotIn('set -euo', SOURCE)
        self.assertNotIn('set -eu', SOURCE)

    def test_cleanup_failure_returns_nonzero_without_blind_rollback(self):
        idx = SOURCE.find('cleanup_rc=$?')
        self.assertGreater(idx, 0)
        window = SOURCE[idx:idx + 500]
        self.assertIn('die', window)
        self.assertIn('暂停发布', window)
        self.assertNotIn('rollback', window.lower())

    def test_prepare_failure_stops(self):
        idx = SOURCE.find('prepare_with_outer_retry; then')
        self.assertGreater(idx, 0, '没找到 prepare 的失败分支')
        self.assertIn('die', SOURCE[idx:idx + 400],
                      'prepare 失败后没有中止')

    def test_run_failure_stops(self):
        idx = SOURCE.find('wait_phase complete')
        self.assertGreater(idx, 0)
        self.assertIn('die', SOURCE[idx:idx + 400],
                      'run 没完成却继续往下走')


class ItWaitsOnTheJournalTests(unittest.TestCase):
    def test_it_reads_phase_from_the_journal(self):
        """判据必须是事务自己报的阶段，不是猜时间。

        注意转义：bash 里写的是 `grep -q "\\"phase\\":\\"$want\\""`，
        源码中不存在裸的 `"phase"`。第一版测试就是照字面找的，被自己绊倒。
        """
        idx = SOURCE.find('wait_phase()')
        self.assertGreater(idx, 0, '没有 wait_phase 函数')
        body = SOURCE[idx:idx + 900]
        self.assertIn('grep -q', body, 'wait_phase 里没有读 journal')
        self.assertIn('phase', body, 'wait_phase 没看 phase')
        self.assertIn('active.json', SOURCE, '没读事务 journal')

    def test_it_stops_early_on_failure(self):
        idx = SOURCE.find('def wait_phase') if 'def wait_phase' in SOURCE else SOURCE.find('wait_phase()')
        self.assertGreater(idx, 0)
        window = SOURCE[idx:idx + 900]
        self.assertIn('failure', window,
                      '事务失败时仍等满超时，会把真失败拖成超时')

    def test_it_has_a_bounded_timeout(self):
        """上限必须落在「上限所在的那一行」上。

        原来是 `assertIn('360', SOURCE)` —— 整份脚本里任何一处出现 360 都能满足，
        注释里的版本号、端口、随机数字都行，等于没断言。改成两句：`wait_phase`
        的默认上限是个数字，调用处也显式传了上限。
        """
        import re
        default = re.search(r'limit=\$\{2:-(\d+)\}', SOURCE)
        self.assertIsNotNone(default, 'wait_phase 没有默认上限')
        limit = int(default.group(1))
        self.assertGreater(limit, 0, '上限不能是 0')
        self.assertLessEqual(limit, 900, f'上限 {limit}s 太长，重启窗口会被拖成故障')
        self.assertRegex(SOURCE, r'wait_phase\s+complete\s+\d+',
                         '调用处没有显式上限')

    def test_run_is_not_killed_on_timeout(self):
        """事务可能有补偿步骤要跑完，杀了它就断在半路。"""
        self.assertNotIn('kill ', SOURCE,
                         '超时时杀掉了 run 进程 —— 补偿可能做到一半')


class ItValidatesArgumentsTests(unittest.TestCase):
    def test_it_requires_both_arguments(self):
        self.assertIn('[ -n "$VERSION" ] || die', SOURCE)
        self.assertIn('[ -n "$TXN" ] || die', SOURCE)

    def test_it_checks_the_release_directory(self):
        self.assertIn('[ -d "$ROOT" ] || die', SOURCE,
                      '没校验版本目录，空参数会指向 releases 目录本身')

    def test_it_checks_both_scripts_exist(self):
        self.assertIn('prepare-retry.sh" ]', SOURCE)
        self.assertIn('post-migration-cleanup.sh" ]', SOURCE)


class ItChecksTheRuntimeAfterwardsTests(unittest.TestCase):
    """事务 complete 不等于进程加载了新代码。

    2026-11-15 手工验证过一次两者一致，但手工查过的东西不写下来，
    下次还得靠人想起。这一步把它变成每次自动核对。

    判据是进程 cwd，不是 `current` 符号链接 ——
    链接指对了、进程还在旧目录里跑，是可能的。
    """

    def test_it_reads_the_current_target(self):
        self.assertIn('readlink -f "$RELEASES/current"', SOURCE)

    def test_it_reads_the_process_cwd(self):
        self.assertIn('/proc/$main_pid/cwd', SOURCE,
                      '没查进程 cwd —— 只信符号链接会漏掉「进程还在旧目录」')

    def test_it_compares_them(self):
        self.assertIn('[ "$proc_cwd" = "$target" ]', SOURCE)

    def test_mismatch_returns_nonzero(self):
        idx = SOURCE.find('[ "$proc_cwd" = "$target" ]')
        self.assertGreater(idx, 0)
        window = SOURCE[idx:idx + 180]
        self.assertIn('die', window, 'cwd 不一致时仍返回成功')

    def test_missing_pid_returns_nonzero(self):
        self.assertIn('主服务没有运行中的 MainPID', SOURCE)

    def test_runtime_release_identity_and_inference_are_checked(self):
        self.assertIn('/api/release', SOURCE)
        self.assertIn('/api/readyz', SOURCE)
        self.assertIn('/v1/models', SOURCE)


class TheReasonIsWrittenDownTests(unittest.TestCase):
    def test_it_says_why_cleanup_matters(self):
        self.assertIn('等于没有这个步骤', SOURCE,
                      '没写出「靠人记着就等于没有」这个理由')
        self.assertIn('ConditionPathExists', SOURCE,
                      '没解释门禁残留为什么会静默')


if __name__ == '__main__':
    unittest.main()
