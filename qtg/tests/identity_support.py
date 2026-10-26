"""K04 / K05 / K07 测试共用装配。

这些测试需要比 `test_api.ApiTestBase` 多一点东西：

* 一枚**内部服务凭据**文件（`QTG_INTERNAL_SERVICE_TOKEN_FILE`），用来模拟
  「Nginx 注入的内部凭据」；
* 把 TestClient 的对端（`testclient`）登记成可信边缘 —— 生产里这一步对应
  `QTG_TRUSTED_EDGE_PEERS=127.0.0.1,::1`（Nginx 与 QTG 同机，走回环）。

刻意**不**把 `testclient` 写进 `Settings` 的默认值：默认值必须是生产安全值，
测试用 `dataclasses.replace()` 显式放宽，谁放宽了什么一眼可见。
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

from qtg.config import Settings
from qtg.identity import Identity
from qtg.main import create_app

from test_api import TOKEN, _mock_adapters, _settings

INTERNAL_TOKEN = "test-internal-service-token-machine-only-64"
IDENTITY_SECRET = "test-identity-signing-secret-separate-from-machine-token"
TRUSTED_PEER = "testclient"
GOOD_ORIGIN = "http://testserver"   # TestClient 默认 Host=testserver


def build_settings(root: Path, **overrides) -> Settings:
    """在 test_api 的基线配置上补齐身份相关字段。"""
    base = _settings(root)
    secret = root / "secrets" / "internal_service_token"
    secret.write_text(INTERNAL_TOKEN, encoding="utf-8")
    signing = root / "secrets" / "identity_signing_key"
    signing.write_text(IDENTITY_SECRET, encoding="utf-8")
    signing.chmod(0o600)
    merged = dict(
        internal_service_token_file=str(secret),
        identity_signing_key_file=str(signing),
        trusted_edge_peers=f"127.0.0.1,::1,{TRUSTED_PEER}",
    )
    merged.update(overrides)
    return dataclasses.replace(base, **merged)


class IdentityTestBase(unittest.TestCase):
    """带内部服务凭据的隔离 app。"""

    settings_overrides: dict = {}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = create_app(build_settings(self.root, **self.settings_overrides))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        # 登录限速是**模块级**计数（8 次/分钟），不清会让同文件的后续用例全变 429。
        # 与 test_api.ApiTestBase 同一处理；这不是放宽产品策略，只是重置测试状态。
        from qtg.routers import auth as _auth_router
        _auth_router._attempts.clear()
        self.rt = self.app.state.rt
        _mock_adapters(self.rt)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}
        self.internal = {"X-QTG-Internal-Auth": INTERNAL_TOKEN}

    # ---- 便利方法 ----
    def login(self, password: str = TOKEN) -> None:
        r = self.client.post("/api/v1/auth/login", json={"password": password})
        self.assertEqual(r.status_code, 200, r.text)

    def session_cookie(self, *, role: str = "admin", sub: str = "admin",
                       auth_method: str = "session_cookie",
                       ttl: int = 600, aud: str = "qtg",
                       scopes: Optional[list] = None) -> str:
        """用真实签名密钥签发一个会话（等价于 /api/v1/auth/login 的产物）。"""
        return self.rt.credentials.issue_session(
            ttl, sub=sub, role=role, auth_method=auth_method,
            scopes=scopes or [], aud=aud,
        )

    def cookie_headers(self, token: str) -> dict:
        return {"Cookie": f"qtg_session={token}"}

    def signed_identity(self, *, sub: str = "manager-user", role: str = "admin",
                        auth_method: str = "edge_session", aud: str = "qtg",
                        exp: Optional[int] = None, scopes: Optional[list] = None) -> str:
        import time as _time

        identity = Identity(
            sub=sub, role=role, scopes=frozenset(scopes or ["read"]),
            auth_method=auth_method, aud=aud,
            exp=int(exp if exp is not None else _time.time() + 600),
            iat=int(_time.time()),
            iss="manager",
        )
        return self.rt.identity.sign(identity)

    def audit_rows(self, limit: int = 100) -> list:
        return self.rt.audit.tail(limit)

    def audit_by_action(self, action: str) -> list:
        return [r for r in self.audit_rows(200) if r.get("action") == action]
