"""结算阶段必须看得到状态码：`state.http_status` 要在 `budget.settle` 之前回填。

## 这条测试挡的是什么

`budget.settle` 里有一条「明确的上游 4xx 应释放预留」的判据：

```python
client_rejected = (state.terminal_state == 'rejected'
                   and isinstance(state.http_status, int)
                   and 400 <= state.http_status < 500
                   and state.http_status != 429)
```

而 `state.http_status` 唯一的实质赋值点在 `observability.send_audited`
—— 响应头真正发给客户端的那一刻。`_record` 是 handler **返回之前**跑的，
它内部又在同一个事务里调 `budget.settle`。

于是那个字段在结算时**恒为 0**，判据永不成立：每一次客户端把参数写错
（不存在模型名、非法请求体）都在 `budget_reservations` 里留一条 `unknown`。
生产库实测 1538 条 `unknown` 里有 129 条对应 400、3 条对应 403 —— 本应 released。

## 为什么用源码顺序而不是跑一遍

跑通这条路径要造一个真实的请求 + 上游 4xx + 事务落库，成本高且会随
handler 结构调整而碎。这条断言只守**一件事**：回填语句必须存在，
且必须排在 `budget.settle(` 调用之前。它抓的是「有人把回填挪走或删掉」
这种回归——正是当初引入缺陷的形态。
"""
from __future__ import annotations

import inspect
import unittest

from server import budget
from server.routers import gateway


class SettleSeesHttpStatusTest(unittest.TestCase):
    def test_record_backfills_http_status_before_settle(self) -> None:
        source = inspect.getsource(gateway._record)

        marker = 'state.http_status = http_status'
        self.assertIn(
            marker, source,
            '_record 必须在调用 budget.settle 之前把 http_status 回填到 state 上，'
            '否则 budget.settle 的 4xx 释放判据永远读不到值',
        )

        settle_calls = [line for line in source.splitlines() if 'budget.settle(' in line]
        self.assertTrue(settle_calls, '_record 里应当仍调用 budget.settle')

        backfill_at = source.index(marker)
        settle_at = source.index('budget.settle(')
        self.assertLess(
            backfill_at, settle_at,
            '回填 state.http_status 的语句必须排在 budget.settle 调用之前',
        )

    def test_settle_still_reads_the_state_field(self) -> None:
        """回填之所以必要，是因为 settle 读的确实是 state.http_status。

        如果哪天 settle 改成接收显式参数，这条测试会失败——那是**好事**：
        提醒回填语句可以一起去掉，而不是留下一条没人用的赋值。
        """
        source = inspect.getsource(budget.settle)
        self.assertIn('state.http_status', source)
        self.assertIn('client_rejected', source)


if __name__ == '__main__':
    unittest.main()
