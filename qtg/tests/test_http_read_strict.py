"""RV05 回归（K26）：http 读模式下任何失败都不得静默回退 SQLite。

缺陷回顾（2026-10-25 独立复核）
------------------------------
`manager_api._request` 把 HTTP 404 翻译成 `None`；`WorkBuddyAdapter._stats_via_http`
又用 `None` 表示"当前是 sqlite 模式，调用方请回退本地 SQL"。两个语义共用一个哨兵，
于是 **`wb_read_mode=http` + 上游 404** 时 `usage_summary()` 会真的调用 `_db()` 直读
Manager 的 SQLite，无库时返回 `{}` —— 把"读不到"伪装成"没有数据"。

本文件锁定修复后的契约：
  1. 六种失败形态（404 / 500 / 超时 / 连接拒绝 / 非 JSON / `null`·`{}` body）
     × 四个读方法（usage_summary / usage_hourly / credits_flow / overview）：
     异常向上抛、**类型与消息可区分**、`_db()` 调用次数为 0；
  2. 其余 HTTP 读分支（keys / logs / task_logs / audit_logs / settings / models /
     by-model / by-key）同样不回退 `_db()`；
  3. 结构异常（非 dict/list、缺必需字段）显式抛 UpstreamPayloadError，不当空值；
  4. 写路径的"目标不存在"语义仍然保留：只有 delete_key / update_key / reset_usage
     显式声明 allow_not_found，404 → False/None，其它调用 404 一律抛错；
  5. sqlite 模式行为不变（读本地库、不发 HTTP 请求）；
  6. 二义性哨兵已取消：sqlite 模式下 `_stats_via_http` 抛 RuntimeError（不再返回
     None），http 模式下 `_db()` 直接抛 RuntimeError，未迁移的读方法抛
     ReadChannelNotMigrated。

跑法：python -X utf8 tests/zhihui_harness/run_tests.py --target qtg \
        --test-path=tests/test_http_read_strict.py --keep
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.qtg import manager_api  # noqa: E402
from app.qtg.adapters.workbuddy import WorkBuddyAdapter  # noqa: E402
from app.qtg.config import Settings  # noqa: E402
from app.qtg.errors import (  # noqa: E402
    UpstreamError,
    UpstreamNotFound,
    UpstreamPayloadError,
    UpstreamTimeout,
    UpstreamUnauthorized,
)

# ---------------------------------------------------------------- 测试夹具

# R1-A：读/写两种服务凭据分开（scope=read / scope=keys）
READ_TOKEN = "synthetic-qtg-read-token"
KEYS_TOKEN = "synthetic-qtg-keys-token"

# 契约探针的固定应答（v2 + 全部能力）。真正的失败形态测试都作用在业务端点上；
# 契约探针由 `_client_factory` 统一路由，避免每个用例都手工拼一份。
CONTRACT_V2 = {
    "version": 2,
    "capabilities": {"stats": True, "keys": True, "logs": True, "tasks": True,
                     "checkin": True, "audit": True, "accounts": True,
                     "models": True, "settings": True, "usage": True},
    "timezone": "CST", "credit_unit": "float", "timestamp_unit": "epoch_seconds",
    "pagination": {"style": "offset_limit", "max_days": 3650},
}


def _settings(token_file: str, read_token_file: str | None = None, **over) -> Settings:
    base = dict(
        wb_api_base="http://127.0.0.1:7863",
        wb_panel_base="http://127.0.0.1:7864",
        wb_config_file="/nonexistent/config.json",
        wb_auths_dir="/nonexistent/auths",
        wb_state_file="/nonexistent/state.json",
        wb_db="/nonexistent/manager.db",
        manager_service_token_file=token_file,
        manager_read_token_file=read_token_file or token_file,
    )
    base.update(over)
    return Settings(**base)


def _client_factory(handler, *, contract=CONTRACT_V2):
    """把 `manager_api._client` 换成 MockTransport 客户端（真实走 _request 的翻译逻辑）。

    默认把契约探针 `/api/read-api/version` 路由到固定 v2 应答——R1-A 起适配器每次
    读取前都会校验契约。要测"契约本身失败"的用例传 `contract=None`。
    """
    import json

    def routed(request: httpx.Request) -> httpx.Response:
        if contract is not None and request.url.path == "/api/read-api/version":
            return httpx.Response(200, content=json.dumps(contract).encode("utf-8"),
                                  headers={"content-type": "application/json"})
        return handler(request)

    def factory(timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(routed),
            timeout=timeout if timeout is not None else 10.0,
        )
    return factory


def _respond(status: int, body: bytes = b"") -> object:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)
    return handler


def _raise(exc: Exception) -> object:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc
    return handler


# 六种失败形态 + 期望的（异常类型, code, 上游状态码, 消息特征）
def _failure_forms() -> list[tuple[str, object, type, str, int | None, str]]:
    return [
        ("404_not_found", _respond(404, b'{"detail":"Not Found"}'),
         UpstreamNotFound, "upstream_not_found", 404, "HTTP 404"),
        ("500_server_error", _respond(500, b'{"detail":"boom"}'),
         UpstreamError, "upstream_server_error", 500, "HTTP 500"),
        ("timeout", _raise(httpx.ReadTimeout("read timed out")),
         UpstreamTimeout, "upstream_timeout", None, "超时"),
        ("connection_refused", _raise(httpx.ConnectError("Connection refused")),
         UpstreamError, "upstream_unreachable", None, "连接失败"),
        ("non_json", _respond(200, b"<html>not json</html>"),
         UpstreamPayloadError, "upstream_payload_error", 200, "非 JSON"),
        ("json_null", _respond(200, b"null"),
         UpstreamPayloadError, "upstream_payload_error", 200, "JSON null"),
        ("empty_object", _respond(200, b"{}"),
         UpstreamPayloadError, "upstream_payload_error", 200, "空对象"),
    ]


SUMMARY_OK = {
    "today_requests": 3, "today_tokens": 30, "today_credit": 0.3,
    "week_requests": 9, "week_tokens": 90, "week_credit": 0.9,
    "total_requests": 105, "total_tokens": 1050, "total_credit": 10.5,
    "active_keys": 1, "top_model": "m",
}
HOURLY_OK = [{"day": "2099-01-01", "hour": h, "requests": 1,
              "prompt_tokens": 10, "completion_tokens": 5,
              "credit": 0.1, "failed": 0} for h in range(24)]
CREDITS_OK = {
    "days": 7, "from": "2099-01-01", "to": "2099-01-07",
    "today": {"income": 1.0, "expense": 0.5, "net": 0.5},
    "week": {"income": 7.0, "expense": 3.5, "net": 3.5},
    "total": {"income": 7.0, "expense": 3.5, "net": 3.5},
    "daily": [{"day": "2099-01-01", "income": 1.0, "expense": 0.5, "net": 0.5}],
}
CHECKIN_OK = {"items": [{"ts": 1790000000, "uid": "u1", "nickname": "n1",
                         "source": "auto", "kind": "daily", "success": True,
                         "code": "0", "message": "ok"}], "total": 1}

# 读方法名 → (调用, http 成功载荷)
HTTP_READERS: list[tuple[str, object, object]] = [
    ("usage_summary", lambda a: a.usage_summary(), SUMMARY_OK),
    ("usage_hourly", lambda a: a.usage_hourly(), HOURLY_OK),
    ("credits_flow", lambda a: a.credits_flow(7), CREDITS_OK),
    ("overview", lambda a: a.overview(), CHECKIN_OK),
]


def _json_handler(payload) -> object:
    import json

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode("utf-8"),
                              headers={"content-type": "application/json"})
    return handler


def _path_router(payloads: dict[str, object]) -> object:
    """按路径返回不同载荷（v2 的 usage 族有两个端点，不能一个 handler 走天下）。"""
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in payloads:
            return httpx.Response(
                200, content=json.dumps(payloads[path]).encode("utf-8"),
                headers={"content-type": "application/json"})
        return httpx.Response(404, content=b'{"detail":"no route"}')
    return handler


class _TokenMixin:
    """每个用例一个临时 token 文件（_headers 会真的读它）。"""

    def setUp(self) -> None:  # noqa: N802 - unittest 约定
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.token_file = str(Path(self._tmp.name) / "manager_service_token")
        Path(self.token_file).write_text(KEYS_TOKEN, encoding="utf-8")
        self.read_token_file = str(Path(self._tmp.name) / "manager_read_token")
        Path(self.read_token_file).write_text(READ_TOKEN, encoding="utf-8")

    def http_adapter(self, **over) -> WorkBuddyAdapter:
        return WorkBuddyAdapter(_settings(self.token_file, self.read_token_file,
                                          wb_read_mode="http", **over))

    def sqlite_adapter(self, db: str, **over) -> WorkBuddyAdapter:
        return WorkBuddyAdapter(_settings(self.token_file, self.read_token_file,
                                          wb_read_mode="sqlite", wb_db=db, **over))


# ---------------------------------------------------------------- 六种失败形态
class HttpFailureNeverFallsBackTest(_TokenMixin, unittest.TestCase):
    """RV05 的核心断言：六种失败 × 四个读方法，异常向上抛且 _db() 零调用。"""

    def test_six_failure_forms_x_four_readers(self) -> None:
        for method_name, call, _ in HTTP_READERS:
            for (form, handler, exc_type, code, status,
                 marker) in _failure_forms():
                with self.subTest(method=method_name, form=form):
                    adapter = self.http_adapter()
                    with mock.patch.object(manager_api, "_client",
                                           _client_factory(handler)), \
                         mock.patch.object(WorkBuddyAdapter, "_db",
                                           return_value=None) as db_spy:
                        with self.assertRaises(exc_type) as ctx:
                            asyncio.run(call(adapter))
                    exc = ctx.exception
                    self.assertEqual(db_spy.call_count, 0,
                                     f"{method_name}/{form}: http 模式不得调用 _db()")
                    self.assertEqual(exc.code, code,
                                     f"{method_name}/{form}: code 不符")
                    self.assertEqual(exc.upstream_status, status,
                                     f"{method_name}/{form}: upstream_status 不符")
                    self.assertIn(marker, str(exc),
                                  f"{method_name}/{form}: 消息应含 {marker!r}")

    def test_failure_categories_are_distinguishable(self) -> None:
        """五类事故的（类型, code）必须两两不同；载荷类的三种形态靠消息区分。"""
        seen = {}
        for form, handler, exc_type, code, _status, _marker in _failure_forms():
            with self.subTest(form=form):
                adapter = self.http_adapter()
                with mock.patch.object(manager_api, "_client",
                                       _client_factory(handler)):
                    try:
                        asyncio.run(adapter.usage_summary())
                    except UpstreamError as exc:
                        seen[form] = (type(exc).__name__, exc.code, str(exc))
                    else:  # pragma: no cover - 失败即测试失败
                        self.fail(f"{form}: 没有抛异常（静默失败了）")
        # 404 / 500 / 超时 / 连接拒绝 / 载荷 五类两两不同
        signatures = {
            "404_not_found": (seen["404_not_found"][0], seen["404_not_found"][1]),
            "500_server_error": (seen["500_server_error"][0], seen["500_server_error"][1]),
            "timeout": (seen["timeout"][0], seen["timeout"][1]),
            "connection_refused": (seen["connection_refused"][0],
                                   seen["connection_refused"][1]),
            "payload": (seen["json_null"][0], seen["json_null"][1]),
        }
        self.assertEqual(len(set(signatures.values())), len(signatures),
                         f"失败类型不可区分：{signatures}")
        # 载荷类内部：非 JSON / null / {} 三种消息必须互不相同
        payload_messages = {seen[k][2] for k in ("non_json", "json_null", "empty_object")}
        self.assertEqual(len(payload_messages), 3,
                         f"载荷异常消息不可区分：{payload_messages}")

    def test_unauthorized_is_not_not_found(self) -> None:
        """401/403 与 404 必须是不同异常类型/消息（RV05 第 1 条）。"""
        for status in (401, 403):
            with self.subTest(status=status):
                adapter = self.http_adapter()
                handler = _respond(status, b'{"detail":"scope missing"}')
                with mock.patch.object(manager_api, "_client",
                                       _client_factory(handler)), \
                     mock.patch.object(WorkBuddyAdapter, "_db",
                                       return_value=None) as db_spy:
                    with self.assertRaises(UpstreamUnauthorized) as ctx:
                        asyncio.run(adapter.usage_summary())
                self.assertEqual(ctx.exception.upstream_status, status)
                self.assertIn(f"HTTP {status}", str(ctx.exception))
                self.assertIn("未被 Manager 授权", str(ctx.exception))
                self.assertEqual(db_spy.call_count, 0)
                self.assertNotIsInstance(ctx.exception, UpstreamNotFound)


# ---------------------------------------------------------------- 其它读分支
class OtherHttpReadersNeverFallBackTest(_TokenMixin, unittest.TestCase):
    """11 类 HTTP 读分支逐处核对：http 模式 404 时没有一条能触达 _db()。"""

    def _assert_no_db(self, label: str, call) -> None:
        adapter = self.http_adapter()
        with mock.patch.object(manager_api, "_client",
                               _client_factory(_respond(404, b'{"detail":"nf"}'))), \
             mock.patch.object(WorkBuddyAdapter, "_db",
                               return_value=None) as db_spy:
            with self.assertRaises(UpstreamNotFound):
                asyncio.run(call(adapter))
        self.assertEqual(db_spy.call_count, 0, f"{label}: http 模式不得调用 _db()")

    def test_all_read_branches(self) -> None:
        cases = [
            ("overview", lambda a: a.overview()),
            ("accounts", lambda a: a.accounts()),
            ("keys", lambda a: a.keys()),
            ("logs", lambda a: a.logs(10)),
            ("task_logs", lambda a: a.task_logs(10)),
            ("audit_logs", lambda a: a.audit_logs(10)),
            ("settings", lambda a: a.settings()),
            ("models", lambda a: a.models()),
            ("usage_summary", lambda a: a.usage_summary()),
            ("usage_hourly", lambda a: a.usage_hourly()),
            ("credits_flow", lambda a: a.credits_flow(7)),
            ("usage_by_model", lambda a: a.usage_by_model(7)),
            ("usage_by_key", lambda a: a.usage_by_key(7)),
            ("usage", lambda a: a.usage(14)),
            ("usage_analytics", lambda a: a.usage_analytics()),
        ]
        for label, call in cases:
            with self.subTest(reader=label):
                self._assert_no_db(label, call)

    def test_read_contract_404_is_explicit(self) -> None:
        """契约探针自己 404 时也必须显式失败（不许当成"没能力就随便读"）。"""
        adapter = self.http_adapter()
        with mock.patch.object(manager_api, "_client",
                               _client_factory(
                                   _respond(404, b'{"detail":"nf"}'),
                                   contract=None)), \
             mock.patch.object(WorkBuddyAdapter, "_db", return_value=None) as db_spy:
            with self.assertRaises(UpstreamNotFound):
                asyncio.run(adapter.read_contract())
        self.assertEqual(db_spy.call_count, 0)

    def test_usage_family_now_migrated(self) -> None:
        """R1-A：usage / usage_analytics 已迁到 Manager v2，不再 501。"""
        matrix = {
            "version": 2, "since": "2020-01-01",
            "rows": [{"day": "2099-01-01", "model": "m", "requests": 3,
                      "prompt_tokens": 30, "completion_tokens": 15,
                      "credit": 0.5, "realm": "cn"}],
            "failures": {"2099-01-01": 1},
        }
        totals = {"version": 2, "requests": 105, "prompt_tokens": 1050,
                  "completion_tokens": 505, "credits": 10.5}
        for label, call, expected in (
            ("usage", lambda a: a.usage(14), ("usage", "credits_daily")),
            ("usage_analytics", lambda a: a.usage_analytics(), ("summary",)),
        ):
            with self.subTest(reader=label):
                adapter = self.http_adapter()
                handler = _path_router({
                    "/api/read-api/usage-matrix": matrix,
                    "/api/read-api/usage-totals": totals,
                })
                with mock.patch.object(manager_api, "_client",
                                       _client_factory(handler)), \
                     mock.patch.object(WorkBuddyAdapter, "_db",
                                       return_value=None) as db_spy:
                    got = asyncio.run(call(adapter))
                self.assertEqual(db_spy.call_count, 0)
                for key in expected:
                    self.assertIn(key, got)
        # 数值口径：usage 的按天聚合与失败数、analytics 的全表拆分
        adapter = self.http_adapter()
        handler = _path_router({
            "/api/read-api/usage-matrix": matrix,
            "/api/read-api/usage-totals": totals,
        })
        with mock.patch.object(manager_api, "_client", _client_factory(handler)):
            usage = asyncio.run(adapter.usage(14))
            analytics = asyncio.run(adapter.usage_analytics())
        day = usage["usage"][0]
        self.assertEqual(day["date"], "2099-01-01")
        self.assertEqual(day["requests"], 3)
        self.assertEqual(day["errors"], 1)
        self.assertEqual(day["ok"], 2)
        self.assertEqual(day["credit"], 0.5)
        self.assertEqual(day["models"][0]["name"], "m")
        self.assertEqual(analytics["summary"]["requests"], 105)
        self.assertEqual(analytics["summary"]["prompt_tokens"], 1050)
        self.assertEqual(analytics["summary"]["completion_tokens"], 505)
        self.assertAlmostEqual(analytics["summary"]["credits"], 10.5)

    def test_usage_family_requires_v2_contract(self) -> None:
        """Manager 只声明 v1 时，usage 族必须显式失败（不许降级/回退）。"""
        adapter = self.http_adapter()
        v1 = {"version": 1, "capabilities": {k: True for k in (
            "stats", "keys", "logs", "tasks", "checkin", "audit", "accounts",
            "models", "settings")}}
        with mock.patch.object(manager_api, "_client",
                               _client_factory(_respond(404), contract=v1)), \
             mock.patch.object(WorkBuddyAdapter, "_db", return_value=None) as db_spy:
            with self.assertRaises(UpstreamError) as ctx:
                asyncio.run(adapter.usage(14))
        self.assertEqual(ctx.exception.code, "read_api_unsupported")
        self.assertIn("usage", str(ctx.exception))
        self.assertEqual(db_spy.call_count, 0)

    def test_unknown_contract_version_fails_loudly(self) -> None:
        """未知契约版本：任何读取都必须显式失败（不按旧语义猜）。"""
        adapter = self.http_adapter()
        weird = {"version": 99, "capabilities": {"stats": True}}
        with mock.patch.object(manager_api, "_client",
                               _client_factory(_respond(404), contract=weird)), \
             mock.patch.object(WorkBuddyAdapter, "_db", return_value=None) as db_spy:
            with self.assertRaises(UpstreamError) as ctx:
                asyncio.run(adapter.usage_summary())
        self.assertEqual(ctx.exception.code, "read_api_unsupported")
        self.assertIn("未知", str(ctx.exception))
        self.assertEqual(db_spy.call_count, 0)

    def test_missing_capability_fails_loudly(self) -> None:
        adapter = self.http_adapter()
        no_logs = {"version": 2, "capabilities": {"stats": True, "logs": False}}
        with mock.patch.object(manager_api, "_client",
                               _client_factory(_respond(404), contract=no_logs)), \
             mock.patch.object(WorkBuddyAdapter, "_db", return_value=None) as db_spy:
            with self.assertRaises(UpstreamError) as ctx:
                asyncio.run(adapter.logs(10))
        self.assertEqual(ctx.exception.code, "read_api_unsupported")
        self.assertIn("logs", str(ctx.exception))
        self.assertEqual(db_spy.call_count, 0)

    def test_http_success_paths_use_manager_and_not_db(self) -> None:
        """正向对照：http 通路本身仍然可用，且不碰 _db()。"""
        for method_name, call, payload in HTTP_READERS:
            with self.subTest(reader=method_name):
                adapter = self.http_adapter()
                with mock.patch.object(manager_api, "_client",
                                       _client_factory(_json_handler(payload))), \
                     mock.patch.object(WorkBuddyAdapter, "_db",
                                       return_value=None) as db_spy:
                    got = asyncio.run(call(adapter))
                self.assertEqual(db_spy.call_count, 0)
                self.assertNotIn(got, ({}, [], None))
                if method_name == "usage_summary":
                    self.assertEqual(got["total_requests"], 105)
                    self.assertEqual(got["realm"], "")
                elif method_name == "usage_hourly":
                    self.assertEqual(len(got), 24)
                    self.assertEqual(got[0]["hour"], 0)
                elif method_name == "credits_flow":
                    self.assertEqual(got["days"], 7)
                    self.assertEqual(got["total"]["income"], 7.0)
                else:
                    # 签到记录与 sqlite 通道一样**按天聚合**（R1-A 统一口径）
                    self.assertEqual(len(got["checkin"]), 1)
                    self.assertEqual(got["checkin"][0]["total"], 1)
                    self.assertEqual(got["checkin"][0]["items"][0]["uid"], "u1")

    def test_other_readers_success_paths(self) -> None:
        """正向对照：其余 HTTP 读分支在合法载荷下照常返回，且不碰 _db()。"""
        cases = [
            ("keys", lambda a: a.keys(),
             [{"id": 1, "prefix": "wbk_abc", "name": "k"}],
             lambda got: got["data"]["keys"][0]["prefix"], "wbk_abc"),
            ("logs", lambda a: a.logs(10),
             {"items": [{"id": 1}], "total": 1},
             lambda got: got["total"], 1),
            ("task_logs", lambda a: a.task_logs(10),
             {"logs": [{"id": 2}], "total": 1},
             lambda got: got["total"], 1),
            ("audit_logs", lambda a: a.audit_logs(10),
             {"items": [{"action": "login"}], "total": 1},
             lambda got: got["items"][0]["action"], "login"),
            ("settings", lambda a: a.settings(),
             {"panel_key": "v", "panel_value": "1"},
             lambda got: got["panel"]["panel_key"], "v"),
            ("models", lambda a: a.models(),
             {"models": [{"id": "m"}], "count": 1},
             lambda got: got["models"][0]["id"], "m"),
            ("read_contract", lambda a: a.read_contract(),
             {"version": 1, "capabilities": {"stats": True}},
             lambda got: got["version"], 2),
            ("usage_by_model", lambda a: a.usage_by_model(7),
             [{"name": "m", "requests": 1, "prompt_tokens": 10,
               "completion_tokens": 5, "credit": 0.1}],
             lambda got: got[0]["name"], "m"),
            ("usage_by_key", lambda a: a.usage_by_key(7),
             [{"name": "wbk_abc", "requests": 1, "prompt_tokens": 10,
               "completion_tokens": 5, "credit": 0.1}],
             lambda got: got[0]["name"], "wbk_abc"),
        ]
        for label, call, payload, getter, expected in cases:
            with self.subTest(reader=label):
                adapter = self.http_adapter()
                with mock.patch.object(manager_api, "_client",
                                       _client_factory(_json_handler(payload))), \
                     mock.patch.object(WorkBuddyAdapter, "_db",
                                       return_value=None) as db_spy:
                    got = asyncio.run(call(adapter))
                self.assertEqual(db_spy.call_count, 0)
                self.assertEqual(getter(got), expected)

    def test_shape_anomalies_raise_payload_error(self) -> None:
        """结构异常显式抛错，不当空值（RV05 第 3 条）。"""
        cases = [
            ("summary_not_object", "summary", [1, 2, 3]),
            ("summary_missing_field", "summary", {"total_requests": 1}),
            ("hourly_not_array", "hourly", {"rows": []}),
            ("hourly_item_missing_hour", "hourly", [{"day": "d", "requests": 1}]),
        ]
        for label, name, payload in cases:
            with self.subTest(case=label):
                adapter = self.http_adapter()
                with mock.patch.object(manager_api, "_client",
                                       _client_factory(_json_handler(payload))), \
                     mock.patch.object(WorkBuddyAdapter, "_db",
                                       return_value=None) as db_spy:
                    with self.assertRaises(UpstreamPayloadError):
                        if name == "summary":
                            asyncio.run(adapter.usage_summary())
                        else:
                            asyncio.run(adapter.usage_hourly())
                self.assertEqual(db_spy.call_count, 0)

    def test_checkin_non_object_item_raises(self) -> None:
        """签到日志里出现非对象条目时必须报错，而不是静默跳过。"""
        adapter = self.http_adapter()
        payload = {"items": [{"ts": 1, "uid": "u1"}, "garbage"], "total": 2}
        with mock.patch.object(manager_api, "_client",
                               _client_factory(_json_handler(payload))), \
             mock.patch.object(WorkBuddyAdapter, "_db", return_value=None) as db_spy:
            with self.assertRaises(UpstreamPayloadError):
                asyncio.run(adapter.overview())
        self.assertEqual(db_spy.call_count, 0)


# ---------------------------------------------------------------- manager_api 语义
class RequestSemanticsTest(_TokenMixin, unittest.TestCase):
    """_request 的严格默认 + allow_not_found 白名单。"""

    def _call(self, handler, coro_factory, *, contract=CONTRACT_V2):
        with mock.patch.object(manager_api, "_client",
                               _client_factory(handler, contract=contract)):
            return asyncio.run(coro_factory())

    def test_delete_key_404_is_false(self) -> None:
        settings = _settings(self.token_file, self.read_token_file)
        got = self._call(_respond(404, b'{"detail":"nf"}'),
                         lambda: manager_api.delete_key(settings, 7))
        self.assertFalse(got)

    def test_delete_key_204_is_true(self) -> None:
        settings = _settings(self.token_file, self.read_token_file)
        got = self._call(_respond(204), lambda: manager_api.delete_key(settings, 7))
        self.assertTrue(got)

    def test_update_key_404_is_none(self) -> None:
        settings = _settings(self.token_file, self.read_token_file)
        got = self._call(_respond(404, b'{"detail":"nf"}'),
                         lambda: manager_api.update_key(settings, 7, {"name": "x"}))
        self.assertIsNone(got)

    def test_reset_usage_404_is_false(self) -> None:
        settings = _settings(self.token_file, self.read_token_file)
        got = self._call(_respond(404, b'{"detail":"nf"}'),
                         lambda: manager_api.reset_usage(settings, 7))
        self.assertFalse(got)

    def test_read_paths_reject_404(self) -> None:
        """读路径不允许把 404 映射成 None（这正是缺陷的源头）。"""
        settings = _settings(self.token_file, self.read_token_file)
        readers = {
            "read_api_version": lambda: manager_api.read_api_version(settings),
            "read_api_health": lambda: manager_api.read_api_health(settings),
            "read_stats": lambda: manager_api.read_stats(settings, "summary"),
            "read_logs": lambda: manager_api.read_logs(settings),
            "read_api_keys": lambda: manager_api.read_api_keys(settings),
            "read_settings_panel": lambda: manager_api.read_settings_panel(settings),
            "list_keys": lambda: manager_api.list_keys(settings),
        }
        for label, call in readers.items():
            with self.subTest(reader=label):
                with self.assertRaises(UpstreamNotFound):
                    # 契约探针路径本身要 404，所以这里关掉契约路由
                    self._call(_respond(404, b'{"detail":"nf"}'), call,
                               contract=None)

    def test_empty_body_is_payload_error(self) -> None:
        """200/202 + 空 body 不是"没有数据"，是载荷异常（204 例外，见下）。"""
        settings = _settings(self.token_file, self.read_token_file)
        for status in (200, 202):
            with self.subTest(status=status):
                with self.assertRaises(UpstreamPayloadError) as ctx:
                    self._call(_respond(status, b""),
                               lambda: manager_api.read_api_version(settings),
                               contract=None)
                self.assertIn("空 body", str(ctx.exception))
                self.assertEqual(ctx.exception.upstream_status, status)

    def test_204_is_empty_object(self) -> None:
        """204 = 成功且无 body：_request 返回 {}（删除类端点可能这样回）。"""
        settings = _settings(self.token_file, self.read_token_file)
        got = self._call(
            _respond(204),
            lambda: manager_api._request(settings, "DELETE", "/api/keys/7",
                                         purpose="keys"))
        self.assertEqual(got, {})

    def test_shape_validation_helpers(self) -> None:
        settings = _settings(self.token_file, self.read_token_file)
        cases = [
            ("keys_dict", lambda: manager_api.read_api_keys(settings), {"a": 1}),
            ("summary_list", lambda: manager_api.read_stats(settings, "summary"), []),
            ("hourly_dict", lambda: manager_api.read_stats(settings, "hourly"), {"a": 1}),
            ("logs_without_items", lambda: manager_api.read_logs(settings), {"total": 1}),
            ("accounts_no_items", lambda: manager_api.read_accounts(settings),
             {"total": 1}),
        ]
        for label, call, payload in cases:
            with self.subTest(case=label):
                with self.assertRaises(UpstreamPayloadError):
                    self._call(_json_handler(payload), call)

    def test_settings_panel_empty_object_is_legal(self) -> None:
        """`/api/settings/panel` 的空表返回 `{}` = "没配置"，是合法状态（不是结构异常）。"""
        settings = _settings(self.token_file, self.read_token_file)
        got = self._call(_json_handler({}),
                         lambda: manager_api.read_settings_panel(settings))
        self.assertEqual(got, {})


# ---------------------------------------------------------------- sqlite 不变
def _make_db(tmp: str) -> str:
    path = str(Path(tmp) / "manager.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE usage_daily(day TEXT, key_id INT, model TEXT, requests INT,
            prompt_tokens INT, completion_tokens INT, credit REAL, realm TEXT,
            cache_hit_tokens INT, cache_miss_tokens INT);
        CREATE TABLE usage_hourly(day TEXT, hour INT, key_id INT, model TEXT,
            requests INT, prompt_tokens INT, completion_tokens INT, credit REAL,
            realm TEXT);
        CREATE TABLE request_logs(ts INT, model TEXT, mapped_model TEXT, status INT,
            prompt_tokens INT, completion_tokens INT, latency_ms INT,
            first_token_ms INT, error TEXT, ip TEXT, key_id INT, realm TEXT);
        CREATE TABLE task_logs(ts INT, uid TEXT, kind TEXT, credits REAL, level TEXT,
            message TEXT, message_cn TEXT, nickname TEXT);
        CREATE TABLE api_keys(id INT, name TEXT, prefix TEXT, enabled INT,
            expires_at INT, models TEXT, quota INT, used_tokens INT, created_at INT,
            last_used_at INT, realm TEXT, quota_credit REAL, used_credit REAL);
        CREATE TABLE checkin_logs(ts INT, uid TEXT, nickname TEXT, source TEXT,
            kind TEXT, success INT, code TEXT, message TEXT);
        CREATE TABLE audit_logs(id INT, ts INT, action TEXT);
        CREATE TABLE settings(panel_key TEXT, panel_value TEXT);
        """
    )
    now = int(time.time())
    conn.execute(
        "INSERT INTO usage_daily VALUES ('2020-01-01',1,'m',100,1000,500,10.0,'cn',0,0)")
    conn.execute(
        "INSERT INTO usage_daily VALUES ('2099-01-01',1,'m',5,50,5,0.5,'cn',0,0)")
    conn.execute(
        "INSERT INTO usage_hourly VALUES ('2099-01-01',9,1,'m',5,50,5,0.5,'cn')")
    conn.execute(
        "INSERT INTO request_logs(ts,model,status,key_id,realm)"
        " VALUES (?, 'm', 500, 1, 'cn')", (now,))
    conn.execute("INSERT INTO task_logs(ts,uid,kind,credits) VALUES (?, 'u1', 'credit', 100.0)",
                 (now,))
    conn.execute(
        "INSERT INTO api_keys VALUES (1,'k','wbk_abc12345',1,0,'[]',0,0,0,0,'cn',0.0,0.0)")
    conn.execute("INSERT INTO checkin_logs VALUES (?, 'u1','n1','auto','daily',1,'0','ok')",
                 (now,))
    conn.execute("INSERT INTO audit_logs VALUES (1, ?, 'login')", (now,))
    conn.execute("INSERT INTO settings VALUES ('k', 'v')")
    conn.commit()
    conn.close()
    return path


