"""R1-A（K26 完整化）：读写凭据分离 + http 模式无 DB 依赖 + sqlite/http 口径对账。

锁定的性质（每条都能在实现里追溯到动机）：

1. **两种服务凭据真的分开用**：读端点送 `scope=read` 的 `manager_read_token_file`，
   写端点送 `scope=keys` 的 `manager_service_token_file`；不是"能跑通就行"，
   而是逐请求断言 Authorization 头里是哪把令牌；
2. **缺失不回退**：只有 keys 令牌时，读路径明确 ConfigMissing（指向 read token 文件），
   反之亦然——不存在"另一把兜底"；
3. **用错令牌会被 Manager 明确 403**：这里起一个**真实 loopback HTTP** 的假 Manager
   （按 scope 校验 Authorization），断言 QTG 得到 UpstreamUnauthorized 且消息指向
   出问题的那把令牌（token_purpose）；
4. **http 模式零 DB 依赖**：`wb_db` 指向不存在的路径时，全部读方法仍返回**真实数据**
   （不是空、不是异常），且 `_db()` 调用次数为 0；
5. **口径对账**：同一份非空 fixture，sqlite 模式与 http 模式（假 Manager 用同一份
   数据、同一口径应答）的输出逐值一致。真实 Manager 的对账见
   evidence/r1-k26/probe_loopback_real_manager.py 的 parity 结果。

跑法：python -X utf8 tests/zhihui_harness/run_tests.py --target qtg --keep \
        --test-path=tests/test_read_token_split.py
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.qtg import manager_api  # noqa: E402
from app.qtg.adapters.workbuddy import WorkBuddyAdapter  # noqa: E402
from app.qtg.config import Settings  # noqa: E402
from app.qtg.errors import (  # noqa: E402
    ConfigMissing,
    UpstreamUnauthorized,
)

READ_TOKEN = "wbt_read_synthetic_token"
KEYS_TOKEN = "wbt_keys_synthetic_token"

CONTRACT_V2 = {
    "version": 2,
    "capabilities": {name: True for name in (
        "stats", "keys", "logs", "tasks", "checkin", "audit", "accounts",
        "models", "settings", "usage")},
    "endpoints": {},
    "timezone": "CST", "credit_unit": "float", "timestamp_unit": "epoch_seconds",
    "pagination": {"style": "offset_limit", "max_days": 3650},
}


# ---------------------------------------------------------------- 假 Manager（真 HTTP）
class _StubManager:
    """按 scope 校验令牌的假 Manager：只实现 QTG 用到的只读端点。

    **它不是生产 Manager 的替代品**，只用于证明两件事：
      * QTG 真的把不同的令牌送到了不同端点（看 Authorization 头）；
      * 用错令牌时 QTG 得到 403 → UpstreamUnauthorized（含 token_purpose）。
    真实 Manager 的验证在 evidence 探针里（真 uvicorn + 真令牌 + 真 scope 白名单）。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.requests: list[tuple[str, str, str]] = []  # (method, path, token)
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # 静音
                return

            def _token(self) -> str:
                auth = self.headers.get("authorization", "")
                return auth[len("Bearer "):] if auth.startswith("Bearer ") else ""

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                token = self._token()
                stub.requests.append((method, path, token))
                status, payload = stub.respond(method, path, token, query)
                self._send(status, payload)

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_PATCH(self) -> None:  # noqa: N802
                self._handle("PATCH")

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- 生命周期 --
    def __enter__(self) -> "_StubManager":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # -- 路由 --
    READ_PATHS_PREFIX = ("/api/read-api/", "/api/stats/", "/api/logs", "/api/task-logs",
                         "/api/checkin-logs", "/api/audit-logs", "/api/accounts",
                         "/api/models", "/api/settings/panel", "/api/keys")

    def _is_read(self, method: str, path: str) -> bool:
        if method != "GET":
            return False
        return any(path.startswith(p) for p in self.READ_PATHS_PREFIX)

    def respond(self, method: str, path: str,
                token: str, query: dict[str, str]) -> tuple[int, Any]:
        # scope 契约（与 Manager 的 security.current_user 语义对齐）：
        #   只读端点只接受 read 令牌；写端点只接受 keys 令牌；其它一律 403。
        if self._is_read(method, path):
            if token != READ_TOKEN:
                return 403, {"detail": "只读服务令牌仅允许读取既有数据"}
        else:
            if token != KEYS_TOKEN:
                return 403, {"detail": "服务令牌仅允许密钥管理操作"}
        try:
            return self._payload(method, path, query)
        except LookupError:
            return 404, {"detail": "no route"}

    def _payload(self, method: str, path: str,
                 query: dict[str, str]) -> tuple[int, Any]:
        conn = sqlite3.connect(self.db_path)
        try:
            if path == "/api/read-api/version":
                return 200, dict(CONTRACT_V2)
            if path == "/api/read-api/health":
                return 200, {"ok": True, "version": 2, "ts": int(time.time())}
            if path == "/api/read-api/usage-matrix":
                since = query.get("since", "1970-01-01")
                rows = conn.execute(
                    "SELECT day, model, requests, prompt_tokens, completion_tokens,"
                    " credit, realm FROM usage_daily WHERE day >= ? ORDER BY day",
                    (since,)).fetchall()
                fails = conn.execute(
                    "SELECT date(ts, 'unixepoch', '+8 hours') AS day,"
                    " SUM(CASE WHEN status >= 400 OR status = 0 THEN 1 ELSE 0 END)"
                    " FROM request_logs WHERE ts >= strftime('%s', ?) GROUP BY day",
                    (since + " 00:00:00",)).fetchall()
                return 200, {
                    "version": 2, "since": since,
                    "rows": [{"day": r[0], "model": r[1], "requests": r[2],
                              "prompt_tokens": r[3], "completion_tokens": r[4],
                              "credit": r[5], "realm": r[6]} for r in rows],
                    "failures": {str(d): int(n or 0) for d, n in fails if d},
                }
            if path == "/api/read-api/usage-totals":
                row = conn.execute(
                    "SELECT SUM(requests), SUM(prompt_tokens), SUM(completion_tokens),"
                    " SUM(credit) FROM usage_daily").fetchone()
                return 200, {"version": 2, "requests": row[0], "prompt_tokens": row[1],
                             "completion_tokens": row[2], "credits": row[3]}
            if path == "/api/stats/summary":
                return 200, self._summary(conn)
            if path == "/api/stats/hourly":
                return 200, self._hourly(conn, query.get("day", ""))
            if path == "/api/stats/credits":
                return 200, self._credits(int(query.get("days", 30)))
            if path == "/api/stats/by-model":
                return 200, self._by_kind(conn, query, "model")
            if path == "/api/stats/by-key":
                return 200, self._by_kind(conn, query, "key")
            if path == "/api/logs":
                rows = conn.execute(
                    "SELECT ts, model, mapped_model, status, prompt_tokens,"
                    " completion_tokens, latency_ms, first_token_ms, error, ip"
                    " FROM request_logs ORDER BY ts DESC LIMIT ?",
                    (int(query.get("limit", 20)),)).fetchall()
                items = [{"ts": r[0], "model": r[1], "mapped_model": r[2],
                          "status": r[3], "prompt_tokens": r[4],
                          "completion_tokens": r[5], "latency_ms": r[6],
                          "first_token_ms": r[7], "error": r[8], "ip": r[9]}
                         for r in rows]
                return 200, {"total": len(items), "items": items}
            if path == "/api/task-logs":
                rows = conn.execute(
                    "SELECT ts, uid, kind, credits FROM task_logs"
                    " ORDER BY ts DESC LIMIT ?",
                    (int(query.get("limit", 20)),)).fetchall()
                logs = [{"ts": r[0], "uid": r[1], "kind": r[2], "credits": r[3]}
                        for r in rows]
                return 200, {"logs": logs, "total": len(logs)}
            if path == "/api/checkin-logs":
                rows = conn.execute(
                    "SELECT ts, uid, nickname, source, kind, success, code, message"
                    " FROM checkin_logs ORDER BY ts DESC LIMIT ?",
                    (int(query.get("limit", 200)),)).fetchall()
                items = [{"ts": r[0], "uid": r[1], "nickname": r[2], "source": r[3],
                          "kind": r[4], "success": bool(r[5]), "code": r[6],
                          "message": r[7]} for r in rows]
                return 200, {"items": items, "total": len(items)}
            if path == "/api/audit-logs":
                rows = conn.execute(
                    "SELECT id, ts, action FROM audit_logs ORDER BY ts DESC LIMIT ?",
                    (int(query.get("limit", 200)),)).fetchall()
                items = [{"id": r[0], "ts": r[1], "action": r[2]} for r in rows]
                return 200, {"items": items, "total": len(items)}
            if path == "/api/accounts":
                return 200, {"total": 0, "accounts": [], "pool_synced": 0,
                             "pool_available": False, "manageable": False}
            if path == "/api/models":
                return 200, {"models": [{"id": "m", "name": "m"}], "count": 1}
            if path == "/api/settings/panel":
                row = conn.execute("SELECT * FROM settings LIMIT 1").fetchone()
                if row is None:
                    return 200, {}
                cols = [c[0] for c in conn.execute(
                    "SELECT * FROM settings LIMIT 1").description or []]
                return 200, dict(zip(cols, row))
            if path == "/api/keys":
                rows = conn.execute(
                    "SELECT id, name, prefix, enabled FROM api_keys ORDER BY id"
                ).fetchall()
                return 200, [{"id": r[0], "name": r[1], "prefix": r[2],
                              "enabled": bool(r[3])} for r in rows]
        finally:
            conn.close()
        raise LookupError(path)

    # -- 各统计族的口径（与 QTG sqlite 读法同口径，供 parity 对比）--
    @staticmethod
    def _summary(conn: sqlite3.Connection) -> dict[str, Any]:
        import datetime as _dt
        tz = _dt.timezone(_dt.timedelta(hours=8))
        today = _dt.datetime.now(tz).date().isoformat()
        week = (_dt.datetime.now(tz).date() - _dt.timedelta(days=6)).isoformat()

        def agg(where: str, args: tuple) -> tuple[int, int, float]:
            row = conn.execute(
                "SELECT COALESCE(SUM(requests),0),"
                " COALESCE(SUM(prompt_tokens + completion_tokens),0),"
                f" COALESCE(SUM(credit),0) FROM usage_daily WHERE {where}", args).fetchone()
            return int(row[0] or 0), int(row[1] or 0), float(row[2] or 0)

        t_req, t_tok, t_credit = agg("day = ?", (today,))
        w_req, w_tok, w_credit = agg("day >= ?", (week,))
        a_req, a_tok, a_credit = agg("1=1", ())
        active = conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE enabled = 1").fetchone()[0]
        top = conn.execute(
            "SELECT model FROM usage_daily GROUP BY model"
            " ORDER BY SUM(requests + prompt_tokens + completion_tokens) DESC"
            " LIMIT 1").fetchone()
        return {
            "today_requests": t_req, "today_tokens": t_tok, "today_credit": t_credit,
            "week_credit": w_credit, "total_credit": a_credit,
            "week_requests": w_req, "week_tokens": w_tok,
            "total_requests": a_req, "total_tokens": a_tok,
            "active_keys": int(active or 0),
            "top_model": str(top[0]) if top else None,
        }

    @staticmethod
    def _hourly(conn: sqlite3.Connection, day: str) -> list[dict[str, Any]]:
        d = day if len(day) == 10 else time.strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT hour, SUM(requests), SUM(prompt_tokens), SUM(completion_tokens),"
            " COALESCE(SUM(credit),0) FROM usage_hourly WHERE day = ? GROUP BY hour",
            (d,)).fetchall()
        by_hour = {int(r[0]): r for r in rows}
        fails: dict[int, int] = {}
        try:
            import datetime as _dt
            tz = _dt.timezone(_dt.timedelta(hours=8))
            start = int(_dt.datetime.fromisoformat(d).replace(tzinfo=tz).timestamp())
            for h, n in conn.execute(
                "SELECT strftime('%H', ts, 'unixepoch', '+8 hours') AS h, COUNT(*)"
                " FROM request_logs WHERE ts >= ? AND ts < ? AND status >= 400"
                " AND key_id IS NOT NULL GROUP BY h", (start, start + 86400)):
                if h is not None:
                    fails[int(h)] = int(n)
        except sqlite3.Error:
            fails = {}
        out = []
        for h in range(24):
            r = by_hour.get(h)
            out.append({
                "day": d, "hour": h,
                "requests": int(r[1] or 0) if r else 0,
                "prompt_tokens": int(r[2] or 0) if r else 0,
                "completion_tokens": int(r[3] or 0) if r else 0,
                "credit": round(float(r[4] or 0), 4) if r else 0.0,
                "failed": fails.get(h, 0),
            })
        return out

    @staticmethod
    def _credits(days: int) -> dict[str, Any]:
        # QTG 的 sqlite 读法自己从库里算；假 Manager 直接复用 QTG 的实现太假，
        # 这里只用它验证"http 分支把 Manager 的数字原样透传"的字段形状，
        # 真实口径对账由同 fixture 的 usage-matrix 覆盖（见 parity 测试说明）。
        return {"days": days, "from": "2020-01-01", "to": "2020-01-01",
                "today": {"income": 0.0, "expense": 0.0, "net": 0.0},
                "week": {"income": 0.0, "expense": 0.0, "net": 0.0},
                "total": {"income": 0.0, "expense": 0.0, "net": 0.0},
                "daily": [{"day": "2020-01-01", "income": 0.0, "expense": 0.0,
                           "net": 0.0}]}

    @staticmethod
    def _by_kind(conn: sqlite3.Connection, query: dict[str, str],
                 kind: str) -> list[dict[str, Any]]:
        """by-model / by-key 的聚合（假 Manager 口径；key 名走 LEFT JOIN）。"""
        import datetime as _dt
        tz = _dt.timezone(_dt.timedelta(hours=8))
        days = max(1, min(400, int(query.get("days", 30))))
        since = (_dt.datetime.now(tz).date() - _dt.timedelta(days=days - 1)).isoformat()
        if kind == "model":
            rows = conn.execute(
                "SELECT model AS name, SUM(requests), SUM(prompt_tokens),"
                " SUM(completion_tokens), COALESCE(SUM(credit),0)"
                " FROM usage_daily WHERE day >= ? GROUP BY model"
                " ORDER BY SUM(prompt_tokens + completion_tokens) DESC",
                (since,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT COALESCE(k.name, u.key_id || '') AS name, SUM(u.requests),"
                " SUM(u.prompt_tokens), SUM(u.completion_tokens),"
                " COALESCE(SUM(u.credit),0) FROM usage_daily u"
                " LEFT JOIN api_keys k ON k.id = u.key_id WHERE u.day >= ?"
                " GROUP BY u.key_id"
                " ORDER BY SUM(u.prompt_tokens + u.completion_tokens) DESC",
                (since,)).fetchall()
        return [{"name": r[0] or "未知", "requests": int(r[1] or 0),
                 "prompt_tokens": int(r[2] or 0),
                 "completion_tokens": int(r[3] or 0),
                 "credit": round(float(r[4] or 0), 4),
                 "cache_hit_tokens": 0, "cache_miss_tokens": 0}
                for r in rows]


# ---------------------------------------------------------------- fixture
def _make_db(tmp: str) -> str:
    """非空 fixture：覆盖 usage_daily/usage_hourly/task_logs/request_logs/... 各表。"""
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
    today = time.strftime("%Y-%m-%d")
    conn.executemany(
        "INSERT INTO usage_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (today, 1, "m-new", 5, 50, 5, 0.5, "cn", 0, 0),
            (today, 2, "m-new", 2, 20, 2, 0.2, "cn", 0, 0),
            ("2020-01-01", 1, "m-old", 100, 1000, 500, 10.0, "cn", 0, 0),
        ])
    conn.executemany(
        "INSERT INTO usage_hourly VALUES (?,?,?,?,?,?,?,?,?)",
        [(today, 9, 1, "m-new", 5, 50, 5, 0.5, "cn")])
    conn.executemany(
        "INSERT INTO request_logs(ts,model,status,key_id,realm,latency_ms)"
        " VALUES (?,?,?,?,?,?)",
        [(now, "m-new", 200, 1, "cn", 12), (now, "m-new", 500, 1, "cn", 30)])
    conn.executemany(
        "INSERT INTO task_logs(ts,uid,kind,credits,level) VALUES (?,?,?,?,?)",
        [(now, "u1", "credit", 100.0, "info"), (now, "u1", "renew", 1.0, "info")])
    conn.execute(
        "INSERT INTO api_keys VALUES (1,'k','wbk_abc12345',1,0,'[]',0,0,0,0,'cn',0.0,0.0)")
    conn.execute("INSERT INTO checkin_logs VALUES (?, 'u1','n1','auto','daily',1,'0','ok')",
                 (now,))
    conn.execute("INSERT INTO audit_logs VALUES (1, ?, 'login')", (now,))
    conn.execute("INSERT INTO settings VALUES ('panel_key', 'panel_value')")
    conn.commit()
    conn.close()
    return path


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.token_file = str(tmp / "manager_service_token")
        Path(self.token_file).write_text(KEYS_TOKEN, encoding="utf-8")
        self.read_token_file = str(tmp / "manager_read_token")
        Path(self.read_token_file).write_text(READ_TOKEN, encoding="utf-8")
        self.db = _make_db(self._tmp.name)

    def settings(self, base: str, **over) -> Settings:
        values = dict(
            wb_api_base=base,
            wb_panel_base=base,
            wb_config_file=str(Path(self._tmp.name) / "absent-config.json"),
            wb_auths_dir=str(Path(self._tmp.name) / "absent-auths"),
            wb_state_file=str(Path(self._tmp.name) / "absent-state.json"),
            # 关键：http 模式下 DB 指向不存在的路径，证明零 DB 依赖
            wb_db=str(Path(self._tmp.name) / "absent-manager.db"),
            manager_service_token_file=self.token_file,
            manager_read_token_file=self.read_token_file,
            manager_api_timeout_sec=5.0,
        )
        values.update(over)
        return Settings(**values)


