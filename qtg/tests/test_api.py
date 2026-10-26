"""API 集成测试：用 TestClient + mock 上游，验证契约、鉴权、审计与指标。

不依赖真实 BFF（避免测试环境打生产上游），但覆盖从路由到响应的完整链路。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from subprocess import CompletedProcess

from fastapi.testclient import TestClient

from qtg.config import Settings
from qtg.main import create_app

TOKEN = "test-admin-token"


def _settings(tmp: Path) -> Settings:
    (tmp / "secrets").mkdir(parents=True, exist_ok=True)
    (tmp / "secrets" / "admin_token").write_text(TOKEN, encoding="utf-8")
    web = tmp / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    script = tmp / "backup.sh"
    script.write_text("#!/bin/sh\necho backup-ok\n", encoding="utf-8")
    script.chmod(0o755)
    return Settings(
        data_dir=str(tmp / "var"),
        static_dir=str(web),
        admin_token_file=str(tmp / "secrets" / "admin_token"),
        session_secret_file=str(tmp / "secrets" / "session_secret"),
        qoder_base="http://qoder.invalid",
        trae_base="http://trae.invalid",
        cache_overview_sec=0.0,
        limits_file=str(tmp / "limits.json"),
        notify_config_file=str(tmp / "notify.json"),
        backup_script_trae=str(tmp / "backup.sh"),
        backup_script_qoder=str(tmp / "backup.sh"),
    )


def _mock_adapters(rt) -> None:
    if getattr(rt, 'wb', None) is not None:
        rt.wb.health = AsyncMock(return_value={'up': True, 'latency_ms': 2, 'last_error': ''})
    q, t = rt.qoder, rt.trae
    q.accounts = AsyncMock(return_value={
        "accounts": [{
            "uid": "q-1", "nickname": "轻松熊", "realm": "cn", "enabled": True,
            "inCooldown": False, "credits": {"remain": 900, "size": 900, "used": 0},
            "plan": "free",
        }]
    })
    q.usage = AsyncMock(return_value={
        "buckets": [{"label": "10/21", "requests": 3, "tokens": 100, "credit": 0.1}],
        "by_model": [{"model": "DeepSeek-Flash", "requests": 3, "tokens": 100, "credit": 0.1}],
        "by_key": [], "rows": [],
    })
    q.usage_analytics = AsyncMock(return_value={"summary": {"today": {"requests": 5, "errors": 1}}})
    q.credits_flow = AsyncMock(return_value={"daily": [{"day": "2026-10-21", "income": 100, "expense": 1}]})
    q.scheduler = AsyncMock(return_value={"enabled": True, "mode": "整点排程", "next_run_time": "2026-10-23 21:00"})
    q.health = AsyncMock(return_value={"up": True, "latency_ms": 3, "last_error": ""})
    q.settings = AsyncMock(return_value={"api_keys": [{"id": "k0", "name": "陈陈", "masked": "qd-1******2"}]})
    q.models = AsyncMock(return_value={"data": [{"id": "Auto", "capabilities": {"vision": True}}]})
    q.logs = AsyncMock(return_value={"logs": [{"ts": "2026-10-22 12:00", "level": "info", "msg": "hi"}]})
    q.alerts = AsyncMock(return_value={"alerts": [{"id": "a1", "time": "2026-10-22", "level": "warn", "title": "低积分"}]})
    q.checkin = AsyncMock(return_value={"results": [{"uid": "q-1", "ok": True, "earned_credit": 100}]})
    q.refresh_account = AsyncMock(return_value={"results": [{"uid": "q-1", "ok": True}]})
    q.set_account = AsyncMock(return_value={"ok": True})
    q.save_keys = AsyncMock(return_value={"ok": True})
    q.ack_alerts = AsyncMock(return_value={"ok": True})
    q.oauth_start = AsyncMock(return_value={"state": "s1", "authUrl": "https://example/auth"})
    q.import_pat = AsyncMock(return_value={"ok": True})
    q.oauth_poll = AsyncMock(return_value={"status": "pending", "message": "等待授权"})
    q.backup = AsyncMock(return_value={"file": "x.tar.gz"})

    t.overview = AsyncMock(return_value={
        "accounts": [{"user_id": "t-1", "name": "前程如风", "remaining_credits": 1550,
                      "total_credits": 1550, "checked_today": True}],
        "health": {"status": "ok"},
    })
    t.usage = AsyncMock(return_value={
        "usage": [{"date": "2026-10-21", "requests": 2, "ok": 2, "errors": 0,
                   "prompt_tokens": 10, "completion_tokens": 20,
                   "models": [{"name": "glm-5.3", "requests": 2, "ok": 2, "errors": 0}]}],
        "credits_daily": [{"date": "2026-10-21", "consumed": 1, "earned": 100, "total": 0}],
    })
    t.settings = AsyncMock(return_value={"scheduler": {"tasks": [{"name": "checkin"}]}, "ip_allowlist": {}})
    t.keys = AsyncMock(return_value={"data": {"auth_disabled": False, "keys": []}})
    t.models = AsyncMock(return_value={"data": [{"id": "glm-5.3", "display": "GLM-5.3"}]})
    t.logs = AsyncMock(return_value={"data": []})
    t.health = AsyncMock(return_value={"up": True, "latency_ms": 4, "last_error": ""})
    t.checkin = AsyncMock(return_value={"ok": True})
    t.refresh_account = AsyncMock(return_value={"ok": True})
    t.set_account = AsyncMock(return_value={"ok": True})
    t.save_keys = AsyncMock(return_value={"ok": True})
    t.add_account = AsyncMock(return_value={"ok": True})
    t.oauth_login_url = AsyncMock(return_value={"data": {"url": "https://example"}})
    t.oauth_finish = AsyncMock(return_value={"ok": True})


class ApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = create_app(_settings(self.root))
        self.client = TestClient(self.app)
        self.client.__enter__()
        from qtg.routers import auth as _auth_router

        _auth_router._attempts.clear()
        _mock_adapters(self.app.state.rt)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()


class HealthTest(ApiTestBase):
    def test_healthz(self) -> None:
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_readyz(self) -> None:
        r = self.client.get("/readyz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_metrics(self) -> None:
        self.client.get("/healthz")
        self.assertEqual(self.client.get('/metrics').status_code, 401)
        r = self.client.get("/metrics", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertIn("qtg_requests_total", r.text)
        self.assertIn("# TYPE qtg_request_duration_seconds histogram", r.text)

    def test_meta(self) -> None:
        # /api/v1/meta 也走鉴权闸门（它不属于健康检查白名单）
        r = self.client.get("/api/v1/meta", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertIn("read", r.json()["endpoints"])

    def test_security_headers(self) -> None:
        r = self.client.get("/healthz")
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        self.assertIn("x-request-id", r.headers)


class ReadApiTest(ApiTestBase):
    def setUp(self) -> None:
        # 鉴权闸门上线后，管理读接口需要凭证；本类默认以「合法调用方」身份跑，
        # 无凭证边界由 UnauthTest 覆盖。
        super().setUp()
        self.client.headers.update(self.auth)

    def test_overview(self) -> None:
        r = self.client.get("/api/v1/overview")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["stats"]), 5)
        self.assertEqual(len(body["accounts"]["qoder"]), 1)
        self.assertEqual(len(body["accounts"]["trae"]), 1)
        # 三合一：workbuddy 是第三个平台（本机有真实 auths 时非空）
        self.assertIn("workbuddy", body["accounts"])
        self.assertEqual(
            body["stats"][0]["value"],
            str(sum(len(body["accounts"][p])
                    for p in ("qoder", "trae", "workbuddy"))),
        )

    def test_accounts(self) -> None:
        r = self.client.get("/api/v1/accounts")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # 三合一：三个平台都要出现在 summary 里，all 必须等于三者之和
        for plat in ("qoder", "trae", "workbuddy"):
            self.assertIn(plat, body["summary"])
        self.assertEqual(
            body["summary"]["all"]["total"],
            sum(body["summary"][p]["total"] for p in ("qoder", "trae", "workbuddy")),
        )

    def test_usage(self) -> None:
        r = self.client.get("/api/v1/usage?days=7")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["days"], 7)
        self.assertGreaterEqual(len(r.json()["by_model"]), 2)

    def test_usage_days_bounds(self) -> None:
        self.assertEqual(self.client.get("/api/v1/usage?days=0").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/usage?days=999").status_code, 422)

    def test_keys_models_logs_alerts_tasks_requests_ops_settings_audit(self) -> None:
        for path in [
            "/api/v1/keys", "/api/v1/models", "/api/v1/logs", "/api/v1/alerts",
            "/api/v1/tasks", "/api/v1/requests?days=3", "/api/v1/ops",
            "/api/v1/settings", "/api/v1/audit",
        ]:
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200, f"{path}: {r.text[:200]}")

    def test_health_detail(self) -> None:
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["overall"])


class AuthApiTest(ApiTestBase):
    def test_login_bad_password(self) -> None:
        r = self.client.post("/api/v1/auth/login", json={"password": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_login_sets_cookie(self) -> None:
        r = self.client.post("/api/v1/auth/login", json={"password": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        # HttpOnly cookie
        raw = r.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", raw)
        self.assertIn("SameSite=strict", raw)
        # 会话可用
        s = self.client.get("/api/v1/auth/session")
        self.assertTrue(s.json()["authenticated"])

    def test_logout(self) -> None:
        self.client.post("/api/v1/auth/login", json={"password": TOKEN})
        self.client.post("/api/v1/auth/logout")
        self.assertFalse(self.client.get("/api/v1/auth/session").json()["authenticated"])

    def test_legacy_token_accepted_by_api(self) -> None:
        """兼容令牌（extra_password_files）对 API Bearer 同样有效。"""
        extra = Path(self.tmp.name) / "legacy_token"
        extra.write_text("legacy-token-abc", encoding="utf-8")
        rt = self.app.state.rt
        rt.credentials.extra_files.append(extra)
        r = self.client.get("/api/v1/accounts", headers={"Authorization": "Bearer legacy-token-abc"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_login_throttle(self) -> None:
        codes = []
        for _ in range(12):
            codes.append(self.client.post("/api/v1/auth/login", json={"password": "bad"}).status_code)
        self.assertIn(429, codes)


class ActionsAuthTest(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        backup = patch('qtg.routers.actions.subprocess.run',
                       return_value=CompletedProcess(['isolated-backup'], 0, 'test-archive', ''))
        self.backup_process = backup.start()
        self.addCleanup(backup.stop)

    WRITE_CALLS = [
        ("post", "/api/v1/actions/checkin", {"platform": "all"}),
        ("post", "/api/v1/actions/accounts/qoder/refresh", {"uid": "q-1"}),
        ("post", "/api/v1/actions/accounts/trae/refresh", {"uid": "t-1"}),
        ("post", "/api/v1/actions/accounts/add", {"platform": "qoder", "credential": "pat"}),
        ("post", "/api/v1/actions/keys", {"platform": "qoder", "keys": []}),
        ("post", "/api/v1/actions/limits", {"enabled": True}),
        ("post", "/api/v1/actions/notify", {"enabled": False}),
        ("post", "/api/v1/actions/backup", {"target": "trae"}),
        ("post", "/api/v1/actions/alerts/ack", None),
        ("post", "/api/v1/actions/oauth/start", {"platform": "qoder"}),
        ("post", "/api/v1/actions/oauth/finish", {"platform": "trae", "callback": "http://x"}),
        ("get", "/api/v1/actions/oauth/poll?platform=qoder&state=s1", None),
    ]

    def test_all_writes_require_auth(self) -> None:
        for method, path, body in self.WRITE_CALLS:
            with self.subTest(path=path):
                if method == "post":
                    r = self.client.post(path, json=body) if body is not None else self.client.post(path)
                else:
                    r = self.client.get(path)
                self.assertEqual(r.status_code, 401, f"{path} 未鉴权却返回 {r.status_code}")
        self.backup_process.assert_not_called()

    def test_all_writes_accept_token(self) -> None:
        for method, path, body in self.WRITE_CALLS:
            with self.subTest(path=path):
                if method == "post":
                    r = self.client.post(path, json=body, headers=self.auth) if body is not None else self.client.post(path, headers=self.auth)
                else:
                    r = self.client.get(path, headers=self.auth)
                self.assertEqual(r.status_code, 200, f"{path}: {r.text[:200]}")
                self.assertTrue(r.json()["ok"])

    def test_audit_written_for_actions(self) -> None:
        self.client.post("/api/v1/actions/checkin", json={"platform": "all"}, headers=self.auth)
        r = self.client.get("/api/v1/audit", headers=self.auth)
        records = r.json()["records"]
        self.assertTrue(any(rec["action"] == "checkin" for rec in records))
        self.assertTrue(r.json()["integrity"]["ok"])

    def test_audit_redacts_secrets(self) -> None:
        self.client.post(
            "/api/v1/actions/accounts/add",
            json={"platform": "qoder", "credential": "pat-SECRET", "name": "x"},
            headers=self.auth,
        )
        rows = self.client.get("/api/v1/audit", headers=self.auth).json()["records"]
        joined = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("pat-SECRET", joined)

    def test_auth_precedes_body_validation(self) -> None:
        """无凭证 + 非法 body：必须 401，而不是 422（不泄露 schema）。"""
        r = self.client.post("/api/v1/actions/backup", json={"target": "xx"})
        self.assertEqual(r.status_code, 401, r.text)
        r2 = self.client.post("/api/v1/actions/checkin", json={"platform": "nope"})
        self.assertEqual(r2.status_code, 401, r2.text)

    def test_bad_platform_rejected(self) -> None:
        r = self.client.post("/api/v1/actions/accounts/gemini/refresh", json={}, headers=self.auth)
        self.assertEqual(r.status_code, 422)

    def test_notify_requires_channel_when_enabled(self) -> None:
        r = self.client.post("/api/v1/actions/notify", json={"enabled": True}, headers=self.auth)
        self.assertEqual(r.status_code, 400)

    def test_limits_atomic_write(self) -> None:
        r = self.client.post(
            "/api/v1/actions/limits",
            json={"enabled": True, "daily_token_limit": 1000},
            headers=self.auth,
        )
        self.assertEqual(r.status_code, 200)
        path = Path(self.app.state.rt.settings.limits_file)
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text())["daily_token_limit"], 1000)
        self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_csrf_origin_check_for_cookie(self) -> None:
        self.client.post("/api/v1/auth/login", json={"password": TOKEN})
        # 带 Cookie 但伪造跨站 Origin
        r = self.client.post(
            "/api/v1/actions/limits",
            json={"enabled": True},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(r.status_code, 403)


class StaticTest(ApiTestBase):
    def setUp(self) -> None:
        # 前端静态资源本身无需鉴权，但 `/api/...` 的 404 语义要带凭证才能测到。
        super().setUp()
        self.client.headers.update(self.auth)

    def test_index_served(self) -> None:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ok", r.text)

    def test_spa_fallback(self) -> None:
        r = self.client.get("/dashboard")
        self.assertEqual(r.status_code, 200)

    def test_api_404_not_html(self) -> None:
        r = self.client.get("/api/v1/nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")


class UnauthTest(ApiTestBase):
    """鉴权闸门回归：管理接口无凭证必须 401，登录/健康检查必须公开。

    背景：2026-10-22 审计发现 `/api/*` 兼容层曾**完全无鉴权**——无 Cookie 无
    Bearer 即可 GET 密钥列表、账号池、审计日志，且未鉴权的 `POST /api/logs/clear`
    会清空限额判据表。`main.py` 加 `_auth_gate` 后，这里把边界固化成测试。
    """

    def _anon(self, path: str):
        # 逐请求覆盖成空 Authorization，模拟「没有任何凭证的浏览器/脚本」
        return self.client.get(path, headers={"Authorization": ""})

    def test_management_api_requires_auth(self) -> None:
        for path in (
            "/api/v1/overview", "/api/v1/keys", "/api/v1/accounts",
            "/api/accounts", "/api/keys", "/api/security/config",
            "/api/audit-logs", "/api/stats/summary", "/api/upstreams",
        ):
            with self.subTest(path=path):
                r = self._anon(path)
                self.assertEqual(r.status_code, 401, f"{path}: {r.status_code} {r.text[:120]}")

    def test_write_api_requires_auth(self) -> None:
        for path in ("/api/logs/clear", "/api/sessions/revoke", "/api/keys"):
            with self.subTest(path=path):
                r = self.client.post(path, json={}, headers={"Authorization": ""})
                self.assertEqual(r.status_code, 401, f"{path}: {r.status_code}")

    def test_public_endpoints_stay_open(self) -> None:
        self.assertEqual(self.client.get("/healthz", headers={"Authorization": ""}).status_code, 200)
        self.assertEqual(self.client.get("/readyz", headers={"Authorization": ""}).status_code, 200)
        self.assertEqual(self.client.get("/metrics", headers={"Authorization": ""}).status_code, 401)
        r = self.client.post(
            "/api/login", json={"username": "admin", "password": TOKEN},
            headers={"Authorization": ""},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_login_sets_session_cookie(self) -> None:
        r = self.client.post(
            "/api/login", json={"username": "admin", "password": TOKEN},
            headers={"Authorization": ""},
        )
        self.assertIn("qtg_session", r.cookies)

    def test_bad_password_rejected(self) -> None:
        r = self.client.post(
            "/api/login", json={"username": "admin", "password": "wrong"},
            headers={"Authorization": ""},
        )
        self.assertFalse((r.json() or {}).get("ok"))


class StaticTraversalTest(ApiTestBase):
    """目录穿越回归：`static_dir / path` 曾可逃逸到 /etc/passwd。"""

    def test_traversal_blocked(self) -> None:
        for bad in (
            "/..%2f..%2f..%2fetc%2fpasswd",
            "/..%2F..%2F..%2Fetc%2Fpasswd",
            "/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
        ):
            with self.subTest(path=bad):
                r = self.client.get(bad)
                self.assertIn(r.status_code, (400, 403, 404), f"{bad}: {r.status_code}")
                self.assertNotIn("root:x:0:0", r.text)

    def test_normal_static_still_served(self) -> None:
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
