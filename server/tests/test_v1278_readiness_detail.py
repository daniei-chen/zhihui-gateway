"""v1.2.78：不就绪时看不到原因。

## 问题

`/readyz` 只返回 `{"ok": true}`。那本身是对的 —— 探针可能每秒打很多次，
不能每次都去碰数据库（它读的是监控循环每 30 秒刷新一次的缓存）。

但**不就绪时只看到一个 false，排障无从下手**：到底是

- 数据库不可写？
- 账本通道降级？
- 回执有积压？还是
- 正在排空？

这四种情况的处置完全不同，而端点把它们压成了一个布尔值。

## 修法

新增 `/api/readyz/detail`，跑一次完整判定并把每一项判据带出来。

**要认证**：它暴露内部状态，且会真的去探数据库可写性 —— 不能让它成为匿名
可触发的高频写压力。
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from server import main


def _route(path):
    hits = [r for r in main.app.routes
            if isinstance(r, APIRoute) and getattr(r, 'path', '') == path]
    return hits


class TheDetailRouteExistsTests(unittest.TestCase):
    def test_it_is_registered_exactly_once(self):
        self.assertEqual(len(_route('/api/readyz/detail')), 1)

    def test_it_points_at_the_detail_handler(self):
        """2026-11-15 踩过装饰器挂错的坑，这里直接看注册结果而不看源码文本。"""
        hits = _route('/api/readyz/detail')
        self.assertEqual(hits[0].endpoint.__name__, 'readiness_detail')

    def test_the_probe_endpoint_is_untouched(self):
        hits = _route('/readyz')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].endpoint.__name__, 'public_ready')

    def test_the_detail_handler_is_not_the_probe_handler(self):
        self.assertIsNot(_route('/api/readyz/detail')[0].endpoint,
                         _route('/readyz')[0].endpoint)


class ItRequiresAuthenticationTests(unittest.TestCase):
    def test_anonymous_is_rejected(self):
        client = TestClient(main.app)
        response = client.get('/api/readyz/detail')
        self.assertIn(response.status_code, (401, 403),
                      f'匿名访问不该成功，实际 {response.status_code}')

    def test_the_probe_stays_open(self):
        """`/readyz` 必须保持无需认证 —— 负载均衡器要用它。"""
        client = TestClient(main.app)
        with mock.patch('server.operational.public_readiness', return_value=True):
            response = client.get('/readyz')
        self.assertEqual(response.status_code, 200)


class ItReturnsTheFullDiagnosisTests(unittest.TestCase):
    def test_every_decision_field_is_present(self):
        """不就绪时要知道是哪一项 —— 这四个的处置完全不同。"""
        from server import operational
        with mock.patch.object(operational, 'database_writable', return_value=True), \
             mock.patch.object(operational, '_ledger_backlog', return_value=0), \
             mock.patch.object(operational, '_ledger_channel_ok', return_value=True), \
             mock.patch('server.drain.is_draining', return_value=False):
            result = operational.readiness()
        for field in ('ok', 'database_write', 'ledger_write', 'ledger_backlog',
                      'ledger_spool'):
            self.assertIn(field, result, f'缺少判据 {field}')

    def test_it_shows_which_criterion_failed(self):
        from server import operational
        with mock.patch.object(operational, 'database_writable', return_value=False), \
             mock.patch.object(operational, '_ledger_backlog', return_value=0), \
             mock.patch.object(operational, '_ledger_channel_ok', return_value=True), \
             mock.patch('server.drain.is_draining', return_value=False):
            result = operational.readiness()
        self.assertFalse(result['ok'])
        self.assertFalse(result['database_write'], '应当指出是数据库不可写')


class TheProbeStaysCheapTests(unittest.TestCase):
    """`/readyz` 不能因为这次改动变得昂贵。"""

    def test_public_readiness_reads_cache_only(self):
        import inspect
        from server import operational
        source = inspect.getsource(operational.public_readiness)
        self.assertIn('_cached_readiness', source)
        for expensive in ('database_writable', 'query_one', 'execute'):
            self.assertNotIn(expensive, source,
                             f'探测端点不该做 {expensive} —— 它会每秒被打很多次')


if __name__ == '__main__':
    unittest.main()
