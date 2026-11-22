"""v1.2.77：账号池冗余度此前完全没有监控。

## 怎么发现的

对比三个平台的账号数：workbuddy **48**、qoder **3**、trae **1**。

**trae 只有一个账号**，而它承载 26 个模型（含全部豆包）。那个账号一旦被限流或
过期，整个平台就没了 —— 而**除了用户报错之外没有任何信号**。

这不是代码缺陷（账号是运维资源），但「只有一个」这件事应该被告知：
**冗余度是运行风险，不是使用量。**

## 判据

| 账号数 | 状态 |
| --- | --- |
| 0 | **不可用** —— 该平台此刻必然失败 |
| 1 | **单点** —— 没有冗余，一次限流就全断 |
| ≥ 2 | 正常 |

只报 0 与 1：两个账号也不算宽裕，但那是「要不要扩容」的判断，不是「现在就有风险」
的事实。**告警只报事实。**

## 读不到的平台跳过

跳过意味着「这条告警这次不评估」，而报 0 意味着「这个平台没有账号了」——
后者是严重误报。所以三处读取（两个目录 glob、一个 SQLite 查询）全部按
「失败就跳过」处理，且 trae 的库用只读模式打开（不能与引擎抢锁）。
"""
from __future__ import annotations

import inspect
import unittest

from server import operational


class AccountPoolAlertTests(unittest.TestCase):
    def _by_name(self, counts):
        return {name: (active, detail)
                for name, active, detail in operational.account_pool_alerts(counts)}

    def test_a_single_account_fires(self):
        """就是 trae 的实际情况。"""
        alerts = self._by_name({'trae': 1})
        self.assertTrue(alerts['account_pool_trae'][0])
        # 文案 2026-11-23 改成中文，断言随之改 —— 检查的是「说了没有冗余」，
        # 不是「用了哪个词」。
        self.assertIn('没有冗余', alerts['account_pool_trae'][1])

    def test_zero_accounts_fires_more_loudly(self):
        alerts = self._by_name({'trae': 0})
        self.assertTrue(alerts['account_pool_trae'][0])
        self.assertIn('没有可用账号', alerts['account_pool_trae'][1])

    def test_two_or_more_does_not_fire(self):
        for count in (2, 3, 48):
            alerts = self._by_name({'workbuddy': count})
            self.assertFalse(alerts['account_pool_workbuddy'][0], f'{count} 个不该告警')

    def test_each_platform_gets_its_own_alert_name(self):
        """共用一个名字会让「trae 从 1 变 0」不产生新通报。"""
        alerts = self._by_name({'workbuddy': 48, 'qoder': 3, 'trae': 1})
        self.assertEqual(len(alerts), 3)
        self.assertEqual(set(alerts), {'account_pool_workbuddy',
                                       'account_pool_qoder',
                                       'account_pool_trae'})

    def test_missing_platform_is_skipped_not_zeroed(self):
        """读不到 ≠ 没有账号。后者是严重误报。"""
        self.assertEqual(operational.account_pool_alerts({}), [])

    def test_none_count_is_skipped(self):
        alerts = self._by_name({'trae': None})
        self.assertEqual(alerts, {})

    def test_detail_carries_the_count_for_healthy_platforms(self):
        alerts = self._by_name({'workbuddy': 48})
        self.assertIn('48', alerts['account_pool_workbuddy'][1])

    def test_details_are_valid_utf8(self):
        """文案要能正常编码成 UTF-8。

        这条原来断言 `detail.encode('ascii')`，理由是「日志里出现非 ASCII 时
        某些采集链路会转义成乱码」。2026-11-23 把 account_pool 告警改成中文后
        重新评估：

        - 生产 journalctl 里同屏的中文告警（account_credits_trae、
          workbuddy_low_accounts）显示正常，没有乱码
        - notify 的两条通道都是 UTF-8（文件写入 encoding='utf-8'，
          HTTP body 用 ensure_ascii=False 编成 utf-8）
        - 系统其余告警本来就是中文，只有这五类要求 ASCII，约束本身不一致

        所以这里改成只要求「是合法 UTF-8 且非空」—— 保留「能安全写出去」的
        意图，去掉「必须是 ASCII」这条已经和实际不符的限制。

        另外四条同类断言（v1269 / v1271 / v1274 / v1283）本轮不动：它们对应的
        告警仍是英文，没有违反这条约束的理由。要统一中文应当逐个确认，而不是
        一次性放开。
        """
        for counts in ({'trae': 0}, {'trae': 1}, {'workbuddy': 48}):
            for _, _, detail in operational.account_pool_alerts(counts):
                self.assertTrue(detail, 'detail 不该为空')
                detail.encode('utf-8')

    def test_ordering_is_stable(self):
        """顺序稳定，日志才可比。"""
        names = [n for n, _, _ in operational.account_pool_alerts(
            {'trae': 1, 'qoder': 3, 'workbuddy': 48})]
        self.assertEqual(names, sorted(names))


class CollectorReadsAllThreeTests(unittest.TestCase):
    def test_it_never_raises_when_paths_are_missing(self):
        """开发机上这三个路径都不存在，不能因此炸掉健康评估。"""
        try:
            counts = operational.collect_account_counts()
        except Exception as exc:      # noqa: BLE001
            self.fail(f'收集账号数不该抛异常：{exc!r}')
        self.assertIsInstance(counts, dict)

    def test_trae_database_is_opened_read_only(self):
        """绝不能用读写模式碰生产库 —— 引擎自己在维护它。"""
        source = inspect.getsource(operational.collect_account_counts)
        self.assertIn('mode=ro', source,
                      'trae 的库必须以只读模式打开，否则可能和引擎抢锁')

    def test_failures_are_swallowed_per_platform(self):
        """单平台失败不该影响其它平台。"""
        source = inspect.getsource(operational.collect_account_counts)
        self.assertIn('except OSError', source)
        self.assertIn('except (sqlite3.Error, OSError, TypeError)', source)


class ItIsWiredIntoTheMonitorTests(unittest.TestCase):
    def test_monitor_calls_the_collector_and_the_rule(self):
        source = inspect.getsource(operational._monitor_tick)
        self.assertIn('collect_account_counts', source)
        self.assertIn('account_pool_alerts', source)

    def test_alerts_go_through_transition(self):
        source = inspect.getsource(operational._monitor_tick)
        start = source.index('account_pool_alerts')
        window = source[start:start + 200]
        self.assertIn('transition', window)


if __name__ == '__main__':
    unittest.main()
