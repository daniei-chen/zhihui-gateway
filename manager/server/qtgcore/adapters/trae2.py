"""Trae2 引擎适配器（aiwork-server，默认 http://127.0.0.1:8080）。

为什么要有它
------------
旧数据面是 `trae-bff`（Go，**源码已丢失**，且它只是 `aiwork-server` 之上的一层薄
封装）。`aiwork-server` 是 Rust 写的完整实现（镜像 `ghcr.io/smart-open/traeworkassistant`
可拉取、可升级），内部已经解决了 trae 上游的 OAuth 换 token、JWT 刷新、
`x-machine-id` / `x-device-*` 设备指纹、签到与额度查询。

所以复刻 trae-bff 的正确做法不是重新逆向 trae 官方协议，而是**直接对接
aiwork-server**，把「多账号池 + OpenAI 兼容 + 统计」这一层用 Python 重写：

    客户端 → qtg(:8795) → aiwork-server(:8080) → trae 上游

对接面（全部实测确认）
----------------------
| 用途 | 入口 |
|---|---|
| 登录 | `POST /api/login {"token": <admin_token>}` → 会话 Cookie |
| 账号列表 | `POST /api/cmd/accounts_list` |
| 池状态 | `POST /api/cmd/pool_list` |
| 设置 | `POST /api/cmd/settings_get` |
| 签到 | `POST /api/cmd/checkin_start`（trae-bff 同款命令） |
| 模型 | `GET /v1/models`（要 aiwork 的 API Key） |
| 用量 | SQLite `api_usage`（bucket,day,data JSON）只读 |
| 密钥 | SQLite `api_keys` 只读 |
| 数据面 | `POST /v1/chat/completions`（AI Key） |

回滚
----
`/opt/workbuddy-manager/engine/var/trae_engine` 写 `bff` 即回到旧 trae-bff，重启 qtg 生效。
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ..config import Settings
from ..errors import UpstreamError
from .base import BaseAdapter


def _schema_ok(conn: sqlite3.Connection, known: set[str]) -> bool:
    """库里出现**任意一张已知表**就认为是"我们认识的库"（K26）。"""
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.Error:
        return False
    names = {str(r[0]) for r in rows}
    return bool(names & known)


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


class Trae2Adapter(BaseAdapter):
    name = "trae2"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings.aiwork_base, settings)
        self.cfg = settings
        self.authorization_generation = secrets.token_hex(16)

    # ---- 鉴权（admin token → 会话 Cookie，httpx 自己维护） ----
    def _credentials(self) -> str:
        try:
            with open(self.cfg.aiwork_admin_token_file, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    async def authenticate(self) -> None:
        token = self._credentials()
        if not token:
            raise UpstreamError(f"aiwork admin token 缺失: {self.cfg.aiwork_admin_token_file}")
        resp = await self._request(
            "POST", "/api/login", json_body={"token": token}, retry=False, auth=False
        )
        if resp.status_code != 200:
            raise UpstreamError(f"aiwork 登录失败: HTTP {resp.status_code} {resp.text[:160]}")
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        if body.get("ok") is False:
            raise UpstreamError(f"aiwork 登录被拒绝: {str(body.get('error'))[:120]}")
        # A new engine session (including after restart) invalidates the previous
        # in-memory PKCE slot. Never complete an old callback after re-authentication.
        self.authorization_generation = secrets.token_hex(16)

    async def _cmd(self, name: str, body: Optional[Dict[str, Any]] = None,
                   required: bool = True) -> Dict[str, Any]:
        """Tauri 风格的命令通道：`POST /api/cmd/<name>`。"""
        out = await self._json(
            "POST", f"/api/cmd/{name}", json_body=body or {}, required=required
        )
        return out if isinstance(out, dict) else {}

    # ---- SQLite（只读；aiwork 的数据目录由 docker-compose 挂到宿主机） ----
    def _db(self) -> Optional[sqlite3.Connection]:
        """只读连接 aiwork 的 SQLite（K26）。

        - `mode=ro` + `PRAGMA query_only=1` 双保险；
        - schema 探测：必需表缺失时返回 None，上层退回命令层而不是 500；
        - 单元测试会用 `Trae2Adapter.__new__` 绕过 __init__（没有 cfg），
          缺配置就当作「没有本地库」。
        """
        cfg = getattr(self, "cfg", None)
        if cfg is None:
            return None
        path = Path(cfg.aiwork_db)
        if not path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            conn.execute("PRAGMA query_only=1")
            if not _schema_ok(conn, {"api_usage", "credits_daily", "checkin_results"}):
                conn.close()
                return None
            return conn
        except sqlite3.Error:
            return None

    def _usage_rows(self, days: int) -> List[Dict[str, Any]]:
        """把 `api_usage` 的 (bucket, day, data) 转成 qtg 熟悉的按天列表。

        只取 `trae` bucket（`wb` / `doubao` 是 aiwork 里的其它池，不属于本平台）。
        """
        conn = self._db()
        if conn is None:
            return []
        try:
            cur = conn.execute(
                "SELECT day, data FROM api_usage WHERE bucket='trae' ORDER BY day DESC LIMIT ?",
                (max(1, min(180, days)),),
            )
            rows: List[Dict[str, Any]] = []
            for day, data in cur.fetchall():
                try:
                    d = json.loads(data or "{}")
                except json.JSONDecodeError:
                    continue
                total = d.get("total") or {}
                rows.append(
                    {
                        "date": str(day),
                        "requests": _i(total.get("requests")),
                        "ok": _i(total.get("ok")),
                        "errors": _i(total.get("errors")),
                        "prompt_tokens": _i(d.get("prompt_tokens")),
                        "completion_tokens": _i(d.get("completion_tokens")),
                        "models": [
                            {"name": k, "requests": _i(v.get("requests")),
                             "ok": _i(v.get("ok")), "errors": _i(v.get("errors"))}
                            for k, v in (d.get("models") or {}).items()
                            if isinstance(v, dict)
                        ],
                        "accounts": [
                            {"name": k, "requests": _i(v.get("requests"))}
                            for k, v in (d.get("accounts") or {}).items()
                            if isinstance(v, dict)
                        ],
                    }
                )
            return rows
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # ---- SQLite 补位：命令层没有的，直接读库（只读，不碰写锁）----
    def _today(self) -> str:
        """aiwork 用服务器本地时间（CST）记 `checkin_results.day`。"""
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date().isoformat()

    def _credits_daily(self, days: int = 90) -> List[Dict[str, Any]]:
        """积分收支。`credits_daily` 每天一行：total(累计余额) / earned(签到 +100) /
        consumed(消耗)。命令层没有对应命令——不读这张表，
        `/api/stats/credits` 的 income 永远是 0。"""
        conn = self._db()
        if conn is None:
            return []
        try:
            cur = conn.execute(
                "SELECT date, total, earned, consumed FROM credits_daily"
                " ORDER BY date DESC LIMIT ?",
                (max(1, min(400, int(days))),),
            )
            rows = [
                {"date": str(r[0]), "total": float(r[1] or 0),
                 "earned": float(r[2] or 0), "consumed": float(r[3] or 0)}
                for r in cur.fetchall()
            ]
            rows.reverse()
            return rows
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def _checkin_rows(self, days: int = 60) -> List[Dict[str, Any]]:
        """签到历史：`checkin_results` 按 (day, uid) 一行，status 取
        success / already / failed；按天聚合成 qtg 的 `checkin[]` 形状。"""
        conn = self._db()
        if conn is None:
            return []
        try:
            cur = conn.execute(
                "SELECT day, uid, name, status, updated_at FROM checkin_results"
                " ORDER BY day DESC, uid LIMIT 2000"
            )
            by_day: Dict[str, Dict[str, Any]] = {}
            for day, uid, name, status, ts in cur.fetchall():
                key = str(day or "")
                b = by_day.setdefault(key, {
                    "date": key, "ok": 0, "already": 0, "failed": 0,
                    "total": 0, "updated_at": str(ts or ""), "items": [],
                })
                b["total"] += 1
                st = str(status or "").lower()
                if st == "success":
                    b["ok"] += 1
                elif st == "already":
                    b["already"] += 1
                else:
                    b["failed"] += 1
                b["items"].append({"uid": str(uid or ""), "name": str(name or ""),
                                   "status": st})
            out = list(by_day.values())
            out.sort(key=lambda x: x["date"], reverse=True)
            return out[: max(1, min(180, int(days)))]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def _today_checkin(self) -> List[Dict[str, Any]]:
        conn = self._db()
        if conn is None:
            return []
        try:
            cur = conn.execute(
                "SELECT uid, name, status FROM checkin_results WHERE day = ?",
                (self._today(),),
            )
            return [{"uid": str(r[0] or ""), "name": str(r[1] or ""),
                     "status": str(r[2] or "").lower()} for r in cur.fetchall()]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def _api_keys(self) -> List[Dict[str, Any]]:
        conn = self._db()
        if conn is None:
            return []
        try:
            cur = conn.execute(
                "SELECT id, name, key, enabled, created_at FROM api_keys ORDER BY created_at DESC LIMIT 50"
            )
            out = []
            for kid, name, key, enabled, created in cur.fetchall():
                raw = str(key or "")
                out.append(
                    {
                        "id": str(kid),
                        "name": str(name or ""),
                        "key": raw,
                        "masked": (raw[:12] + "..." + raw[-4:]) if len(raw) > 20 else raw,
                        "enabled": bool(enabled),
                        "created_at": str(created or ""),
                    }
                )
            return out
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # ---- 只读 ----
    async def healthz(self) -> Any:
        """aiwork 没有可直接探的健康端点（`/healthz` 要 API Key，`/api/healthz` 是 404）；
        能拿到 `settings_get` 就说明会话与 DB 都活着。"""
        r = await self._cmd("settings_get", required=False)
        return {"ok": isinstance(r, dict) and r.get("data") is not None}

    async def overview(self) -> Dict[str, Any]:
        """qtg 的 `map_trae_account()` 期望 `{"accounts": [...]}`；
        aiwork 的 `accounts_list` 字段名与它**完全同源**
        （user_id / remaining_credits / total_credits / credits_expire_at /
        checked_today / cooldown_until / refresh_token_fails …），无需转换。"""
        r = await self._cmd("accounts_list", required=False)
        rows = r.get("data") or []
        if isinstance(rows, dict):
            rows = [rows]
        return {"accounts": rows, "checkin": self._checkin_rows()}

    async def usage(self, days: int = 14) -> Dict[str, Any]:
        """用量：**优先走 `api_usage_stats` 命令**（形状与旧 trae-bff 完全一致），
        SQLite 只做兜底——直接读库虽然快，但会被 aiwork 的写锁影响到。"""
        days = max(1, min(90, int(days)))
        r = await self._cmd("api_usage_stats", {"days": days}, required=False)
        rows = r.get("data")
        if isinstance(rows, list) and rows:
            usage = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                usage.append(
                    {
                        "date": str(row.get("date") or ""),
                        "requests": _i(row.get("total_requests") or row.get("ok")),
                        "ok": _i(row.get("ok")),
                        "errors": _i(row.get("errors")),
                        "prompt_tokens": _i(row.get("prompt_tokens")),
                        "completion_tokens": _i(row.get("completion_tokens")),
                        "models": row.get("models") or [],
                        "accounts": row.get("accounts") or [],
                    }
                )
            return {"usage": usage, "credits_daily": self._credits_daily(days)}
        return {"usage": self._usage_rows(days), "credits_daily": self._credits_daily(days)}

    async def settings(self) -> Dict[str, Any]:
        r = await self._cmd("settings_get", required=False)
        data = r.get("data") or {}
        pool = await self._cmd("pool_list", required=False)
        return {
            "scheduler": {
                "enabled": bool(data.get("auto_start_proxy")),
                "mode": "aiwork 内置调度",
                "tasks": [],
            },
            "pool": pool.get("data") or {},
            "raw": data,
        }

    async def keys(self) -> Dict[str, Any]:
        r = await self._cmd("api_keys_list", required=False)
        data = r.get("data")
        if isinstance(data, dict) and data.get("keys") is not None:
            return {"data": data}
        return {"data": {"auth_disabled": False, "keys": self._api_keys()}}

    async def models(self) -> Dict[str, Any]:
        """aiwork 的 `/v1/models` 要它自己的 API Key（不是 admin token）。"""
        token = ""
        try:
            token = Path(self.cfg.aiwork_api_key_file).read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if not token:
            return {"data": []}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    self.base_url + "/v1/models",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code != 200:
                return {"data": []}
            body = resp.json()
            return body if isinstance(body, dict) else {"data": []}
        except Exception:  # noqa: BLE001
            return {"data": []}

    async def logs(self) -> Dict[str, Any]:
        out: List[Dict[str, Any]] = []
        log_dir = Path(self.cfg.aiwork_log_dir)
        if log_dir.is_dir():
            files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:2]
            for f in files:
                try:
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
                except OSError:
                    continue
                out.extend({"ts": "", "level": "info", "msg": ln[:300]} for ln in lines)
        return {"data": out[-150:]}

    # ---- 写操作 ----
    async def checkin(self) -> Dict[str, Any]:
        """aiwork 的 `checkin_start` 是**异步任务**（返回 ok 后后台跑，重复调用会报
        「已有签到任务正在进行中」）。这里把它翻译成 qtg 认识的 `results[]`，
        否则兼容层的批量签到会显示「成功 0 / 0」。"""
        r = await self._cmd("checkin_start", {"scope": "all"}, required=False)
        ok = bool(r.get("ok"))
        err = str(r.get("error") or "")
        if not ok:
            return {"ok": False,
                    "results": [{"nickname": "Trae", "ok": False,
                                 "message": err or "签到失败"}]}
        # `checkin_start` 只是「启动」；真实结果由 aiwork 写进 `checkin_results`
        # （status = success / already / failed）。不读这张表就会把「今天早就签过」
        # 也报成「签到成功」——2026-10-23 用户实测发现的 bug。
        rows: List[Dict[str, Any]] = []
        for _ in range(5):
            rows = self._today_checkin()
            if rows:
                break
            await asyncio.sleep(1.6)
        if not rows:
            return {"ok": True,
                    "results": [{"nickname": "Trae", "ok": True,
                                 "message": "签到任务已提交，上游仍在执行"
                                            "（约 10 秒后刷新可见结果）"}]}
        results = []
        any_fail = False
        for row in rows:
            st = row.get("status") or ""
            name = row.get("name") or row.get("uid") or "Trae"
            if st == "success":
                results.append({"nickname": name, "ok": True, "status": st,
                                "message": "签到成功（+100 积分）"})
            elif st == "already":
                results.append({"nickname": name, "ok": True, "status": st,
                                "message": "今日已签到过（未重复发放积分）"})
            else:
                any_fail = True
                results.append({"nickname": name, "ok": False, "status": st or "failed",
                                "message": "签到失败（%s）" % (st or "未知状态")})
        return {"ok": not any_fail, "results": results}

    async def refresh_account(self, uid: str = "") -> Dict[str, Any]:
        """`refresh_jwt` —— aiwork 里刷新某个账号 JWT 的命令（要 user_id）。"""
        if not uid:
            return {"ok": False, "error": "缺少 user_id"}
        return await self._cmd("refresh_jwt", {"user_id": uid}, required=False) or {"ok": False}

    async def set_account(self, uid: str, enabled: bool) -> Dict[str, Any]:
        """aiwork 没有「启用/禁用单个账号」，只有 `pool_set` 整体重写 enabled_uids，
        所以这里读当前池 → 增删该 uid → 写回。"""
        if not uid:
            return {"ok": False, "error": "缺少 user_id"}
        pool = await self._cmd("pool_list", required=False)
        cur = list(((pool.get("data") or {}).get("enabled_uids") or []))
        if enabled:
            if uid not in cur:
                cur.append(uid)
        else:
            cur = [u for u in cur if u != uid]
        # aiwork 的 pool_set 只认 `uids`（不是它 pool_list 返回的那个键名）。
        # 传错键名它照样回 ok:true，池子却不变 —— 2026-10-23 实测踩过，
        # 表现是「点了启用界面说成功，数据面继续 503」。
        return await self._cmd("pool_set", {"uids": cur}, required=False) or {"ok": False}

    async def add_account(self, name: str, jwt: str) -> Dict[str, Any]:
        return await self._cmd(
            "accounts_import", {"content": jwt, "name": name}, required=False
        ) or {"ok": False}

    async def save_keys(self, keys: List[Dict[str, Any]], auth_disabled: Optional[bool] = None) -> Dict[str, Any]:
        return {"ok": False, "error": "aiwork 的 API Key 请在它自己的设置页维护"}

    async def oauth_login_url(self) -> Dict[str, Any]:
        """Pinned aiwork command; never probe aliases or WorkBuddy OAuth."""
        out = await self._json("POST", "/api/cmd/oauth_get_login_url", json_body={}, retry=False)
        if not isinstance(out, dict) or out.get('ok') is not True or not isinstance(out.get('data'), dict):
            raise UpstreamError('Trae authorization response was not confirmed')
        return out['data']

    async def oauth_finish(self, callback_url: str, name: str = "") -> Dict[str, Any]:
        body = {"callback_url": callback_url}
        if name:
            body['account_name'] = name
        # Do not transparently log in again here: the engine may have restarted
        # and lost PKCE state. A 401 is an explicit unconfirmed completion.
        out = await self._json("POST", "/api/cmd/oauth_login", json_body=body, retry=False, auth=False)
        if not isinstance(out, dict) or out.get('ok') is not True or not isinstance(out.get('data'), dict):
            raise UpstreamError('Trae account insertion was not confirmed')
        return out['data']

    # ---- 健康 ----
    async def health(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            h = await self.healthz() or {}
        except Exception as exc:  # noqa: BLE001
            return {"up": False, "latency_ms": None, "last_error": str(exc)[:200], "engine": "trae2"}
        latency = int((time.monotonic() - started) * 1000)
        return {
            "up": bool(h.get("ok")),
            "latency_ms": latency,
            "last_error": "",
            "engine": "trae2",
        }
