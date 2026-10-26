"""K26 读取通道行为测试。

覆盖：
  1. 默认 read_mode=sqlite（不改变现网行为）
  2. 非法 mode 回落 sqlite 且不抛
  3. http 模式下 read_contract 会调用 Manager 并透传契约
  4. http 模式失败时**抛异常而非回退 sqlite**（这是 K26 的核心约束）
  5. sqlite 模式下 read_contract 不发网络请求

跑法：python -m unittest qtg.tests.test_read_mode -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.qtg import manager_api  # noqa: E402
from app.qtg.adapters.workbuddy import WorkBuddyAdapter  # noqa: E402
from app.qtg.config import Settings  # noqa: E402

# R1-A（K26 v2）：适配器每次 http 读取前都会探测并校验 Manager 的读取契约
# （版本/能力）。本文件只测"通道分流"本身；契约校验由
# tests/test_http_read_strict.py / test_read_token_split.py 专门覆盖，
# 因此这里统一把契约校验打桩，避免每个用例都要起一个契约应答。
_GATE_PATCH = None


def setUpModule() -> None:  # noqa: N802 - unittest 约定
    global _GATE_PATCH
    _GATE_PATCH = mock.patch.object(
        WorkBuddyAdapter, "_ensure_read_capability", new=mock.AsyncMock())
    _GATE_PATCH.start()


def tearDownModule() -> None:  # noqa: N802 - unittest 约定
    if _GATE_PATCH is not None:
        _GATE_PATCH.stop()


def _settings(**over) -> Settings:
    base = dict(
        wb_api_base="http://127.0.0.1:7863",
        wb_panel_base="http://127.0.0.1:7864",
        wb_config_file="/nonexistent/config.json",
        wb_auths_dir="/nonexistent/auths",
        wb_state_file="/nonexistent/state.json",
        wb_db="/nonexistent/manager.db",
        manager_service_token_file="/nonexistent/token",
        manager_read_token_file="/nonexistent/read-token",
    )
    base.update(over)
    return Settings(**base)


class ReadModeTest(unittest.TestCase):
    def test_default_is_sqlite(self) -> None:
        """默认必须仍是 sqlite —— 迁移未完成前不得改变现网行为。"""
        s = _settings()
        self.assertEqual(s.wb_read_mode, "sqlite")
        self.assertEqual(WorkBuddyAdapter(s).read_mode, "sqlite")

    def test_invalid_mode_falls_back_to_sqlite(self) -> None:
        a = WorkBuddyAdapter(_settings(wb_read_mode="carrier-pigeon"))
        self.assertEqual(a.read_mode, "sqlite")
        self.assertFalse(a._http_reads_enabled())

    def test_http_mode_enables_http_reads(self) -> None:
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        self.assertEqual(a.read_mode, "http")
        self.assertTrue(a._http_reads_enabled())

    def test_read_contract_sqlite_makes_no_request(self) -> None:
        """sqlite 模式不得发网络请求（否则会泄露"要用 HTTP"的错误期望）。"""
        a = WorkBuddyAdapter(_settings())
        with mock.patch.object(manager_api, "read_api_version",
                               new=mock.AsyncMock()) as patched:
            got = asyncio.run(a.read_contract())
        patched.assert_not_called()
        self.assertEqual(got["mode"], "sqlite")
        self.assertIsNone(got["version"])

    def test_read_contract_http_passes_through(self) -> None:
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        contract = {"version": 1, "capabilities": {"stats": True},
                    "timezone": "CST", "credit_unit": "float"}
        with mock.patch.object(manager_api, "read_api_version",
                               new=mock.AsyncMock(return_value=contract)) as patched:
            got = asyncio.run(a.read_contract())
        patched.assert_called_once()
        self.assertEqual(got["mode"], "http")
        self.assertEqual(got["version"], 1)
        self.assertEqual(got["capabilities"], {"stats": True})

    def test_http_failure_does_not_fall_back_to_sqlite(self) -> None:
        """核心约束：HTTP 不可用时抛错，**不得**静默回退直读 DB。"""
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        boom = manager_api.UpstreamError("Manager 密钥服务不可用")
        with mock.patch.object(manager_api, "read_api_version",
                               new=mock.AsyncMock(side_effect=boom)):
            with self.assertRaises(manager_api.UpstreamError):
                asyncio.run(a.read_contract())
        # 若实现悄悄回退了 sqlite，这里会拿到 sqlite 模式的返回值而不是异常

    def test_checkin_rows_http_maps_fields(self) -> None:
        """http 模式下签到记录与直连 SQL 一样**按天聚合**（R1-A 统一口径）。"""
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        payload = {"items": [
            {"ts": 1790000000, "uid": "u1", "nickname": "n1", "source": "auto",
             "kind": "daily", "success": True, "code": "0", "message": "ok"},
        ]}
        with mock.patch.object(manager_api, "read_checkin_logs",
                               new=mock.AsyncMock(return_value=payload)):
            rows = asyncio.run(a._checkin_rows_via_mode())
        self.assertEqual(len(rows), 1)
        day = rows[0]
        self.assertEqual(day["total"], 1)
        self.assertEqual(day["ok"], 1)
        self.assertEqual(day["failed"], 0)
        item = day["items"][0]
        self.assertEqual(item["uid"], "u1")
        self.assertEqual(item["name"], "n1")
        self.assertEqual(item["status"], "success")
        self.assertEqual(item["message"], "ok")

    def test_checkin_rows_http_bad_shape_raises(self) -> None:
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        with mock.patch.object(manager_api, "read_checkin_logs",
                               new=mock.AsyncMock(return_value={"items": "not-a-list"})):
            with self.assertRaises(manager_api.UpstreamError):
                asyncio.run(a._checkin_rows_via_mode())

    def test_checkin_rows_sqlite_uses_local_db(self) -> None:
        """sqlite 模式仍走本地实现（现有行为不变）。"""
        a = WorkBuddyAdapter(_settings())
        with mock.patch.object(WorkBuddyAdapter, "_checkin_rows",
                               return_value=[{"uid": "local"}]) as patched, \
             mock.patch.object(manager_api, "read_checkin_logs",
                               new=mock.AsyncMock()) as http:
            rows = asyncio.run(a._checkin_rows_via_mode())
        patched.assert_called_once()
        http.assert_not_called()
        self.assertEqual(rows, [{"uid": "local"}])

    # ---------- K26 第二批：keys/logs/task_logs/audit_logs/models ----------

    def test_keys_http_uses_manager_api(self) -> None:
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        rows = [{"id": 1, "name": "k1", "prefix": "wbk_abc"}]
        with mock.patch.object(manager_api, "read_api_keys",
                               new=mock.AsyncMock(return_value=rows)) as patched:
            got = asyncio.run(a.keys())
        patched.assert_called_once()
        self.assertEqual(got["data"]["keys"], rows)

    def test_keys_http_bad_shape_raises(self) -> None:
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        with mock.patch.object(manager_api, "read_api_keys",
                               new=mock.AsyncMock(return_value={"not": "a list"})):
            with self.assertRaises(manager_api.UpstreamError):
                asyncio.run(a.keys())

    def test_keys_sqlite_does_not_call_http(self) -> None:
        a = WorkBuddyAdapter(_settings())
        with mock.patch.object(manager_api, "read_api_keys",
                               new=mock.AsyncMock()) as patched:
            asyncio.run(a.keys())
        patched.assert_not_called()

    def test_logs_http_normalizes_list(self) -> None:
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        with mock.patch.object(manager_api, "read_logs",
                               new=mock.AsyncMock(return_value=[{"id": 1}])):
            got = asyncio.run(a.logs(10))
        self.assertEqual(got["logs"], [{"id": 1}])
        self.assertEqual(got["total"], 1)

    def test_logs_http_passes_through_dict(self) -> None:
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        payload = {"logs": [{"id": 2}], "total": 1}
        with mock.patch.object(manager_api, "read_logs",
                               new=mock.AsyncMock(return_value=payload)):
            got = asyncio.run(a.logs(10))
        self.assertEqual(got, payload)

    def test_task_logs_http(self) -> None:
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        with mock.patch.object(manager_api, "read_task_logs",
                               new=mock.AsyncMock(return_value={"logs": [], "total": 0})):
            got = asyncio.run(a.task_logs(5))
        self.assertEqual(got["total"], 0)

    def test_audit_logs_http(self) -> None:
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        with mock.patch.object(manager_api, "read_audit_logs",
                               new=mock.AsyncMock(return_value=[{"action": "login"}])):
            got = asyncio.run(a.audit_logs(5))
        self.assertEqual(got["total"], 1)

    def test_models_http(self) -> None:
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        with mock.patch.object(manager_api, "read_models",
                               new=mock.AsyncMock(return_value={"models": [{"id": "m"}]})):
            got = asyncio.run(a.models())
        self.assertEqual(got["models"], [{"id": "m"}])

    def test_stats_family_http_failure_propagates(self) -> None:
        """统计族在 http 模式下失败必须抛错，不得静默返回空。"""
        a = WorkBuddyAdapter(_settings(wb_read_mode="http"))
        boom = manager_api.UpstreamError("stats down")
        with mock.patch.object(manager_api, "read_stats",
                               new=mock.AsyncMock(side_effect=boom)):
            with self.assertRaises(manager_api.UpstreamError):
                asyncio.run(a.usage_summary("cn"))

    def test_stats_family_sqlite_uses_local_sql_only(self) -> None:
        """sqlite 模式下 _stats_via_http 不再返回 None 哨兵（RV05）。

        旧实现用「返回 None」表示"调用方请回退本地 SQL"，而 HTTP 404 也被翻译成
        None，两个语义共用一个哨兵 → http 模式 404 时静默直读 SQLite。现在通道判定
        只由调用方开头的 `_http_reads_enabled()` 负责，sqlite 模式下调用本助手属于
        内部错误（RuntimeError），且统计族读方法本身不会发 HTTP。
        """
        a = WorkBuddyAdapter(_settings())
        with self.assertRaises(RuntimeError):
            asyncio.run(a._stats_via_http("summary", None))
        with mock.patch.object(manager_api, "read_stats",
                               new=mock.AsyncMock()) as patched:
            with mock.patch.object(WorkBuddyAdapter, "_db", return_value=None):
                asyncio.run(a.usage_summary("cn"))
        patched.assert_not_called()


if __name__ == "__main__":
    unittest.main()
