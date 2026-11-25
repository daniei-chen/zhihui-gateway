"""日配额接近上限要提前告警，而不是等用户被拒绝。

## 为什么

配额是**强制**的：`budget.py` 里超了直接 `BudgetRejected`，文案是
「今日积分额度不足，次日按服务器时区重置」。

但在此之前**没有任何信号**。用户看到的第一条信息就是「额度不足」，
而那时已经调不通了。

这一点是和同类产品对比出来的（2026-11-25 竞品调研）：

| 产品 | 对应能力 |
| --- | --- |
| LiteLLM | budget alert |
| Portkey | budget limits |
| New API | 额度预警 |

它们都把「快用完了」当成独立能力。我们缺。

## 单位坑

`daily_credit_quota` 是**积分**（200.0），`usage_daily.credit_units` 是
**百万分之一积分**（5,410,000 = 5.41 积分）。`ledger.SCALE = 1_000_000`。

直接拿两者比会差一百万倍 —— 要么永远不告警，要么每秒都告警。
本测试专门盯这一条。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 用了 ≥80% 就告警 | 用户被拒绝时仍无征兆 |
| 用了 <80% 不告警 | 告警噪声，运维会开始忽略它 |
| 单位换算正确 | 差一百万倍，告警永远不触发 |
| 查询失败不抛异常 | 一条 SQL 出错会让整个健康评估停摆（2026-11-15 踩过） |
| 没配额的密钥不参与 | 每个密钥都报一条，等于没告警 |
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from server import credit_watch


def _row(key_id=1, name='测试密钥', credit_quota=0.0, token_quota=0,
         used_units=0, used_tokens=0):
    return {'id': key_id, 'name': name, 'credit_quota': credit_quota,
            'token_quota': token_quota, 'used_units': used_units,
            'used_tokens': used_tokens}


class CreditQuotaTests(unittest.TestCase):
    def test_warns_when_credit_usage_crosses_threshold(self):
        # 200 积分配额，已用 180 → 90%
        with patch.object(credit_watch.db, 'query', return_value=[
                _row(credit_quota=200.0, used_units=180_000_000)]):
            result = credit_watch.quota_alerts()
        name, active, detail = result[0]
        self.assertEqual(name, 'key_quota_near_limit')
        self.assertTrue(active, f'用了 90% 却没告警：{detail}')
        self.assertIn('90.0%', detail)

    def test_stays_quiet_below_threshold(self):
        # 200 积分配额，已用 100 → 50%
        with patch.object(credit_watch.db, 'query', return_value=[
                _row(credit_quota=200.0, used_units=100_000_000)]):
            _, active, detail = credit_watch.quota_alerts()[0]
        self.assertFalse(active, f'才用 50% 就告警，这是噪声：{detail}')

    def test_unit_conversion_is_not_off_by_a_million(self):
        """配额是积分，已用是百万分之一积分。比错就差一百万倍。"""
        # 恰好 80%（阈值线上）：200 积分 × 0.8 = 160 积分 = 160_000_000 units
        with patch.object(credit_watch.db, 'query', return_value=[
                _row(credit_quota=200.0, used_units=160_000_000)]):
            _, active, _ = credit_watch.quota_alerts()[0]
        self.assertTrue(active, '恰好 80% 应该触发（>= 阈值）')

        # 差一点点（79.99%）
        with patch.object(credit_watch.db, 'query', return_value=[
                _row(credit_quota=200.0, used_units=159_980_000)]):
            _, active, _ = credit_watch.quota_alerts()[0]
        self.assertFalse(active, '差一点点就触发说明比较的是错的量纲')


class TokenQuotaTests(unittest.TestCase):
    def test_warns_when_token_usage_crosses_threshold(self):
        with patch.object(credit_watch.db, 'query', return_value=[
                _row(token_quota=20_000_000, used_tokens=17_000_000)]):
            _, active, detail = credit_watch.quota_alerts()[0]
        self.assertTrue(active, f'Token 用了 85% 却没告警：{detail}')
        self.assertIn('85.0%', detail)


class NoQuotaTests(unittest.TestCase):
    def test_keys_without_quota_are_not_reported(self):
        """SQL 已经过滤掉没配额的密钥；这里守住「结果为空也不报错」。"""
        with patch.object(credit_watch.db, 'query', return_value=[]):
            _, active, detail = credit_watch.quota_alerts()[0]
        self.assertFalse(active)
        self.assertIn('没有配置日配额', detail)


class FailureIsolationTests(unittest.TestCase):
    def test_query_failure_does_not_raise(self):
        """一条 SQL 出错不能让整个健康评估停摆（2026-11-15 踩过）。"""
        with patch.object(credit_watch.db, 'query', side_effect=RuntimeError('boom')):
            _, active, detail = credit_watch.quota_alerts()[0]
        self.assertFalse(active, '查询失败不能报成「额度告急」')
        self.assertIn('查询失败', detail)

    def test_disabled_keys_are_filtered_by_sql(self):
        """禁用密钥不该产生告警；SQL 里必须带 enabled 条件。"""
        import inspect
        source = inspect.getsource(credit_watch.quota_alerts)
        self.assertIn('enabled', source)


class WiringTests(unittest.TestCase):
    def test_operational_evaluate_calls_it(self):
        """接了才有效 —— 纯函数没人调用等于没写。"""
        from server import operational
        import inspect
        self.assertIn('quota_alerts', inspect.getsource(operational))


if __name__ == '__main__':
    unittest.main()
