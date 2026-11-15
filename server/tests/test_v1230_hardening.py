"""v1.2.30 加固回归：来源地址与日志文本的注入面、传输池等待预算、
运行时指标的裁剪频率。

这些性质都是"看起来没问题、实际每请求都在付代价"的那一类：
它们不会让功能测试变红，但会在压力下变成日志伪造、请求挂住或全站 503。
"""
import os
import unittest
from unittest.mock import patch

from fastapi import Request

from server import config, iputil, runtimemetrics, security


def _request(peer='127.0.0.1', headers=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({'type': 'http', 'method': 'GET', 'path': '/api/login',
                    'headers': raw, 'client': (peer, 1234),
                    'query_string': b'', 'server': ('testserver', 80),
                    'scheme': 'http', 'http_version': '1.1'})


class ClientIpInjectionTest(unittest.TestCase):
    def test_control_characters_in_real_ip_are_rejected(self):
        """反代把客户端头透传成 X-Real-IP 时，换行不能进日志。"""
        with patch.object(config, 'TRUST_PROXY', True), \
                patch.object(config, 'TRUSTED_PROXY_CIDRS', ['127.0.0.0/8']), \
                patch.object(config, 'TRUSTED_PROXY_HOPS', 1):
            forged = '1.2.3.4\n2026-11-14 12:00:00 INFO workbuddy.auth: 登录成功: admin'
            request = _request(headers={'x-real-ip': forged})
            self.assertEqual(iputil.client_ip(request), '127.0.0.1')

    def test_escape_sequence_in_forwarded_for_is_rejected(self):
        with patch.object(config, 'TRUST_PROXY', True), \
                patch.object(config, 'TRUSTED_PROXY_CIDRS', ['127.0.0.0/8']), \
                patch.object(config, 'TRUSTED_PROXY_HOPS', 1):
            request = _request(headers={'x-forwarded-for': '9.9.9.9\x1b[31mred'})
            self.assertEqual(iputil.client_ip(request), '127.0.0.1')

    def test_ordinary_addresses_still_parse(self):
        self.assertEqual(iputil._clean_ip('203.0.113.7:443'), '203.0.113.7')
        self.assertEqual(iputil._clean_ip('[2001:db8::1]:8443'), '2001:db8::1')
        self.assertEqual(iputil._clean_ip('2001:db8::1'), '2001:db8::1')
        self.assertEqual(iputil._clean_ip('  198.51.100.9  '), '198.51.100.9')
        self.assertEqual(iputil._clean_ip('not-an-address'), '')

    def test_cidr_parsing_is_cached_not_rebuilt_per_call(self):
        iputil._parse_network.cache_clear()
        with patch('ipaddress.ip_network', wraps=iputil.ipaddress.ip_network) as spy:
            for _ in range(5):
                self.assertTrue(iputil.ip_matches('10.1.2.3', '10.0.0.0/8'))
        self.assertEqual(spy.call_count, 1, '同一 CIDR 不应每次重新解析')


class TransportPoolBudgetTest(unittest.TestCase):
    def test_pool_wait_is_bounded_independently_of_total_timeout(self):
        """`Timeout(300, connect=5)` 会把 pool 也设成 300s —— 等连接不该按总预算算。"""
        import httpx
        bounded = config._bounded_pool_timeout(httpx.Timeout(600, connect=5))
        self.assertEqual(bounded.pool, config._POOL_WAIT_MAX_S)
        self.assertEqual(bounded.connect, 5)
        self.assertEqual(bounded.read, 600)
        self.assertEqual(bounded.write, 600)

    def test_already_short_pool_timeout_is_left_alone(self):
        import httpx
        original = httpx.Timeout(connect=1, read=2, write=3, pool=2.5)
        self.assertIs(config._bounded_pool_timeout(original), original)

    def test_http_client_applies_the_bound(self):
        client = config.http_client(600, connect=5)
        self.assertEqual(client.timeout.pool, config._POOL_WAIT_MAX_S)
        self.assertEqual(client.timeout.read, 600)
        self.assertEqual(client.timeout.connect, 5)


class LogTextTest(unittest.TestCase):
    def test_newlines_and_escapes_are_neutralised(self):
        self.assertEqual(security.log_text('admin\nINFO fake line'), 'admin?INFO fake line')
        # \r、\t、ESC 都是控制字符，替换为 '?'；可见的 [31m 原样保留。
        self.assertEqual(security.log_text('\r\t\x1b[31m'), '???[31m')
        self.assertEqual(security.log_text('normal-user'), 'normal-user')

    def test_length_is_capped(self):
        self.assertEqual(len(security.log_text('x' * 500)), 128)
        self.assertEqual(len(security.log_text('x' * 500, 8)), 8)

    def test_empty_values_are_empty(self):
        self.assertEqual(security.log_text(None), '')
        self.assertEqual(security.log_text(''), '')


class MetricsPruneTest(unittest.TestCase):
    def test_prune_runs_once_per_minute(self):
        clock = {'now': 0.0}
        metrics = runtimemetrics.Metrics(clock=lambda: clock['now'])
        runs = []
        original = metrics._drop_expired_minutes

        def counting(cutoff):
            runs.append(cutoff)
            return original(cutoff)

        metrics._drop_expired_minutes = counting
        for _ in range(5):
            metrics.observe(protocol='http', traffic='other', http_status=200,
                            terminal='complete', latency_ms=1.0)
        self.assertEqual(len(runs), 1, '同一分钟内重复遍历分钟桶是每请求的固定浪费')
        clock['now'] = 61.0
        metrics.observe(protocol='http', traffic='other', http_status=200,
                        terminal='complete', latency_ms=1.0)
        self.assertEqual(len(runs), 2)

    def test_old_minutes_still_get_dropped(self):
        clock = {'now': 0.0}
        metrics = runtimemetrics.Metrics(clock=lambda: clock['now'])
        metrics.observe(protocol='http', traffic='other', http_status=200,
                        terminal='complete', latency_ms=1.0)
        first = int(clock['now'] // 60)
        self.assertIn(first, metrics.minutes)
        clock['now'] = (first + 1441) * 60.0
        metrics.observe(protocol='http', traffic='other', http_status=200,
                        terminal='complete', latency_ms=1.0)
        self.assertNotIn(first, metrics.minutes)


class LoginBodyLimitTest(unittest.TestCase):
    """未鉴权的 `/api/login` 不能沿用 16 MiB 的管理面上限。"""

    def test_login_limit_is_small_and_below_the_api_default(self):
        from server import main
        self.assertEqual(main.API_PATH_LIMITS['/api/login'], 64 * 1024)
        self.assertLess(main.LOGIN_BODY_LIMIT, main.MAX_API_BODY_BYTES)

    def test_bodyguard_reads_the_same_limit(self):
        """声明长度预检与实际读取必须用同一份上限，否则各自为政。"""
        from server import main
        from server.bodyguard import APIBodyGuard
        guard = APIBodyGuard(None, maximum=main.MAX_API_BODY_BYTES,
                             path_limits=main.API_PATH_LIMITS)
        self.assertEqual(guard._limit_for('/api/login'), main.LOGIN_BODY_LIMIT)
        self.assertEqual(guard._limit_for('/api/login/'), main.LOGIN_BODY_LIMIT)
        self.assertEqual(guard._limit_for('/api/keys'), main.MAX_API_BODY_BYTES)


if __name__ == '__main__':
    unittest.main()
