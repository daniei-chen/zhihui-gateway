"""第 7 轮新增的兼容层端点：结构、如实 409、IP 管控真实生效。

不 mock 上游也能跑——这些端点要么读本机状态，要么如实拒绝。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
    return Settings(
        data_dir=str(tmp / "var"),
        static_dir=str(web),
        admin_token_file=str(tmp / "secrets" / "admin_token"),
        session_secret_file=str(tmp / "secrets" / "session_secret"),
        qoder_base="http://qoder.invalid",
        trae_base="http://trae.invalid",
        cache_overview_sec=0.0,
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(_settings(Path(self.tmp.name)))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.h = {"Authorization": f"Bearer {TOKEN}"}


class SettingsCompatTest(_Base):
    def test_users_returns_array(self) -> None:
        r = self.client.get("/api/users", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), list)
        self.assertEqual(r.json()[0]["username"], "admin")

    def test_upstream_settings_declares_unavailable(self) -> None:
        r = self.client.get("/api/settings/upstream", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["available"])
        self.assertTrue(r.json()["error"])

    def test_tokens_empty_and_create_409(self) -> None:
        self.assertEqual(self.client.get("/api/tokens", headers=self.h).json(), [])
        r = self.client.post("/api/tokens", headers=self.h, json={"name": "x"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("detail", r.json())

    def test_proxy_routes_empty_and_write_409(self) -> None:
        data = self.client.get("/api/proxy-routes", headers=self.h).json()
        self.assertEqual(data["routes"], [])
        self.assertIn("pool", data)
        r = self.client.post("/api/proxy-routes", headers=self.h, json={"name": "a", "url": "http://1.2.3.4:1"})
        self.assertEqual(r.status_code, 409)

    def test_pg_sync_shape(self) -> None:
        data = self.client.get("/api/settings/pg-sync", headers=self.h).json()
        self.assertIn("config", data)
        self.assertIn("status", data)
        self.assertFalse(data["config"]["enabled"])

    def test_changelog_unavailable(self) -> None:
        data = self.client.get("/api/system/changelog", headers=self.h).json()
        self.assertFalse(data["available"])
        self.assertEqual(data["versions"], [])

    def test_update_status_and_update_409(self) -> None:
        data = self.client.get("/api/system/update-status", headers=self.h).json()
        self.assertFalse(data["available"])
        self.assertTrue(data["version"])
        r = self.client.post("/api/system/update", headers=self.h, json={"target": "manager"})
        self.assertEqual(r.status_code, 409)

    def test_restart_refuses(self) -> None:
        r = self.client.post("/api/restart", headers=self.h, json={})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertIn("docker", r.json()["message"])

    def test_stats_repair_usage_shape(self) -> None:
        data = self.client.post("/api/stats/repair-usage", headers=self.h, json={}).json()
        self.assertEqual(data["repaired"], 0)
        self.assertIn("requests", data)
        data2 = self.client.post("/api/stats/rebuild-usage", headers=self.h, json={}).json()
        self.assertIn("rows_before", data2)
        self.assertIn("tokens_delta", data2)


class IpGuardTest(_Base):
    def test_default_disabled(self) -> None:
        data = self.client.get("/api/security/config", headers=self.h).json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["mode"], "whitelist")

    def test_rule_crud_and_validation(self) -> None:
        bad = self.client.post("/api/security/rules", headers=self.h,
                               json={"kind": "allow", "cidr": "not-a-cidr"})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/api/security/rules", headers=self.h,
                              json={"kind": "allow", "cidr": "192.0.2.0/24", "note": "t"})
        self.assertEqual(ok.status_code, 200)
        rid = ok.json()["id"]
        rules = self.client.get("/api/security/rules", headers=self.h).json()
        self.assertEqual(len(rules), 1)
        self.assertEqual(self.client.delete(f"/api/security/rules/{rid}", headers=self.h).status_code, 200)
        self.assertEqual(self.client.get("/api/security/rules", headers=self.h).json(), [])

    def test_enable_requires_self_allowed(self) -> None:
        # testclient 的来源不是合法 IP，白名单放行判断必然为假 → 必须 409，不能把自己锁死
        r = self.client.post("/api/security/config", headers=self.h,
                             json={"enabled": True, "mode": "whitelist"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("不会", r.json()["detail"])

    def test_whitelist_blocks_and_allows(self) -> None:
        self.client.post("/api/security/rules", headers=self.h,
                         json={"kind": "allow", "cidr": "192.0.2.0/24"})
        r = self.client.post("/api/security/config", headers={**self.h, "X-Real-IP": "127.0.0.1"},
                             json={"enabled": True, "mode": "whitelist"})
        self.assertEqual(r.status_code, 200)
        # 白名单内放行
        ok = self.client.get("/api/users", headers={**self.h, "X-Real-IP": "192.0.2.10"})
        self.assertEqual(ok.status_code, 200)
        # 白名单外拦截
        blocked = self.client.get("/api/users", headers={**self.h, "X-Real-IP": "198.51.100.9"})
        self.assertEqual(blocked.status_code, 403)
        # 回环永远放行
        loop = self.client.get("/api/users", headers={**self.h, "X-Real-IP": "127.0.0.1"})
        self.assertEqual(loop.status_code, 200)
        # 拦截记录可读、可清
        logs = self.client.get("/api/security/logs", headers={**self.h, "X-Real-IP": "127.0.0.1"}).json()
        self.assertTrue(any(x["ip"] == "198.51.100.9" for x in logs))
        self.client.post("/api/security/logs/clear", headers={**self.h, "X-Real-IP": "127.0.0.1"})
        self.assertEqual(self.client.get("/api/security/logs", headers={**self.h, "X-Real-IP": "127.0.0.1"}).json(), [])
        self.client.post("/api/security/config", headers=self.h, json={"enabled": False})


class AccountExtTest(_Base):
    def test_force_cooling_refuses(self) -> None:
        r = self.client.post("/api/accounts/qoder:q-1/force-cooling", headers=self.h,
                             json={"duration_seconds": 60})
        self.assertEqual(r.status_code, 409)

    def test_weight_proxy_move_refuse(self) -> None:
        for method, path, body in (
            ("put", "/api/accounts/qoder:q-1/weight", {"weight": 2}),
            ("put", "/api/accounts/qoder:q-1/proxy", {"proxy": ""}),
            ("post", "/api/accounts/qoder:q-1/move", {"to_upstream_id": 0}),
        ):
            r = getattr(self.client, method)(path, headers=self.h, json=body)
            self.assertEqual(r.status_code, 409, path)
            self.assertIn("detail", r.json())