# ---------------------------------------------------------------- 1) 令牌路由
class TokenRoutingTest(_Base):
    """逐请求断言：读端点送 read 令牌，写端点送 keys 令牌。"""

    def _capture(self) -> tuple[list[list[str]], object]:
        seen: list[list[str]] = []
        payloads = {
            "/api/read-api/version": CONTRACT_V2,
            "/api/stats/summary": {
                "today_requests": 1, "today_tokens": 1, "today_credit": 0.0,
                "week_requests": 1, "week_tokens": 1, "week_credit": 0.0,
                "total_requests": 1, "total_tokens": 1, "total_credit": 0.0,
                "active_keys": 1},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append([request.method, request.url.path,
                         request.headers.get("authorization", "")])
            if request.url.path == "/api/keys" and request.method == "GET":
                return httpx.Response(200, content=b'[{"id":1,"prefix":"wbk_abc"}]',
                                      headers={"content-type": "application/json"})
            if request.method == "POST":
                return httpx.Response(200, content=b'{"ok":true,"id":9}',
                                      headers={"content-type": "application/json"})
            if request.method == "PATCH":
                return httpx.Response(200, content=b'{"id":9,"name":"k"}',
                                      headers={"content-type": "application/json"})
            if request.method == "DELETE":
                return httpx.Response(200, content=b'{"ok":true}',
                                      headers={"content-type": "application/json"})
            payload = payloads.get(request.url.path)
            if payload is None:
                return httpx.Response(404, content=b'{"detail":"nf"}')
            return httpx.Response(200, content=json.dumps(payload).encode(),
                                  headers={"content-type": "application/json"})
        return seen, handler

    def test_read_paths_use_read_token(self) -> None:
        seen, handler = self._capture()
        adapter = WorkBuddyAdapter(self.settings("http://127.0.0.1:7864",
                                                 wb_read_mode="http"))
        with mock.patch.object(manager_api, "_client",
                               lambda timeout=None: httpx.AsyncClient(
                                   transport=httpx.MockTransport(handler),
                                   timeout=timeout or 5.0)):
            asyncio.run(adapter.usage_summary())
            asyncio.run(adapter.keys())
            asyncio.run(manager_api.list_keys(adapter.cfg))
        read_calls = [c for c in seen if c[1] != "/api/read-api/version"]
        self.assertTrue(read_calls, "应当至少发生一次读取")
        for method, path, auth in read_calls:
            self.assertEqual(auth, f"Bearer {READ_TOKEN}",
                             f"{method} {path} 必须用只读令牌")
        # 契约探针同样用只读令牌
        probe = [c for c in seen if c[1] == "/api/read-api/version"]
        self.assertTrue(probe)
        for _method, _path, auth in probe:
            self.assertEqual(auth, f"Bearer {READ_TOKEN}")

    def test_write_paths_use_keys_token(self) -> None:
        seen, handler = self._capture()
        adapter = WorkBuddyAdapter(self.settings("http://127.0.0.1:7864",
                                                 wb_read_mode="http"))
        with mock.patch.object(manager_api, "_client",
                               lambda timeout=None: httpx.AsyncClient(
                                   transport=httpx.MockTransport(handler),
                                   timeout=timeout or 5.0)):
            asyncio.run(manager_api.create_key(adapter.cfg, {"name": "x"}))
            asyncio.run(manager_api.update_key(adapter.cfg, 9, {"name": "y"}))
            asyncio.run(manager_api.delete_key(adapter.cfg, 9))
            asyncio.run(manager_api.reset_usage(adapter.cfg, 9))
        self.assertEqual(len(seen), 4, seen)
        for method, path, auth in seen:
            self.assertEqual(auth, f"Bearer {KEYS_TOKEN}",
                             f"{method} {path} 必须用密钥管理令牌")
        self.assertTrue(any(c[0] == "POST" and c[1] == "/api/keys" for c in seen))


# ---------------------------------------------------------------- 2) 缺失不回退
class MissingTokenTest(_Base):
    def test_missing_read_token_does_not_fall_back_to_keys_token(self) -> None:
        Path(self.read_token_file).unlink()
        adapter = WorkBuddyAdapter(self.settings("http://127.0.0.1:7864",
                                                 wb_read_mode="http"))
        with mock.patch.object(manager_api, "_client",
                               side_effect=AssertionError("不得发出请求")) as spy:
            with self.assertRaises(ConfigMissing) as ctx:
                asyncio.run(adapter.usage_summary())
        self.assertIn("manager_read_token_file", str(ctx.exception))
        self.assertIn("scope=read", str(ctx.exception))
        self.assertFalse(spy.called, "缺读令牌时不得用 keys 令牌兜底发请求")

    def test_missing_keys_token_does_not_fall_back_to_read_token(self) -> None:
        Path(self.token_file).unlink()
        adapter = WorkBuddyAdapter(self.settings("http://127.0.0.1:7864",
                                                 wb_read_mode="http"))
        with mock.patch.object(manager_api, "_client",
                               side_effect=AssertionError("不得发出请求")) as spy:
            with self.assertRaises(ConfigMissing) as ctx:
                asyncio.run(manager_api.create_key(adapter.cfg, {"name": "x"}))
        self.assertIn("manager_service_token_file", str(ctx.exception))
        self.assertIn("scope=keys", str(ctx.exception))
        self.assertFalse(spy.called, "缺 keys 令牌时不得用读令牌兜底发请求")

    def test_blank_read_token_rejected(self) -> None:
        Path(self.read_token_file).write_text("   \n", encoding="utf-8")
        adapter = WorkBuddyAdapter(self.settings("http://127.0.0.1:7864",
                                                 wb_read_mode="http"))
        with self.assertRaises(ConfigMissing) as ctx:
            asyncio.run(adapter.usage_summary())
        self.assertIn("为空", str(ctx.exception))

    def test_error_messages_distinguish_two_tokens(self) -> None:
        read_missing = self._missing_message(read=True)
        keys_missing = self._missing_message(read=False)
        self.assertNotEqual(read_missing, keys_missing)
        self.assertIn("read", read_missing)
        self.assertIn("keys", keys_missing)

    def _missing_message(self, *, read: bool) -> str:
        (Path(self.read_token_file) if read else Path(self.token_file)).unlink()
        adapter = WorkBuddyAdapter(self.settings("http://127.0.0.1:7864",
                                                 wb_read_mode="http"))
        coro = (adapter.usage_summary() if read
                else manager_api.create_key(adapter.cfg, {"name": "x"}))
        with mock.patch.object(manager_api, "_client",
                               side_effect=AssertionError("no http")):
            try:
                asyncio.run(coro)
            except ConfigMissing as exc:
                return str(exc)
        self.fail("应当抛 ConfigMissing")


# ---------------------------------------------------------------- 3) 用错令牌 → 403
class WrongTokenForbiddenTest(_Base):
    """真 loopback HTTP + 按 scope 校验令牌的假 Manager。"""

    def _run(self, coro_factory):
        with _StubManager(self.db) as stub:
            adapter = WorkBuddyAdapter(self.settings(stub.base, wb_read_mode="http"))
            return asyncio.run(coro_factory(adapter)), stub

    def test_keys_token_on_read_endpoint_is_403(self) -> None:
        """把读令牌换成 keys 令牌 → Manager 403，QTG 报 UpstreamUnauthorized。"""
        with _StubManager(self.db) as stub:
            adapter = WorkBuddyAdapter(self.settings(
                stub.base, wb_read_mode="http",
                manager_read_token_file=self.token_file))  # 故意错用 keys 令牌
            with self.assertRaises(UpstreamUnauthorized) as ctx:
                asyncio.run(adapter.usage_summary())
        exc = ctx.exception
        self.assertEqual(exc.upstream_status, 403)
        self.assertEqual(exc.token_purpose, "read")
        self.assertIn("只读服务凭据", str(exc))

    def test_read_token_on_write_endpoint_is_403(self) -> None:
        with _StubManager(self.db) as stub:
            adapter = WorkBuddyAdapter(self.settings(
                stub.base, wb_read_mode="http",
                manager_service_token_file=self.read_token_file))  # 故意错用读令牌
            with self.assertRaises(UpstreamUnauthorized) as ctx:
                asyncio.run(manager_api.create_key(adapter.cfg, {"name": "x"}))
        exc = ctx.exception
        self.assertEqual(exc.upstream_status, 403)
        self.assertEqual(exc.token_purpose, "keys")
        self.assertIn("密钥管理服务凭据", str(exc))


# ---------------------------------------------------------------- 4) 零 DB 依赖
# 全部读方法的调用清单（16 条：14 条既有 + v2 新增的 usage/usage_analytics）
# models 在 sqlite 模式走数据面 HTTP，但 http 模式走 Manager /api/models，
# 因此也纳入"http 模式不得碰 DB"的清单。
READ_CALLS: list[tuple[str, object, object]] = [
    ("overview", lambda a: a.overview(), "checkin"),
    ("accounts", lambda a: a.accounts(), "accounts"),
    ("keys", lambda a: a.keys(), "keys"),
    ("logs", lambda a: a.logs(10), "logs"),
    ("task_logs", lambda a: a.task_logs(10), "logs"),
    ("audit_logs", lambda a: a.audit_logs(10), "logs"),
    ("settings", lambda a: a.settings(), "panel"),
    ("models", lambda a: a.models(), "models"),
    ("read_contract", lambda a: a.read_contract(), "version"),
    ("usage_summary", lambda a: a.usage_summary(), "total_requests"),
    ("usage_hourly", lambda a: a.usage_hourly(), "buckets"),
    ("credits_flow", lambda a: a.credits_flow(7), "daily"),
    ("usage_by_model", lambda a: a.usage_by_model(7), "rows"),
    ("usage_by_key", lambda a: a.usage_by_key(7), "rows"),
    ("usage", lambda a: a.usage(14), "usage"),
    ("usage_analytics", lambda a: a.usage_analytics(), "summary"),
]


class HttpModeNoDatabaseTest(_Base):
    """http 模式 + WB_DB 指向不存在路径：全部读都返回真实数据且零 _db 调用。"""

    def test_all_reads_work_without_database(self) -> None:
        with _StubManager(self.db) as stub:
            adapter = WorkBuddyAdapter(self.settings(stub.base, wb_read_mode="http"))
            self.assertFalse(Path(adapter.cfg.wb_db).exists(),
                             "前提：WB_DB 必须指向不存在的路径")
            for name, call, marker in READ_CALLS:
                with self.subTest(reader=name):
                    with mock.patch.object(WorkBuddyAdapter, "_db", autospec=True,
                                           return_value=None) as db_spy:
                        got = asyncio.run(call(adapter))
                    self.assertEqual(db_spy.call_count, 0,
                                     f"{name}: http 模式不得调用 _db()")
                    self.assertIsNotNone(got, f"{name}: 不应是 None")
                    self.assertNotEqual(got, {}, f"{name}: 不应是空对象")
                    self.assertNotEqual(got, [], f"{name}: 不应是空数组")
                    if marker == "total_requests":
                        self.assertGreater(got["total_requests"], 0, name)
                    elif marker == "buckets":
                        self.assertEqual(len(got), 24, name)
                        self.assertGreater(sum(b["requests"] for b in got), 0, name)
                    elif marker == "usage":
                        self.assertTrue(got["usage"], name)
                    elif marker == "summary":
                        self.assertGreater(got["summary"]["requests"], 0, name)
                    elif marker == "rows":
                        self.assertTrue(got, name)
                    elif marker == "checkin":
                        self.assertTrue(got["checkin"], name)
                    elif marker == "version":
                        self.assertEqual(got["mode"], "http", name)
                        self.assertEqual(got["version"], 2, name)

    def test_unknown_version_blocks_every_read(self) -> None:
        """契约版本未知时，任何读取都必须显式失败（不是"读个大概"）。"""
        contract = dict(CONTRACT_V2)
        contract["version"] = 7
        payloads = {"/api/read-api/version": contract}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = payloads.get(request.url.path)
            if payload is None:
                return httpx.Response(404, content=b'{"detail":"nf"}')
            return httpx.Response(200, content=json.dumps(payload).encode())
        adapter = WorkBuddyAdapter(self.settings("http://127.0.0.1:7864",
                                                 wb_read_mode="http"))
        with mock.patch.object(manager_api, "_client",
                               lambda timeout=None: httpx.AsyncClient(
                                   transport=httpx.MockTransport(handler),
                                   timeout=timeout or 5.0)), \
             mock.patch.object(WorkBuddyAdapter, "_db", autospec=True,
                               return_value=None) as db_spy:
            with self.assertRaises(manager_api.UpstreamError) as ctx:
                asyncio.run(adapter.usage_summary())
        self.assertEqual(ctx.exception.code, "read_api_unsupported")
        self.assertEqual(db_spy.call_count, 0)


# ---------------------------------------------------------------- 5) 口径对账
class ParityTest(_Base):
    """同一份非空 fixture：sqlite 与 http 两种读模式逐值对账。

    假 Manager 的口径与 QTG sqlite 读法同源（同一份库、同一条 SQL 语义），
    因此这条对账能把 **QTG 自己两种通道的映射漂移** 钉住（例如签到记录：
    以前 http 返回逐条、sqlite 返回按天聚合）。真实 Manager 的端到端口径对账
    在 evidence/r1-k26/probe_loopback_real_manager.py 里另做一次。
    """

    def test_parity_projection(self) -> None:
        with _StubManager(self.db) as stub:
            sqlite_adapter = WorkBuddyAdapter(self.settings(
                stub.base, wb_read_mode="sqlite", wb_db=self.db))
            http_adapter = WorkBuddyAdapter(self.settings(stub.base,
                                                          wb_read_mode="http"))

            def run(adapter, call):
                return asyncio.run(call(adapter))

            pairs = {
                # 数值口径类：直接整份对比
                "usage_summary": (lambda a: a.usage_summary(),
                                  lambda got: {k: got[k] for k in (
                                      "today_requests", "today_tokens",
                                      "week_requests", "week_tokens",
                                      "total_requests", "total_tokens",
                                      "active_keys", "top_model")}),
                "usage_hourly": (lambda a: a.usage_hourly(),
                                 lambda got: [(b["hour"], b["requests"],
                                               b["prompt_tokens"],
                                               b["completion_tokens"],
                                               b["credit"], b["failed"])
                                              for b in got]),
                "usage_by_model": (lambda a: a.usage_by_model(30),
                                   lambda got: [(r["name"], r["requests"],
                                                 r["prompt_tokens"],
                                                 r["completion_tokens"],
                                                 r["credit"]) for r in got]),
                "usage": (lambda a: a.usage(14),
                          lambda got: [(u["date"], u["requests"], u["errors"],
                                        u["ok"], u["credit"],
                                        [(m["name"], m["requests"])
                                         for m in u["models"]])
                                       for u in got["usage"]]),
                "usage_analytics": (lambda a: a.usage_analytics(),
                                    lambda got: dict(got["summary"])),
                # 列表类：按业务主键/计数对账（http 通道是 Manager 的原始行）
                "keys": (lambda a: a.keys(),
                         lambda got: sorted((k["id"], k["prefix"], k["enabled"])
                                            for k in got["data"]["keys"])),
                "logs": (lambda a: a.logs(10),
                         lambda got: sorted((r["ts"], r["model"], r["status"])
                                            for r in (got.get("data") or got.get("items")
                                                      or []))),
                "task_logs": (lambda a: a.task_logs(10),
                              lambda got: len(got.get("data")
                                              or got.get("logs") or [])),
                "audit_logs": (lambda a: a.audit_logs(10),
                               lambda got: len(got.get("data")
                                               or got.get("items") or [])),
                # checkin：R1-A 修掉了"http 逐条 vs sqlite 按天"的漂移
                "overview": (lambda a: a.overview(),
                             lambda got: [(d["date"], d["ok"], d["failed"],
                                           d["total"]) for d in got["checkin"]]),
            }
            for name, (call, project) in pairs.items():
                with self.subTest(method=name):
                    sq = project(run(sqlite_adapter, call))
                    ht = project(run(http_adapter, call))
                    self.assertEqual(sq, ht, f"{name}: sqlite 与 http 口径不一致")

    def test_settings_panel_accessible_in_both_modes(self) -> None:
        with _StubManager(self.db) as stub:
            sqlite_adapter = WorkBuddyAdapter(self.settings(
                stub.base, wb_read_mode="sqlite", wb_db=self.db))
            http_adapter = WorkBuddyAdapter(self.settings(stub.base,
                                                          wb_read_mode="http"))
            sq = asyncio.run(sqlite_adapter.settings())
            ht = asyncio.run(http_adapter.settings())
        self.assertEqual(sq["raw"]["panel"], ht["panel"],
                         "设置面板内容两种模式必须一致（形状不同，值必须相同）")


if __name__ == "__main__":
    unittest.main()
