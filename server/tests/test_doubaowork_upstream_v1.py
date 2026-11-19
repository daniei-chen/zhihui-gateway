"""v1 上游（`niyouqianma/doubaowork2api`）的能力接入测试。

v0.3.0 没有 `/admin/analytics`、`/admin/checkin`、`/quota`；v1 有。
适配器靠 `/health` 的 `version` 字段判断，再决定走哪条路。

这里钉死三件事：
1. **版本判定只在 `/health` 的 version 上**——不靠探端点猜（404 和 405 在
   httpx 里长得一样，猜不准）。
2. `usage()` 在 v1 上解析 `by_day`，在 v0.3.0 上如实报 unsupported。
3. `checkin()` 在 v1 上走上游，状态映射正确；v0.3.0 仍走本地实现。
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from server.qtgcore.adapters.doubaowork import DoubaoWorkAdapter


class FakeSettings:
    doubaowork_base = "http://127.0.0.1:18789"
    doubaowork_admin_password_file = "/nonexistent/admin_password"
    doubaowork_accounts_dir = "/nonexistent/accounts"
    upstream_timeout_sec = 5.0
    breaker_fail_threshold = 5
    breaker_cooldown_sec = 30


def make() -> DoubaoWorkAdapter:
    return DoubaoWorkAdapter(FakeSettings())


class VersionDetectionTest(unittest.TestCase):
    def test_v1_is_detected(self):
        a = make()
        a._upstream_version = "1.0.0"
        self.assertTrue(a._supports_v1())

    def test_v0_is_not(self):
        a = make()
        a._upstream_version = "0.3.0"
        self.assertFalse(a._supports_v1())

    def test_empty_version_is_not_supported(self):
        """`/health` 没有 version 字段（v0.3.0）→ 一律当不支持。"""
        a = make()
        self.assertEqual(a._upstream_version, "")
        self.assertFalse(a._supports_v1())

    def test_garbage_version_does_not_crash(self):
        for raw in ("abc", "1", "v1.0", "  2.1.0  "):
            a = make()
            a._upstream_version = raw
            self.assertIsInstance(a._supports_v1(), bool, raw)

    def test_healthz_records_version(self):
        a = make()
        with mock.patch.object(a, "_json",
                               return_value={"ok": True, "version": "1.2.3"}):
            import asyncio
            asyncio.run(a.healthz())
        self.assertEqual(a.upstream_version, "1.2.3")


class UsageTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_upstream_reports_unsupported(self):
        a = make()
        out = await a.usage(7)
        self.assertEqual(out["usage"], [])
        self.assertIn("usage", out["unsupported"])
        self.assertIn("不伪造", out["note"])

    async def test_v1_parses_by_day(self):
        a = make()
        a._upstream_version = "1.0.0"
        payload = {
            "days": 7,
            "by_day": [
                {"id": "2026-11-16", "requests": 3, "success": 3, "errors": 0,
                 "average_ms": 800, "input_tokens": 100, "output_tokens": 50},
                {"id": "2026-11-15", "requests": 1, "success": 0, "errors": 1,
                 "average_ms": 0, "input_tokens": 0, "output_tokens": 0},
            ],
            "by_model": [{"id": "person-deepseek-v4-1-flash-261017", "requests": 4}],
        }
        with mock.patch.object(a, "_admin", return_value=payload):
            out = await a.usage(7)
        self.assertEqual(len(out["usage"]), 2)
        first = out["usage"][0]
        self.assertEqual(first["day"], "2026-11-16")
        self.assertEqual(first["requests"], 3)
        self.assertEqual(first["prompt_tokens"], 100)
        self.assertEqual(first["completion_tokens"], 50)
        self.assertEqual(first["total_tokens"], 150)
        second = out["usage"][1]
        self.assertEqual(second["failed"], 1)
        # credits 仍然拿不到，必须如实标注
        self.assertIn("credits", out["unsupported"])
        self.assertNotIn("usage", out["unsupported"])

    async def test_analytics_unavailable_degrades_without_faking(self):
        a = make()
        a._upstream_version = "1.0.0"
        with mock.patch.object(a, "_admin", side_effect=RuntimeError("boom")):
            out = await a.usage(7)
        self.assertEqual(out["usage"], [])
        self.assertIn("不伪造", out["note"])

    async def test_days_is_clamped_to_upstream_limit(self):
        """上游硬限制 1~30，越界会 ValueError → 我们夹住，不让统计页挂掉。"""
        a = make()
        a._upstream_version = "1.0.0"
        seen = {}

        async def fake(method, path, **kw):
            seen["params"] = kw.get("params")
            return {"by_day": []}

        with mock.patch.object(a, "_admin", side_effect=fake):
            await a.usage(999)
        self.assertEqual(seen["params"]["days"], 30)


class UpstreamCheckinTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_mapping(self):
        a = make()
        a._upstream_version = "1.0.0"
        results = [
            {"id": "a1", "name": "A", "status": "done"},
            {"id": "a2", "name": "B", "status": "claimed", "points": 400},
            {"id": "a3", "name": "C", "status": "skipped"},
            {"id": "a4", "name": "D", "status": "busy"},
            {"id": "a5", "name": "E", "status": "manual_required", "code": "710010702"},
            {"id": "a6", "name": "F", "status": "verification"},
            {"id": "a7", "name": "G", "status": "error", "code": "500"},
            {"id": "a8", "name": "H", "status": "pending"},
            {"id": "a9", "name": "I", "status": "something_new"},
        ]

        async def fake_accounts():
            return []

        async def fake_admin(method, path, **kw):
            return {"results": results}

        with mock.patch.object(a, "_accounts", side_effect=fake_accounts), \
             mock.patch.object(a, "_admin", side_effect=fake_admin):
            out = await a.checkin()

        by_uid = {r["uid"]: r for r in out["results"]}
        self.assertEqual(by_uid["a1"]["status"], "claimed")
        self.assertTrue(by_uid["a2"]["ok"])
        self.assertEqual(by_uid["a2"]["reward"], 400)
        self.assertEqual(by_uid["a3"]["status"], "already")
        # 待确认：既不是成功，也不该被无脑重试
        for uid in ("a4", "a5", "a8"):
            self.assertEqual(by_uid[uid]["status"], "confirming", uid)
            self.assertFalse(by_uid[uid]["ok"], uid)
        # 真失败
        for uid in ("a6", "a7"):
            self.assertEqual(by_uid[uid]["status"], "fail", uid)
        # 未知状态不能当成成功
        self.assertEqual(by_uid["a9"]["status"], "fail")
        self.assertEqual(out["source"], "upstream")
        # `done` 和 `claimed` 都算「已确认签到」，所以 succeeded 是 2
        self.assertEqual((out["succeeded"], out["already"], out["failed"], out["confirming"]),
                         (2, 1, 3, 3))

    async def test_confirming_is_reported_not_hidden(self):
        a = make()
        a._upstream_version = "1.0.0"

        async def fake_admin(method, path, **kw):
            return {"results": [{"id": "x", "status": "manual_required"}]}

        with mock.patch.object(a, "_accounts", return_value=[]), \
             mock.patch.object(a, "_admin", side_effect=fake_admin):
            out = await a.checkin()
        self.assertFalse(out["ok"])
        self.assertIn("待确认", out["message"])

    async def test_broken_contract_is_not_guessed(self):
        """上游返回没有 results 的形状 → 明确报契约不符，不当成功。"""
        a = make()
        a._upstream_version = "1.0.0"
        with mock.patch.object(a, "_accounts", return_value=[]), \
             mock.patch.object(a, "_admin", return_value={"oops": True}):
            out = await a.checkin()
        self.assertFalse(out["ok"])
        self.assertIn("契约不符", out["message"])

    async def test_sends_ids_when_available(self):
        a = make()
        a._upstream_version = "1.0.0"
        seen = {}

        async def fake_admin(method, path, **kw):
            seen["body"] = kw.get("json_body")
            return {"results": []}

        with mock.patch.object(a, "_accounts",
                               return_value=[{"id": "acc-1"}, {"id": "acc-2"}]), \
             mock.patch.object(a, "_admin", side_effect=fake_admin):
            await a.checkin()
        self.assertEqual(seen["body"], {"ids": ["acc-1", "acc-2"]})

    async def test_old_upstream_does_not_call_upstream_checkin(self):
        """v0.3.0 必须走本地实现，不能去调一个不存在的端点。"""
        a = make()
        a._upstream_version = "0.3.0"
        called = {"admin": False}

        async def fake_admin(*args, **kw):
            called["admin"] = True
            return {}

        with mock.patch.object(a, "_admin", side_effect=fake_admin):
            out = await a.checkin()
        self.assertFalse(called["admin"], "v0.3.0 不该调 /admin/checkin")
        # 账号目录存在但没有可签账号（或不存在）→ 明确报失败，不假装成功
        self.assertFalse(out["ok"])
        self.assertIn("没有可签到的账号", out["message"])


if __name__ == "__main__":
    unittest.main()
