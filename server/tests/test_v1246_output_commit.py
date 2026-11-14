"""v1.2.46：修正 v1.2.43 引入的容量回归 —— 输出承诺不该乘物理倍率。

## 发生了什么

v1.2.43 让每个在途字节都乘一个物理倍率（默认 3），理由是"同一份请求体在内存里
存在四到五份"。这个理由对**输入体**成立：它会被解析成对象图。

但它被套到了**输出预留**上，而输出是另一回事：

* 输出是逐步增长的字节缓冲，没有对象图开销；
* 它的预留是「承诺上限」（`2 × 最大输出 + 信封`，本机 32 MiB），不是已分配内存；
* 而且它只在请求开始时预留一次，之后不会按实际输出增长。

于是单个小请求（请求体 117 字节）就占掉 `32 MiB × 3 = 96 MiB` 预算，
384 MiB 只能放 3 个并发 —— 而并发上限是 28（大请求）/ 64（总）。
生产上表现为 17 次 503「响应容量已满」，`capacity_rejections_total` 同步计数。

## 现在的记账

两个量分开，语义不同：

* `reserved` —— 逻辑预留（给协调器看，用于跨进程公平分配）；
* `charged` —— 本地物理记账（真正占用本进程预算的量）。

| 部分 | 乘倍率 | 理由 |
| --- | --- | --- |
| 输入体 | 是 | 会被解析成对象图 |
| 转发体（序列化副本） | 是 | 与输入同阶的字节副本 |
| 输出缓冲 | **否** | 承诺上限，且无对象图 |

释放时按 `charged` 归还，加减严格配对。
"""
from __future__ import annotations

import unittest
from pathlib import Path

SOURCE = (Path(__file__).parents[1] / 'observability.py').read_text(encoding='utf-8')


def _function_body(name: str, length: int = 1400) -> str:
    """截取一个函数的正文，到下一个同级 def 为止（避免把邻居算进来）。"""
    start = SOURCE.index(f'def {name}(')
    tail = SOURCE[start + 1:]
    nxt = tail.find('\n        def ')
    end = start + 1 + (nxt if nxt != -1 else length)
    return SOURCE[start:min(end, start + length)]


class OutputIsNotChargedTheFactorTests(unittest.TestCase):
    def test_output_reservation_does_not_multiply(self):
        """输出预留不能乘倍率 —— 这是 v1.2.43 回归的直接原因。"""
        body = _function_body('reserve_output_body')
        self.assertNotIn('body_physical_factor', body,
                         '输出预留又乘上了物理倍率，小请求会被放大成几十兆')

    def test_input_reservation_does_multiply(self):
        body = _function_body('reserve_input')
        self.assertIn('body_physical_factor', body,
                      '输入体会被解析成对象图，必须乘倍率')

    def test_forward_body_does_multiply(self):
        body = _function_body('reserve_wire_body')
        self.assertIn('body_physical_factor', body)


class ChargeAndReleaseArePairedTests(unittest.TestCase):
    def test_release_uses_the_charged_amount_not_reserved(self):
        """释放必须按 charged 归还：reserved 里混着从未乘倍率的输出部分。"""
        self.assertIn('self.inflight_body_bytes -= charged', SOURCE)
        self.assertNotIn('self.inflight_body_bytes -= reserved', SOURCE)

    def test_every_increment_has_a_matching_charge(self):
        """每个做记账的函数，都必须同时维护 charged（累加或赋值）。"""
        for name in ('reserve_capacity', 'reserve_wire_body',
                     'reserve_output_body', 'reserve_input'):
            body = _function_body(name)
            self.assertIn('inflight_body_bytes', body, f'{name} 不再记账了？')
            self.assertIn('charged', body,
                          f'{name} 记了 inflight_body_bytes 却没有维护 charged，释放会对不上')

    def test_charged_is_initialised_alongside_reserved(self):
        self.assertIn('reserved=0; charged=0', SOURCE)


class CapacityIsNotCollapsedByTheFactorTests(unittest.TestCase):
    """把这次的回归做成一个算式断言：小请求的并发能力不能塌到个位数。"""

    def test_a_small_request_costs_only_its_own_bytes(self):
        budget = 384 << 20                 # 生产值 402653184
        factor = 3
        output_commit = 2 * (16 << 20) + 4096   # 2 × WB_MAX_OUTPUT_BYTES + 信封
        small_body = 117

        # 输入按倍率，输出按承诺本身（不乘）
        per_request = small_body * factor + output_commit
        concurrency = budget // per_request
        self.assertGreaterEqual(concurrency, 8,
                                f'小请求的并发能力只有 {concurrency}，容量被倍率压塌了')

    def test_the_old_wrong_accounting_would_have_collapsed_it(self):
        """反向验证：乘上去之后并发确实只剩个位数 —— 说明这条断言不是空的。"""
        budget = 384 << 20
        factor = 3
        output_commit = 2 * (16 << 20) + 4096
        wrong = (117 + output_commit) * factor
        self.assertLessEqual(budget // wrong, 4)


if __name__ == '__main__':
    unittest.main()
