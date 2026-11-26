"""低余额告警不该对「余额在回升」的账号持续报警。

## 问题

2026-11-26 线上 `workbuddy_low_accounts` 连续 19.5 小时处于 active，指向两个号
（ì 801、慢点 928）。而这两个号近 24 小时的余额都在**增加**：每日签到各 +100，
实际日消耗约 24 分。文案早就写了「会自行回升」，但 active 一直是 True ——
`/api/health/alerts` 因此永远显示 `count=1`。

一条永远亮着的告警等于没有告警：它训练读的人忽略这一栏。

## 判据

| 近 24 小时的变化 | 是否告警 |
| --- | --- |
| 全部低余额账号都在回升 | 否（文案说明原因） |
| 只有部分在回升 | **是**（剩下的确实在掉） |
| 拿不到 24 小时前的快照 | **是**（不拿最近一条冒充历史） |
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from server import credit_watch


def _row(accounts, low, captured_at=1795660479):
    return {
        'platform': 'workbuddy', 'account': 'pool', 'plan': '24 个可用账号',
        'total': 100000.0, 'used': 1000.0, 'remain': 99000.0,
        'captured_at': captured_at,
        'raw': json.dumps({'accounts': accounts, 'low_accounts': low}),
    }


class LowAccountTrendTests(unittest.TestCase):
    def _alerts(self, accounts, low, before_accounts):
        row = _row(accounts, low)
        before = (None if before_accounts is None
                  else {'raw': json.dumps({'accounts': before_accounts}),
                        'captured_at': row['captured_at'] - 86400})
        with mock.patch.object(credit_watch, 'latest', return_value=row), \
                mock.patch.object(credit_watch.db, 'query_one',
                                  return_value=before), \
                mock.patch.object(credit_watch, 'usage_credit_rate',
                                  return_value=None), \
                mock.patch.object(credit_watch, 'today_gain',
                                  return_value=None), \
                mock.patch.object(credit_watch, 'low_ratio_threshold',
                                  return_value=0.2):
            return {name: (active, detail)
                    for name, active, detail in credit_watch.workbuddy_alerts()}

    def test_rising_accounts_do_not_hold_an_alert(self):
        """余额低于阈值、但每天都在回升时不该一直占着告警位。"""
        now = [{'uid': 'a', 'nickname': 'ì', 'remain': 880},
               {'uid': 'b', 'nickname': '慢点', 'remain': 1000}]
        low = [{'uid': 'a', 'nickname': 'ì', 'remain': 880}]
        old = [{'uid': 'a', 'nickname': 'ì', 'remain': 780},
               {'uid': 'b', 'nickname': '慢点', 'remain': 900}]
        alerts = self._alerts(now, low, old)
        active, detail = alerts['workbuddy_low_accounts']
        self.assertFalse(active, '余额在回升的账号不该一直占着告警位')
        self.assertIn('都在回升', detail)

    def test_reports_the_direction_every_time(self):
        """2026-11-26 线上形态：两个号都在掉，文案必须说出掉多少。"""
        now = [{'uid': 'a', 'nickname': 'ì', 'remain': 801},
               {'uid': 'b', 'nickname': '慢点', 'remain': 905}]
        old = [{'uid': 'a', 'nickname': 'ì', 'remain': 831},
               {'uid': 'b', 'nickname': '慢点', 'remain': 993}]
        active, detail = self._alerts(now, list(now), old)['workbuddy_low_accounts']
        self.assertTrue(active, '在掉的号必须继续报')
        self.assertIn('近 24 小时', detail)
        self.assertIn('-30', detail)
        self.assertIn('-88', detail)
        # 掉得最快的那个（慢点 905，-88/天）约 10 天用完 —— 这个数字才让人知道急不急。
        self.assertIn('掉得最快', detail)
        self.assertIn('10 天用完', detail)

    def test_mixed_still_alerts(self):
        now = [{'uid': 'a', 'nickname': '涨的', 'remain': 800},
               {'uid': 'b', 'nickname': '掉的', 'remain': 700}]
        low = list(now)
        old = [{'uid': 'a', 'nickname': '涨的', 'remain': 700},
               {'uid': 'b', 'nickname': '掉的', 'remain': 750}]
        active, detail = self._alerts(now, low, old)['workbuddy_low_accounts']
        self.assertTrue(active, '还有一个在掉，就该继续报')
        self.assertIn('+100', detail)
        self.assertIn('-50', detail)

    def test_falling_accounts_keep_the_original_wording(self):
        now = [{'uid': 'a', 'nickname': '掉的', 'remain': 600}]
        low = list(now)
        old = [{'uid': 'a', 'nickname': '掉的', 'remain': 900}]
        active, detail = self._alerts(now, low, old)['workbuddy_low_accounts']
        self.assertTrue(active)
        self.assertIn('近 24 小时', detail)

    def test_no_history_means_no_verdict(self):
        """没有 24 小时前的快照时不判断 —— 不能拿最近一条冒充历史。"""
        now = [{'uid': 'a', 'nickname': 'ì', 'remain': 800}]
        active, _ = self._alerts(now, list(now), None)['workbuddy_low_accounts']
        self.assertTrue(active, '判不出来时宁可报，不可静默')

    def test_trend_helper_reports_increases(self):
        row = _row([{'uid': 'a', 'remain': 800}, {'uid': 'b', 'remain': 900}],
                   [{'uid': 'a', 'remain': 800}])
        before = {'raw': json.dumps({'accounts': [{'uid': 'a', 'remain': 700},
                                                  {'uid': 'b', 'remain': 950}]})}
        with mock.patch.object(credit_watch, 'latest', return_value=row), \
                mock.patch.object(credit_watch.db, 'query_one',
                                  return_value=before):
            trend = credit_watch._account_trend('workbuddy')
        self.assertEqual(trend['a'], 100)
        self.assertEqual(trend['b'], -50)

    def test_trend_helper_is_silent_without_an_older_snapshot(self):
        row = _row([{'uid': 'a', 'remain': 800}], [{'uid': 'a', 'remain': 800}])
        with mock.patch.object(credit_watch, 'latest', return_value=row), \
                mock.patch.object(credit_watch.db, 'query_one',
                                  return_value=None):
            self.assertEqual(credit_watch._account_trend('workbuddy'), {})


if __name__ == '__main__':
    unittest.main()
