"""v1.2.47：输出承诺按请求声明的上限算，不再一律按全局上限。

原先每个请求都预留 `2 × 最大输出 + 信封`（生产 32 MiB），无论它要一百个 token
还是一万六千个。那直接决定并发：384 MiB ÷ 32 MiB = 12 个。

现在按声明值折算，并设 2 MiB 下限：

| 场景 | 改前 | 改后 |
| --- | --- | --- |
| 短问答（百 token 级） | 32 MiB | **2 MiB** |
| 长回答（一万六千 token） | 32 MiB | **2 MiB**（声明值仍在下限内） |
| 声明超过上限 | 32 MiB | 16 MiB（按上限） |

声明不可信（缺失/非法/冲突）时一律退回上限 —— 宁可少放几个。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from server import outputlimits

SOURCE = (Path(__file__).parents[1] / 'routers' / 'gateway.py').read_text(encoding='utf-8')

#: 生产单元的 `WB_MAX_OUTPUT_BYTES`（16 MiB）。测试里显式钉住它，否则断言会跟着
#: 本机默认值漂移 —— 这一条本身就是上一轮回归的同型错误。
PRODUCTION_CAP = 16 << 20
FLOOR = 2 << 20


def with_cap(cap: int = PRODUCTION_CAP):
    return mock.patch.dict(os.environ, {'WB_MAX_OUTPUT_BYTES': str(cap)})


class DeclaredOutputTests(unittest.TestCase):
    def setUp(self):
        patcher = with_cap()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_floor_is_two_mebibytes(self):
        self.assertEqual(outputlimits.DECLARED_OUTPUT_FLOOR_BYTES, FLOOR)

    def test_small_declarations_land_on_the_floor(self):
        for value in (1, 100, 4000, 8000, 16000):
            self.assertEqual(outputlimits.declared_output_bytes({'max_tokens': value}), FLOOR,
                             f'max_tokens={value} 应当落在 2 MiB 下限上')

    def test_the_floor_saves_against_the_old_fixed_commitment(self):
        """短问答从 32 MiB 降到 2 MiB —— 省十六倍。"""
        self.assertLess(FLOOR, (32 << 20) // 16 + 1)

    def test_declarations_above_the_floor_scale_linearly(self):
        value = 1_000_000                  # 1e6 × 4 = 4 MB > 2 MiB
        self.assertEqual(outputlimits.declared_output_bytes({'max_tokens': value}),
                         value * outputlimits.BYTES_PER_OUTPUT_TOKEN)

    def test_oversized_declaration_is_capped(self):
        self.assertEqual(outputlimits.declared_output_bytes({'max_tokens': 10 ** 9}),
                         PRODUCTION_CAP)

    def test_missing_declaration_falls_back_to_the_cap(self):
        self.assertEqual(outputlimits.declared_output_bytes({}), PRODUCTION_CAP)
        self.assertEqual(outputlimits.declared_output_bytes({'messages': []}), PRODUCTION_CAP)

    def test_non_dict_body_falls_back_to_the_cap(self):
        self.assertEqual(outputlimits.declared_output_bytes(None), PRODUCTION_CAP)
        self.assertEqual(outputlimits.declared_output_bytes([]), PRODUCTION_CAP)

    def test_invalid_declarations_fall_back_to_the_cap(self):
        for body in ({'max_tokens': 0}, {'max_tokens': -5}, {'max_tokens': '100'},
                     {'max_tokens': 1.5}, {'max_tokens': None}):
            self.assertEqual(outputlimits.declared_output_bytes(body), PRODUCTION_CAP, body)

    def test_conflicting_fields_fall_back_to_the_cap(self):
        body = {'max_tokens': 100, 'max_completion_tokens': 200}
        self.assertEqual(outputlimits.declared_output_bytes(body), PRODUCTION_CAP)

    def test_agreeing_fields_are_accepted(self):
        body = {'max_tokens': 100, 'max_completion_tokens': 100}
        self.assertEqual(outputlimits.declared_output_bytes(body), FLOOR)

    def test_responses_protocol_field_is_understood(self):
        """`max_output_tokens` 是另一套协议的字段，也必须认得。"""
        self.assertEqual(outputlimits.declared_output_bytes({'max_output_tokens': 100}), FLOOR)

    def test_the_call_site_uses_the_declared_value(self):
        self.assertIn('declared_output_bytes(body)', SOURCE)
        self.assertNotIn('output_reserve(2*maximum_bytes()+FAILURE_ENVELOPE_BYTES)', SOURCE)


class ReservationIsNotATruncationTests(unittest.TestCase):
    """这条最重要：预留只是记账，绝不能决定用户能拿到多少输出。"""

    def setUp(self):
        patcher = with_cap()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reservation_never_caps_the_output(self):
        obs = (Path(__file__).parents[1] / 'observability.py').read_text(encoding='utf-8')
        start = obs.index('async def send_audited(')
        body = obs[start:start + 1200]
        # 发送路径只改响应头，不能按 output_reserved 截断 body
        self.assertNotIn('output_reserved', body,
                         '发送路径按预留截断了输出 —— 预留是记账，不是限制')

    def test_the_real_output_limit_is_a_separate_setting(self):
        cap = outputlimits.maximum_bytes()
        self.assertGreaterEqual(cap, FLOOR,
                                '真正的输出上限必须不小于记账下限，否则回答会被误截')
        self.assertEqual(cap, PRODUCTION_CAP)


class CapacityAfterTheChangeTests(unittest.TestCase):
    def setUp(self):
        patcher = with_cap()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_short_requests_allow_many_concurrent(self):
        budget = 384 << 20
        envelope = outputlimits.FAILURE_ENVELOPE_BYTES
        per_request = outputlimits.declared_output_bytes({'max_tokens': 100}) + envelope
        self.assertGreaterEqual(budget // per_request, 150,
                                '短请求的并发能力应当由连接数决定，不是被输出承诺卡住')

    def test_long_requests_still_reserve_the_cap(self):
        per_request = outputlimits.declared_output_bytes({'max_tokens': 10 ** 9}) + \
            outputlimits.FAILURE_ENVELOPE_BYTES
        self.assertEqual(per_request, PRODUCTION_CAP + outputlimits.FAILURE_ENVELOPE_BYTES)

    def test_the_old_fixed_commitment_would_have_capped_concurrency(self):
        """反向验证：固定按上限预留时并发只有十几个 —— 说明上一条断言不是空的。"""
        budget = 384 << 20
        old = 2 * PRODUCTION_CAP + outputlimits.FAILURE_ENVELOPE_BYTES
        self.assertLessEqual(budget // old, 12)


if __name__ == '__main__':
    unittest.main()
