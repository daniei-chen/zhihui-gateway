"""实例租约长时间线（soak）验证：纯逻辑时钟推进，不 sleep、不跳过。

四个严格用例：
1. 固定时钟 1000→1300 每 10s 续租（TTL 30），1120 drain、1300 resume，
   逐步校验 owner/fence/role/lease_until。
2. 租约过期后 resume_serving 被拒，角色仍为 draining。
3. 相同 instance_id 被新 owner/fence 接管后，旧 owner 的 heartbeat / 改 role 被拒。
4. drain.resume() 在实例租约同步失败时只恢复本地准入状态（两层断言）。

不导入、不继承任何已存在的具体测试类；每个用例独占临时 SQLite + 真实迁移。
"""

import tempfile
import unittest
from pathlib import Path

from server import config, db, drain, instancelease, tasklease


class TestInstanceLeaseSoak(unittest.TestCase):
    """独立临时 DB，真实 db.connect()（含 v8 迁移），cleanup 恢复全局状态。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = config.DB_PATH
        self.original_data = getattr(config, 'DATA_DIR', None)
        instancelease.reset_identity()
        tasklease.reset_identity()
        drain.reset_for_tests()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = Path(self.tmp.name) / 'instance-lease-soak.db'
        db._conn = None
        self.addCleanup(self._cleanup)
        db.connect()

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
    def _row(self, instance_id: str):
        with db.transaction() as conn:
            row = instancelease._row(conn, instance_id)
        self.assertIsNotNone(row, 'instance_leases 行不存在')
        return row

    def _assert_lease(self, instance_id: str, *, owner: str, fence: int,
                      role: str, lease_until: int, updated_at: int | None = None) -> None:
        row = self._row(instance_id)
        self.assertEqual(str(row['owner'] or ''), owner)
        self.assertEqual(int(row['fence'] or 0), fence)
        self.assertEqual(str(row['role']), role)
        self.assertEqual(int(row['lease_until'] or 0), lease_until)
        if updated_at is not None:
            self.assertEqual(int(row['updated_at'] or 0), updated_at)

    # ── 1. 5 分钟时间线：续租 / drain / resume ─────────────────────────
    def test_soak_renew_then_drain_then_resume_timeline(self) -> None:
        iid = 'soak-host:9001:111:soak' + 'a' * 32
        owner = 'soak-host:' + 'a' * 32
        ttl = 30
        claimed, fence = instancelease.claim(
            iid, ttl_seconds=ttl, owner=owner, now=1000)
        self.assertTrue(claimed)
        self.assertEqual(fence, 1)
        self._assert_lease(iid, owner=owner, fence=fence, role='serving',
                           lease_until=1000 + ttl, updated_at=1000)

        for stamp in range(1010, 1301, 10):
            self.assertGreater(int(self._row(iid)['lease_until']), stamp,
                               '下一次续租之前也必须持续持有有效租约')
            renewed = instancelease.heartbeat(
                iid, fence=fence, ttl_seconds=ttl, owner=owner, now=stamp)
            self.assertTrue(renewed, f'{stamp} 续租失败')
            role = 'serving'
            if stamp == 1120:
                self.assertTrue(instancelease.begin_drain(
                    iid, fence, owner=owner, now=stamp))
                role = 'draining'
            elif stamp > 1120:
                role = 'draining'
            if stamp == 1300:
                self.assertTrue(instancelease.resume_serving(
                    iid, fence, owner=owner, now=stamp))
                role = 'serving'
            self._assert_lease(iid, owner=owner, fence=fence, role=role,
                               lease_until=stamp + ttl, updated_at=stamp)
            self.assertGreater(int(self._row(iid)['lease_until']), stamp,
                               '数据库中的租约到期时间必须晚于当前时钟')

    # ── 2. 过期后 resume 被拒，角色保持 draining ───────────────────────
    def test_expired_resume_serving_rejected_and_role_stays_draining(self) -> None:
        iid = 'expired-host:9101:222:exp' + 'b' * 32
        owner = 'expired-host:' + 'b' * 32
        claimed, fence = instancelease.claim(
            iid, ttl_seconds=30, owner=owner, now=1000)
        self.assertTrue(claimed)
        self.assertTrue(instancelease.begin_drain(iid, fence, owner=owner, now=1010))
        self._assert_lease(iid, owner=owner, fence=fence, role='draining',
                           lease_until=1030, updated_at=1010)

        self.assertFalse(instancelease.resume_serving(
            iid, fence, owner=owner, now=1500))
        row = self._row(iid)
        self.assertEqual(str(row['role']), 'draining')
        self.assertEqual(str(row['phase']), 'draining')
        self.assertEqual(str(row['detail']), 'drain begin')
        self._assert_lease(iid, owner=owner, fence=fence, role='draining',
                           lease_until=1030, updated_at=1010)

    # ── 3. 同 instance_id 被新 owner/fence 接管后被隔离 ─────────────────
    def test_takeover_invalidates_old_owner_heartbeat_and_role(self) -> None:
        iid = 'ghost-host:9201:333:ghost' + 'c' * 32
        old_owner = 'ghost-host:' + 'c' * 32
        new_owner = 'ghost-host:' + 'd' * 32
        claimed, fence = instancelease.claim(
            iid, ttl_seconds=30, owner=old_owner, now=1000)
        self.assertTrue(claimed)
        self.assertEqual(fence, 1)

        # 过期后另一 owner 接管：fence +1，owner 变更。
        claimed, new_fence = instancelease.claim(
            iid, ttl_seconds=30, owner=new_owner, now=1100)
        self.assertTrue(claimed)
        self.assertEqual(new_fence, 2)
        self._assert_lease(iid, owner=new_owner, fence=new_fence, role='serving',
                           lease_until=1130, updated_at=1100)

        # 旧 fence 的续租与改角色一律被拒。
        self.assertFalse(instancelease.heartbeat(
            iid, fence=fence, ttl_seconds=30, owner=old_owner, now=1105))
        self.assertFalse(instancelease.set_role(
            iid, fence, role='draining', owner=old_owner, now=1105))
        # 旧 owner + 新 fence 同样被拒（owner 不匹配）。
        self.assertFalse(instancelease.heartbeat(
            iid, fence=new_fence, ttl_seconds=30, owner=old_owner, now=1105))
        self.assertFalse(instancelease.set_role(
            iid, new_fence, role='draining', owner=old_owner, now=1105))
        self._assert_lease(iid, owner=new_owner, fence=new_fence, role='serving',
                           lease_until=1130, updated_at=1100)

    # ── 4. drain.resume：本地准入与实例租约解耦 ─────────────────────────
    def test_drain_resume_restores_local_only_when_lease_sync_fails(self) -> None:
        me = instancelease.identity()
        iid = me['instance_id']
        intruder = 'intruder-host:' + 'e' * 32
        claimed, fence = instancelease.claim(
            iid, ttl_seconds=3600, role='draining', owner=intruder)
        self.assertTrue(claimed)
        self.assertEqual(fence, 1)
        before = self._row(iid)

        # 本地进入 draining；实例租约归别人持有，同步必然失败（不写库）。
        drain.begin()
        self.assertTrue(drain.is_draining())
        self.assertTrue(drain.snapshot()['draining'])
        after_begin = self._row(iid)
        for key in ('owner', 'fence', 'lease_until', 'updated_at', 'role', 'phase', 'detail'):
            self.assertEqual(after_begin[key], before[key],
                             f'drain.begin() 不应改写实例租约字段 {key}')

        # 本地回退成功，但租约层本轮同步依旧失败。
        self.assertTrue(drain.resume())
        self.assertFalse(drain.is_draining())
        self.assertFalse(drain.snapshot()['draining'])
        after_resume = self._row(iid)
        for key in ('owner', 'fence', 'lease_until', 'updated_at', 'role', 'phase', 'detail'):
            self.assertEqual(after_resume[key], before[key],
                             f'drain.resume() 不应改写实例租约字段 {key}')
        # 两层语义不同：本地准入已恢复，实例租约仍是 draining 且 owner 为他人。
        self.assertEqual(str(after_resume['role']), 'draining')
        self.assertEqual(str(after_resume['owner']), intruder)
        self.assertEqual(int(after_resume['fence'] or 0), 1)


if __name__ == '__main__':
    unittest.main()
