"""R3-L5：真实消费者的双凭据过渡（QTG → Manager 令牌、内部 Bearer、会话密钥）。

锁定的性质（对应 RV04 的 G4-1/G4-2）：

1. **`.next` 是真实读取路径的一部分**（不是测试脚本自建的 read()）：
   `manager_api._request` 在 http 模式下逐槽位尝试令牌文件；
2. 顺序明确：**next 优先、current 兜底**，且**只在 401/403**（凭据不被接受）时回退；
   5xx/超时/连接失败不换令牌（换也不会变好，只会放大故障）；
3. 过渡能力：`.next` 存在但无效时，请求仍成功（回退 current），且能看出两次尝试的槽位；
4. 两个槽位都缺失 → **一条**明确的 ConfigMissing，列出两个路径，不静默；
5. **轮换不中断**：用真实 loopback HTTP + 真实 `manager_api` 路径，边读边轮换
   （stage → promote → 撤销旧值），失败数必须为 0；
6. 内部 Bearer 双接受：`<path>` 与 `<path>.next` 同时有效；
7. 会话密钥双验证：先当前 secret、失败再 `.previous`；删除 `.previous` 后旧会话立即失效。

跑法：python -X utf8 tests/zhihui_harness/run_tests.py --target qtg --keep \
        --test-path=tests/test_credential_rotation.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.qtg import manager_api  # noqa: E402
from app.qtg.config import Settings  # noqa: E402
from app.qtg.errors import ConfigMissing, UpstreamError  # noqa: E402
from app.qtg.identity import IdentityResolver  # noqa: E402
from app.qtg.security import CredentialStore  # noqa: E402

READ_OLD = "wbt_read_old_" + "a" * 40
READ_NEW = "wbt_read_new_" + "b" * 40
KEYS_OLD = "wbt_keys_old_" + "c" * 40
KEYS_NEW = "wbt_keys_new_" + "d" * 40

CONTRACT = {
    "version": 2,
    "capabilities": {name: True for name in (
        "stats", "keys", "logs", "tasks", "checkin", "audit", "accounts",
        "models", "settings", "usage")},
    "timezone": "CST", "credit_unit": "float", "timestamp_unit": "epoch_seconds",
    "pagination": {"style": "offset_limit", "max_days": 3650},
}
SUMMARY = {
    "today_requests": 1, "today_tokens": 1, "today_credit": 0.0,
    "week_requests": 1, "week_tokens": 1, "week_credit": 0.0,
    "total_requests": 1, "total_tokens": 1, "total_credit": 0.0, "active_keys": 1,
}


def _settings(tmp: Path, *, base: str = "http://127.0.0.1:7864", **over) -> Settings:
    values = dict(
        wb_api_base=base,
        wb_panel_base=base,
        wb_config_file=str(tmp / "absent-config.json"),
        wb_auths_dir=str(tmp / "absent-auths"),
        wb_state_file=str(tmp / "absent-state.json"),
        wb_db=str(tmp / "absent-manager.db"),
        manager_service_token_file=str(tmp / "manager_service_token"),
        manager_read_token_file=str(tmp / "manager_read_token"),
        manager_api_timeout_sec=5.0,
    )
    values.update(over)
    return Settings(**values)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.read_file = self.tmp / "manager_read_token"
        self.keys_file = self.tmp / "manager_service_token"
        self.read_file.write_text(READ_OLD, encoding="utf-8")
        self.keys_file.write_text(KEYS_OLD, encoding="utf-8")

    def settings(self, **over) -> Settings:
        return _settings(self.tmp, **over)

    def mock_client(self, handler):
        """替换 manager_api._client，记录每次请求的 authorization 与 slot 头。"""
        calls: list[dict] = []

        def routed(request: httpx.Request) -> httpx.Response:
            calls.append({
                "path": request.url.path,
                "authorization": request.headers.get("authorization", ""),
                "slot": request.headers.get("x-qtg-token-slot", ""),
            })
            return handler(request)

        def factory(timeout: float | None = None) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(routed),
                                     timeout=timeout or 5.0)
        return calls, factory

    @staticmethod
    def json_response(payload, status: int = 200) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(payload).encode("utf-8"),
                              headers={"content-type": "application/json"})


class NextSlotPreferenceTest(_Base):
    def test_next_preferred_when_both_present(self) -> None:
        (self.tmp / "manager_read_token.next").write_text(READ_NEW, encoding="utf-8")
        calls, factory = self.mock_client(lambda r: self.json_response(CONTRACT))
        with mock.patch.object(manager_api, "_client", factory):
            asyncio.run(manager_api.read_api_version(self.settings()))
        self.assertEqual(calls[0]["authorization"], f"Bearer {READ_NEW}")
        self.assertEqual(calls[0]["slot"], "next")

    def test_only_current_still_works(self) -> None:
        calls, factory = self.mock_client(lambda r: self.json_response(CONTRACT))
        with mock.patch.object(manager_api, "_client", factory):
            asyncio.run(manager_api.read_api_version(self.settings()))
        self.assertEqual(calls[0]["authorization"], f"Bearer {READ_OLD}")
        self.assertEqual(calls[0]["slot"], "current")

    def test_only_next_works(self) -> None:
        self.read_file.unlink()
        (self.tmp / "manager_read_token.next").write_text(READ_NEW, encoding="utf-8")
        calls, factory = self.mock_client(lambda r: self.json_response(CONTRACT))
        with mock.patch.object(manager_api, "_client", factory):
            asyncio.run(manager_api.read_api_version(self.settings()))
        self.assertEqual(calls[0]["slot"], "next")

    def test_both_missing_raises_configmissing_with_both_paths(self) -> None:
        self.read_file.unlink()
        with self.assertRaises(ConfigMissing) as ctx:
            asyncio.run(manager_api.read_api_version(self.settings()))
        message = str(ctx.exception)
        self.assertIn("manager_read_token", message)
        self.assertIn(".next", message)
        self.assertIn("manager_read_token_file", message)

    def test_read_purpose_does_not_use_keys_token(self) -> None:
        """读令牌缺失时**不得**改用 keys 令牌（最小权限，R1-A 的约束继续保持）。"""
        self.read_file.unlink()
        calls, factory = self.mock_client(lambda r: self.json_response(CONTRACT))
        with self.assertRaises(ConfigMissing) as ctx:
            with mock.patch.object(manager_api, "_client", factory):
                asyncio.run(manager_api.read_api_version(self.settings()))
        self.assertEqual(calls, [], "读令牌缺失时不应发出任何请求")
        # 提示语里会提到"不要用 scope=keys 兼任"，那是**说明**；这里要断言的是
        # 没有改用 keys 的**文件/配置项**。
        self.assertIn("manager_read_token_file", str(ctx.exception))
        self.assertNotIn("manager_service_token_file", str(ctx.exception))


class FallbackOnAuthFailureTest(_Base):
    def test_fallback_to_current_on_401(self) -> None:
        (self.tmp / "manager_read_token.next").write_text("wbt_broken", encoding="utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("authorization", "")
            if request.url.path == "/api/read-api/version":
                if auth.endswith("wbt_broken"):
                    return self.json_response({"detail": "invalid token"}, 401)
                return self.json_response(CONTRACT)
            return self.json_response(SUMMARY)
        calls, factory = self.mock_client(handler)
        with mock.patch.object(manager_api, "_client", factory):
            data = asyncio.run(manager_api.read_api_version(self.settings()))
        self.assertEqual(data["version"], 2)
        self.assertEqual([c["slot"] for c in calls], ["next", "current"],
                         "应先试 next，401 后回退 current")
        self.assertTrue(calls[1]["authorization"].endswith(READ_OLD))

    def test_fallback_on_403(self) -> None:
        (self.tmp / "manager_read_token.next").write_text("wbt_no_scope", encoding="utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers.get("authorization", "").endswith("wbt_no_scope"):
                return self.json_response({"detail": "scope"}, 403)
            return self.json_response(CONTRACT)
        calls, factory = self.mock_client(handler)
        with mock.patch.object(manager_api, "_client", factory):
            asyncio.run(manager_api.read_api_version(self.settings()))
        self.assertEqual(len(calls), 2)

    def test_no_fallback_on_500(self) -> None:
        (self.tmp / "manager_read_token.next").write_text(READ_NEW, encoding="utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            return self.json_response({"detail": "boom"}, 500)
        calls, factory = self.mock_client(handler)
        with mock.patch.object(manager_api, "_client", factory):
            with self.assertRaises(UpstreamError):
                asyncio.run(manager_api.read_api_version(self.settings()))
        self.assertEqual(len(calls), 1, "5xx 不应换令牌重试（换也修不好，会放大故障）")

    def test_no_fallback_on_timeout(self) -> None:
        (self.tmp / "manager_read_token.next").write_text(READ_NEW, encoding="utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")
        calls, factory = self.mock_client(handler)
        with mock.patch.object(manager_api, "_client", factory):
            with self.assertRaises(manager_api.UpstreamTimeout):
                asyncio.run(manager_api.read_api_version(self.settings()))
        self.assertEqual(len(calls), 1)

    def test_both_rejected_reports_tried_slots(self) -> None:
        (self.tmp / "manager_read_token.next").write_text(READ_NEW, encoding="utf-8")
        calls, factory = self.mock_client(
            lambda r: self.json_response({"detail": "nope"}, 401))
        with mock.patch.object(manager_api, "_client", factory):
            with self.assertRaises(manager_api.UpstreamUnauthorized) as ctx:
                asyncio.run(manager_api.read_api_version(self.settings()))
        tried = getattr(ctx.exception, "token_slots_tried", None)
        self.assertEqual([t.get("slot") for t in tried or []], ["next", "current"])
        self.assertEqual(len(calls), 2)
        self.assertIn("token_slot=current", str(ctx.exception))


class _FakeManager:
    """最小 loopback HTTP 假 Manager：只认 accepted 集合里的 Bearer。"""

    def __init__(self) -> None:
        self.accepted: set[str] = set()
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # 静音
                return

            def do_GET(self) -> None:  # noqa: N802
                auth = self.headers.get("authorization", "")
                token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
                fake.requests.append({"path": self.path, "token": token,
                                      "slot": self.headers.get("x-qtg-token-slot", "")})
                if token not in fake.accepted:
                    body = b'{"detail":"invalid token"}'
                    self.send_response(401)
                else:
                    payload = CONTRACT if self.path.startswith("/api/read-api/version") else SUMMARY
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_FakeManager":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"


class RotationWithoutDowntimeTest(_Base):
    """真实 loopback HTTP + 真实 manager_api 路径：边读边轮换，失败数必须为 0。"""

    def test_rotation_loop_zero_failures(self) -> None:
        with _FakeManager() as fake:
            fake.accepted = {READ_OLD}
            settings = self.settings(base=fake.base)
            failures: list[str] = []
            slots: list[str] = []
            total = 30

            async def reader(index: int) -> None:
                try:
                    data = await manager_api.read_api_version(settings)
                    if data.get("version") != 2:
                        failures.append(f"#{index}: unexpected version {data}")
                except Exception as exc:  # noqa: BLE001 - 统计任何失败
                    failures.append(f"#{index}: {type(exc).__name__}: {exc}")
                last = fake.requests[-1] if fake.requests else {}
                slots.append(last.get("slot", ""))

            async def run() -> None:
                for i in range(total):
                    if i == 5:
                        # stage：新值写入 .next（QTG 立即优先使用它）
                        (self.tmp / "manager_read_token.next").write_text(
                            READ_NEW, encoding="utf-8")
                        fake.accepted.add(READ_NEW)  # Manager 侧新令牌生效
                    if i == 12:
                        # promote：.next 原子改名为 current；旧值仍在 accepted 里
                        os.replace(self.tmp / "manager_read_token.next", self.read_file)
                    if i == 18:
                        # 撤销旧令牌（Manager 侧删掉旧 token）
                        fake.accepted.discard(READ_OLD)
                    await reader(i)

            asyncio.run(run())
        self.assertEqual(failures, [], f"轮换期间出现失败：{failures[:5]}")
        self.assertIn("next", slots, "stage 之后应实际用到 next 槽位")
        self.assertIn("current", slots, "promote 之后应回落到 current 槽位")
        final = self.read_file.read_text(encoding="utf-8").strip()
        self.assertEqual(final, READ_NEW, "promote 后 current 应是新值")


class InternalBearerDualAcceptTest(_Base):
    def _store(self, *, with_next: bool) -> CredentialStore:
        internal = self.tmp / "internal_service_token"
        internal.write_text("old-internal-token-" + "x" * 32, encoding="utf-8")
        if with_next:
            (self.tmp / "internal_service_token.next").write_text(
                "new-internal-token-" + "y" * 32, encoding="utf-8")
        return CredentialStore(token_file=self.tmp / "admin_token",
                               secret_file=self.tmp / "session_secret",
                               internal_service_token_file=internal)

    def test_accepts_both_current_and_next(self) -> None:
        store = self._store(with_next=True)
        self.assertEqual(len(store.internal_service_tokens()), 2)
        resolver = IdentityResolver(credentials=store, audience="qtg",
                                     trusted_peers=("127.0.0.1",))
        self.assertTrue(resolver.internal_token_ok("old-internal-token-" + "x" * 32))
        self.assertTrue(resolver.internal_token_ok("new-internal-token-" + "y" * 32))
        self.assertFalse(resolver.internal_token_ok("wrong-token"))

    def test_current_only_still_works(self) -> None:
        store = self._store(with_next=False)
        resolver = IdentityResolver(credentials=store, audience="qtg",
                                     trusted_peers=("127.0.0.1",))
        self.assertTrue(resolver.internal_token_ok("old-internal-token-" + "x" * 32))
        self.assertFalse(resolver.internal_token_ok("new-internal-token-" + "y" * 32))

    def test_removing_next_revokes_old_value(self) -> None:
        store = self._store(with_next=True)
        resolver = IdentityResolver(credentials=store, audience="qtg",
                                     trusted_peers=("127.0.0.1",))
        self.assertTrue(resolver.internal_token_ok("old-internal-token-" + "x" * 32))
        # 收口：current 换成新值、删掉 .next → 旧值立即不再被接受
        (self.tmp / "internal_service_token").write_text(
            "new-internal-token-" + "y" * 32, encoding="utf-8")
        (self.tmp / "internal_service_token.next").unlink()
        # 触发 mtime 失效（有些文件系统 mtime 精度粗，显式改一次）
        os.utime(self.tmp / "internal_service_token", None)
        self.assertTrue(resolver.internal_token_ok("new-internal-token-" + "y" * 32))
        self.assertFalse(resolver.internal_token_ok("old-internal-token-" + "x" * 32))


class SessionSecretDualVerifyTest(_Base):
    def _store(self) -> CredentialStore:
        return CredentialStore(token_file=self.tmp / "admin_token",
                               secret_file=self.tmp / "session_secret")

    def test_old_session_valid_until_previous_removed(self) -> None:
        store = self._store()
        old_secret = store.secret()
        token = store.issue_session(600, sub="admin", role="admin")
        self.assertIsNotNone(store.verify_session(token))
        # 轮换：current = 新 secret，.previous = 旧 secret
        new_secret = b"n" * 64
        (self.tmp / "session_secret.previous").write_bytes(old_secret)
        (self.tmp / "session_secret").write_bytes(new_secret)
        os.utime(self.tmp / "session_secret", None)
        rotated = self._store()
        self.assertIsNotNone(rotated.verify_session(token),
                             "过渡期内旧会话应仍可验签（经 .previous）")
        # 新签发的会话用新 secret 签，且只验当前值也能过
        fresh = rotated.issue_session(600, sub="admin", role="admin")
        self.assertIsNotNone(rotated.verify_session(fresh))
        # 收口：删掉 .previous → 旧会话立即失效
        (self.tmp / "session_secret.previous").unlink()
        closed = self._store()
        self.assertIsNone(closed.verify_session(token), "删除 .previous 后旧会话必须失效")
        self.assertIsNotNone(closed.verify_session(fresh), "新会话不受影响")


if __name__ == "__main__":
    unittest.main()
