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
    """v1.2.46 起不变量变了：**输入**乘倍率、**输出**不乘、释放按 charged 配对。

    原先这里断言"每处都乘同一个倍率" —— 那正是 v1.2.43 的回归本身：把「承诺上限」
    当成「已分配内存」乘了上去，小请求也能占掉 96 MiB。
    正确的不变量见 `test_v1246_output_commit.py`。
    """

    def test_charge_system_is_used_everywhere(self):
        for name in ('reserve_capacity', 'reserve_wire_body',
                     'reserve_output_body', 'reserve_input'):
            start = SOURCE.index(f'def {name}(')
            tail = SOURCE[start + 1:]
            nxt = tail.find('\n        def ')
            body = SOURCE[start:start + 1 + (nxt if nxt != -1 else 1400)]
            self.assertIn('charged', body,
                          f'{name} 记了在途字节却没有维护 charged，加减会对不上')

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
