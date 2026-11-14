"""v1.2.43：内存预算按物理占用记账；body 的读取预算不再按"读两遍"计算。

`WB_GATEWAY_MAX_INFLIGHT_BODY_BYTES` 原先只按"一份 body 字节"记账，而同一份 body
在解析与转发期间实际存在四到五份（中间件缓存、读取阶段的第二份、`json.loads` 的
对象图、序列化副本）。主机 3.7 GB、进程组上限 2 GiB，几个 64 MiB 的并发请求就能
把整组打进去。

同时 `BodyReadWindow` 被创建了两次（中间件一遍、`_read_json_body` 一遍），一次慢速
上传的时间预算因此翻倍。
"""
from __future__ import annotations

import unittest
from pathlib import Path

SOURCE = (Path(__file__).parents[1] / 'observability.py').read_text(encoding='utf-8')
GATEWAY = (Path(__file__).parents[1] / 'routers' / 'gateway.py').read_text(encoding='utf-8')


class PhysicalBodyAccountingTests(unittest.TestCase):
    def test_every_inflight_mutation_applies_the_same_factor(self):
        """加、减、以及三处比较必须用同一个倍率，否则会漏记或泄漏。"""
        lines = [line.strip() for line in SOURCE.splitlines()
                 if 'inflight_body_bytes' in line and ('+=' in line or '-=' in line)]
        self.assertGreaterEqual(len(lines), 4, f'应当有多处记账，实际 {len(lines)}')
        for line in lines:
            self.assertIn('body_physical_factor', line,
                          f'这处记账没有乘物理倍率，会导致配额与释放不匹配：{line}')

    def test_comparisons_also_apply_the_factor(self):
        comparisons = [line.strip() for line in SOURCE.splitlines()
                       if 'maximum_body_bytes' in line and 'inflight_body_bytes' in line]
        self.assertTrue(comparisons)
        for line in comparisons:
            self.assertIn('body_physical_factor', line,
                          f'这处比较没有乘物理倍率，会比实际宽松：{line}')

    def test_factor_defaults_above_one_and_is_configurable(self):
        self.assertIn("WB_GATEWAY_BODY_PHYSICAL_FACTOR", SOURCE)
        self.assertIn("max(1, _env_int('WB_GATEWAY_BODY_PHYSICAL_FACTOR', 3))", SOURCE)


class BodyReadWindowIsSharedTests(unittest.TestCase):
    def test_middleware_publishes_its_window_on_the_scope(self):
        self.assertIn("scope['wb_body_window'] = body_window", SOURCE)

    def test_second_read_reuses_the_published_window(self):
        self.assertIn("request.scope.get('wb_body_window') or BodyReadWindow()", GATEWAY)

    def test_only_one_window_is_created_per_request(self):
        created = GATEWAY.count('BodyReadWindow()')
        self.assertEqual(created, 1,
                         '网关侧只应有那一处"取不到才新建"的兜底创建')
        self.assertEqual(SOURCE.count('body_window=BodyReadWindow()'), 1)


if __name__ == '__main__':
    unittest.main()
