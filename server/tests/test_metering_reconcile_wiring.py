"""账本对账接入后台循环（P1）。

`metering.reconcile` 早就存在但**没有任何调度** —— 一个"能跑却没人跑"的只读对账，
等于没有对账。这几个用例守的是接线本身：

* 会按配置的窗口大小真去扫；
* 只有**必须为零**的分类才会升级成警告，天然非零的分类（容量拒绝等）只记信息；
* 窗口设为 0 时彻底关掉；
* 扫描抛异常不吞，由 `_monitor_tick` 那一层的 try 兜住。

分类名取自 `metering.CATEGORY_DESCRIPTIONS` 的真实键。这一点不能想当然：
第一版测试里我用了自己编的分类名（`log_without_usage` 之类），代码与测试一起错、
照样全绿 —— 真实库上跑一次才发现。
"""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

from server import metering, operational


def _report(findings: dict | None = None, scanned: int = 42) -> dict:
    """构造与 `metering.reconcile` 返回结构一致的报告（真实分类名）。"""
    buckets = {name: {'count': 0, 'samples': [], 'description': ''}
               for name in metering.CATEGORY_DESCRIPTIONS}
    for name, count in (findings or {}).items():
        buckets[name] = {'count': count, 'samples': [{'request_id': 'r1'}], 'description': ''}
    return {'scanned_logs': scanned, 'findings': buckets}


class MeteringReconcilePassTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(operational._METERING_LAST.clear)

    def test_scans_with_configured_limit_and_stays_silent_when_clean(self) -> None:
        with mock.patch.object(operational, '_reconcile_env_int', return_value=250), \
                mock.patch('server.metering.reconcile', return_value=_report()) as reconcile, \
                mock.patch.object(operational.log, 'warning') as warn, \
                mock.patch.object(operational.log, 'info') as info:
            summary = operational.metering_reconcile_pass(1000)
        self.assertEqual(reconcile.call_args.kwargs['limit'], 250)
        self.assertEqual(summary['scanned'], 42)
        self.assertEqual(summary['findings'], {})
        warn.assert_not_called()
        info.assert_not_called()
        self.assertEqual(operational._METERING_LAST['at'], 1000)

    def test_ledger_mismatch_escalates_to_warning(self) -> None:
        with mock.patch('server.metering.reconcile',
                        return_value=_report({'token_mismatch': 2})), \
                mock.patch.object(operational.log, 'warning') as warn:
            summary = operational.metering_reconcile_pass(2000)
        self.assertEqual(summary['strict'], {'token_mismatch': 2})
        self.assertTrue(warn.called)
        self.assertIn('token_mismatch', str(warn.call_args))

    def test_expected_categories_do_not_raise_an_alarm(self) -> None:
        # 生产库实测：500 条窗口里 186 条 outcome_without_log。这属于预期内，
        # 只要不是"必须为零"的那几类，就不能升级成 warning —— 否则每 15 分钟一条。
        report = _report({'outcome_without_log': 186, 'log_without_request_id': 12})
        with mock.patch('server.metering.reconcile', return_value=report), \
                mock.patch.object(operational.log, 'warning') as warn, \
                mock.patch.object(operational.log, 'info') as info:
            summary = operational.metering_reconcile_pass(3000)
        self.assertEqual(summary['strict'], {})
        warn.assert_not_called()
        self.assertTrue(info.called)
        # 但数字仍然要留下来，便于看趋势
        self.assertEqual(summary['findings']['outcome_without_log'], 186)

    def test_strict_categories_are_a_subset_of_real_categories(self) -> None:
        unknown = operational.METERING_STRICT_CATEGORIES - set(metering.CATEGORY_DESCRIPTIONS)
        self.assertEqual(unknown, frozenset(),
                         '严格分类里出现了 metering 不认识的分类名，说明写错了')

    def test_zero_limit_disables_the_pass_without_touching_the_db(self) -> None:
        with mock.patch('server.metering.reconcile') as reconcile:
            summary = operational.metering_reconcile_pass(4000, limit=0)
        reconcile.assert_not_called()
        self.assertTrue(summary['skipped'])

    def test_scan_failure_propagates_for_the_caller_to_contain(self) -> None:
        with mock.patch('server.metering.reconcile', side_effect=RuntimeError('db busy')):
            with self.assertRaises(RuntimeError):
                operational.metering_reconcile_pass(5000)

    def test_monitor_tick_wires_the_pass_inside_its_own_try(self) -> None:
        # 这里刻意用源码断言而不是"跑一遍 tick"：`_monitor_tick` 会碰宿主机命令、
        # 归档线程、账本重放等一堆副作用，为了跑通它去 mock 十几处，测试会变成
        # "别人加一行日志就假红"的负担。而这一步要守的其实只有两件事：
        # 接线接上了、并且异常不会掀翻整轮（有 try + log.exception）。
        source = inspect.getsource(operational._monitor_tick)
        self.assertIn('metering_reconcile_pass(now)', source)
        self.assertIn("log.exception('metering reconcile pass failed')", source)
        # 顺序上必须排在预算结清之后：对账要读到结清之后的状态。
        self.assertLess(source.index('incremental budget reconcile pass failed'),
                        source.index('metering_reconcile_pass(now)'))


if __name__ == '__main__':
    unittest.main()
