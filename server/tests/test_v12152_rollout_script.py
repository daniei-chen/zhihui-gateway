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
| 收尾 | **不判发布失败**（新版本已在跑，收尾只清残留） |

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


class ItKeepsTheThreeFailuresDistinctTests(unittest.TestCase):
    def test_no_set_e(self):
        """`set -e` 会把三种失败压成同一个退出码。"""
        self.assertIn('set -uo pipefail', SOURCE)
        self.assertNotIn('set -euo', SOURCE)
        self.assertNotIn('set -eu', SOURCE)

    def test_cleanup_failure_does_not_fail_the_release(self):
        """新版本已经在跑，收尾失败不该把它判成失败。"""
        idx = SOURCE.find('cleanup_rc=$?')
        self.assertGreater(idx, 0)
        window = SOURCE[idx:idx + 500]
        self.assertIn('发布本身已完成', window,
                      '没说明收尾失败不影响发布结论')
        self.assertNotIn('die', window,
                         '收尾失败调用了 die —— 会把成功发布判成失败')

    def test_prepare_failure_stops(self):
        idx = SOURCE.find('prep_rc=$?')
        self.assertGreater(idx, 0)
        self.assertIn('die', SOURCE[idx:idx + 300])

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
        self.assertIn('360', SOURCE, '等待没有上限')

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


class TheReasonIsWrittenDownTests(unittest.TestCase):
    def test_it_says_why_cleanup_matters(self):
        self.assertIn('等于没有这个步骤', SOURCE,
                      '没写出「靠人记着就等于没有」这个理由')
        self.assertIn('ConditionPathExists', SOURCE,
                      '没解释门禁残留为什么会静默')


if __name__ == '__main__':
    unittest.main()
