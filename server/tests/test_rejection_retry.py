"""上游「明确拒绝且未处理」的有界重试（2026-10-28）。

## 为什么要有这个

现场取证：上游把**内容拒绝**伪装成 `11102 模型不存在`，而且是 1–2 分钟的短窗口 ——
单日 421 次、涉及 31/32 个账号，同一分钟同一模型既有 200 也有被拒，同一账号同时
出现在成功侧与被拒侧。既不是账号缺模型，也不是模型映射没生效
（失败请求的 `mapped_model` 都是 `cn:deepseek-v4.1-flash`）。

这类响应意味着请求**没有被上游处理**：没有生成、没有 usage、没有计费 ——
与 `budget._REJECTED_MARKERS` 是同一判据。所以重放安全，可以在网关内补一次短重试，
把窗口挡在客户端之外（客户端默认要重试 8 次、间隔更长）。

本文件钉住三件事：

1. **判据只认「明确拒绝且未处理」**：带标记体的 400/502/503 才算；
   500 不算（不是同一类），无标记的 502 不算，空体不算；
2. **上限与钳制**：默认 2 次尝试，设置项可关（0/1），最大钳到 4，脏值回落默认；
3. **不在热路径上查库**：设置项有 60 秒缓存 —— 转发路径每条请求查一次库
   正是 U27 修掉的毛病，不能在这里再引入。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import db  # noqa: E402
from server.routers import gateway  # noqa: E402

from ._db_fixture import isolate_database  # noqa: E402


def _reset_cache() -> None:
    gateway._rejection_limit_cache = (0.0, -1)


class IsNotProcessedRejectionTest(unittest.TestCase):
    def test_no_healthy_account_on_503(self) -> None:
        body = '{"error":{"code":"no_healthy_account","gateway_hint":"upstream ..."}}'
        self.assertTrue(gateway._is_not_processed_rejection(503, body))

    def test_11102_on_400(self) -> None:
        body = '{"code":11102,"msg":"model [deepseek-v4.1-flash] service info not found"}'
        self.assertTrue(gateway._is_not_processed_rejection(400, body))

    def test_service_info_not_found_on_502(self) -> None:
        self.assertTrue(gateway._is_not_processed_rejection(
            502, 'model [x] service info not found'))

    def test_500_is_not_the_same_class(self) -> None:
        """500 可能是「已处理但失败」，不在可重放集合里。"""
        self.assertFalse(gateway._is_not_processed_rejection(
            500, '{"code":11102}'))

    def test_unrelated_body_is_not_a_rejection(self) -> None:
        self.assertFalse(gateway._is_not_processed_rejection(
            503, '{"error":{"message":"rate limited"}}'))

    def test_empty_body_is_not_a_rejection(self) -> None:
        self.assertFalse(gateway._is_not_processed_rejection(503, ''))
        self.assertFalse(gateway._is_not_processed_rejection(503, None))

    def test_2xx_never_counts(self) -> None:
        self.assertFalse(gateway._is_not_processed_rejection(
            200, '{"code":11102}'))

    def test_channel_rejections_never_replayed(self) -> None:
        for code in ('11128', '11-128', 'channel_check_rejected', 'channelcheckrejected'):
            for status in (400, 502, 503):
                self.assertFalse(gateway._is_not_processed_rejection(
                    status, json.dumps({'error': {'code': code, 'message':
                        'Illegal API invocation from an unapproved channel'}})))


class RetryLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        isolate_database(self)
        _reset_cache()
        self.addCleanup(_reset_cache)

    def test_default_is_two_attempts(self) -> None:
        self.assertEqual(gateway._rejection_retry_limit(), 2)

    def test_setting_can_disable(self) -> None:
        for value in (0, 1):
            db.set_setting('upstream_rejection_retry', value)
            _reset_cache()
            self.assertEqual(gateway._rejection_retry_limit(), value)

    def test_setting_is_clamped(self) -> None:
        db.set_setting('upstream_rejection_retry', 99)
        _reset_cache()
        self.assertEqual(gateway._rejection_retry_limit(), gateway._REJECTION_RETRY_MAX)

    def test_dirty_setting_falls_back_to_default(self) -> None:
        db.set_setting('upstream_rejection_retry', 'not-a-number')
        _reset_cache()
        self.assertEqual(gateway._rejection_retry_limit(), 2)

    def test_boolean_setting_falls_back_to_default(self) -> None:
        db.set_setting('upstream_rejection_retry', True)
        _reset_cache()
        self.assertEqual(gateway._rejection_retry_limit(), 2)

    def test_setting_is_cached_not_read_per_request(self) -> None:
        """改设置后、缓存过期前仍是旧值 —— 证明没有每条请求查库。"""
        db.set_setting('upstream_rejection_retry', 3)
        _reset_cache()
        self.assertEqual(gateway._rejection_retry_limit(), 3)
        db.set_setting('upstream_rejection_retry', 1)
        self.assertEqual(gateway._rejection_retry_limit(), 3, '60 秒内应命中缓存')

    def test_cache_expires(self) -> None:
        db.set_setting('upstream_rejection_retry', 3)
        _reset_cache()
        self.assertEqual(gateway._rejection_retry_limit(), 3)
        db.set_setting('upstream_rejection_retry', 1)
        # 把缓存时间推到 TTL 之外
        gateway._rejection_limit_cache = (
            gateway._rejection_limit_cache[0] - gateway._REJECTION_LIMIT_TTL_S - 1,
            gateway._rejection_limit_cache[1])
        self.assertEqual(gateway._rejection_retry_limit(), 1)


if __name__ == '__main__':
    unittest.main()
