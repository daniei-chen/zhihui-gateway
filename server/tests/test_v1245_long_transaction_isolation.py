"""v1.2.45：长事务不再冻结全站 DB；统计重建去掉 N+1；连接池容量与并发匹配。

三项都属于「低频或运维动作不该影响在线请求」。
"""
from __future__ import annotations

import unittest
from pathlib import Path

PGSYNC = (Path(__file__).parents[1] / 'services' / 'pgsync.py').read_text(encoding='utf-8')
DB = (Path(__file__).parents[1] / 'db.py').read_text(encoding='utf-8')
CONFIG = (Path(__file__).parents[1] / 'config.py').read_text(encoding='utf-8')


class VacuumDoesNotHoldTheGlobalLockTests(unittest.TestCase):
    MARKER = "handle.execute('VACUUM INTO ?'"

    def test_vacuum_uses_its_own_connection(self):
        self.assertIn("handle = sqlite3.connect(str(config.DB_PATH), timeout=30.0)", PGSYNC)
        self.assertIn(self.MARKER, PGSYNC)
        self.assertIn('handle.close()', PGSYNC)

    def test_vacuum_is_not_inside_the_process_write_lock(self):
        """进程级写锁必须不出现在 VACUUM 前后：它会让全站 DB 访问排队等导出。"""
        index = PGSYNC.index(self.MARKER)
        window = PGSYNC[max(0, index - 1200):index + 300]
        self.assertNotIn('with db._lock:', window,
                         'VACUUM 仍在全局写锁内，导出期间全站 DB 访问会被冻结')

    def test_rationale_is_recorded(self):
        index = PGSYNC.index(self.MARKER)
        window = PGSYNC[max(0, index - 1200):index]
        self.assertIn('一致性快照', window)


class RebuildHasNoNPlusOneTests(unittest.TestCase):
    def test_credit_units_are_written_with_the_insert(self):
        start = DB.index('def rebuild_usage_from_logs')
        body = DB[start:start + 6000]
        self.assertIn('credit_units', body)
        # 逐行 SELECT rowid + UPDATE 的写法必须消失
        self.assertNotIn("SELECT rowid,credit FROM", body,
                         '重建仍在逐行反推，那是一次 N+1')
        self.assertNotIn("SET credit_units=?,credit=? WHERE rowid=?", body)

    def test_normalisation_still_matches_the_ledger(self):
        start = DB.index('def rebuild_usage_from_logs')
        body = DB[start:start + 6000]
        self.assertIn('ledger.units', body)
        self.assertIn('ledger.SCALE', body)


class PoolCapacityTests(unittest.TestCase):
    def test_pool_defaults_were_widened(self):
        self.assertIn("WB_HTTP_MAX_POOLS', 32)", CONFIG)
        self.assertIn("WB_HTTP_POOL_CONNECTIONS', 16)", CONFIG)
        self.assertIn("WB_HTTP_POOL_KEEPALIVE', 8)", CONFIG)

    def test_keepalive_never_exceeds_connections(self):
        import server.config as config
        self.assertLessEqual(config.HTTP_POOL_KEEPALIVE, config.HTTP_POOL_CONNECTIONS)

    def test_each_pool_can_cover_a_burst(self):
        import server.config as config
        self.assertGreaterEqual(config.HTTP_POOL_CONNECTIONS, 16,
                                '每桶连接数至少要能覆盖一次常见突发')


if __name__ == '__main__':
    unittest.main()
