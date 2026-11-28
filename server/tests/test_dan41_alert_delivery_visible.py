"""DAN-41：投递通道未配置时，运行告警不许静默消失。

## 病灶

`server/notify.py` 的 `_mode()` 在配置缺失时返回 `disabled`，而 `dispatch()`
在这一支直接返回 `{'mode': 'disabled', 'delivered': False}` —— 告警不落盘、
不发 HTTP、也不打日志。默认部署（没有 `/opt/notify/config.json`）下，
`operational.transition()` 算出的每一条运行告警都是这样消失的：
2026-11 的 P0-1「每日备份连续失败」是靠人工审计发现的，不是告警发现的。

## 本文件守什么

一个本该触发的 `backup_stale`，在未配置时必须至少落在三层中的一处
（实现上三层都有）：

| 层 | 断言 |
| --- | --- |
| 本地 JSONL | `DATA_DIR/notify/undelivered.jsonl` 里有一行同名记录 |
| 应用日志 | `workbuddy.notify` 记一条 WARNING，点名告警名 |
| 健康端点 | `/api/health/alerts` 的 `notify` 字段标明未配置并列出丢弃的告警名 |

另外三条：启动自检要明确说「告警未配置，运行告警将被丢弃」；降级路径不许抛
（`dispatch()` 永不抛的契约）；已配置时投递行为不变。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from server import config, notify


class _IsolatedDeliveryTest(unittest.TestCase):
    """两个用例类共用的隔离：配置路径、数据目录、进程内丢弃计数。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / 'notify.json'
        patcher = mock.patch.dict(os.environ, {'WB_NOTIFY_CONFIG': str(self.config_path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        data = mock.patch.object(config, 'DATA_DIR', self.root / 'data')
        data.start()
        self.addCleanup(data.stop)
        counter = mock.patch.object(notify, '_DROPPED',
                                    {'count': 0, 'names': {}, 'last_at': 0}, create=True)
        counter.start()
        self.addCleanup(counter.stop)

    def spool(self) -> Path:
        return self.root / 'data' / 'notify' / 'undelivered.jsonl'

    def journal_rows(self) -> list:
        if not self.spool().exists():
            return []
        return [json.loads(line) for line in
                self.spool().read_text(encoding='utf-8').splitlines() if line.strip()]


class UnconfiguredDeliveryTest(_IsolatedDeliveryTest):
    """默认部署形态：`WB_NOTIFY_CONFIG` 指向的文件根本不存在。"""

    def test_dropped_alert_is_journaled_locally(self) -> None:
        result = notify.dispatch('backup_stale', True, 'no valid backup freshness evidence')
        self.assertFalse(result['delivered'])
        self.assertTrue(result.get('dropped'),
                        '未配置时 dispatch 必须标出「被丢弃」，否则调用方无从区分')
        rows = self.journal_rows()
        self.assertEqual([row['name'] for row in rows], ['backup_stale'])
        self.assertTrue(rows[0]['active'])
        self.assertEqual(rows[0]['schema'], 'zhihui-alert/1')
        self.assertEqual(rows[0]['reason'], 'notify_unconfigured')
        self.assertIn(str(self.root), result.get('spool') or '')

    def test_dropped_alert_is_logged(self) -> None:
        with self.assertLogs('workbuddy.notify', level='WARNING') as captured:
            notify.dispatch('backup_stale', True, 'no valid backup freshness evidence')
        text = '\n'.join(captured.output)
        self.assertIn('backup_stale', text)
        self.assertIn('dropped', text)

    def test_drop_is_counted_for_the_health_endpoint(self) -> None:
        notify.dispatch('backup_stale', True, 'no valid backup freshness evidence')
        notify.dispatch('recovery_chain_stale', True, 'chain older than 50 hours')
        summary = notify.dropped_summary()
        self.assertEqual(summary['count'], 2)
        self.assertEqual(sorted(summary['names']), ['backup_stale', 'recovery_chain_stale'])

    def test_health_endpoint_marks_unconfigured_and_names_dropped_alerts(self) -> None:
        notify.dispatch('backup_stale', True, 'no valid backup freshness evidence')
        delivery = self.health_alerts().get('notify') or {}
        self.assertFalse(delivery.get('configured'))
        self.assertEqual(delivery.get('mode'), 'disabled')
        self.assertIn('告警未配置', delivery.get('warning') or '')
        self.assertEqual((delivery.get('dropped') or {}).get('names', {}).get('backup_stale'), 1)
        self.assertIn(str(self.root), delivery.get('spool') or '')

    def test_startup_check_announces_that_alerts_will_be_dropped(self) -> None:
        with self.assertLogs('workbuddy.notify', level='WARNING') as captured:
            state = notify.log_startup_state()
        self.assertFalse(state['configured'])
        text = '\n'.join(captured.output)
        self.assertIn('告警未配置，运行告警将被丢弃', text)
        self.assertIn(str(self.root), text)

    def test_degraded_paths_never_raise(self) -> None:
        # 账写不进去（`notify` 这个父路径是个普通文件）也不能让 dispatch 抛 ——
        # 调用方是监控循环里的 `transition()`。
        blocker = self.root / 'data' / 'notify'
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text('not a directory', encoding='utf-8')
        result = notify.dispatch('backup_stale', True, 'no valid backup freshness evidence')
        self.assertFalse(result['delivered'])
        self.assertTrue(result.get('dropped'))
        self.assertEqual(result.get('spool'), '')
        self.assertEqual(notify.dropped_summary()['count'], 1)

    def test_transition_drops_the_alert_into_the_journal(self) -> None:
        """走真实调用点：`operational.transition()` → `notify.dispatch()`。

        直接调 `dispatch()` 只能证明投递层；这里证明**告警状态机那条线没有断**——
        `backup_stale` 是 `operational` 真会算出来的一条，它进入时必须在未配置的
        部署里留下痕迹。
        """
        from server import db, operational
        saved = (config.DB_PATH, db._conn, operational.alerts)
        self.addCleanup(self._restore, saved)
        config.DB_PATH = self.root / 'alerts.db'
        db._conn = None
        db.connect()
        operational.alerts = {}
        operational.transition('backup_stale', True, 'verified backup report older than 26 hours')
        rows = self.journal_rows()
        self.assertEqual([row['name'] for row in rows], ['backup_stale'])
        self.assertIn('26 hours', rows[0]['detail'])
        self.assertIn('backup_stale', notify.dropped_summary()['names'])

    def _restore(self, saved) -> None:
        from server import db, operational
        if db._conn is not None:
            try:
                db._conn.close()
            except Exception:      # noqa: BLE001
                pass
        config.DB_PATH, db._conn, operational.alerts = saved

    def health_alerts(self) -> dict:
        from fastapi.testclient import TestClient
        from server.main import app
        client = TestClient(app, client=('127.0.0.1', 12345))
        response = client.get('/api/health/alerts')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class ConfiguredDeliveryIsUnchangedTest(_IsolatedDeliveryTest):
    """已配置时投递行为不变：file 模式直接写配置里的路径，不额外写丢弃账。"""

    def setUp(self) -> None:
        super().setUp()
        self.target = self.root / 'alerts.jsonl'
        self.config_path.write_text(json.dumps(
            {'enabled': True, 'mode': 'file', 'file': str(self.target)}), encoding='utf-8')

    def test_file_mode_delivers_to_the_configured_path_only(self) -> None:
        result = notify.dispatch('backup_stale', True, 'no valid backup freshness evidence')
        self.assertTrue(result['delivered'])
        self.assertNotIn('dropped', result)
        rows = [json.loads(line) for line in
                self.target.read_text(encoding='utf-8').splitlines() if line.strip()]
        self.assertEqual([row['name'] for row in rows], ['backup_stale'])
        self.assertFalse(self.spool().exists(), '已配置时不该再写丢弃账')
        self.assertEqual(notify.dropped_summary()['count'], 0)

    def test_startup_check_stays_quiet_when_configured(self) -> None:
        state = notify.log_startup_state()
        self.assertTrue(state['configured'])
        self.assertEqual(state['mode'], 'file')
        self.assertEqual(state['warning'], '')


if __name__ == '__main__':
    unittest.main()
