"""K07 完整版：Cookie 请求被内部 Bearer 绕过 CSRF 检查（修复 + 回归）。

## 被锁定的行为

旧 `_csrf_gate`：请求头里只要有 `Authorization: Bearer ...` 就跳过 Origin 校验。
生产 Nginx 为了"让浏览器请求能过 QTG 闸门"会给**每个** `/api/` 请求注入一枚内部
Bearer，于是浏览器写请求实际没有 CSRF 保护。

新规则（本文件逐条锁定）：**看有没有浏览器会话 Cookie，不看有没有 Bearer**。

1. Cookie + 合法 Origin → 放行；
2. Cookie + 无 Origin / 错 Origin / 相似域名 / 不同端口 / 不同 scheme / `null` /
   多个 Origin 头 → 403 `csrf_forbidden`（并给出具体 reason）；
3. Cookie + 内部 Bearer（模拟 Nginx 注入）→ **仍然**必须校验 Origin（K07 核心）；
4. 内部 Bearer（无 Cookie，机器调用）→ 放行（机器没有浏览器环境）；
5. 外部伪造 Bearer → 认证失败 401，不能借"Bearer 存在"获得任何豁免；
6. 匿名写请求 → 401（不是 403）；
7. GET 等安全方法不受 CSRF 检查影响。
"""

from __future__ import annotations

import unittest

from identity_support import GOOD_ORIGIN, IdentityTestBase

WRITE_PATH = "/api/v1/actions/limits"
WRITE_BODY = {"enabled": True}


class CsrfCookieTest(IdentityTestBase):
    """浏览器会话（Cookie）场景。"""

    def test_cookie_with_valid_origin_passes(self) -> None:
        self.login()
        r = self.client.post(WRITE_PATH, json=WRITE_BODY, headers={"Origin": GOOD_ORIGIN})
        self.assertEqual(r.status_code, 200, r.text)

    def test_cookie_with_referer_only_passes(self) -> None:
        self.login()
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY,
            headers={"Referer": f"{GOOD_ORIGIN}/dashboard/keys?tab=1"},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_cookie_without_origin_rejected(self) -> None:
        self.login()
        r = self.client.post(WRITE_PATH, json=WRITE_BODY)
        self.assertEqual(r.status_code, 403, r.text)
        body = r.json()
        self.assertEqual(body["code"], "csrf_forbidden")
        self.assertEqual(body["reason"], "csrf_origin_missing")

    def test_cookie_with_foreign_origin_rejected(self) -> None:
        self.login()
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY, headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["reason"], "csrf_origin_mismatch")

    def test_cookie_with_similar_domain_rejected(self) -> None:
        """旧实现 `host not in source` 的子串匹配对相似域名无效。"""
        self.login()
        for bad in (
            "http://testserver.evil.example",
            "http://evil-testserver",
            "http://testserverx",
        ):
            with self.subTest(origin=bad):
                r = self.client.post(
                    WRITE_PATH, json=WRITE_BODY, headers={"Origin": bad}
                )
                self.assertEqual(r.status_code, 403, f"{bad}: {r.text}")
                self.assertEqual(r.json()["reason"], "csrf_origin_mismatch")

    def test_cookie_with_different_port_rejected(self) -> None:
        self.login()
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY, headers={"Origin": "http://testserver:8080"}
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["reason"], "csrf_origin_mismatch")

    def test_cookie_with_different_scheme_rejected(self) -> None:
        self.login()
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY, headers={"Origin": "https://testserver"}
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["reason"], "csrf_origin_mismatch")

    def test_cookie_with_userinfo_origin_rejected(self) -> None:
        self.login()
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY,
            headers={"Origin": "http://testserver@evil.example"},
        )
        self.assertEqual(r.status_code, 403)
        # userinfo 形态在解析阶段就被判 malformed（比 mismatch 更早、更准确）
        self.assertIn(r.json()["reason"], ("csrf_origin_mismatch", "csrf_origin_malformed"))

    def test_cookie_with_null_origin_rejected(self) -> None:
        self.login()
        r = self.client.post(WRITE_PATH, json=WRITE_BODY, headers={"Origin": "null"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["reason"], "csrf_origin_malformed")

    def test_referer_with_query_smuggling_rejected(self) -> None:
        """`https://evil.example/?u=http://testserver` 这类拼 host 的 Referer 不算合法来源。"""
        self.login()
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY,
            headers={"Referer": "https://evil.example/?u=http://testserver"},
        )
        self.assertEqual(r.status_code, 403)

    def test_forwarded_proto_from_trusted_edge_is_honored(self) -> None:
        """可信边缘声明 https 时，期望来源按 https 比：http 的 Origin 必须被拒。"""
        self.login()
        ok = self.client.post(
            WRITE_PATH, json=WRITE_BODY,
            headers={"Origin": "https://testserver", "X-Forwarded-Proto": "https"},
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        bad = self.client.post(
            WRITE_PATH, json=WRITE_BODY,
            headers={"Origin": "http://testserver", "X-Forwarded-Proto": "https"},
        )
        self.assertEqual(bad.status_code, 403)

    def test_allowed_origins_whitelist(self) -> None:
        """反代域名与面板域名不一致时，用 QTG_ALLOWED_ORIGINS 显式豁免。"""
        from qtg.security import parse_origin

        self.login()
        self.rt.auth.allowed_origins.add(parse_origin("https://panel.example.com"))
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY, headers={"Origin": "https://panel.example.com"}
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_safe_methods_are_not_checked(self) -> None:
        self.login()
        r = self.client.get("/api/v1/keys")
        self.assertEqual(r.status_code, 200, r.text)


class CsrfInternalBearerTest(IdentityTestBase):
    """K07 核心：Nginx 注入的内部 Bearer 不能取消浏览器写请求保护。"""

    def test_cookie_plus_internal_bearer_still_requires_origin(self) -> None:
        """浏览器请求同时带 Cookie 与内部凭据、但没有 Origin → 必须 403。

        旧实现：`Authorization` 以 `bearer ` 开头 → 直接跳过 → 返回 200（漏洞）。
        """
        self.login()
        for headers in (
            self.internal,                                  # 内部服务凭据头
            {**self.internal, **self.auth},                  # 内部凭据 + 管理令牌
            self.auth,                                      # 只有管理令牌（用户手工带上）
        ):
            with self.subTest(headers=sorted(headers)):
                r = self.client.post(WRITE_PATH, json=WRITE_BODY, headers=headers)
                self.assertEqual(r.status_code, 403, f"{r.status_code}: {r.text}")
                self.assertEqual(r.json()["code"], "csrf_forbidden")

    def test_cookie_plus_internal_bearer_with_valid_origin_passes(self) -> None:
        self.login()
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY,
            headers={**self.internal, "Origin": GOOD_ORIGIN},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_machine_caller_without_cookie_passes(self) -> None:
        """无 Cookie 的内部服务调用没有浏览器环境，不应被 CSRF 拦。"""
        r = self.client.post(WRITE_PATH, json=WRITE_BODY, headers=self.internal)
        self.assertEqual(r.status_code, 200, r.text)

    def test_machine_caller_with_foreign_origin_rejected(self) -> None:
        """机器调用不该带 Origin；带了跨站 Origin 说明可能是被注入的浏览器上下文。"""
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY,
            headers={**self.internal, "Origin": "https://evil.example"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["reason"], "csrf_cross_origin_machine")

    def test_cookie_without_origin_but_with_identity_header_rejected(self) -> None:
        """带签名身份断言 + Cookie、但没有 Origin：仍按浏览器会话处理 → 403。"""
        self.login()
        headers = {"X-QTG-Identity": self.signed_identity()}
        r = self.client.post(WRITE_PATH, json=WRITE_BODY, headers=headers)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "csrf_forbidden")


