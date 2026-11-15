"""Resource safety is tested with separate real processes, not two Python dicts."""
import multiprocessing as mp
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from server.sharedadmission import Coordinator, Unavailable


POLICY = dict(active=2, large=1, bytes=400, preauth=2, queue=2,
              queue_per_key=1, key_default=2, key_quota=1)


def _process_owner(path, business, ready, done):
    from server import config
    config.DB_PATH = Path(business)
    coordinator = Coordinator(path, POLICY)
    assert coordinator.key('child', 41, 1)
    assert coordinator.reserve('child', 300, True)
    ready.put(True)
    done.wait(10)
    # Exit normally without release: the lifetime OS lock, not TTL, is the proof.


class SharedAdmissionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'admission.db'
        self.coordinators = []
        self.a = self.peer('a')
        self.b = self.peer('b')

    def tearDown(self):
        for peer in self.coordinators:
            peer.close()
        self.directory.cleanup()

    def peer(self, owner, alive=lambda _: None, policy=POLICY):
        result = Coordinator(self.path, policy, owner=lambda: owner, alive=alive)
        self.coordinators.append(result)
        return result

    def test_active_and_per_key_limits_span_generations(self):
        self.assertTrue(self.a.key('a1', 11, 1))
        self.assertFalse(self.b.key('b1', 11, 1))
        self.assertTrue(self.a.reserve('a1', 20, False))
        self.assertTrue(self.b.key('b2', 12, 2))
        self.assertTrue(self.b.reserve('b2', 20, False))
        self.assertFalse(self.a.reserve('a3', 20, False))
        self.a.release('a1')
        self.assertTrue(self.b.key('b1', 11, 1))
        self.assertTrue(self.b.reserve('b1', 20, False))

    def test_raw_plus_transformed_bytes_and_large_upgrade_are_atomic(self):
        self.assertTrue(self.a.reserve('a1', 150, False))
        self.assertTrue(self.b.reserve('b1', 150, False))
        self.assertFalse(self.a.reserve('a1', 300, True))
        self.assertEqual(self.b.snapshot()['body_bytes'], 300)
        self.assertTrue(self.a.reserve('a1', 250, True))
        self.assertFalse(self.b.reserve('b1', 150, True))
        self.assertEqual(self.a.snapshot()['large'], 1)
        self.assertTrue(self.a.reserve('a1', 10, False))
        self.assertEqual(self.a.snapshot()['body_bytes'], 400)

    def test_preauth_and_queue_have_cross_process_bounds(self):
        self.assertTrue(self.a.preauthorize('a1'))
        self.assertTrue(self.b.preauthorize('b1'))
        self.assertFalse(self.a.preauthorize('a2'))
        self.a.end_preauthorize('a1')
        self.assertTrue(self.a.preauthorize('a2'))
        for peer, request, key in ((self.a, 'a1', 11), (self.b, 'b1', 11), (self.a, 'a2', 12)):
            self.assertTrue(peer.key(request, key, 2))
        self.assertTrue(self.a.queue('a1'))
        self.assertFalse(self.b.queue('b1'))
        self.assertTrue(self.a.queue('a2'))
        self.assertFalse(self.b.queue('b2'))

    def test_unknown_owner_is_never_reaped(self):
        self.assertTrue(self.a.reserve('a1', 300, True))
        self.assertFalse(self.b.reserve('b1', 200, False))
        self.assertEqual(self.b.snapshot()['active'], 1)

    def test_known_dead_owner_frees_only_resource_leases(self):
        self.assertTrue(self.a.reserve('a1', 300, True))
        c = self.peer('c', alive=lambda owner: False if owner == 'a' else None)
        self.assertTrue(c.reserve('c1', 300, True))
        self.assertEqual(c.snapshot()['active'], 1)

    def test_other_owner_cannot_release_or_reuse_execution_id(self):
        self.assertTrue(self.a.reserve('execution', 100, False))
        with self.assertRaisesRegex(Unavailable, 'owner_mismatch'):
            self.b.release('execution')
        with self.assertRaisesRegex(Unavailable, 'owner_mismatch'):
            self.b.reserve('execution', 100, False)
        self.assertEqual(self.a.snapshot()['active'], 1)

    def test_peer_cannot_install_a_second_larger_policy(self):
        self.a.reserve('a1', 20, False)
        with self.assertRaisesRegex(Unavailable, 'policy_'):
            self.peer('bad', policy={**POLICY, 'active': 20})

    def test_busy_store_fails_closed_and_finished_release_retries(self):
        self.a.reserve('a1', 300, True)
        lock = sqlite3.connect(self.path)
        lock.execute('BEGIN IMMEDIATE')
        try:
            started = time.monotonic()
            with self.assertRaises(Unavailable):
                self.b.reserve('b1', 200, False)
            with self.assertRaises(Unavailable):
                self.a.release('a1')
            # 写锁预算已从 25 ms 提到 500 ms（25 ms 会把瞬时争用误报成"容量已满"）。
            # 两次调用各等一次预算，合计 1 秒量级；这条断言要验证的是"会失败退出、
            # 不会永久挂住"，不是"25 毫秒内失败"。
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertEqual(self.b.snapshot()['active'], 1)
            self.assertFalse(self.b.snapshot()['healthy'])
        finally:
            lock.rollback()
            lock.close()
        self.assertTrue(self.a.reserve('a2', 300, True))
        self.assertTrue(self.b.probe()['healthy'])
        self.assertEqual(self.b.snapshot()['active'], 1)

    def test_real_process_lifetime_lock_not_wall_clock_reclaims_owner(self):
        from server import budgetowner, config
        old = config.DB_PATH
        config.DB_PATH = Path(self.directory.name) / 'business.db'
        # A separate DB avoids fixture owner names that intentionally stay unknown.
        actual_path = Path(self.directory.name) / 'process.db'
        ctx = mp.get_context('spawn')
        ready, done = ctx.Queue(), ctx.Event()
        child = ctx.Process(target=_process_owner, args=(actual_path, config.DB_PATH, ready, done))
        child.start()
        try:
            self.assertTrue(ready.get(timeout=10))
            parent = Coordinator(actual_path, POLICY)
            self.coordinators.append(parent)
            self.assertFalse(parent.key('parent', 41, 1))
            self.assertFalse(parent.reserve('parent', 200, False))
            done.set()
            child.join(10)
            self.assertEqual(child.exitcode, 0)
            # `_reap` 现在按时间限频（默认十秒一次）：它检测的是"某个 owner 已经
            # 不在了"，晚十秒发现不会让活着的人被拒。把计时器归零等价于"限频窗口
            # 已过"，免得为一个纯性能优化在测试里真睡十秒。
            parent._last_reap = 0.0
            self.assertTrue(parent.key('parent', 41, 1))
            self.assertTrue(parent.reserve('parent', 300, True))
            parent.release('parent')
            self.assertEqual(parent.snapshot()['leases'], 0)
        finally:
            done.set()
            child.join(10)
            config.DB_PATH = old
            budgetowner.close()
            ready.close()
