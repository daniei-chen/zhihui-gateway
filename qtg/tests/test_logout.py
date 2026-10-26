"""退出闭环回归测试（K03，2026-10-23 S0）。

背景：qoder/trae 平台下前端调用 `/api/logout` 会按平台头落到 QTG，
原实现只删 `qtg_session`，浏览器里的 `wb_session` 仍然有效 —— 用户以为退出了，
实际仍以原身份登录。修复后 QTG 把退出转发给 Manager，并把 Manager 的
删 cookie 头合并回响应；Manager 不可达时降级（仍删本地会话，但标记 degraded）。

本文件锁定：
1. 带 wb_session 的退出必须调用 Manager 转发，并把删除 wb_session 的 Set-Cookie 合并回来；
2. Manager 转发失败时返回 degraded=True，且仍删除本地 qtg_session；
3. 没有 wb_session 时不发起无意义转发；
4. 重复退出幂等。
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from test_api import ApiTestBase

WB_SET_COOKIE = "wb_session=; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; path=/"


class CompatLogoutTest(ApiTestBase):
    def test_forwards_to_manager_and_merges_cookie(self) -> None:
        with patch("qtg.routers.compat.forward_logout", new=AsyncMock(return_value=(True, [WB_SET_COOKIE]))) as m:
            r = self.client.post(
                "/api/logout", cookies={"wb_session": "signed-value", "qtg_session": "old"}
            )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("ok"))
        self.assertFalse(r.json().get("degraded"))
        m.assert_awaited_once()
        headers = [h.lower() for h in r.headers.get_list("set-cookie")]
        self.assertTrue(any(h.startswith("wb_session=") for h in headers), headers)
        self.assertTrue(any(h.startswith("qtg_session=") for h in headers), headers)

    def test_degrades_when_manager_unreachable(self) -> None:
        with patch("qtg.routers.compat.forward_logout", new=AsyncMock(return_value=(False, []))):
            r = self.client.post("/api/logout", cookies={"wb_session": "signed-value"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("degraded"))
        headers = [h.lower() for h in r.headers.get_list("set-cookie")]
        self.assertTrue(any(h.startswith("qtg_session=") for h in headers))

    def test_skips_forward_without_wb_cookie(self) -> None:
        with patch("qtg.routers.compat.forward_logout", new=AsyncMock(return_value=(True, []))) as m:
            r = self.client.post("/api/logout", cookies={"qtg_session": "old"})
        self.assertEqual(r.status_code, 200)
        m.assert_not_awaited()

    def test_idempotent(self) -> None:
        with patch("qtg.routers.compat.forward_logout", new=AsyncMock(return_value=(True, [WB_SET_COOKIE]))):
            first = self.client.post("/api/logout", cookies={"wb_session": "v"})
            second = self.client.post("/api/logout", cookies={"wb_session": "v"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)


class NativeLogoutTest(ApiTestBase):
    def test_forwards_to_manager(self) -> None:
        with patch("qtg.routers.auth.forward_logout", new=AsyncMock(return_value=(True, [WB_SET_COOKIE]))) as m:
            r = self.client.post("/api/v1/auth/logout", cookies={"wb_session": "signed-value"})
        self.assertEqual(r.status_code, 200)
        m.assert_awaited_once()
        headers = [h.lower() for h in r.headers.get_list("set-cookie")]
        self.assertTrue(any(h.startswith("wb_session=") for h in headers), headers)

    def test_degrades_when_manager_unreachable(self) -> None:
        with patch("qtg.routers.auth.forward_logout", new=AsyncMock(return_value=(False, []))):
            r = self.client.post("/api/v1/auth/logout", cookies={"wb_session": "signed-value"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("degraded"))


if __name__ == "__main__":
    unittest.main()
