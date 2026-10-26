"""数据面单测：密钥生命周期、流水统计、路由决策、鉴权边界。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from qtg.store import Store, hash_key


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "qtg.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_and_verify(self) -> None:
        created = self.store.create_key("测试", platforms=["qoder"], daily_limit=1000)
        self.assertTrue(created["key"].startswith("sk-qtg-"))
        key = self.store.verify_key(created["key"])
        self.assertIsNotNone(key)
        self.assertEqual(key.name, "测试")
        self.assertEqual(key.platforms, ["qoder"])
        self.assertEqual(key.daily_limit, 1000)

    def test_invalid_and_disabled(self) -> None:
        self.assertIsNone(self.store.verify_key("sk-qtg-deadbeef"))
        self.assertIsNone(self.store.verify_key("not-a-key"))
        created = self.store.create_key("x")
        self.store.set_key_enabled(created["id"], False)
        self.assertIsNone(self.store.verify_key(created["key"]))
        self.store.set_key_enabled(created["id"], True)
        self.assertIsNotNone(self.store.verify_key(created["key"]))

    def test_prefix_masked(self) -> None:
        created = self.store.create_key("掩码测试")
        pub = self.store.list_keys()[0]
        self.assertIn("****", pub["masked"])
        self.assertNotIn(created["key"], str(pub), "列表不得泄露明文密钥")

    def test_delete(self) -> None:
        created = self.store.create_key("删除")
        self.assertTrue(self.store.delete_key(created["id"]))
        self.assertIsNone(self.store.verify_key(created["key"]))
        self.assertFalse(self.store.delete_key(created["id"]))

    def test_record_and_stats(self) -> None:
        created = self.store.create_key("统计")
        key = self.store.verify_key(created["key"])
        for i in range(3):
            self.store.record_request(
                key=key, platform="qoder", model="Auto", status=200,
                prompt_tokens=100, completion_tokens=50, duration_ms=120,
            )
        self.store.record_request(key=key, platform="trae", model="glm-5.3", status=500, error="boom")
        stats = self.store.stats(1)
        self.assertEqual(stats["requests"], 4)
        self.assertEqual(stats["tokens"], 450)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.store.key_usage_today(key.id), 450)  # 失败请求未计 token

    def test_list_filters(self) -> None:
        created = self.store.create_key("过滤")
        key = self.store.verify_key(created["key"])
        self.store.record_request(key=key, platform="qoder", model="a", status=200)
        self.store.record_request(key=key, platform="trae", model="b", status=200)
        self.assertEqual(len(self.store.list_requests(10, platform="qoder")), 1)
        self.assertEqual(len(self.store.list_requests(10, platform="trae")), 1)
        self.assertEqual(len(self.store.list_requests(10)), 2)

    def test_prune(self) -> None:
        created = self.store.create_key("清理")
        key = self.store.verify_key(created["key"])
        self.store.record_request(key=key, platform="qoder", model="a", status=200)
        self.assertEqual(self.store.prune(keep_days=365), 0)  # 刚写入的不会被删


class RouteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "qtg.db")
        self.key = self.store.verify_key(self.store.create_key("路由")["key"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def _route_with(self, catalog, model, key=None, credits=None):
        from qtg.routers import proxy as P

        class FakeRt:
            settings = type("S", (), {"proxy_model_cache_sec": 0})()

        rt = FakeRt()
        P._catalog = AsyncMock(return_value=catalog)
        P._platform_credits = AsyncMock(return_value=credits or {})
        return await P._route(rt, key or self.key, model)

    async def test_prefix_routing(self) -> None:
        p, m, reason = await self._route_with({}, "qoder/Auto")
        self.assertEqual((p, m), ("qoder", "Auto"))
        self.assertEqual(reason, "prefix")
        p, _, _ = await self._route_with({}, "trae/glm-5.3")
        self.assertEqual(p, "trae")

    async def test_prefix_respects_platform_permission(self) -> None:
        qonly = self.store.verify_key(self.store.create_key("仅Q", platforms=["qoder"])["key"])
        with self.assertRaises(Exception) as ctx:
            await self._route_with({}, "trae/glm-5.3", key=qonly)
        self.assertIn("platform_not_allowed", str(getattr(ctx.exception, "code", "")))

    async def test_catalog_single_platform(self) -> None:
        p, m, reason = await self._route_with({"qoder": ["Auto"], "trae": ["glm-5.3"]}, "glm-5.3")
        self.assertEqual((p, m, reason), ("trae", "glm-5.3", "catalog"))

    async def test_catalog_both_platforms_uses_balance(self) -> None:
        p, _, reason = await self._route_with(
            {"qoder": ["Auto"], "trae": ["Auto"]}, "Auto", credits={"qoder": 100, "trae": 900}
        )
        self.assertEqual(p, "trae")
        self.assertIn("balance", reason)
        p2, _, _ = await self._route_with(
            {"qoder": ["Auto"], "trae": ["Auto"]}, "Auto", credits={"qoder": 900, "trae": 100}
        )
        self.assertEqual(p2, "qoder")

    async def test_unknown_model(self) -> None:
        with self.assertRaises(Exception) as ctx:
            await self._route_with({"qoder": [], "trae": []}, "nope")
        self.assertIn("model_unroutable", str(getattr(ctx.exception, "code", "")))

    async def test_model_allowlist(self) -> None:
        restricted = self.store.verify_key(
            self.store.create_key("限定", models=["Auto"])["key"]
        )
        p, m, _ = await self._route_with({"qoder": ["Auto"]}, "qoder/Auto", key=restricted)
        self.assertEqual(p, "qoder")
        with self.assertRaises(Exception):
            await self._route_with({}, "gpt-4", key=restricted)


if __name__ == "__main__":
    unittest.main()
