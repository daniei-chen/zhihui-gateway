"""v1.2.96：积压的 1486 条只能逐条核销，等于没人会真的去做。

## 现状

`budget_unknown` 积压 **1486 条**，最新一条停在 2026-11-15 04:02 —— 也就是说
v1.2.90 修好「拒绝路径不释放预留」之后**不再增长**，剩下的是纯历史遗留。

但核销只有单条端点：`POST /api/budgets/{request_id}/resolve`。

**1486 次人工操作不是「有耐心」，是没人会做。** 于是账本永远不干净、告警永远在响。

## 本版做的事

加批量端点 `POST /api/budgets/resolve-batch`。三条边界：

| 边界 | 取值 | 理由 |
| --- | --- | --- |
| 单次上限 | 200 条 | 一次误操作不能改掉整段历史 |
| `dry_run` | 支持 | **先看清清单再动手** |
| **不提供「按条件全库批量」** | — | 那会把逐条核对变成一键抹平 |

批量接口复用单条路径的 `expect_charged_zero=True` 安全闸：扫描与结清之间若有晚到
用量落库，那条会被跳过并保留为 unknown，而不是被「结清为零」抹掉已消耗配额。

**并发下它只会少改，不会多改。**
"""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

from server import budget
from server.routers import budgets


class _User(dict):
    pass


def _row(request_id, *, charged_tokens=0, charged_credit=0):
    return {'request_id': request_id, 'state': 'unknown',
            'charged_tokens': charged_tokens, 'charged_credit': charged_credit}


class _Guard:
    """替掉 authorization_guard 的上下文管理器。"""

    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _run(body, *, pending_rows, resolve_result=True):
    """跑 resolve_batch，返回 (结果, 被调用的 request_id 列表)。"""
    calls = []

    def fake_resolve(request_id, tokens, credit, actor, reason, **kw):
        calls.append((request_id, kw))
        return resolve_result

    with mock.patch.object(budget, 'unresolved', return_value=pending_rows), \
            mock.patch.object(budget, 'resolve', side_effect=fake_resolve), \
            mock.patch.object(budgets.security, 'authorization_guard', _Guard):
        result = budgets.resolve_batch(body, user=_User(username='tester'))
    return result, calls


class DryRunChangesNothingTests(unittest.TestCase):
    BODY = budgets.BatchResolution(
        request_ids=['a', 'b'], reason='历史积压批量核销', evidence='手工核对无用量凭据', dry_run=True)

    def test_dry_run_reports_but_does_not_write(self):
        result, calls = _run(self.BODY, pending_rows=[_row('a'), _row('b')])
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['would_resolve'], ['a', 'b'])
        self.assertEqual(calls, [], 'dry_run 绝不能调 resolve')

    def test_dry_run_lists_the_skips_too(self):
        body = budgets.BatchResolution(
            request_ids=['a', 'charged', 'gone'], reason='历史积压批量核销',
            evidence='手工核对无用量凭据', dry_run=True)
        result, _ = _run(body, pending_rows=[_row('a'), _row('charged', charged_tokens=5)])
        self.assertEqual(result['would_resolve'], ['a'])
        self.assertEqual(result['skipped_already_charged'], ['charged'])
        self.assertEqual(result['not_pending'], ['gone'])


class TheSafetyGatesAreKeptTests(unittest.TestCase):
    def test_already_charged_rows_are_never_zeroed(self):
        """已有 charged 的行不能「结清为零」——那会抹掉已消耗的配额。"""
        body = budgets.BatchResolution(
            request_ids=['charged'], reason='历史积压批量核销', evidence='手工核对无用量凭据')
        result, calls = _run(body, pending_rows=[_row('charged', charged_tokens=42)])
        self.assertEqual(calls, [])
        self.assertEqual(result['skipped_already_charged'], ['charged'])

    def test_it_asks_for_the_charged_zero_guard(self):
        """逐条走同一条安全闸：扫描与结清之间若有晚到用量，跳过而不是覆盖。"""
        body = budgets.BatchResolution(
            request_ids=['a'], reason='历史积压批量核销', evidence='手工核对无用量凭据')
        _result, calls = _run(body, pending_rows=[_row('a')])
        self.assertEqual(len(calls), 1)
        _rid, kwargs = calls[0]
        self.assertTrue(kwargs.get('expect_charged_zero'),
                        '必须声明「我读到 charged 为 0」，让事务内重核')

    def test_race_skips_are_reported_separately(self):
        body = budgets.BatchResolution(
            request_ids=['a'], reason='历史积压批量核销', evidence='手工核对无用量凭据')
        result, _calls = _run(body, pending_rows=[_row('a')], resolve_result=False)
        self.assertEqual(result['skipped_race'], ['a'])
        self.assertEqual(result['resolved'], [])


class BoundsAreEnforcedTests(unittest.TestCase):
    def test_more_than_200_is_rejected_by_the_model(self):
        with self.assertRaises(Exception):
            budgets.BatchResolution(
                request_ids=[f'id-{i}' for i in range(201)],
                reason='历史积压批量核销', evidence='手工核对无用量凭据')

    def test_exactly_200_is_allowed(self):
        body = budgets.BatchResolution(
            request_ids=[f'id-{i}' for i in range(200)],
            reason='历史积压批量核销', evidence='手工核对无用量凭据', dry_run=True)
        self.assertEqual(len(body.request_ids), 200)

    def test_empty_list_is_rejected(self):
        with self.assertRaises(Exception):
            budgets.BatchResolution(request_ids=[], reason='历史积压批量核销',
                                    evidence='手工核对无用量凭据')

    def test_short_reason_or_evidence_is_rejected(self):
        body = budgets.BatchResolution(
            request_ids=['a'], reason='x', evidence='手工核对无用量凭据')
        with self.assertRaises(Exception):
            _run(body, pending_rows=[_row('a')])

    def test_negative_tokens_are_rejected(self):
        with self.assertRaises(Exception):
            budgets.BatchResolution(request_ids=['a'], tokens=-1,
                                    reason='历史积压批量核销', evidence='手工核对无用量凭据')


class RouteOrderIsCorrectTests(unittest.TestCase):
    def test_batch_route_registers_before_the_id_route(self):
        """否则 `/resolve-batch` 会被当成一个 request_id。"""
        paths = [getattr(r, 'path', '') for r in budgets.router.routes]
        self.assertIn('/api/budgets/resolve-batch', paths)
        self.assertLess(paths.index('/api/budgets/resolve-batch'),
                        paths.index('/api/budgets/{request_id}/resolve'))


class NoCarpetSweepExistsTests(unittest.TestCase):
    def test_there_is_no_resolve_all_endpoint(self):
        """本接口刻意不提供「按条件全库批量」——那会把核对变成抹平。"""
        paths = [getattr(r, 'path', '') for r in budgets.router.routes]
        self.assertFalse([p for p in paths if p.endswith(('/resolve-all', '/bulk-resolve'))])

    def test_the_source_explains_why(self):
        source = inspect.getsource(budgets.resolve_batch)
        self.assertIn('抹平', source)
        self.assertIn('1486', source, '要写出积压规模，否则看不出为什么需要批量')

    def test_request_ids_are_required(self):
        """不允许「不指定 id 就按条件扫全库」。"""
        fields = budgets.BatchResolution.model_fields
        self.assertIn('request_ids', fields)
        self.assertTrue(fields['request_ids'].is_required())


if __name__ == '__main__':
    unittest.main()
