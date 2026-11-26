"""持续 active 的告警，detail 也必须跟着更新。

## 问题

`operational.transition()` 原来只在「进入告警」时写 detail。告警一旦持续 active，
库里那条 detail 就永远停在第一次进入时的样子。

2026-11-26 撞上：`workbuddy_low_accounts` 的 detail 是

> 2 个 WorkBuddy 账号余额低于 1000 分（ì 809、慢点 838）；每日签到各 +100，
> 若日消耗低于 100 会自行回升

而实测两个号都在掉（ì -30/天、慢点 -88/天）。文案里的那句解释是十小时前的观察，
看的人既拿不到新数字，也不知道它已经不成立了。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 持续 active 时 detail 会被刷新 | 库里永远是第一次进入时的文案 |
| `since` 不被重置 | 「挂了多久」失真，看起来像刚发生 |
| detail 没变时不重复写 | 每 30 秒一次无用 UPSERT |
| 日志有节流 | monitor 每 30 秒一轮，余额数字每轮都变，日志会爆 |
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server import config, db, operational


class AlertDetailRefreshTest(unittest.TestCase):
    def setUp(self):
        # Windows 上 sqlite 文件句柄释放得慢，清理失败不应算测试失败。
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / 'alerts.db'
        patcher = patch.object(config, 'DB_PATH', self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._saved_conn = db._conn
        db._conn = None
        db.connect()
        self.addCleanup(self._restore_conn)
        self.alerts = patch.object(operational, 'alerts', {})
        self.alerts.start()
        self.addCleanup(self.alerts.stop)
        notify_patcher = patch('server.notify.dispatch')
        self.notify = notify_patcher.start()
        self.addCleanup(notify_patcher.stop)

    def _restore_conn(self):
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = self._saved_conn

    def _stored(self, name):
        row = db.query_one(
            'SELECT since, detail FROM operational_alerts WHERE name=?', (name,))
        return dict(row) if row else None

    def test_detail_is_refreshed_while_active(self):
        operational.transition('demo', True, '旧文案')
        first = self._stored('demo')
        operational.transition('demo', True, '新文案：ì -30、慢点 -88')
        after = self._stored('demo')
        self.assertEqual(after['detail'], '新文案：ì -30、慢点 -88')
        self.assertEqual(after['since'], first['since'], 'since 是「何时开始」，不该被刷新')

    def test_unchanged_detail_is_not_rewritten(self):
        operational.transition('demo', True, '同一句话')
        first = self._stored('demo')
        operational.transition('demo', True, '同一句话')
        self.assertEqual(self._stored('demo'), first)

    def test_refresh_does_not_re_dispatch(self):
        operational.transition('demo', True, '一')
        operational.transition('demo', True, '二')
        # 只在状态进入时投递一次；文案刷新不该再发一遍通知。
        self.assertEqual(self.notify.call_count, 1)

    def test_logging_is_throttled(self):
        operational.transition('demo', True, 'v0')
        self.assertEqual(operational.alerts['demo'].get('detail_logged_at'), None)
        operational.transition('demo', True, 'v1')
        stamped = operational.alerts['demo']['detail_logged_at']
        self.assertIsInstance(stamped, int)
        # 十分钟内再变也不重复打日志。
        operational.alerts['demo']['detail_logged_at'] = int(time.time()) - 60
        operational.transition('demo', True, 'v2')
        self.assertEqual(operational.alerts['demo']['detail_logged_at'],
                         int(time.time()) - 60)

    def test_recovery_still_removes_the_row(self):
        operational.transition('demo', True, '有事')
        operational.transition('demo', True, '有事，数字变了')
        operational.transition('demo', False, '')
        self.assertIsNone(self._stored('demo'))


if __name__ == '__main__':
    unittest.main()
