"""v1.2.148：审计链完整性进周期校验，以及我写这一版时踩的坑。

## 补的是什么

`ledger.verify_chain()` 早就存在，但**生产里从来没有被调用过**。

它只被 `retention.verify_admin_chain()` 用到，而那条路径挂在归档流程上 ——
归档按 90 天阈值触发，而库里最老的数据还没到那个年龄。

**于是「审计链有没有被篡改」从上线到现在一次都没验证过。**

2026-11-15 手工验了一次：25993 行、创世 1 个、断链 0，**链是好的**。
但那是**有人去查**才发现的，不是系统自己说的。

实测全表 25993 行、断链检测 0.02 秒 —— 放进 300 秒的归档周期里可以忽略。

## 写的时候踩的坑

我用 `def _loop():` 当替换锚点去插新函数，结果**把那个函数头删掉了**，
循环体被吞进 `_check_audit_chain` 内部。

**`ast.parse` 依然通过** —— 因为缩进合法，循环体合法地成了另一个函数的一部分。
**语法检查完全抓不到这种错。**

只有数了一遍模块级的函数清单才发现 `_loop` 不见了。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| `_loop` 在模块级存在 | 循环体被别的函数吞掉，归档线程立刻退出，静默不再归档 |
| `_check_audit_chain` 在模块级存在 | 同上 |
| 链校验挂在周期里 | 又回到「从来没验证过」 |
| 「链断了」与「没查成」分开 | 磁盘故障被误读成历史被篡改 |
| 校验失败不拖垮归档 | 一次读库失败让归档整体停摆 |
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / 'archiveworker.py').read_text(
    encoding='utf-8')
TREE = ast.parse(SOURCE)
MODULE_FUNCTIONS = {n.name for n in TREE.body if isinstance(n, ast.FunctionDef)}


class TheLoopSurvivedTests(unittest.TestCase):
    """我删过一次函数头，语法检查一声不吭。"""

    def test_loop_exists_at_module_level(self):
        self.assertIn('_loop', MODULE_FUNCTIONS,
                      '_loop 不在模块级 —— 循环体被别的函数吞了，归档线程会立刻退出')

    def test_check_audit_chain_exists_at_module_level(self):
        self.assertIn('_check_audit_chain', MODULE_FUNCTIONS)

    def test_loop_still_contains_the_run_once_call(self):
        loop = next(n for n in TREE.body
                    if isinstance(n, ast.FunctionDef) and n.name == '_loop')
        body_src = ast.get_source_segment(SOURCE, loop) or ''
        self.assertIn('run_once()', body_src, '_loop 里没有 run_once，归档不会执行')

    def test_loop_still_reschedules(self):
        loop = next(n for n in TREE.body
                    if isinstance(n, ast.FunctionDef) and n.name == '_loop')
        body_src = ast.get_source_segment(SOURCE, loop) or ''
        self.assertIn('ARCHIVE_INTERVAL_SEC', body_src, '_loop 不再重排下一次')

    def test_check_audit_chain_does_not_contain_the_loop(self):
        fn = next(n for n in TREE.body
                  if isinstance(n, ast.FunctionDef) and n.name == '_check_audit_chain')
        body_src = ast.get_source_segment(SOURCE, fn) or ''
        self.assertNotIn('while not _stop.is_set()', body_src,
                         '循环体又跑进 _check_audit_chain 里了')


class TheChainCheckIsWiredTests(unittest.TestCase):
    def test_it_is_called_from_the_cycle(self):
        loop = next(n for n in TREE.body
                    if isinstance(n, ast.FunctionDef) and n.name == '_loop')
        body_src = ast.get_source_segment(SOURCE, loop) or ''
        self.assertIn('_check_audit_chain()', body_src,
                      '链校验没挂在归档周期上 —— 又回到从来没验证过')

    def test_it_uses_the_existing_verifier(self):
        idx = SOURCE.find('def _check_audit_chain')
        window = SOURCE[idx:idx + 1600]
        self.assertIn('ledger.verify_chain()', window,
                      '没复用既有实现，等于另写一套口径')


class ItSeparatesTwoKindsOfFailureTests(unittest.TestCase):
    """「链断了」和「没查成」不是一回事。"""

    def test_integrity_and_error_are_different_fields(self):
        self.assertIn("'chain_intact'", SOURCE)
        self.assertIn("'chain_error'", SOURCE)

    def test_broken_chain_is_logged_as_error(self):
        idx = SOURCE.find('def _check_audit_chain')
        window = SOURCE[idx:idx + 1600]
        self.assertIn('chain is broken', window)

    def test_check_failure_does_not_raise(self):
        """读库失败不该让归档线程死掉。"""
        idx = SOURCE.find('def _check_audit_chain')
        window = SOURCE[idx:idx + 1600]
        self.assertIn('except (Exception, SystemExit)', window,
                      '校验失败会抛出去，拖垮整个归档周期')


class TheReasonIsWrittenDownTests(unittest.TestCase):
    def test_the_comment_records_that_it_was_never_called(self):
        idx = SOURCE.find('def _check_audit_chain')
        window = SOURCE[idx:idx + 1200]
        self.assertIn('从来没有被调用过', window,
                      '没写出「它一直没被调用」这个事实，后来人不会当回事')
        self.assertIn('25993', window, '没写出实测行数')


MAIN_SOURCE = (Path(__file__).resolve().parents[1] / 'main.py').read_text(
    encoding='utf-8')


class TheMetricHasThreeStatesTests(unittest.TestCase):
    """没跑过、链是好的、链断了，三者必须能分开。

    把「没跑过」也报成 0 会和「真的断了」混在一起 —— 而那是**最糟的一种歧义**：
    一个从没验证过的系统，看起来和刚验过且完好的系统一模一样。
    """

    def _block(self) -> str:
        start = MAIN_SOURCE.find("_chain_intact = _arch_status.get('chain_intact')")
        self.assertGreater(start, 0, '没找到链指标那段')
        return MAIN_SOURCE[start:start + 1800]

    def test_metric_is_emitted(self):
        self.assertIn('workbuddy_audit_chain_intact', self._block())

    def test_never_checked_reports_minus_one(self):
        block = self._block()
        self.assertIn('else -1', block,
                      '首次校验之前必须报 -1，不能报 0')

    def test_broken_reports_zero(self):
        block = self._block()
        self.assertIn('0 if _chain_intact is False', block)

    def test_intact_reports_one(self):
        block = self._block()
        self.assertIn('1 if _chain_intact is True', block)

    def test_check_failure_is_a_separate_metric(self):
        """「读不到库」和「链断了」不是一个告警。"""
        block = self._block()
        self.assertIn('workbuddy_audit_chain_check_failed', block,
                      '校验本身失败没有单独的信号，会被误读成链断了')

    def test_events_count_is_exposed(self):
        block = self._block()
        self.assertIn('workbuddy_audit_chain_events', block,
                      '没有行数就无法判断「校验的是不是全部历史」')


if __name__ == '__main__':
    unittest.main()
