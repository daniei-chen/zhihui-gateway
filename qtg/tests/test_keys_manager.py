"""密钥管理转发 Manager 的回归测试（K01-S2，2026-10-23）。

QTG 不再持有公开推理密钥的权威：列表/创建/修改/删除/重置用量全部转成对
Manager 的调用。本文件锁定：
1. 列表按 Manager 响应映射，失败时明确报错（不返回空列表冒充"没有密钥"）；
2. 创建时把前端字段（quota/quota_credit/realm/upstream_id/expires_at/ip/max_ips）
   完整转发给 Manager；
3. 更新/删除/重置用量都走 Manager，404 语义保持。
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from test_api import ApiTestBase

MANAGER_ROW = {
    "id": 7,
    "name": "k1",
    "prefix": "wbk_abcdef12",
    "enabled": True,
    "expires_at": None,
    "max_ips": 0,
    "ip_allowlist": [],
    "models": ["m1"],
    "realm": "cn",
    "quota": 100,
    "used_tokens": 5,
    "quota_credit": 1.5,
    "used_credit": 0.2,
    "upstream_id": None,
    "created_at": 111,
    "last_used_at": None,
    "platforms": ["qoder"],
}


class ManagerKeysListTest(ApiTestBase):
    def test_list_maps_manager_response(self) -> None:
        with patch("qtg.manager_api.list_keys", new=AsyncMock(return_value=[MANAGER_ROW])):
            r = self.client.get("/api/keys", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], 7)
        self.assertEqual(data[0]["prefix"], "wbk_abcdef12")
        self.assertEqual(data[0]["source"], "manager")
        self.assertEqual(data[0]['platforms'], ['qoder'])

    def test_list_fails_loudly_when_manager_down(self) -> None:
        from qtg.errors import UpstreamError

        with patch("qtg.manager_api.list_keys", new=AsyncMock(side_effect=UpstreamError("down"))):
            r = self.client.get("/api/keys", headers=self.auth)
        self.assertGreaterEqual(r.status_code, 500)


class ManagerKeysCreateTest(ApiTestBase):
    def test_create_forwards_all_fields(self) -> None:
        mock = AsyncMock(return_value={"id": 9, "name": "n", "key": "wbk_plain"})
        with patch("qtg.manager_api.create_key", new=mock):
            r = self.client.post(
                "/api/keys",
                headers=self.auth,
                json={
                    "name": "n",
                    "models": ["m1", "m2"],
                    "quota": 100,
                    "quota_credit": 5.5,
                    "realm": "cn",
                    "upstream_id": 3,
                    "expires_at": 0,
                    "max_ips": 2,
                    "ip_allowlist": ["1.2.3.4"],
                },
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["key"], "wbk_plain")
        self.assertEqual(r.json()["source"], "manager")
        payload = mock.await_args.args[1]
        self.assertEqual(payload["name"], "n")
        self.assertEqual(payload["models"], ["m1", "m2"])
        self.assertEqual(payload["quota"], 100)
        self.assertEqual(payload["quota_credit"], 5.5)
        self.assertEqual(payload["realm"], "cn")
        self.assertEqual(payload["upstream_id"], 3)
        self.assertEqual(payload["max_ips"], 2)
        self.assertEqual(payload["ip_allowlist"], ["1.2.3.4"])

    def test_create_fails_loudly(self) -> None:
        from qtg.errors import UpstreamError

        with patch("qtg.manager_api.create_key", new=AsyncMock(side_effect=UpstreamError("no token"))):
            r = self.client.post("/api/keys", headers=self.auth, json={"name": "n"})
        self.assertGreaterEqual(r.status_code, 500)
        self.assertNotIn("key", r.json())


class ManagerKeysMutateTest(ApiTestBase):
    def test_update_forwards_patch(self) -> None:
        mock = AsyncMock(return_value={"id": 5, "name": "x"})
        with patch("qtg.manager_api.update_key", new=mock):
            r = self.client.patch(
                "/api/keys/5", headers=self.auth, json={"name": "x", "quota": 10}
            )
        self.assertEqual(r.status_code, 200)
        args = mock.await_args.args
        self.assertEqual(args[1], 5)
        self.assertEqual(args[2]["quota"], 10)
        self.assertEqual(args[2]["name"], "x")

    def test_update_missing_returns_404(self) -> None:
        with patch("qtg.manager_api.update_key", new=AsyncMock(return_value=None)):
            r = self.client.patch("/api/keys/5", headers=self.auth, json={})
        self.assertEqual(r.status_code, 404)

    def test_delete_forwards(self) -> None:
        mock = AsyncMock(return_value=True)
        with patch("qtg.manager_api.delete_key", new=mock):
            r = self.client.delete("/api/keys/5", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(mock.await_args.args[1], 5)

    def test_reset_usage_forwards(self) -> None:
        mock = AsyncMock(return_value=True)
        with patch("qtg.manager_api.reset_usage", new=mock):
            r = self.client.post("/api/keys/5/reset-usage", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(mock.await_args.args[1], 5)

    def test_bad_id_returns_404(self) -> None:
        r = self.client.delete("/api/keys/not-a-number", headers=self.auth)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()


class ManagerKeysN21ContractTest(ApiTestBase):
    """K29/N21（2026-10-24）：删除不存在 404、空名创建 422——与 Manager 语义对齐。"""

    def test_delete_missing_returns_404(self) -> None:
        with patch("qtg.manager_api.delete_key", new=AsyncMock(return_value=False)):
            r = self.client.delete("/api/keys/999999", headers=self.auth)
        self.assertEqual(r.status_code, 404)
        self.assertIn("detail", r.json())

    def test_create_rejects_empty_name(self) -> None:
        mock = AsyncMock(return_value={"id": 1})
        with patch("qtg.manager_api.create_key", new=mock):
            r = self.client.post("/api/keys", json={}, headers=self.auth)
            r2 = self.client.post("/api/keys", json={"name": "   "}, headers=self.auth)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r2.status_code, 422)
        mock.assert_not_awaited()
