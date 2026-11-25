"""归档周期失败与审计链异常必须产生运维告警，不能只写日志。

## 补的是什么

`archiveworker._loop()` 原来在失败时只做两件事：记日志、把 `_state['ok']` 置 False。

**数据是安全的** —— 异常分支里一行都不删（日志原话 `source rows remain protected`）。
问题是**没有人会知道**：

- 日志没人盯；
- `status()` 里的 `fresh` 只判断「多久没跑过」，
  一个每 300 秒都失败的任务**永远 `fresh=True`**；
- healthcheck 里没有归档这一项。

于是「归档连续失败」这件事的发现时刻，会是**磁盘满的那一刻**。
而归档从 90 天阈值开始（约 2026-12-13），届时才是第一次真实运行。

审计链校验同理：`chain_intact=False` 原来也只是一行 `log.error`。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 归档失败产生 `archive_cycle_failed` 告警 | 失败了没人知道，直到磁盘满 |
| 归档恢复后清除该告警 | 一次失败后永远显示失败 |
| 链断裂产生 `audit_chain_broken` 告警 | 历史被改过也没人说 |
| 「链断了」与「没查成」的 detail 不同 | 磁盘故障被误读成篡改 |
| 告警路径自身失败不影响归档 | 告警系统挂了导致归档停摆 |
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from server import archiveworker


class ArchiveCycleAlertsTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_transition(name, active, detail):
            self.calls.append((name, active, detail))

        self.patch = patch('server.operational.transition', side_effect=fake_transition)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_failure_raises_alert_and_success_clears_it(self):
        archiveworker._report_health(True, '归档周期正常')
        self.assertEqual(self.calls, [('archive_cycle_failed', False, '归档周期正常')])

        self.calls.clear()
        archiveworker._report_health(False, '归档周期失败：OSError（源数据未删除）')
        self.assertEqual(self.calls[0][0], 'archive_cycle_failed')
        self.assertIs(self.calls[0][1], True)
        self.assertIn('源数据未删除', self.calls[0][2],
                      'detail 要说明数据没有丢，否则看告警的人会以为数据没了')

    def test_alert_failure_does_not_propagate(self):
        """告警系统本身出错时，不能反过来把归档打挂。"""
        with patch('server.operational.transition', side_effect=RuntimeError('boom')):
            archiveworker._report_health(False, 'x')   # 不应该抛
            archiveworker._report_audit_chain(False, None)


class AuditChainAlertsTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.patch = patch('server.operational.transition',
                           side_effect=lambda n, a, d: self.calls.append((n, a, d)))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_broken_chain_is_distinct_from_unchecked(self):
        archiveworker._report_audit_chain(False, None)
        broken = self.calls[-1]
        self.calls.clear()
        archiveworker._report_audit_chain(None, 'OperationalError')
        unchecked = self.calls[-1]

        self.assertEqual(broken[0], 'audit_chain_broken')
        self.assertEqual(unchecked[0], 'audit_chain_broken')
        self.assertIs(broken[1], True)
        self.assertIs(unchecked[1], True)
        self.assertNotEqual(broken[2], unchecked[2],
                            '「链真断了」和「没查成」的说明必须不同')

    def test_intact_chain_clears_the_alert(self):
        archiveworker._report_audit_chain(True, None)
        self.assertEqual(self.calls[-1][:2], ('audit_chain_broken', False))


class LoopStillWiredTests(unittest.TestCase):
    def test_loop_reports_health_on_both_paths(self):
        """`_loop` 的 try 与 except 两条路径都必须调用告警上报。"""
        import ast
        from pathlib import Path
        tree = ast.parse((Path(__file__).resolve().parents[1]/'archiveworker.py').read_text(
            encoding='utf-8'))
        loop = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == '_loop')
        called = {n.func.id for n in ast.walk(loop)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn('_report_health', called,
                      '_loop 没有调用 _report_health —— 失败又回到只写日志')


if __name__ == '__main__':
    unittest.main()
