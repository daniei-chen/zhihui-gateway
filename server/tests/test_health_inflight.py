"""`GET /api/health/inflight`：发布脚本判断候选实例在途归零的只读探针。

2026-11-28 审计 D-04：蓝绿发布切回旧实例之后，候选实例上还留着切流前接下的请求。
停它之前必须确认这些请求跑完了 —— 否则 `TimeoutStopSec` 一到就是 SIGKILL，而流式
截断通常不落 5xx，发布脚本的「窗口零 5xx」根本看不见它。

在途数只有这里说得准：`request_outcomes` 只有终态；`/metrics` 要采集器令牌；
`/api/readyz/detail` 要管理员身份。发布脚本是 root 的 shell，没有会话凭据。

本测试守四件事：
1. 只回环可读，非回环 403（与 `/api/health/alerts` 同一套守卫）；
2. 报的数字来自 `drain` 的请求级跟踪，`in_flight` 是按类别的明细；
3. 探针自己**不**计入在途数 —— 它必须留在 DrainMiddleware 的控制路径名单里，
   否则「归零」永远等不到；
4. draining 期间仍然答得上来（发布脚本要在排空窗口里继续看这个数）。
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from server import drain, main

LOOPBACK = ('127.0.0.1', 40000)
EXTERNAL = ('203.0.113.7', 40000)
PATH = '/api/health/inflight'


class HealthInflightProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        drain.reset_for_tests()
        self.client = TestClient(main.app, client=LOOPBACK)
        self.addCleanup(drain.reset_for_tests)

    def test_loopback_reads_the_counts(self):
        response = self.client.get(PATH)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['service'], 'zhihui-gateway')
        self.assertEqual(body['total_in_flight'], 0)
        self.assertEqual(body['in_flight'], {})
        self.assertIs(body['draining'], False)
        self.assertIn('no-store', response.headers.get('cache-control', ''))

    def test_the_probe_does_not_count_itself(self):
        # 探针若被 DrainMiddleware 计入，计数永远不会是 0，发布脚本就会一直等到上限。
        for _ in range(3):
            self.assertEqual(self.client.get(PATH).json()['total_in_flight'], 0)
        self.assertEqual(drain.total_in_flight(), 0)

    def test_in_flight_requests_are_visible_by_kind(self):
        with drain.track('inference_stream'):
            body = self.client.get(PATH).json()
            self.assertEqual(body['total_in_flight'], 1)
            self.assertEqual(body['in_flight'], {'inference_stream': 1})
        self.assertEqual(self.client.get(PATH).json()['total_in_flight'], 0)

    def test_non_loopback_callers_are_refused(self):
        response = TestClient(main.app, client=EXTERNAL).get(PATH)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json(), {'error': 'loopback only'})

    def test_the_probe_stays_readable_while_draining(self):
        drain.begin()
        body = self.client.get(PATH).json()
        self.assertIs(body['draining'], True)
        # 对照：业务路径在 draining 期间被拒，探针不受影响。
        self.assertEqual(self.client.get('/api/keys').status_code, 503)
        self.assertEqual(self.client.get(PATH).status_code, 200)


if __name__ == '__main__':
    unittest.main()