class CsrfForgedBearerTest(IdentityTestBase):
    """外部伪造 Bearer：既不能通过认证，也不能借 Bearer 获得 CSRF 豁免。"""

    def test_forged_bearer_rejected_without_cookie(self) -> None:
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY,
            headers={"Authorization": "Bearer forged-token", "Origin": GOOD_ORIGIN},
        )
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["code"], "unauthorized")

    def test_forged_internal_header_rejected(self) -> None:
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY,
            headers={"X-QTG-Internal-Auth": "guessed-token"},
        )
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["code"], "bad_internal_token")

    def test_forged_internal_header_with_cookie_still_csrf_checked(self) -> None:
        self.login()
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY,
            headers={"X-QTG-Internal-Auth": "guessed-token"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "csrf_forbidden")

    def test_anonymous_write_is_401_not_403(self) -> None:
        r = self.client.post(WRITE_PATH, json=WRITE_BODY)
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["code"], "unauthorized")

    def test_anonymous_write_with_kitchen_sink_headers(self) -> None:
        """把能想到的伪造头全带上：仍然是 401，不能被"看起来像内部调用"骗过。"""
        r = self.client.post(
            WRITE_PATH, json=WRITE_BODY,
            headers={
                "Authorization": "Bearer nope",
                "X-QTG-Internal-Auth": "nope",
                "X-User": "admin",
                "X-Role": "admin",
            },
        )
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn(r.json()["code"], ("spoofed_identity_header", "bad_internal_token", "unauthorized"))

    def test_csrf_rejection_is_audited(self) -> None:
        self.login()
        r = self.client.post(WRITE_PATH, json=WRITE_BODY)
        self.assertEqual(r.status_code, 403)
        rows = self.audit_by_action("csrf_rejected")
        self.assertTrue(rows, "CSRF 拒绝必须留审计记录（含 reason/cookie/origin）")
        detail = rows[-1]["detail"]
        self.assertEqual(detail["ambient_cookie"], "qtg_session")
        self.assertEqual(detail["reason"], "csrf_origin_missing")


if __name__ == "__main__":
    unittest.main()
