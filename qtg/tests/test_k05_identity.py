"""K05：内部身份断言、外部伪造头、审计可归因。

## 被锁定的行为

1. 身份断言保留 `sub / role / scopes / auth_method / aud / exp` 六项；
2. 外部伪造的身份头必须失败（明文头、坏签名、错受众、过期、超长有效期、未知角色）；
3. 内部凭据只在可信对端上生效，且必须与 `internal_service_token` 常量时间相等；
4. 推理密钥不能当控制面凭证；
5. 高危动作（吊销会话、清日志）只允许真人会话，管理 Token 403；
6. 拒绝/放行都写审计，且审计可归因到具体 sub/role/auth_method。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import unittest

from identity_support import IDENTITY_SECRET, INTERNAL_TOKEN, IdentityTestBase
from qtg.identity import _derive_key

IDENTITY_PATH = "/api/v1/authz/identity"
WRITE_PATH = "/api/v1/actions/limits"
READ_PATH = "/api/v1/keys"


def _craft_assertion(payload: dict, secret: str = IDENTITY_SECRET) -> str:
    """手工构造断言（用来测缺字段/坏签名等 verify 侧边界）。"""
    def _b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(_derive_key(secret, "identity"), body.encode(), hashlib.sha256).digest())
    return f"v1.{body}.{sig}"


class IdentityAssertionTest(IdentityTestBase):
    def test_session_identity_carries_all_required_claims(self) -> None:
        self.login()
        r = self.client.get(IDENTITY_PATH)
        self.assertEqual(r.status_code, 200, r.text)
        ident = r.json()["identity"]
        for key in ("sub", "role", "scopes", "auth_method", "aud", "exp"):
            self.assertIn(key, ident, f"身份断言缺 {key}")
        self.assertEqual(ident["sub"], "admin")
        self.assertEqual(ident["role"], "admin")
        self.assertEqual(ident["auth_method"], "session_cookie")
        self.assertEqual(ident["aud"], "qtg")
        self.assertGreater(int(ident["exp"]), int(time.time()))

    def test_admin_token_identity(self) -> None:
        r = self.client.get(IDENTITY_PATH, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        ident = r.json()["identity"]
        self.assertEqual(ident["auth_method"], "admin_token")
        self.assertEqual(ident["role"], "admin")
        self.assertEqual(ident["sub"], "admin-token")

    def test_internal_service_identity(self) -> None:
        r = self.client.get(IDENTITY_PATH, headers=self.internal)
        self.assertEqual(r.status_code, 200, r.text)
        ident = r.json()["identity"]
        self.assertEqual(ident["role"], "internal_service")
        self.assertEqual(ident["auth_method"], "internal_service")
        self.assertIn("service", ident["scopes"])
        self.assertGreater(int(ident["exp"]), int(time.time()))

    def test_signed_identity_preserved(self) -> None:
        headers = {"X-QTG-Identity": self.signed_identity(
            sub="真实用户", role="viewer", scopes=["read", "keys:read"],
            auth_method="edge_session",
        )}
        r = self.client.get(IDENTITY_PATH, headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        ident = r.json()["identity"]
        self.assertEqual(ident["sub"], "真实用户")       # 中文用户名可用
        self.assertEqual(ident["role"], "viewer")
        self.assertEqual(ident["auth_method"], "edge_session")
        self.assertEqual(ident["scopes"], ["keys:read", "read"])
        self.assertEqual(ident["aud"], "qtg")

    def test_unsigned_edge_headers_cannot_claim_a_human_identity(self) -> None:
        """The machine token must not mint a human identity via plaintext headers."""
        headers = {
            **self.internal,
            "X-QTG-Sub": "edge-user",
            "X-QTG-Role": "viewer",
            "X-QTG-Scopes": "read,keys:read",
            "X-QTG-Auth-Method": "edge_session",
            "X-QTG-Aud": "qtg",
            "X-QTG-Exp": str(int(time.time()) + 300),
        }
        r = self.client.get(IDENTITY_PATH, headers=headers)
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()['code'], 'identity_signature_required')

    def test_cookie_session_beats_injected_internal_credential(self) -> None:
        """浏览器同时带 Cookie 与内部凭据时，身份必须仍是**这个人**，不能被抹成服务身份。"""
        self.login()
        r = self.client.get(IDENTITY_PATH, headers=self.internal)
        self.assertEqual(r.status_code, 200, r.text)
        ident = r.json()["identity"]
        self.assertEqual(ident["auth_method"], "session_cookie")
        self.assertEqual(ident["role"], "admin")


class ForgedIdentityTest(IdentityTestBase):
    def _expect(self, headers: dict, code: str) -> None:
        r = self.client.get(IDENTITY_PATH, headers=headers)
        self.assertEqual(r.status_code, 401, f"{code}: {r.status_code} {r.text}")
        self.assertEqual(r.json()["code"], code)

    def test_plain_identity_header_rejected(self) -> None:
        self._expect({"X-User": "admin", "X-Role": "admin"}, "spoofed_identity_header")
        self._expect({"X-Auth-Request-User": "admin"}, "spoofed_identity_header")

    def test_edge_fields_without_internal_credential_rejected(self) -> None:
        self._expect(
            {"X-QTG-Sub": "admin", "X-QTG-Role": "admin"}, "spoofed_identity_header"
        )

    def test_tampered_signature_rejected(self) -> None:
        good = self.signed_identity()
        prefix, body, sig = good.split(".")
        tampered = f"{prefix}.{body}.{sig[:-2]}xx"
        self._expect({"X-QTG-Identity": tampered}, "identity_tampered")

    def test_malformed_signature_rejected(self) -> None:
        self._expect({"X-QTG-Identity": "not-a-token"}, "identity_malformed")

    def test_wrong_audience_rejected(self) -> None:
        headers = {"X-QTG-Identity": self.signed_identity(aud="manager")}
        self._expect(headers, "identity_wrong_audience")

    def test_expired_identity_rejected(self) -> None:
        headers = {"X-QTG-Identity": self.signed_identity(exp=int(time.time()) - 600)}
        self._expect(headers, "identity_expired")

    def test_missing_exp_rejected(self) -> None:
        payload = {
            "sub": "u", "role": "admin", "scopes": [], "auth_method": "edge_session",
            "aud": "qtg", "iat": int(time.time()),
        }
        self._expect({"X-QTG-Identity": _craft_assertion(payload)}, "identity_missing_exp")

    def test_unbounded_ttl_rejected(self) -> None:
        headers = {"X-QTG-Identity": self.signed_identity(exp=int(time.time()) + 7 * 86400)}
        self._expect(headers, "identity_exp_too_long")

    def test_unknown_role_rejected(self) -> None:
        headers = {"X-QTG-Identity": self.signed_identity(role="root")}
        self._expect(headers, "identity_bad_role")

    def test_inference_key_not_control_plane(self) -> None:
        self._expect(
            {"Authorization": "Bearer sk-qtg-fake-for-isolated-tests"}, "inference_key_not_control_plane"
        )

    def test_wrong_internal_token_rejected(self) -> None:
        r = self.client.get(IDENTITY_PATH, headers={"X-QTG-Internal-Auth": "wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "bad_internal_token")

    def test_rejections_are_audited(self) -> None:
        self.client.get(IDENTITY_PATH, headers={"X-User": "admin"})
        rows = self.audit_by_action("identity_rejected")
        self.assertTrue(rows, "伪造身份头必须留审计")
        self.assertEqual(rows[-1]["result"], "denied")
        self.assertEqual(rows[-1]["detail"]["code"], "spoofed_identity_header")


class UntrustedPeerTest(IdentityTestBase):
    """对端不在可信边缘列表里时，内部凭据与身份断言一律不采信。"""

    settings_overrides = {"trusted_edge_peers": "127.0.0.1,::1"}

    def test_internal_credential_from_untrusted_peer_rejected(self) -> None:
        r = self.client.get(IDENTITY_PATH, headers=self.internal)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "identity_untrusted_source")

    def test_signed_identity_from_untrusted_peer_rejected(self) -> None:
        r = self.client.get(IDENTITY_PATH, headers={"X-QTG-Identity": self.signed_identity()})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "identity_untrusted_source")


class HumanSessionOnlyTest(IdentityTestBase):
    """高危动作只给真人会话（对齐 Manager 的 require_session_admin）。"""

    def test_revoke_with_admin_token_denied(self) -> None:
        r = self.client.post("/api/sessions/revoke", headers=self.auth)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["code"], "human_session_required")

    def test_revoke_with_session_allowed(self) -> None:
        self.login()
        # 浏览器写请求要带 Origin（K07）：同源面板会带，这里显式模拟
        r = self.client.post("/api/sessions/revoke", headers={"Origin": "http://testserver"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])

    def test_logs_clear_with_admin_token_denied(self) -> None:
        r = self.client.post("/api/logs/clear", headers=self.auth)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["code"], "human_session_required")

    def test_internal_service_denied_on_human_only_action(self) -> None:
        r = self.client.post("/api/sessions/revoke", headers=self.internal)
        self.assertEqual(r.status_code, 403)
        # 内部服务既不是 admin 角色、也不是真人会话：两种理由都算正确拒绝
        self.assertIn(r.json()["code"],
                      ("role_write_denied", "human_session_required"))


class AuditAttributionTest(IdentityTestBase):
    def test_write_denial_attributes_identity(self) -> None:
        token = self.session_cookie(role="viewer", sub="只读用户")
        r = self.client.post(WRITE_PATH, json={"enabled": True},
                             headers={**self.cookie_headers(token), "Origin": "http://testserver"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["code"], "role_write_denied")
        rows = self.audit_by_action("authz_denied")
        self.assertTrue(rows, "越权写必须留审计")
        rec = rows[-1]
        self.assertEqual(rec["actor"], "viewer:只读用户")
        ident = rec["detail"]["identity"]
        self.assertEqual(ident["sub"], "只读用户")
        self.assertEqual(ident["role"], "viewer")
        self.assertEqual(ident["auth_method"], "session_cookie")
        self.assertEqual(ident["aud"], "qtg")
        self.assertIn("exp", ident)
        self.assertEqual(rec["detail"]["decision"]["code"], "role_write_denied")

    def test_probe_records_actor(self) -> None:
        r = self.client.get("/api/v1/authz/probe", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        rows = self.audit_by_action("authz_probe")
        self.assertTrue(rows, "探针调用要留审计（否则无法归因）")
        self.assertEqual(rows[-1]["actor"], "admin:admin-token")
        self.assertEqual(rows[-1]["detail"]["identity"]["auth_method"], "admin_token")

    def test_audit_never_contains_tokens(self) -> None:
        self.client.get(IDENTITY_PATH, headers=self.auth)
        self.client.get(IDENTITY_PATH, headers=self.internal)
        import json as _json

        blob = _json.dumps(self.audit_rows(200), ensure_ascii=False)
        for secret in ("test-admin-token", INTERNAL_TOKEN, IDENTITY_SECRET):
            self.assertNotIn(secret, blob, "审计里不能出现任何令牌明文")


if __name__ == "__main__":
    unittest.main()
