"""v1.2.42：准入预留按阶梯、准入库去掉逐事务 fsync、转发层可无中断重启。

三项都属于"把代价从每个请求上挪开"：
  * 8 MiB 请求原先要 128 次准入事务 + 128 次跨线程往返；
  * 每次准入事务都要等一次 fsync（`synchronous=FULL`），而这张库判活靠文件锁；
  * 转发层重启有 11 秒空窗，因为 uvicorn 会等 keep-alive 空闲连接结束。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from server.routers import gateway


class ReservationLadderTests(unittest.TestCase):
    def test_ladder_is_monotonic_and_ends_large(self):
        steps = gateway._RESERVE_STEPS
        self.assertEqual(list(steps), sorted(steps), '阶梯必须递增')
        self.assertGreaterEqual(steps[-1], 64 << 20, '最后一档要覆盖常见的最大转发体')

    def test_small_sizes_map_to_the_first_step(self):
        self.assertEqual(gateway._reserve_step(1), 64 << 10)
        self.assertEqual(gateway._reserve_step(64 << 10), 64 << 10)

    def test_crossing_a_step_rounds_up(self):
        self.assertEqual(gateway._reserve_step((64 << 10) + 1), 256 << 10)
        self.assertEqual(gateway._reserve_step(1 << 20), 1 << 20)
        self.assertEqual(gateway._reserve_step((1 << 20) + 1), 4 << 20)

    def test_beyond_the_largest_step_reserves_the_exact_size(self):
        huge = (64 << 20) + 12345
        self.assertEqual(gateway._reserve_step(huge), huge)

    def test_a_large_body_needs_far_fewer_reservations_than_blocks(self):
        """8 MiB 在 64 KiB 分块下是 128 块；阶梯后应当只有个位数次预留。"""
        size = 8 << 20
        blocks = size // (64 << 10)
        reserved, calls = 0, 0
        for i in range(1, blocks + 1):
            want = i * (64 << 10)
            if want <= reserved:
                continue
            target = gateway._reserve_step(want)
            if target <= reserved:
                continue
            reserved, calls = target, calls + 1
        self.assertEqual(blocks, 128)
        self.assertLessEqual(calls, 8, f'8 MiB 请求应只需个位数次预留，实际 {calls}')
        self.assertGreaterEqual(reserved, size, '预留量必须覆盖实际字节')


class AdmissionStoreDurabilityTests(unittest.TestCase):
    def test_synchronous_is_not_full(self):
        source = (Path(__file__).parents[1] / 'sharedadmission.py').read_text(encoding='utf-8')
        self.assertNotIn('synchronous=FULL', source,
                         '准入库的租约判活靠文件锁，不需要每个事务等 fsync')
        self.assertIn('synchronous=NORMAL', source)

    def test_lock_budget_allows_transient_contention(self):
        source = (Path(__file__).parents[1] / 'sharedadmission.py').read_text(encoding='utf-8')
        marker = 'sqlite3.connect(self.path, timeout='
        index = source.index(marker)
        value = float(source[index + len(marker):].split(',')[0])
        self.assertGreaterEqual(value, 0.2, '25ms 级别的预算会把瞬时争用误报成容量不足')


class RelayRestartScriptTests(unittest.TestCase):
    def test_script_switches_traffic_before_restarting(self):
        path = Path(__file__).parents[2] / 'deploy' / 'relay-restart-safe.sh'
        body = path.read_text(encoding='utf-8')
        # 顺序即正确性：先摘流量、再重启、就绪后交回
        direct = body.index('switch_to "$DIRECT"')
        restart = body.index('systemctl restart')
        back = body.index('switch_to "$RELAY"')
        self.assertLess(direct, restart)
        self.assertLess(restart, back)

    def test_script_validates_config_before_reload(self):
        body = (Path(__file__).parents[2] / 'deploy' / 'relay-restart-safe.sh').read_text(encoding='utf-8')
        self.assertIn('nginx -t', body)
        self.assertLess(body.index('nginx -t'), body.index('nginx -s reload'))

    def test_script_leaves_traffic_on_the_direct_path_on_failure(self):
        body = (Path(__file__).parents[2] / 'deploy' / 'relay-restart-safe.sh').read_text(encoding='utf-8')
        self.assertIn('exit 1', body)
        self.assertIn('入站仍直连主服务', body)


if __name__ == '__main__':
    unittest.main()
