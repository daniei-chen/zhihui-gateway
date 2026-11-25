"""告警状态必须跨进程重启存活，否则同一个未恢复的告警会被反复当成「新的」重报。

背景（2026-11-25）：`low_accounts` 在 24 小时内报了 35 次，而当天只有 7 次部署。
`alerts` 是内存字典，部署重启后清空，monitor 下一轮看到 `old is None` 就判定为
「状态进入」，把同一条从未恢复的告警又投递一遍。

这里验证三件事：
  1. 进入告警时写进库；
  2. 模拟重启（清空内存 + 重置加载标记）后，同一条告警**不再**被当成新的；
  3. 告警恢复时把行删掉，重启后也不会「诈尸」。
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server import config, db, operational


class AlertPersistenceTest(unittest.TestCase):
    def setUp(self):
        # 用独立的库文件，避免碰真实的 manager.db。
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._close_and_cleanup)
        self.db_path = Path(self.directory.name) / 'alerts.db'
        self.db_patch = patch.object(config, 'DB_PATH', self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        # db 模块缓存了连接，换路径后必须丢掉旧连接才会重新打开。
        # 直接赋值而不是 patch：patch 的 stopall 会在 cleanup 链里把 _conn 复原成
        # 那个旧连接，于是我们关掉的是测试连接、留下的却是原库连接。
        self._saved_conn = db._conn
        db._conn = None
        # connect() 会执行 executescript(SCHEMA)，顺带把 operational_alerts 建好。
        db.connect()

        self.alerts = patch.object(operational, 'alerts', {})
        self.alerts.start()
        self.addCleanup(self.alerts.stop)

        self.dispatched = []
        # notify 是在函数里惰性导入的，patch 模块属性即可生效。
        self.notify_patch = patch('server.notify.dispatch',
                                  side_effect=lambda name, active, detail: self.dispatched.append((name, active)))
        self.notify_patch.start()
        self.addCleanup(self.notify_patch.stop)

        self._simulate_restart()

    def _close_and_cleanup(self):
        """先关连接再删目录——Windows 上打开的文件句柄会挡住 unlink。"""
        conn = getattr(db, '_conn', None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        db._conn = self._saved_conn
        self.directory.cleanup()

    def _simulate_restart(self):
        """清空内存状态并重置加载标记，等价于进程重新起来。"""
        operational.alerts.clear()
        operational._alerts_loaded = False

    def test_alert_survives_restart_without_repeating(self):
        operational.transition('low_accounts', True, '只剩 2 个可用账号')
        self.assertEqual(self.dispatched, [('low_accounts', True)])
        self.assertIn('low_accounts', operational.alerts)

        # 模拟部署重启：内存清空，告警条件仍未恢复
        self._simulate_restart()
        operational.transition('low_accounts', True, '只剩 2 个可用账号')

        # 仍然只投递过那一次 —— 重启没有让它变成「新告警」
        self.assertEqual(self.dispatched, [('low_accounts', True)],
                         '重启后同一条未恢复的告警不应再次投递')
        self.assertIn('low_accounts', operational.alerts)

    def test_recovery_removes_row_so_it_does_not_reappear(self):
        operational.transition('backup_stale', True, '超过 26 小时未备份')
        operational.transition('backup_stale', False, 'recovered')
        self.assertNotIn('backup_stale', operational.alerts)

        # 恢复之后再重启，不应该冒出旧告警
        self._simulate_restart()
        operational.transition('some_other', True, 'x')
        self.assertNotIn('backup_stale', operational.alerts,
                         '已恢复的告警不应在重启后复活')

    def test_since_timestamp_is_preserved_across_restart(self):
        before = int(time.time())
        operational.transition('model_completion', True, '模型补全失败')
        since = operational.alerts['model_completion']['since']
        self.assertGreaterEqual(since, before)

        time.sleep(0.05)
        self._simulate_restart()
        operational.transition('model_completion', True, '模型补全失败')

        # since 应该是最初进入告警的时刻，不是重启时刻 —— 否则「已经多久了」会失真
        self.assertEqual(operational.alerts['model_completion']['since'], since)

    def test_persist_failure_does_not_break_state_machine(self):
        # patch 底层的 transaction，而不是 _persist_alert 本身 ——
        # 要验证的是 _persist_alert 内部的 try/except 真的兜住了写库失败。
        with patch.object(db, 'transaction', side_effect=RuntimeError('disk full')):
            operational.transition('disk_pressure', True, '磁盘接近阈值')
        self.assertIn('disk_pressure', operational.alerts)
        self.assertEqual(self.dispatched, [('disk_pressure', True)],
                         '写库失败不应影响告警投递')

    def test_load_failure_does_not_block_alerts(self):
        self._simulate_restart()
        with patch.object(db, 'query', side_effect=RuntimeError('db locked')):
            operational.transition('load_fail_case', True, 'x')
        self.assertIn('load_fail_case', operational.alerts)


if __name__ == '__main__':
    unittest.main()
