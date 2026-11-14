"""v1.2.90：每一次「用错参数」都在账本里留一条待人工核对的记录。

## 怎么发现的

监控告警 `budget_rejected_unknown` 报出：24 小时内有 2 条请求被拒，却留着未决预留。

查那两条，一条是我拿不存在的模型名试探（`trae/NoSuchModel-XYZ`），另一条是请求体
不合法。**两次都是明确的参数错误，上游看都没看就退回。**

它们本不该留下任何账目。

## 根因

结算时的释放条件写的是：

```
row['state'] == 'reserved' 且 没观察到用量  →  released
```

**但请求在转发前就已经是 `forwarded` 了**，所以那个分支永远走不到。剩下的路只有
`unknown`。

后果：每一次「用错参数」都变成一条待人工核对记录。对账的人逐条去看，最后发现是
调用方写错了参数。

## 修法

补一条判据：**明确的 4xx 拒绝**（排除 429）视为零用量确定，直接释放。

**排除 429**：限流不代表上游没处理过，它可能已经排队或部分执行 —— 那种情况仍然
只能保守记为 `unknown`。

## 边界

| 情形 | 结果 | 理由 |
| --- | --- | --- |
| 400 / 404 / 422 等 | `released` | 上游在参数校验阶段退回，没有执行 |
| **429** | `unknown` | 限流不代表没处理 |
| 5xx | `unknown` | 上游可能已经执行 |
| 200 且用量完整 | `settled` | 正常记账 |
| 状态码不是整数 | `unknown` | 判不了就别猜 |
"""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

from server import budget


class _State:
    """settle 需要的请求级状态。"""

    def __init__(self, *, terminal_state, http_status, request_id='req-1'):
        self.terminal_state = terminal_state
        self.http_status = http_status
        self.request_id = request_id
        self.budget_reserved = True
        self.key_id = 1
        self.terminal = terminal_state
        self.budget_settled_pending = False


class _Row(dict):
    """db.query_one 返回的行。"""


def _run_settle(*, terminal_state, http_status, row_state, observed, known_hint=False):
    """跑真实的 settle，返回它写进去的 state。"""
    state = _State(terminal_state=terminal_state, http_status=http_status)
    row = _Row(state=row_state, owner='o', fence=0)

    written = {}

    class _Measured:
        complete = known_hint
        malformed = False

        def __init__(self, observed_flag):
            self.observed = observed_flag

        def storage_tokens(self):
            return (0, 0)

    def fake_execute(sql, args):
        if 'UPDATE budget_reservations' in sql:
            written['state'] = args[0]

    with mock.patch.object(budget.observability, 'current') as current, \
            mock.patch.object(budget.db, 'query_one', side_effect=[row, None]), \
            mock.patch.object(budget.db, 'execute', side_effect=fake_execute), \
            mock.patch.object(budget, '_owned', return_value=True), \
            mock.patch('server.portalwallet.receipt', return_value=None), \
            mock.patch('server.metering.parse_usage', return_value=_Measured(observed)), \
            mock.patch('server.metering.parse_credit', return_value=(None,)), \
            mock.patch('server.metering._int_or_none', return_value=(None, [])), \
            mock.patch.object(budget.time, 'time', return_value=0):
        current.get.return_value = state
        budget.settle({}, None, None, None)
    return written.get('state')


class ExplicitClientRejectionIsReleasedTests(unittest.TestCase):
    def test_400_is_released(self):
        self.assertEqual(
            _run_settle(terminal_state='rejected', http_status=400,
                        row_state='forwarded', observed=False),
            'released')

    def test_404_is_released(self):
        self.assertEqual(
            _run_settle(terminal_state='rejected', http_status=404,
                        row_state='forwarded', observed=False),
            'released')

    def test_422_is_released(self):
        self.assertEqual(
            _run_settle(terminal_state='rejected', http_status=422,
                        row_state='forwarded', observed=False),
            'released')


class TheExceptionsStayConservativeTests(unittest.TestCase):
    def test_429_stays_unknown(self):
        """限流不代表上游没处理过 —— 它可能已经排队或部分执行。"""
        self.assertEqual(
            _run_settle(terminal_state='rejected', http_status=429,
                        row_state='forwarded', observed=False),
            'unknown')

    def test_5xx_stays_unknown(self):
        self.assertEqual(
            _run_settle(terminal_state='upstream_error', http_status=500,
                        row_state='forwarded', observed=False),
            'unknown')

    def test_missing_status_stays_unknown(self):
        """状态码不是整数时判不了，不猜。"""
        self.assertEqual(
            _run_settle(terminal_state='rejected', http_status='400',
                        row_state='forwarded', observed=False),
            'unknown')

    def test_transport_error_with_no_usage_stays_unknown(self):
        """已转发但既没用量也非拒绝 —— 上游可能已经执行过。"""
        self.assertEqual(
            _run_settle(terminal_state='transport_error', http_status=0,
                        row_state='forwarded', observed=False),
            'unknown')


class NormalPathsAreUnchangedTests(unittest.TestCase):
    def test_reserved_and_unobserved_is_released(self):
        """既有分支不能被新判据挤掉。"""
        self.assertEqual(
            _run_settle(terminal_state=None, http_status=0,
                        row_state='reserved', observed=False),
            'released')

    def test_completed_with_complete_usage_is_settled(self):
        self.assertEqual(
            _run_settle(terminal_state='completed', http_status=200,
                        row_state='forwarded', observed=True, known_hint=True),
            'settled')


class TheReasonIsRecordedTests(unittest.TestCase):
    def test_source_explains_the_forwarded_trap(self):
        source = inspect.getsource(budget.settle)
        start = source.index('client_rejected')
        window = source[max(0, start - 1400):start + 200]
        self.assertIn('forwarded', window,
                      '要写出「转发前就变 forwarded，所以旧分支走不到」这个坑')

    def test_source_explains_why_429_is_excluded(self):
        source = inspect.getsource(budget.settle)
        start = source.index('client_rejected')
        window = source[max(0, start - 400):start + 400]
        self.assertIn('429', window)


if __name__ == '__main__':
    unittest.main()
