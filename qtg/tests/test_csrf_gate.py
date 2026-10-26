"""CSRF 中间件回归测试（K07 子项，2026-10-23 S0 发现）。

背景：`qtg/app/qtg/main.py` 的 `_csrf_gate` 曾调用 `rt.credentials.check_origin`，
但 `check_origin` 定义在 `security.Auth` 上；`AttributeError` 被 `except Exception`
吞掉后，所有非 Bearer 的 `/api/*` 写请求一律 403。既有测试
`test_api.py::test_csrf_origin_check_for_cookie` 断言"伪造 Origin 得 403"，
在该缺陷下**假通过**（它无法区分"被 CSRF 拒绝"与"因异常被拒"）。

本文件锁定四条行为：
1. Cookie + 合法 Origin 必须放行（不能再假 403）；
2. Cookie + 无 Origin 必须 403；
3. Cookie + 伪造 Origin 必须 403；
4. Bearer 调用方不受影响（无 Origin 也可通过）。
"""

from __future__ import annotations

import unittest

from test_api import ApiTestBase, TOKEN

WRITE_PATH = "/api/v1/actions/limits"
VALID_ORIGIN = "http://testserver"  # TestClient 默认 Host=testserver


class CsrfGateTest(ApiTestBase):
    def _login_cookie(self) -> None:
        r = self.client.post("/api/v1/auth/login", json={"password": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertIn("qtg_session", self.client.cookies)

    def test_cookie_with_valid_origin_is_not_rejected(self) -> None:
        self._login_cookie()
        r = self.client.post(
            WRITE_PATH,
            json={"enabled": True},
            headers={"Origin": VALID_ORIGIN},
        )
        self.assertNotEqual(r.status_code, 403, f"合法 Origin 仍被拒: {r.text}")
        self.assertNotIn("csrf_forbidden", r.text)

    def test_cookie_without_origin_rejected(self) -> None:
        self._login_cookie()
        r = self.client.post(WRITE_PATH, json={"enabled": True})
        self.assertEqual(r.status_code, 403)
        self.assertIn("csrf_forbidden", r.text)

    def test_cookie_with_foreign_origin_rejected(self) -> None:
        self._login_cookie()
        r = self.client.post(
            WRITE_PATH,
            json={"enabled": True},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertIn("csrf_forbidden", r.text)

    def test_bearer_without_origin_passes_gate(self) -> None:
        r = self.client.post(WRITE_PATH, json={"enabled": True}, headers=self.auth)
        self.assertNotEqual(r.status_code, 403, f"Bearer 被 CSRF 误拦: {r.text}")
        self.assertNotIn("csrf_forbidden", r.text)

    def test_rejection_reason_is_csrf_not_attribute_error(self) -> None:
        """防止回退：403 必须来自 Forbidden，而不是被吞掉的 AttributeError。"""
        self._login_cookie()
        r = self.client.post(WRITE_PATH, json={"enabled": True})
        self.assertEqual(r.status_code, 403)
        detail = (r.json().get("detail") or "")
        self.assertNotIn("AttributeError", detail)
        self.assertNotIn("check_origin", detail)


if __name__ == "__main__":
    unittest.main()
