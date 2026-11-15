"""v1.2.64：准入快照的分支键集不一致，害得三个指标静默消失。

## 问题

`admission_snapshot()` 有两条返回路径：

| 分支 | 何时走 | 返回的键 |
| --- | --- | --- |
| 共享准入开启 | 生产正是这种 | 少了 `key_slots` / `queue_timeouts` / `queue_overflows` |
| 未开共享 | 开发/单机 | 齐全 |

`/metrics` 用下标取这十个字段，配合外层一个宽泛的 `except`，于是「少三个键」变成
「后面所有指标一起消失」。生产日志里只有一行：

    WARNING Metrics admission snapshot unavailable (KeyError)

**排队超时、排队溢出、键槽占用三个指标长期没有输出** —— 而它们恰恰是回答
「为什么 503」时最该看的数字。

## 修法

1. 数据源：两条分支返回**同一组键**，缺数据时给 0；
2. 消费端：逐项 `.get()`，以后任何一侧再漂移，最多缺一个指标，不会整段静默。
"""
from __future__ import annotations

import pathlib
import unittest
from unittest import mock

from server import observability

#: `/metrics` 会读的全部键。
EXPECTED = {
    'active', 'large', 'maximum', 'inflight_body_bytes', 'maximum_body_bytes',
    'capacity_rejections', 'key_slots', 'queued', 'queue_timeouts', 'queue_overflows',
}


class _FakeGate:
    pending = 3
    timeouts = 7
    overflows = 2


class _FakeInstance:
    active = 2
    preauth_active = 1
    large = 1
    maximum = 64
    large_maximum = 28
    inflight_body_bytes = 1234
    maximum_body_bytes = 4096
    capacity_rejections = 5
    fair_gate = _FakeGate()


class _FakeShared:
    def __init__(self, payload):
        self.payload = payload

    def snapshot(self):
        return dict(self.payload)

    def probe(self):
        return dict(self.payload)

    def close(self):
        pass


SHARED_PAYLOAD = {'leases': 2, 'active': 1, 'large': 0, 'body_bytes': 999,
                  'preauth': 0, 'queued': 1, 'scope': 'same_host_shared',
                  'healthy': True}


class BothBranchesReturnTheSameKeysTests(unittest.TestCase):
    def _with_instance(self, instance):
        return mock.patch.object(observability, '_middleware_ref', instance)

    def test_the_shared_branch_returns_every_key_metrics_needs(self):
        instance = _FakeInstance()
        instance.shared = _FakeShared(SHARED_PAYLOAD)
        with self._with_instance(instance):
            snap = observability.admission_snapshot()
        missing = EXPECTED - set(snap)
        self.assertFalse(missing, f'共享分支缺少这些键：{sorted(missing)}')

    def test_the_local_branch_returns_every_key_metrics_needs(self):
        instance = _FakeInstance()
        instance.shared = None
        with self._with_instance(instance):
            snap = observability.admission_snapshot()
        missing = EXPECTED - set(snap)
        self.assertFalse(missing, f'本地分支缺少这些键：{sorted(missing)}')

    def test_the_shared_branch_still_exposes_the_shared_numbers(self):
        """补键不能把共享侧的真实数字盖掉。"""
        instance = _FakeInstance()
        instance.shared = _FakeShared(SHARED_PAYLOAD)
        with self._with_instance(instance):
            snap = observability.admission_snapshot()
        self.assertEqual(snap['active'], 1, '共享分支的 active 应来自协调库')
        self.assertEqual(snap['queued'], 1)
        self.assertEqual(snap['inflight_body_bytes'], 999)

    def test_the_queue_counters_come_from_the_fair_gate(self):
        instance = _FakeInstance()
        instance.shared = None
        with self._with_instance(instance):
            snap = observability.admission_snapshot()
        self.assertEqual(snap['queue_timeouts'], 7)
        self.assertEqual(snap['queue_overflows'], 2)

    def test_coordination_failure_still_returns_the_keys(self):
        """协调库不可用时返回的是降级结构，也必须有这几个键。"""
        instance = _FakeInstance()

        class Broken:
            def snapshot(self):
                raise RuntimeError('db gone')

        instance.shared = Broken()
        with self._with_instance(instance):
            snap = observability.admission_snapshot()
        self.assertFalse(snap['healthy'])
        self.assertEqual(snap['active'], None)
        self.assertIn('key_slots', snap, '降级结构里也要有排队相关的键')

    def test_no_instance_returns_none(self):
        with mock.patch.object(observability, '_middleware_ref', None):
            self.assertIsNone(observability.admission_snapshot())


class MetricsEndpointToleratesMissingKeysTests(unittest.TestCase):
    """消费端兜底：缺一个键不该打掉整段指标。"""

    def setUp(self):
        self.source = (pathlib.Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8')
        start = self.source.index('def prometheus_metrics')
        end = self.source.index('\n    return PlainTextResponse', start)
        self.block = self.source[start:end]

    def test_it_no_longer_subscripts_the_snapshot_directly(self):
        for key in ('active', 'key_slots', 'queued', 'queue_timeouts', 'queue_overflows'):
            self.assertNotIn(f"snap['{key}']", self.block,
                             f'又改回下标访问了：{key} 缺一个就整段静默')

    def test_it_reads_every_field_through_the_tolerant_helper(self):
        for key in ('active', 'large', 'maximum', 'key_slots', 'queued',
                    'queue_timeouts', 'queue_overflows'):
            self.assertIn(f"_g('{key}')", self.block)

    def test_all_ten_metrics_are_still_emitted(self):
        for name in ('workbuddy_gateway_inflight', 'workbuddy_gateway_inflight_large',
                     'workbuddy_gateway_inflight_max', 'workbuddy_gateway_inflight_body_bytes',
                     'workbuddy_gateway_inflight_body_bytes_max',
                     'workbuddy_gateway_capacity_rejections_total',
                     'workbuddy_gateway_key_slots', 'workbuddy_gateway_queued',
                     'workbuddy_gateway_queue_timeouts_total',
                     'workbuddy_gateway_queue_overflows_total'):
            self.assertIn(name, self.block, f'指标 {name} 不见了')


if __name__ == '__main__':
    unittest.main()
