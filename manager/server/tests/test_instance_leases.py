"""R3-L2 实例租约（v8）与「发布互斥隔离」的四项对照测试。

这是本批次**最容易被后续改动破坏的边界**：`instance_leases` 与 `task_leases`
是两套独立的所有权，而 `begin_publish()`（K28 发布门禁）必须继续只看任务租约。
任何一次「顺手把实例行塞进任务租约查询」都会让发布被自己的实例永久阻塞。

四项硬要求（`design/reviews/20261025-R3-盘点与实验设计.md` §1.6.2）：
  ① 实例租约活跃时 `begin_publish()` **不被阻塞**；
  ② 任务租约活跃时发布仍按 K28 语义被阻塞（`PublishBlocked`）；
  ③ 两套租约的 fence / 过期互不干扰；
  ④ DDL 列/索引存在，且 TTL 常量独立（心跳 10s / 过期 30s，不复用
     `tasklease.DEFAULT_TTL` 的 300s）。

另有状态机与 drain 联动的回归：claim → serving → draining → handoff → stopped，
release 不删行、fence 单调；`drain.begin()/resume()` 同步 role 并写 drain_snapshot。
"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from server import config, db, drain, instancelease, migrations, tasklease

REQUIRED_COLUMNS = {
    'instance_id', 'owner', 'lease_until', 'fence', 'updated_at', 'source_sha',
    'build_id', 'host', 'pid', 'start_ticks', 'role', 'phase', 'drain_snapshot', 'detail',
}
REQUIRED_INDEXES = {
    'idx_instance_leases_until', 'idx_instance_leases_role_until', 'idx_instance_leases_host',
}


class _LeaseCase(unittest.TestCase):
    """独立临时 SQLite + 真实 db.connect()（含 v8 迁移）。"""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = config.DB_PATH
        self.original_data = getattr(config, 'DATA_DIR', None)
        instancelease.reset_identity()
        tasklease.reset_identity()
        drain.reset_for_tests()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = Path(self.tmp.name) / 'instance-lease.db'
        db._conn = None
        db.connect()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        drain.reset_for_tests()
        instancelease.reset_identity()
        tasklease.reset_identity()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = self.original_db
        if self.original_data is not None:
            config.DATA_DIR = self.original_data
        self.tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def other_instance(self, label: str = 'b') -> dict:
        token = label * 32
        return {'instance_id': f'other-host:4242:777:{token}',
                'owner': f'other-host:{token}'}

    def claim_other(self, role: str = 'serving', ttl: int = 60) -> dict:
        other = self.other_instance()
        claimed, fence = instancelease.claim(
            other['instance_id'], ttl_seconds=ttl, role=role,
            owner=other['owner'])
        self.assertTrue(claimed)
        other['fence'] = fence
        return other


class PublishIsolationTest(_LeaseCase):
    """四项硬要求 ①②③：实例租约与发布互斥完全隔离。"""

    def test_active_instance_lease_does_not_block_begin_publish(self) -> None:
        """① 实例租约活跃 ≠ 有活跃任务：发布必须照常开始。"""
        self.claim_other(role='serving', ttl=120)
        with db.transaction() as conn:
            state = tasklease.begin_publish(conn, 'release:r3l2:test', 60)
        self.assertEqual(state['state'], 'publishing')
        # 发布状态确实写进去了（也证明上面那句没有被实例租约干扰）
        self.assertIsNotNone(tasklease.publish_state(db.connect()))
        with db.transaction() as conn:
            self.assertTrue(tasklease.end_publish(conn, 'release:r3l2:test'))

    def test_active_task_lease_still_blocks_publish(self) -> None:
        """② 任务租约活跃时，发布仍按 K28 语义被拒（PublishBlocked）。"""
        claimed, _fence = tasklease.claim('r3l2-daily', 120)
        self.assertTrue(claimed)
        with self.assertRaises(tasklease.PublishBlocked) as caught:
            with db.transaction() as conn:
                tasklease.begin_publish(conn, 'release:r3l2:blocked', 60)
        self.assertIn('r3l2-daily', str(caught.exception))
        self.assertIsNone(tasklease.publish_state(db.connect()))

    def test_fences_and_expiry_do_not_interfere(self) -> None:
        """③ 两套租约的 fence/过期互不影响。"""
        # 任务租约先建立
        task_ok, task_fence = tasklease.claim('r3l2-daily', 120)
        self.assertTrue(task_ok)
        # 实例租约过期后被另一个实例接管：fence 递增，但任务租约的 fence 不变
        other = self.claim_other(role='serving', ttl=60)
        db.execute('UPDATE instance_leases SET lease_until=? WHERE instance_id=?',
                   (int(time.time()) - 5, other['instance_id']))
        third = {'instance_id': 'other-host:4243:888:' + 'c' * 32,
                 'owner': 'other-host:' + 'c' * 32}
        taken, third_fence = instancelease.claim(
            third['instance_id'], ttl_seconds=60, role='serving', owner=third['owner'])
        self.assertTrue(taken)
        self.assertEqual(third_fence, 1, '新实例自己的 fence 从 0 起')
        after = db.query_one('SELECT fence FROM task_leases WHERE task=?', ('r3l2-daily',))
        self.assertEqual(int(after['fence']), task_fence,
                         '实例租约接管不能改任务租约的 fence')
        # 反向：任务租约过期接管，也不能改实例 fence
        db.execute('UPDATE task_leases SET lease_until=? WHERE task=?',
                   (int(time.time()) - 5, 'r3l2-daily'))
        re_taken, re_fence = tasklease.claim('r3l2-daily', 120)
        self.assertTrue(re_taken)
        self.assertEqual(re_fence, task_fence + 1)
        instance_row = instancelease.describe(third['instance_id'])
        self.assertEqual(instance_row['fence'], third_fence,
                         '任务租约接管不能改实例租约的 fence')

    def test_tasklease_module_not_imported_by_publish_path_for_instances(self) -> None:
        """`begin_publish` 的判定只读 task_leases：即使实例表里全是 serving 也照常发布。

        这是①②③的补充：直接检查 `tasklease.active_leases()` 不会看到实例行。
        """
        first = self.other_instance('b')
        second = {'instance_id': 'other-host:4243:888:' + 'e' * 32,
                  'owner': 'other-host:' + 'e' * 32}
        instancelease.claim(first['instance_id'], ttl_seconds=120, role='serving',
                            owner=first['owner'])
        instancelease.claim(second['instance_id'], ttl_seconds=120, role='draining',
                            owner=second['owner'])
        conn = db.connect()
        self.assertEqual(tasklease.active_leases(conn), [],
                         'tasklease.active_leases 不能包含实例租约')
        self.assertEqual(len(instancelease.active_instances(now=int(time.time()))), 2)


class MigrationAndConstantsTest(_LeaseCase):
    """四项硬要求 ④：DDL 列/索引存在，TTL 常量独立。"""

    def test_instance_leases_table_columns_and_indexes(self) -> None:
        columns = {row[1] for row in db.query('PRAGMA table_info(instance_leases)')}
        self.assertEqual(columns, REQUIRED_COLUMNS)
        indexes = {str(r[0]) for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='instance_leases'")}
        self.assertTrue(REQUIRED_INDEXES.issubset(indexes), indexes)
        version = migrations.CODE_SCHEMA_VERSION
        self.assertGreaterEqual(version, 8)
        applied = {int(r['version']) for r in db.query(
            "SELECT version FROM schema_migrations WHERE state='applied'")}
        self.assertIn(8, applied, 'v8 instance_leases 必须已应用')

    def test_ttl_constants_are_independent(self) -> None:
        self.assertEqual(instancelease.HEARTBEAT_SEC, 10)
        self.assertEqual(instancelease.LEASE_TTL_SEC, 30)
        self.assertNotEqual(instancelease.LEASE_TTL_SEC, tasklease.DEFAULT_TTL,
                            '实例租约不能复用任务租约的 300s TTL')
        self.assertLess(instancelease.HEARTBEAT_SEC, instancelease.LEASE_TTL_SEC)

    def test_role_check_constraint(self) -> None:
        with self.assertRaises(Exception):
            db.execute("INSERT INTO instance_leases(instance_id, owner, lease_until, "
                       "fence, updated_at, role) VALUES('x','o',1,0,1,'bogus')")


class ClaimAndStateMachineTest(_LeaseCase):
    """claim / heartbeat / 状态机 / release / 查询。"""

    def test_claim_creates_serving_row_with_identity(self) -> None:
        me = instancelease.identity()
        claimed, fence = instancelease.claim()
        self.assertTrue(claimed)
        self.assertEqual(fence, 1)
        row = instancelease.describe()
        self.assertEqual(row['instance_id'], me['instance_id'])
        self.assertEqual(row['owner'], me['owner'])
        self.assertEqual(row['role'], 'serving')
        self.assertEqual(row['pid'], me['pid'])
        self.assertEqual(row['host'], me['host'])

    def test_reauth_bumps_fence_and_old_fence_fails(self) -> None:
        _, first = instancelease.claim()
        again, second = instancelease.claim()
        self.assertTrue(again)
        self.assertEqual(second, first + 1)
        self.assertFalse(instancelease.heartbeat(fence=first),
                         '旧 fence 在重入后必须失效')
        self.assertTrue(instancelease.heartbeat(fence=second))

    def test_expired_instance_lease_cannot_mutate_and_new_instance_serves(self) -> None:
        """过期实例不得再改状态；新实例用自己的行成为 serving（§1.6.3）。"""
        stale = self.claim_other(role='serving', ttl=60)
        db.execute('UPDATE instance_leases SET lease_until=? WHERE instance_id=?',
                   (int(time.time()) - 5, stale['instance_id']))
        # 过期实例：任何状态写入都必须被拒（SIGKILL/假死后的迟到写入）
        self.assertFalse(instancelease.begin_drain(stale['instance_id'], stale['fence'],
                                                   owner=stale['owner']),
                         '过期的实例租约不得再改 role')
        self.assertFalse(instancelease.finalize_handoff(stale['instance_id'],
                                                        stale['fence'], owner=stale['owner']))
        self.assertFalse(instancelease.still_holding(stale['instance_id'], stale['fence'],
                                                     owner=stale['owner']))
        # 新实例用自己的 instance_id claim（每实例一行，不共享主键），成为唯一 serving
        taker = {'instance_id': 'third-host:99:1:' + 'd' * 32,
                 'owner': 'third-host:' + 'd' * 32}
        taken, fence = instancelease.claim(
            taker['instance_id'], ttl_seconds=60, owner=taker['owner'])
        self.assertTrue(taken)
        self.assertEqual(fence, 1)
        serving = instancelease.active_instances(role='serving', now=int(time.time()))
        self.assertEqual([item['instance_id'] for item in serving], [taker['instance_id']],
                         '过期实例不再出现在 serving 里')

    def test_state_machine_serving_draining_handoff_stopped(self) -> None:
        _, fence = instancelease.claim()
        snapshot = {'in_flight': {'inference_stream': 2}, 'total_in_flight': 2,
                    'readyz_ok': False, 't_mono': 12.5}
        self.assertTrue(instancelease.begin_drain(fence=fence, snapshot=snapshot))
        row = instancelease.describe()
        self.assertEqual((row['role'], row['phase']), ('draining', 'draining'))
        self.assertEqual(row['drain_snapshot']['in_flight']['inference_stream'], 2)
        self.assertTrue(instancelease.finalize_handoff(
            fence=fence, snapshot={'total_in_flight': 0, 'readyz_ok': False}))
        row = instancelease.describe()
        self.assertEqual((row['role'], row['phase']), ('handoff', 'handoff'))
        self.assertTrue(instancelease.release(fence=fence,
                                              snapshot={'t_mono': 20.0}))
        row = instancelease.describe()
        self.assertEqual(row['owner'], '')
        self.assertEqual(row['lease_until'], 0)
        self.assertEqual(row['role'], 'stopped')
        self.assertEqual(row['fence'], fence, 'release 不回退 fence')
        self.assertIsNotNone(row, 'release 不删行（防 ABA）')

    def test_resume_serving_rolls_back_from_draining(self) -> None:
        _, fence = instancelease.claim()
        self.assertTrue(instancelease.begin_drain(fence=fence))
        self.assertTrue(instancelease.resume_serving(fence=fence))
        self.assertEqual(instancelease.describe()['role'], 'serving')

    def test_wrong_fence_cannot_change_role(self) -> None:
        _, fence = instancelease.claim()
        self.assertFalse(instancelease.begin_drain(fence=fence + 5))
        self.assertEqual(instancelease.describe()['role'], 'serving')

    def test_describe_and_active_instances(self) -> None:
        me = instancelease.identity()
        self.assertIsNone(instancelease.describe('does-not-exist'))
        self.assertEqual(instancelease.active_instances(now=int(time.time())), [])
        _, fence = instancelease.claim()
        active = instancelease.active_instances(role='serving', now=int(time.time()))
        self.assertEqual([item['instance_id'] for item in active], [me['instance_id']])
        self.assertEqual(instancelease.active_instances(role='draining'), [])
        self.assertEqual(len(instancelease.active_instances(host=me['host'])), 1)
        # 过期后不再出现在 active 里
        db.execute('UPDATE instance_leases SET lease_until=? WHERE instance_id=?',
                   (int(time.time()) - 1, me['instance_id']))
        self.assertEqual(instancelease.active_instances(now=int(time.time())), [])
        self.assertFalse(instancelease.still_holding(fence=fence))

    def test_pid_alive_helpers(self) -> None:
        import os
        self.assertIn(instancelease.pid_alive(os.getpid(),
                                              instancelease.start_ticks()), (True, None))
        self.assertFalse(instancelease.pid_alive(999_999_999))
        self.assertIsNone(instancelease.owner_alive('other-host:' + 'e' * 32))
        self.assertIs(instancelease.owner_alive(instancelease.identity()['owner']), True)


class DrainIntegrationTest(_LeaseCase):
    """drain.begin()/resume() 与 role 状态机联动，readyz 语义不变。"""

    def test_drain_begin_marks_instance_draining_with_snapshot(self) -> None:
        _, fence = instancelease.claim()
        drain.begin()
        row = instancelease.describe()
        self.assertEqual(row['role'], 'draining')
        snapshot = row['drain_snapshot']
        self.assertIn('in_flight', snapshot)
        self.assertEqual(snapshot['readyz_ok'], False)
        self.assertIn('t_mono', snapshot)
        self.assertTrue(drain.is_draining(), 'drain 自身状态不受租约影响')

    def test_drain_resume_returns_instance_to_serving(self) -> None:
        _, fence = instancelease.claim()
        drain.begin()
        self.assertTrue(drain.resume())
        self.assertEqual(instancelease.describe()['role'], 'serving')
        self.assertFalse(drain.is_draining())

    def test_drain_works_without_instance_lease(self) -> None:
        """没有实例租约时 drain 必须照常工作（向后兼容）。"""
        self.assertIsNone(instancelease.describe())
        drain.begin()
        self.assertTrue(drain.is_draining())
        self.assertGreaterEqual(drain.handoff_snapshot()['t_epoch'], 1)
        self.assertTrue(drain.resume())

    def test_handoff_snapshot_has_four_kind_counts(self) -> None:
        snapshot = drain.handoff_snapshot()
        self.assertIn('in_flight', snapshot)
        self.assertIn('total_in_flight', snapshot)
        self.assertEqual(snapshot['in_flight'], {})
        # 四类计数来自 classify()：流式推理/推理/写/读
        self.assertEqual(drain.classify('POST', '/v1/chat/completions',
                                        {'accept': 'text/event-stream'}),
                         'inference_stream')
        self.assertEqual(drain.classify('POST', '/v1/chat/completions', {}), 'inference')
        self.assertEqual(drain.classify('POST', '/api/keys', {}), 'write')
        self.assertEqual(drain.classify('GET', '/api/logs', {}), 'read')


if __name__ == '__main__':
    unittest.main()
