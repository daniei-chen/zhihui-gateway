"""v1.2.98：归档线程——唯一会删数据的后台任务——此前完全不可见。

## 我在这件事上判断错过一次

2026-11-15 排查「磁盘一直涨」，我看到 `archive_runs` 表是空的，就断言
**归档从来没有跑过**。

**那是错的。** 归档在跑：

| 事实 | 值 |
| --- | --- |
| `request_logs` 保留期 | **90 天** |
| 库里最早的数据 | **41 天**（2026-10-05） |
| `archive cycle failed` 日志 | **0 条** |

没有到期行，所以从未产生归档记录 —— 完全正常。

**而我会误判，是因为这件事没有任何指标。** 只能靠 `archive_runs` 是否为空
去猜，而那个信号在「正常但没有到期数据」和「从没跑过」两种情况下**完全一样**。

## 本版做的事

### 一、指标

| 指标 | 回答 |
| --- | --- |
| `workbuddy_archive_worker_alive` | 线程还在不在 |
| `workbuddy_archive_enabled` | 是明确关掉还是故障 |
| `workbuddy_archive_last_cycle_age_seconds` | 上一轮完成距今多久 |
| `workbuddy_archive_pending_datasets` | 有几个数据集有到期行待归档 |
| `workbuddy_archive_last_cycle_failed` | 上一轮是否抛异常 |

**「上一轮距今多久」是这里最有用的一个**：归档周期 300 秒，即使没有数据也会
照跑。所以这个值很大意味着**线程卡死**，而不是「没事干」—— 这正好区分了
我误判的那两种情形。

### 二、告警 `archive_worker_stalled`

| 情形 | 是否告警 |
| --- | --- |
| 明确关掉了（`enabled=false`） | 否 —— 不是故障 |
| 线程不在 | **是** |
| 上一轮完成已超过 30 分钟 | **是** |
| 还没跑过第一轮（启动后 30 秒内） | 否 —— 给它时间 |
| 拿不到状态（导入期问题） | 否 —— 不制造噪音 |

阈值 30 分钟 = 6 个周期。一次慢周期不该报警，连续半小时没完成就该有人看。
"""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

from server import main, operational


class _Status:
    """构造一个 archiveworker.status() 的返回值。"""

    @staticmethod
    def make(**kw):
        base = {'enabled': True, 'worker_alive': True, 'age_seconds': 120,
                'ok': True, 'pending': {'request_logs': False}, 'running': False}
        base.update(kw)
        return base


def _run(status=None, *, raises=False):
    """跑告警，返回 archive_worker_stalled 的三态：None=没记录 / True / False。

    ## 为什么 patch 真模块的 status 而不是 sys.modules

    `from . import archiveworker` 在包内解析时**优先取包的属性**
    （`server.archiveworker`），而不是重新查 `sys.modules`。所以只替换
    `sys.modules` 那一项不起作用 —— 函数里取到的还是原来那个模块对象。

    直接 patch 那个模块的 `status` 才是可靠的。
    """
    from server import archiveworker as real
    if raises:
        patcher = mock.patch.object(real, 'status', side_effect=RuntimeError('boom'))
    else:
        patcher = mock.patch.object(
            real, 'status',
            return_value=status if status is not None else _Status.make())
    with patcher, mock.patch.object(operational, 'transition') as tr:
        operational._archive_worker_alert()
        for call in tr.call_args_list:
            if call.args and call.args[0] == 'archive_worker_stalled':
                return call.args[1]
    return None


class TheThreadMustBeVisibleTests(unittest.TestCase):
    def test_healthy_worker_is_quiet(self):
        self.assertFalse(_run())

    def test_dead_thread_raises(self):
        self.assertTrue(_run(_Status.make(worker_alive=False)))

    def test_stalled_thread_raises_after_thirty_minutes(self):
        self.assertTrue(_run(_Status.make(age_seconds=31*60)))

    def test_slow_cycle_just_under_threshold_is_quiet(self):
        self.assertFalse(_run(_Status.make(age_seconds=29*60)))

    def test_six_missed_cycles_is_the_line(self):
        """周期 300 秒，30 分钟 = 6 个周期。"""
        self.assertFalse(_run(_Status.make(age_seconds=6*300 - 1)))
        self.assertTrue(_run(_Status.make(age_seconds=6*300 + 1)))


class DisabledIsNotAFailureTests(unittest.TestCase):
    def test_explicitly_disabled_clears_the_alert(self):
        got = _run(_Status.make(enabled=False, worker_alive=False, age_seconds=999999))
        self.assertFalse(got, '明确关掉不是故障，不该报警')

    def test_first_cycle_not_yet_run_is_quiet(self):
        self.assertFalse(_run(_Status.make(age_seconds=None)))

    def test_boolean_age_is_not_treated_as_a_number(self):
        """`True` 是 int 子类；当成 1 秒会掩盖真正的卡死。"""
        self.assertFalse(_run(_Status.make(age_seconds=True)))


class NoNoiseOnImportProblemsTests(unittest.TestCase):
    def test_unreadable_status_does_not_raise(self):
        self.assertIsNone(_run(raises=True))


class TheMetricsExistTests(unittest.TestCase):
    SOURCE = inspect.getsource(main.prometheus_metrics)

    def test_all_five_metrics_are_emitted(self):
        for name in ('workbuddy_archive_worker_alive', 'workbuddy_archive_enabled',
                     'workbuddy_archive_last_cycle_age_seconds',
                     'workbuddy_archive_pending_datasets',
                     'workbuddy_archive_last_cycle_failed'):
            self.assertIn(name, self.SOURCE, name)

    def test_the_age_metric_explains_why_it_matters(self):
        start = self.SOURCE.index('workbuddy_archive_last_cycle_age_seconds')
        window = self.SOURCE[start:start + 500]
        self.assertIn('stuck', window,
                      '要写明「值很大是卡死而不是空闲」，否则后来人会误读')

    def test_the_worker_alive_metric_says_silence_is_not_benign(self):
        start = self.SOURCE.index('workbuddy_archive_worker_alive')
        window = self.SOURCE[start:start + 400]
        self.assertIn('deletes rows', window)


class TheMisjudgementIsRecordedTests(unittest.TestCase):
    def test_alert_docstring_records_the_wrong_conclusion(self):
        source = inspect.getsource(operational._archive_worker_alert)
        self.assertIn('实际它在跑', source)
        self.assertIn('90 天', source)

    def test_metric_comment_records_it_too(self):
        source = inspect.getsource(main.prometheus_metrics)
        start = source.index('workbuddy_archive_worker_alive')
        window = source[max(0, start - 1400):start]
        self.assertIn('判断错过一次', window)


if __name__ == '__main__':
    unittest.main()