class SqliteModeUnchangedTest(_TokenMixin, unittest.TestCase):
    """sqlite（默认）模式行为不变：读本地库、不发 HTTP。"""

    def setUp(self) -> None:  # noqa: N802 - unittest 约定
        super().setUp()
        self._dbtmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._dbtmp.cleanup)
        self.db = _make_db(self._dbtmp.name)

    def _run(self, call):
        adapter = self.sqlite_adapter(self.db)
        with mock.patch.object(manager_api, "_client",
                               side_effect=AssertionError(
                                   "sqlite 模式不得发 HTTP 请求")) as http_spy:
            got = asyncio.run(call(adapter))
        self.assertFalse(http_spy.called, "sqlite 模式不得发 HTTP 请求")
        return got

    def test_sqlite_readers_keep_working(self) -> None:
        summary = self._run(lambda a: a.usage_summary())
        self.assertEqual(summary["total_requests"], 105, "sqlite 汇总口径不得改变")
        self.assertEqual(summary["active_keys"], 1)

        hourly = self._run(lambda a: a.usage_hourly("2099-01-01"))
        self.assertEqual(len(hourly), 24, "必须固定 24 个桶")
        self.assertEqual(sum(r["requests"] for r in hourly), 5)

        flow = self._run(lambda a: a.credits_flow(7))
        self.assertEqual(len(flow["daily"]), 7)
        self.assertEqual(flow["today"]["income"], 100.0)

        usage = self._run(lambda a: a.usage(14))
        self.assertIn("usage", usage)
        self.assertIn("credits_daily", usage)

        analytics = self._run(lambda a: a.usage_analytics())
        self.assertEqual(analytics["summary"]["requests"], 105)

        keys = self._run(lambda a: a.keys())
        self.assertEqual(keys["data"]["keys"][0]["prefix"], "wbk_abc12345")

        logs = self._run(lambda a: a.logs(10))
        self.assertEqual(len(logs["data"]), 1)

        task_logs = self._run(lambda a: a.task_logs(10))
        self.assertEqual(len(task_logs["data"]), 1)

        audit_logs = self._run(lambda a: a.audit_logs(10))
        self.assertEqual(len(audit_logs["data"]), 1)

        settings = self._run(lambda a: a.settings())
        self.assertIn("raw", settings)
        self.assertEqual(settings["raw"]["panel"], {"panel_key": "k", "panel_value": "v"})

        overview = self._run(lambda a: a.overview())
        self.assertIn("accounts", overview)
        self.assertEqual(len(overview["checkin"]), 1,
                         "checkin_logs 的逐条记录按天聚合成一条")
        self.assertEqual(overview["checkin"][0]["total"], 1)

    def test_sqlite_mode_calls_db(self) -> None:
        adapter = self.sqlite_adapter(self.db)
        with mock.patch.object(WorkBuddyAdapter, "_db", autospec=True,
                               return_value=None) as db_spy:
            asyncio.run(adapter.usage_summary())
        self.assertGreater(db_spy.call_count, 0, "sqlite 模式必须走 _db()")

    def test_stats_via_http_refuses_in_sqlite_mode(self) -> None:
        """二义性哨兵已取消：sqlite 模式下调用 HTTP 助手是内部错误。"""
        adapter = self.sqlite_adapter(self.db)
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(adapter._stats_via_http("summary", None))
        self.assertIn("_stats_via_http", str(ctx.exception))

    def test_db_refuses_in_http_mode(self) -> None:
        """硬兜底：http 模式下 _db() 直接失败（即使某个分支漏了分流）。"""
        adapter = self.http_adapter()
        with self.assertRaises(RuntimeError) as ctx:
            adapter._db()
        self.assertIn("K26", str(ctx.exception))


# ---------------------------------------------------------------- 其它语义
class NoSentinelTest(_TokenMixin, unittest.TestCase):
    def test_helper_does_not_return_none_in_http_mode(self) -> None:
        adapter = self.http_adapter()
        with mock.patch.object(manager_api, "_client",
                               _client_factory(_json_handler(SUMMARY_OK))):
            got = asyncio.run(adapter._stats_via_http("summary", None))
        self.assertIsNotNone(got, "http 模式不得返回 None（那会变成'回退 SQL'的哨兵）")

    def test_http_reader_alias_matches_four_methods(self) -> None:
        """契约自检：被覆盖的读方法名字与探针脚本保持一致。"""
        names = {name for name, _, _ in HTTP_READERS}
        self.assertEqual(names, {"usage_summary", "usage_hourly",
                                 "credits_flow", "overview"})


if __name__ == "__main__":
    unittest.main()
