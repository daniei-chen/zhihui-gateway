"""账号余额告警必须真的发出去 —— 它原来只落在页面里。

## 背景

系统里有两套告警池：

| 池子 | 出口 | 谁在用 |
| --- | --- | --- |
| `operational_alerts` | 数据库表 + 仅回环的 `/health/alerts` | 页面、巡检脚本 |
| `steward_deliveries` | **邮件**（带去重窗口、AI 研判） | 主动打扰运维 |

2026-11-25 查证时 `platform_workbuddy` 会发邮件，但 `workbuddy_low_accounts`
从 15:50 挂了一个多小时，**一封都没发**。

原因不是遗漏了「加一条规则」这么简单，而是**两个信号的语义不同**：

```python
# 原来唯一的平台规则
Signal('platform_' + name, platform['state'] != 'up', ...)
```

它看的是**只读探针状态**。探针还是 `up` 的时候，某个账号可能只剩 809 分 ——
而那个账号会在一次长上下文请求里被打空、被摘出池子，之后请求被拒。

## 判据为什么是「单号余额」而不是「池子总量」

`credits_remain` 是总和。池子总计 15 万分时，某个号剩 600 分，总量看起来
依然健康。`credit_watch.workbuddy_alerts` 的注释专门解释过这件事。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 单号低于阈值会被计数 | 只报总量，个别账号悄悄打空 |
| 阈值来自 `credit_watch`，不是另写一个 | 两处各写一份，改一处忘另一处 → 页面告警、邮件说没事 |
| 采不到数据报 `None` 而不是 `False` | 「未知」被当成「正常」，正是 2026-10-20 踩过的坑 |
| 规则名带 `_credits` 后缀 | 和 `platform_<name>`（探针状态）撞名会互相覆盖 |
"""
from __future__ import annotations

import unittest

from server import credit_watch, steward_rules, steward_snapshot


def _body(credits):
    return {'accounts': [{'credits': value} for value in credits],
            'scope': 'engine_account_list'}


class ProjectionTests(unittest.TestCase):
    def test_counts_accounts_below_threshold(self):
        # 890 和 809 低于 1000；5000 和 20000 不低于
        pool = steward_snapshot.pool_projection(_body([20000, 5000, 890, 809]), 'workbuddy')
        self.assertEqual(pool['low_credit_accounts'], 2)

    def test_total_can_look_healthy_while_individual_is_empty(self):
        """这正是只看总量的盲区：4 个号合计 2.6 万，但其中 2 个快空了。"""
        pool = steward_snapshot.pool_projection(_body([20000, 5000, 890, 809]), 'workbuddy')
        self.assertGreater(pool['credits_remain'], 26000)
        self.assertEqual(pool['low_credit_accounts'], 2,
                         '总量健康时，快空的单号仍要能被数出来')

    def test_threshold_matches_credit_watch(self):
        pool = steward_snapshot.pool_projection(_body([500]), 'workbuddy')
        self.assertEqual(pool['low_credit_threshold'], credit_watch.WB_LOW_ACCOUNT,
                         '两处阈值必须同源，否则页面告警而邮件不告警')

    def test_missing_credits_yield_none_not_zero(self):
        """采集不到余额时报 None（未知），不能报 0（等于「没有低分账号」）。"""
        pool = steward_snapshot.pool_projection({'accounts': [{}]}, 'workbuddy')
        self.assertIsNone(pool['low_credit_accounts'])


class RuleTests(unittest.TestCase):
    def _snapshot(self, pool):
        return {'resources': {'disk': {'used_percent': 10, 'free_bytes': 50 * 1024**3},
                              'memory': {'used_percent': 10}},
                'traffic': {'requests': 0, 'failed': 0, 'failure_percent': None,
                            'p95_total_latency_ms': None, 'latency_samples': 0},
                'budget': {'unknown_count': 0},
                'ledger': {'state': 'ok', 'writable': True, 'pending': 0, 'dead': 0},
                'backup': {'status': 'ok'},
                'certificate': {'status': 'ok', 'days_left': 90},
                'platforms': [{'name': 'workbuddy', 'state': 'up', 'pool': pool}]}

    def _rule(self, pool):
        policy = __import__('server.steward_config', fromlist=['Policy']).Policy()
        signals = steward_rules.evaluate(self._snapshot(pool), policy)
        found = [s for s in signals if s.rule == 'platform_workbuddy_credits']
        self.assertEqual(len(found), 1, '缺少 platform_workbuddy_credits 规则')
        return found[0]

    def test_fires_when_an_account_is_low(self):
        signal = self._rule({'total': 4, 'low_credit_accounts': 2,
                             'low_credit_threshold': 1000, 'credits_coverage': 4})
        self.assertTrue(signal.active, f'两个账号余额偏低却没触发：{signal.detail}')
        self.assertIn('2', signal.detail)

    def test_quiet_when_all_accounts_healthy(self):
        signal = self._rule({'total': 4, 'low_credit_accounts': 0,
                             'low_credit_threshold': 1000, 'credits_coverage': 4})
        self.assertFalse(signal.active, '全部健康时不该发邮件')

    def test_unknown_when_pool_missing(self):
        signal = self._rule(None)
        self.assertIsNone(signal.active,
                          '采不到余额必须报「未知」，不能报成「正常」')

    def test_does_not_collide_with_probe_rule(self):
        """探针规则叫 platform_workbuddy，余额规则叫 platform_workbuddy_credits。"""
        policy = __import__('server.steward_config', fromlist=['Policy']).Policy()
        signals = steward_rules.evaluate(
            self._snapshot({'total': 1, 'low_credit_accounts': 1,
                            'low_credit_threshold': 1000, 'credits_coverage': 1}), policy)
        rules = {s.rule for s in signals}
        self.assertIn('platform_workbuddy', rules)
        self.assertIn('platform_workbuddy_credits', rules)


if __name__ == '__main__':
    unittest.main()
