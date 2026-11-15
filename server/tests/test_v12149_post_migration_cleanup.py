"""v1.2.149：把迁移收尾从「靠人记着」变成脚本。

## 补的是什么

每次 `migration_entry.py run` 都做两件「停掉」的事，**没有对应的恢复**：

| 动作 | 后果（如果没人恢复） |
| --- | --- |
| 写 `<unit>.start-blocked` | drop-in 是 `ConditionPathExists=!<marker>`，**unit 被 stackd 静默 skip**，`Restart=` 不生效，日志只有一句 skipped |
| `systemctl stop wb-budget-autoreconcile.timer` | 每日预算对账只剩「每次部署时跑一次」，不部署就停 |

这两件事以前靠我每次在命令行手动做。**记不住就是静默失效**：
helper 起不来没人知道，对账停跑也没人知道。

## 脚本的安全边界

**只在事务 `phase=complete` 之后才动 marker。**

marker 的语义是「迁移进行中，别让旧实现起来」。在未完成的事务上清它，
等于在迁移过程中打开门禁 —— 那正是它要拦的场景。

**这条判断是脚本里唯一不可妥协的部分**，测试专门钉它。

## 为什么清 marker 之后要 daemon-reload

`ConditionPathExists` 是 systemd 在启动时求值的，但它有缓存。
marker 删了不 reload，unit 仍可能维持 skipped 状态 ——
**清了等于没清，而且更难查**（文件不在了，服务还是起不来）。

## 为什么恢复 timer 不会引发重任务

timer 是 `OnCalendar` + `Persistent=yes`，错过的时间点会补跑。
但 migration 期间本来就会手动跑一次同一个 oneshot service，
所以补跑不构成额外的重活。

2026-11-15 实测：`systemctl start` 之后 timer 变 active、
`NextElapse` 指向次日 06:11，**service 没有立刻触发**。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 未 complete 的事务不清 marker | 迁移途中门禁被打开，旧实现可能与新的并存 |
| 无事务名时不动作 | 误调用把正常运行中的门禁清掉 |
| 清 marker 后 daemon-reload | 文件删了但 unit 仍 skipped |
| 恢复 budget timer | 每日对账静默停跑 |
| 恒 exit 0 | 收尾失败把一次成功的部署判成失败 |
"""
from __future__ import annotations

import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'post-migration-cleanup.sh'
SOURCE = SCRIPT.read_text(encoding='utf-8')


class ItRefusesToOpenTheGateEarlyTests(unittest.TestCase):
    """最关键的一条：未完成的事务绝不清 marker。"""

    def test_it_requires_a_transaction_id(self):
        self.assertIn('[ -z "$TXN" ]', SOURCE,
                      '没检查事务名，误调用会把运行中的门禁清掉')

    def test_it_requires_phase_complete(self):
        self.assertIn('"phase":"complete"', SOURCE,
                      '没检查 complete —— 迁移途中清 marker 会打开门禁')

    def test_both_guards_exit_before_touching_markers(self):
        """两个检查都必须出现在删除动作之前。"""
        first_rm = SOURCE.find('rm -f "$marker"')
        self.assertGreater(first_rm, 0)
        complete_guard = SOURCE.find('"phase":"complete"')
        txn_guard = SOURCE.find('[ -z "$TXN" ]')
        self.assertLess(txn_guard, first_rm)
        self.assertLess(complete_guard, first_rm)


class ItActuallyRestoresThingsTests(unittest.TestCase):
    def test_it_clears_start_blocked_markers(self):
        self.assertIn('start-blocked', SOURCE)
        self.assertIn('rm -f "$marker"', SOURCE)

    def test_it_reloads_systemd_after_clearing(self):
        idx = SOURCE.find('rm -f "$marker"')
        window = SOURCE[idx:idx + 700]
        self.assertIn('daemon-reload', window,
                      '删了 marker 但不 reload，unit 可能仍维持 skipped')

    def test_it_restarts_the_budget_timer(self):
        self.assertIn('wb-budget-autoreconcile.timer', SOURCE,
                      '不恢复 timer，每日对账就只剩部署时才跑')

    def test_it_is_idempotent_about_the_timer(self):
        self.assertIn('is-active --quiet', SOURCE,
                      '没先判断是否已在运行')

    def test_it_reports_the_next_elapse(self):
        """只写「已启动」不够 —— 下次触发时间才是「真的排上了」的证据。"""
        self.assertIn('NextElapseUSecRealtime', SOURCE)


class ItNeverFailsTheDeploymentTests(unittest.TestCase):
    def test_it_exits_zero(self):
        self.assertRegex(SOURCE, r'(?m)^exit 0\s*$')

    def test_it_does_not_set_e(self):
        """`set -e` 会让某一步失败直接终止，收尾就变成「部署失败」。

        注意：这个脚本开头有很长的注释说明，所以不能只看前几百字符 ——
        第一版测试就是这么写错的，脚本本身没问题。
        """
        self.assertIn('set -uo pipefail', SOURCE)
        self.assertNotIn('set -euo', SOURCE)
        self.assertNotIn('set -eu', SOURCE)

    def test_restore_failures_are_reported_not_raised(self):
        self.assertIn('不阻断', SOURCE,
                      '恢复失败没有说明「不阻断」的意图')


class TheReasonIsWrittenDownTests(unittest.TestCase):
    def test_it_names_both_things_that_get_stopped(self):
        self.assertIn('ConditionPathExists', SOURCE,
                      '没写出 marker 为什么会让 unit 静默 skip')
        self.assertIn('systemctl stop wb-budget-autoreconcile.timer', SOURCE,
                      '没写出 timer 是被 migration 停的')

    def test_it_records_the_measured_behaviour(self):
        self.assertIn('实测', SOURCE,
                      '没写出实测结论 —— 后来人不敢用这个脚本')


if __name__ == '__main__':
    unittest.main()
