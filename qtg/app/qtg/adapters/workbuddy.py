"""WorkBuddy 引擎适配器（workbuddy2api :7863 + workbuddy-manager :7864）。

为什么是「只读聚合」
--------------------
workbuddy 这一侧本来就已经是成熟的两层结构，而且**正在对外服务**：

    客户端 → workbuddy2api(:7863, docker) → codebuddy 上游
                    ↑ 管理
            workbuddy-manager(:7864, FastAPI + Next.js 面板)

用户明确要求：不改造、不重启、不改写这两个服务，只把它们的页面与数据
并进 qtg 做「三合一」。所以这里一律**只读**，数据源三类：

| 数据 | 来源 | 说明 |
|---|---|---|
| 账号凭证 | `/opt/workbuddy2api/auths/*.json` | 34 个文件，含 uid/nickname/expiresAt/realm |
| 账号运行态 | `/opt/workbuddy2api/data/state.json` | 积分、冷却、成功/失败计数、过期积分 |
| 密钥/用量/日志 | `/opt/workbuddy-manager/data/manager.db` | SQLite，22 张表，17 万条请求日志 |

写操作（签到、启停、刷新 token）走 manager 的 HTTP API 需要面板登录态，
本轮**不接**：与其假装成功，不如明确返回未实现（见 checkin / set_account）。

回滚
----
`/opt/qtg/var/wb_engine` 写 `off` 即从聚合里摘掉这一平台，重启 qtg 生效。

读通道（K26，2026-10-25 RV05 修复）
----------------------------------
`wb_read_mode` 决定走哪条通道，**只有两条，且不互相回退**：

* `sqlite`（默认，现网行为）：只读打开 Manager 的 SQLite，所有 `_*_from_sql` 实现；
* `http`：全部经 Manager 的只读 API（`manager_api.read_*`）。任何失败——404、500、
  超时、连接失败、非 JSON、`null`/`{}` body、结构不符——**向上抛**，绝不落到 `_db()`。

历史实现用「`_stats_via_http` 返回 None」表示"该走 sqlite"，而 404 也被翻译成
`None`，两个语义共用一个哨兵，于是 http 模式下上游 404 会静默直读本地 SQLite，
无库时返回 `{}`（把"读不到"伪装成"没有数据"）。现在通道判定只在每个读方法的开头
由 `_http_reads_enabled()` 做一次，`_db()` 自身在 http 模式下直接抛 RuntimeError
作为兜底。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import manager_api
from ..config import Settings
from .base import BaseAdapter


log = logging.getLogger("qtg.adapters.workbuddy")

# 读取契约探测的缓存时长（秒）。Manager 的契约只在升级时变化，没必要每次读都探一次；
# 失败不缓存，Manager 恢复后下一次读取立即生效。
_READ_CONTRACT_TTL_SEC = 300.0


def _schema_ok(conn: sqlite3.Connection, known: set[str]) -> bool:
    """库里出现**任意一张已知表**就认为这是"我们认识的库"（K26）。

    不要求所有表都在：上游可能裁剪表、测试夹具只建部分表。真正的"表缺失"
    由各查询自己的错误处理降级；这里只负责识别"这根本不是一个 Manager 库"。
    """
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


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 结构校验（RV05）
# HTTP 读路径的载荷由 Manager 决定；**缺字段不等于 0，形状不对不等于没有数据**。
# 这里统一显式抛错（UpstreamPayloadError 是 UpstreamError 的子类，调用方与
# manager_api 的其它上游失败用同一套处理）。
def _require_fields(payload: Dict[str, Any], fields: tuple[str, ...],
                    what: str) -> Dict[str, Any]:
    missing = [name for name in fields if name not in payload]
    if missing:
        raise manager_api.UpstreamPayloadError(
            f"Manager 的{what}缺少必需字段：{', '.join(missing)}")
    return payload


def _require_items(payload: Any, what: str,
                   item_fields: tuple[str, ...] = ()) -> List[Dict[str, Any]]:
    """从 `{items|data|logs|rows: [...]}` 或纯数组里取出条目并逐项校验。"""
    if isinstance(payload, dict):
        items: Any = None
        for key in ("items", "data", "logs", "rows"):
            if key in payload:
                items = payload[key]
                break
        if items is None:
            raise manager_api.UpstreamPayloadError(
                f"Manager 的{what}结构异常：没有 items/data/logs/rows 条目数组")
    else:
        items = payload
    if not isinstance(items, list):
        raise manager_api.UpstreamPayloadError(
            f"Manager 的{what}结构异常：条目不是数组（{type(items).__name__}）")
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise manager_api.UpstreamPayloadError(
                f"Manager 的{what}第 {index} 项不是对象（{type(item).__name__}）")
        _require_fields(item, item_fields, f"{what}第 {index} 项")
        out.append(item)
    return out


class WorkBuddyAdapter(BaseAdapter):
    name = "workbuddy"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings.wb_api_base, settings)
        self.cfg = settings

    # ---------------- 凭证 ----------------
    def _credentials(self) -> str:
        """workbuddy2api 的数据面 API Key，藏在它自己的 config.json 里。"""
        try:
            data = json.loads(Path(self.cfg.wb_config_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str(data.get("api_key") or "").strip()

    def api_key(self) -> str:
        """给数据面转发用的上游 Key（proxy._upstream_key 会调）。"""
        return self._credentials()

    async def authenticate(self) -> None:
        key = self._credentials()
        if not key:
            raise RuntimeError(f"workbuddy API Key 缺失: {self.cfg.wb_config_file}")
        # 数据面（/v1/*）用 Bearer；面板（:7864）另有一套会话，不走这里
        self.client.headers["Authorization"] = f"Bearer {key}"
        self.client.headers.pop("X-Panel-Token", None)

    # ---------------- K26 读取通道 ----------------
    @property
    def read_mode(self) -> str:
        """当前读取方式（"sqlite" | "http"）。非法值按 sqlite 处理并记一次告警。"""
        mode = str(getattr(self.cfg, "wb_read_mode", "sqlite") or "sqlite").strip().lower()
        if mode not in ("sqlite", "http"):
            log.warning("wb_read_mode=%r 非法，按 sqlite 处理", mode)
            return "sqlite"
        return mode

    def _http_reads_enabled(self) -> bool:
        return self.read_mode == "http"

    async def read_contract(self) -> Dict[str, Any]:
        """探测 Manager 的读取契约（K26 步骤 A）。

        只有 http 模式才会真的发请求；sqlite 模式返回本地声明，便于监控面板
        统一展示"当前用哪条通道、契约版本是多少"。
        """
        if not self._http_reads_enabled():
            return {"mode": "sqlite", "version": None, "capabilities": {}}
        info = await manager_api.read_api_version(self.cfg)
        return {"mode": "http", **info}

    async def _stats_via_http(self, name: str, params: Dict[str, Any] | None = None) -> Any:
        """统计族 HTTP 读取：**只能在 http 读模式下调用**，绝不返回"请走 sqlite"。

        2026-10-25（RV05）：原实现用 `None` 表示"当前是 sqlite 模式，调用方请回退
        本地 SQL"，而 `manager_api._request` 又把 HTTP 404 翻译成同一个 `None`——
        两个完全不同的语义共用一个哨兵。于是 http 模式下上游 404 时，`usage_summary()`
        会真的去调 `_db()` 直读 SQLite，无库时返回 `{}`。

        现在通道判定只由调用方开头的 `_http_reads_enabled()` 负责，本函数在 sqlite
        模式下被调用属于**内部错误**：抛 RuntimeError，不返回任何"请回退"信号。
        """
        if not self._http_reads_enabled():
            raise RuntimeError(
                "内部错误：_stats_via_http 只能在 wb_read_mode=http 下调用；"
                "sqlite 分支必须直接调用对应的 _*_from_sql 实现")
        await self._ensure_read_capability("stats")
        return await manager_api.read_stats(self.cfg, name, params)

    # ---------------- 读取契约（K26 v2） ----------------
    async def _read_contract_cached(self) -> Dict[str, Any]:
        """探测读取契约（带 TTL 缓存）。

        缓存的是**成功结果**；失败不缓存（Manager 恢复后下一次立即重试）。
        这样每次读只多一次探针（300s 一次），而不是每个请求两次往返。
        """
        now = _time.monotonic()
        cached = getattr(self, "_contract_cache", None)
        if cached is not None and now - cached[0] < _READ_CONTRACT_TTL_SEC:
            return cached[1]
        info = await manager_api.read_api_version(self.cfg)
        self._contract_cache = (now, info)
        return info

    async def _ensure_read_capability(self, family: str, *,
                                      min_version: int = 1) -> Dict[str, Any]:
        """读取前校验契约：**未知版本或缺能力必须显式失败**，不得按旧语义猜。

        RV05 第 2 条要求"读取契约探测要在实际运行链路应用"：这里就是那个点。
        - 版本不在 `manager_api.KNOWN_READ_API_VERSIONS` → UpstreamError
          (code=read_api_unsupported)；
        - 版本低于该族的最低版本（如 usage 需要 v2）→ 同上；
        - 能力表里该族不可用/缺失 → 同上。
        任何一条都**不会**降级成"读个大概"或回退 SQLite。
        """
        info = await self._read_contract_cached()
        version = info.get("version")
        known = manager_api.KNOWN_READ_API_VERSIONS
        if version not in known:
            raise manager_api.UpstreamError(
                f"Manager 读取契约版本未知：{version!r}（QTG 支持 {sorted(known)}）；"
                "拒绝按未经验证的语义读取",
                code="read_api_unsupported")
        if version < min_version:
            raise manager_api.UpstreamError(
                f"Manager 读取契约版本 {version} 不提供 {family} 族"
                f"（需要 >= {min_version}）：请升级 Manager 或先用 sqlite 读模式",
                code="read_api_unsupported")
        if not manager_api.supports(info, family):
            raise manager_api.UpstreamError(
                f"Manager 未声明 {family} 读取能力（契约版本 {version}）；"
                "拒绝把「缺能力」当成「没有数据」",
                code="read_api_unsupported")
        return info

    # ---------------- 原始数据源 ----------------
    def _db(self) -> Optional[sqlite3.Connection]:
        """只读连接 Manager 的 SQLite（**仅 sqlite 读模式**）。

        - **读模式兜底**：`wb_read_mode=http` 时这里直接抛 RuntimeError。即使将来
          某个分支漏了分流，得到的也是一个显式错误，而不是悄悄读到本地库、把
          "读不到"伪装成"没有数据"（RV05 的根因）；
        - `mode=ro` + `PRAGMA query_only=1` 双保险，任何写入尝试都会被拒绝；
        - **schema 探测**：必需表不存在时返回 None，让调用方走"没有数据"分支，
          而不是把 sqlite3.OperationalError 抛成 500；
        - 连接由调用方 close（现有调用均已成对）。
        """
        cfg = getattr(self, "cfg", None)
        if cfg is None:
            return None
        if self._http_reads_enabled():
            raise RuntimeError(
                "K26 违规：wb_read_mode=http 时不得直读 Manager SQLite"
                "（_db() 只服务 sqlite 读模式；HTTP 失败必须向上抛，不得回退 DB）")
        path = Path(cfg.wb_db)
        if not path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            conn.execute("PRAGMA query_only=1")
            if not _schema_ok(conn, {"api_keys", "request_logs", "usage_daily",
                                     "usage_hourly", "task_logs"}):
                conn.close()
                return None
            return conn
        except sqlite3.Error:
            return None

    def _state(self) -> Dict[str, Any]:
        try:
            raw = Path(self.cfg.wb_state_file).read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _auths(self) -> List[Dict[str, Any]]:
        """读 auths 目录。每个文件 = 一个账号（凭证 + 昵称）。"""
        out: List[Dict[str, Any]] = []
        base = Path(self.cfg.wb_auths_dir)
        if not base.is_dir():
            return out
        for path in sorted(base.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            acc = raw.get("account") or {}
            auth = raw.get("auth") or {}
            uid = str(acc.get("uid") or path.stem.replace("workbuddy-", ""))
            if not uid:
                continue
            out.append({
                "uid": uid,
                "file": path.name,
                "nickname": str(acc.get("nickname") or ""),
                "enterprise_id": str(acc.get("enterpriseId") or ""),
                "expires_at": _i(auth.get("expiresAt")),
                "realm": str(auth.get("realm") or "cn").lower(),
                "domain": str(auth.get("domain") or ""),
                "has_refresh": bool(auth.get("refreshToken")),
            })
        return out

    # ---------------- 账号 ----------------
    async def overview(self) -> Dict[str, Any]:
        """合并「凭证文件」与「运行态 state.json」——两边缺一不可：
        只有凭证就不知道积分/冷却，只有 state 就不知道昵称和过期时间。"""
        state = self._state().get("accounts") or {}
        rows: List[Dict[str, Any]] = []
        for acc in self._auths():
            st = state.get(acc["uid"]) or {}
            for k, v in st.items():
                acc.setdefault(k, v)
            # state 里显式给了这些键，直接覆盖（auths 里没有）
            for k in ("credits", "credits_known", "disabled", "manual_disabled",
                      "until", "cool_kind", "success_count", "err_total",
                      "last_success", "last_err", "soft_streak", "session_dead_fails",
                      "consecutive_fails", "credits_expiring", "model_costs"):
                if k in st:
                    acc[k] = st[k]
            rows.append(acc)
        # K26：http 模式读 Manager 的签到日志端点；sqlite 模式保持原直连。
        # 两条通道都失败时**不互相回退**——回退会让"共享文件系统"这个前提
        # 在容器化部署里悄悄复活，掩盖真正的问题。
        checkin = await self._checkin_rows_via_mode()
        return {"accounts": rows, "checkin": checkin}

    async def _checkin_rows_via_mode(self) -> List[Dict[str, Any]]:
        """签到历史：按 wb_read_mode 分流（K26 迁移示例）。

        http：读 Manager /api/checkin-logs，失败/结构异常一律向上抛（不回退 SQL）；
        sqlite：保持原直连实现。两条通道**不互相回退**。
        """
        if self._http_reads_enabled():
            return await self._checkin_rows_from_manager()
        return self._checkin_rows()

    async def _checkin_rows_from_manager(self) -> List[Dict[str, Any]]:
        """http 通道：读原始签到记录后走**与 sqlite 相同的按天聚合**。

        R1-A 之前这里直接返回逐条记录，而 sqlite 通道返回按天聚合——同一个面板在
        两种读模式下形状不同，属于 K26 的"口径漂移"。现在两条通道共用
        `_aggregate_checkin()`，parity 测试逐字段对比。
        """
        await self._ensure_read_capability("checkin")
        data = await manager_api.read_checkin_logs(
            self.cfg, {"limit": 200, "offset": 0})
        items = _require_items(data, "签到日志", ("ts", "uid"))
        rows = [
            (_i(it.get("ts")), str(it.get("uid") or ""), str(it.get("nickname") or ""),
             str(it.get("source") or ""), str(it.get("kind") or ""),
             bool(it.get("success")), str(it.get("code") or ""),
             str(it.get("message") or ""))
            for it in items
        ]
        return self._aggregate_checkin(rows)

    def _checkin_rows(self, limit: int = 200) -> List[Dict[str, Any]]:
        """签到历史（按天聚合）。manager.db 的 checkin_logs 是逐条记录。"""
        conn = self._db()
        if conn is None:
            return []
        try:
            cur = conn.execute(
                "SELECT ts, uid, nickname, source, kind, success, code, message"
                " FROM checkin_logs ORDER BY ts DESC LIMIT ?",
                (max(1, min(2000, int(limit))),),
            )
            return self._aggregate_checkin([tuple(r) for r in cur.fetchall()])
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    @staticmethod
    def _aggregate_checkin(rows: List[tuple]) -> List[Dict[str, Any]]:
        """按天聚合签到记录（sqlite / http 共用，保证两种读模式形状与数值一致）。"""
        import datetime as _dt
        tz = _dt.timezone(_dt.timedelta(hours=8))
        by_day: Dict[str, Dict[str, Any]] = {}
        for ts, uid, nickname, source, kind, success, code, msg in rows:
            try:
                day = _dt.datetime.fromtimestamp(float(ts), tz).date().isoformat()
            except (ValueError, OSError, TypeError):
                continue
            b = by_day.setdefault(day, {
                "date": day, "ok": 0, "already": 0, "failed": 0,
                "total": 0, "updated_at": "", "items": [],
            })
            b["total"] += 1
            ok = bool(success)
            if ok:
                b["ok"] += 1
            else:
                b["failed"] += 1
            b["items"].append({
                "uid": str(uid or ""), "name": str(nickname or ""),
                "status": "success" if ok else "failed",
                "message": str(msg or "")[:120],
            })
        return sorted(by_day.values(), key=lambda x: x["date"], reverse=True)

    async def accounts(self) -> Dict[str, Any]:
        return await self.overview()

    # ---------------- 用量 ----------------
    # ---------------- 用量统计（照抄 workbuddy manager 的口径） ----------------
    def _stats_agg(self, conn, where: str, args: tuple, realm: str):
        """按 manager 的写法汇总 usage_daily：requests / tokens / credit。"""
        rf = " AND realm = ?" if realm in ("cn", "global") else ""
        rargs = (realm,) if rf else ()
        row = conn.execute(
            "SELECT COALESCE(SUM(requests),0),"
            "       COALESCE(SUM(prompt_tokens + completion_tokens),0),"
            "       COALESCE(SUM(credit),0)"
            "  FROM usage_daily WHERE " + where + rf,
            args + rargs,
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0), float(row[2] or 0)

    async def usage_summary(self, realm: str = "") -> Dict[str, Any]:
        """与 workbuddy manager 的 /api/stats/summary 同一口径。

        2026-10-23 用户反馈「用量统计不对」的根因就在这：qtg 原先从
        「最近 14 天窗口」推 total_*，而 manager 的 total_* 是**全表**汇总，
        于是 16 万次请求只显示 3.3 万次。week_* 也必须是最近 7 天（含今天）。

        K26/RV05：http 模式直接取 Manager 的 /api/stats/summary（同一口径），
        失败向上抛；sqlite 模式走本地实现。**没有第三条路**。
        """
        if self._http_reads_enabled():
            return await self._usage_summary_from_manager(realm)
        return self._usage_summary_from_sql(realm)

    async def _usage_summary_from_manager(self, realm: str) -> Dict[str, Any]:
        data = await self._stats_via_http(
            "summary", {"realm": realm} if realm else None)
        # 缺字段 = 结构异常，不当作 0 展示（RV05 第 3 条）。
        _require_fields(data, ("today_requests", "today_tokens", "today_credit",
                               "week_requests", "week_tokens", "week_credit",
                               "total_requests", "total_tokens", "total_credit",
                               "active_keys"), "summary")
        out = dict(data)
        out.setdefault("realm", realm or "")
        return out

    def _usage_summary_from_sql(self, realm: str) -> Dict[str, Any]:
        conn = self._db()
        if conn is None:
            return {}
        try:
            import datetime as _dt
            tz = _dt.timezone(_dt.timedelta(hours=8))
            now = _dt.datetime.now(tz)
            today = now.date().isoformat()
            week = (now.date() - _dt.timedelta(days=6)).isoformat()
            t_req, t_tok, t_credit = self._stats_agg(conn, "day = ?", (today,), realm)
            w_req, w_tok, w_credit = self._stats_agg(conn, "day >= ?", (week,), realm)
            a_req, a_tok, a_credit = self._stats_agg(conn, "1=1", (), realm)
            rf = " AND realm = ?" if realm in ("cn", "global") else ""
            rargs = (realm,) if rf else ()
            if realm in ("cn", "global"):
                active = conn.execute(
                    "SELECT COUNT(DISTINCT key_id) FROM usage_daily WHERE realm = ?",
                    (realm,)).fetchone()[0]
            else:
                active = conn.execute(
                    "SELECT COUNT(*) FROM api_keys WHERE enabled = 1").fetchone()[0]
            top = conn.execute(
                "SELECT model, SUM(requests + prompt_tokens + completion_tokens) AS score"
                "  FROM usage_daily WHERE 1=1" + rf +
                " GROUP BY model ORDER BY score DESC LIMIT 1", rargs).fetchone()
            return {
                "today_requests": t_req,
                "today_tokens": t_tok,
                "today_credit": round(t_credit, 4),
                "week_credit": round(w_credit, 4),
                "total_credit": round(a_credit, 4),
                "week_requests": w_req,
                "week_tokens": w_tok,
                "total_requests": a_req,
                "total_tokens": a_tok,
                "active_keys": int(active or 0),
                "top_model": str(top[0]) if top else None,
                "realm": realm or "",
            }
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    async def usage_hourly(self, day: str = "", realm: str = "") -> List[Dict[str, Any]]:
        """照抄 manager 的 /stats/hourly：

        * 24 个桶**固定补零**，X 轴不随「今天走到几点」抖动；
        * 用量取自 `usage_hourly`（与 usage_daily 同源写入）；
        * `failed` 取自 `request_logs`（usage_* 只累计有 token/扣费的请求）。
        qtg 原先是拿自己的代理流水分桶 —— workbuddy 的调用不经 qtg 转发时不落库，
        所以图上全是 0。

        K26/RV05：http 模式读 Manager，失败向上抛；sqlite 模式走本地实现。
        """
        if self._http_reads_enabled():
            return await self._usage_hourly_from_manager(day, realm)
        return self._usage_hourly_from_sql(day, realm)

    async def _usage_hourly_from_manager(self, day: str,
                                        realm: str) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if day:
            params["day"] = day
        if realm:
            params["realm"] = realm
        rows = await self._stats_via_http("hourly", params or None)
        for index, row in enumerate(rows):
            _require_fields(row, ("day", "hour", "requests"), f"hourly 第 {index} 项")
        return rows

    def _usage_hourly_from_sql(self, day: str, realm: str) -> List[Dict[str, Any]]:
        conn = self._db()
        if conn is None:
            return []
        try:
            import datetime as _dt
            tz = _dt.timezone(_dt.timedelta(hours=8))
            d = (day or "").strip()
            if len(d) != 10 or d[4] != "-" or d[7] != "-":
                d = _dt.datetime.now(tz).date().isoformat()
            rf = " AND realm = ?" if realm in ("cn", "global") else ""
            args = (d,) + ((realm,) if rf else ())
            cur = conn.execute(
                "SELECT hour, SUM(requests), SUM(prompt_tokens), SUM(completion_tokens),"
                "       COALESCE(SUM(credit),0)"
                "  FROM usage_hourly WHERE day = ?" + rf + " GROUP BY hour", args)
            by_hour = {int(r[0]): r for r in cur.fetchall()}
            fails: Dict[int, int] = {}
            try:
                base = _dt.datetime.fromisoformat(d).replace(tzinfo=tz)
                start = int(base.timestamp())
                rf2 = " AND COALESCE(realm,'cn') = ?" if realm in ("cn", "global") else ""
                a2 = (start, start + 86400) + ((realm,) if rf2 else ())
                cur2 = conn.execute(
                    "SELECT strftime('%H', ts, 'unixepoch', '+8 hours') AS h, COUNT(*)"
                    "  FROM request_logs WHERE ts >= ? AND ts < ? AND status >= 400"
                    "   AND key_id IS NOT NULL" + rf2 + " GROUP BY h", a2)
                for h, n in cur2.fetchall():
                    if h is not None:
                        fails[int(h)] = int(n)
            except sqlite3.Error:
                fails = {}
            out: List[Dict[str, Any]] = []
            for h in range(24):
                r = by_hour.get(h)
                out.append({
                    "day": d, "hour": h,
                    "requests": _i(r[1]) if r else 0,
                    "prompt_tokens": _i(r[2]) if r else 0,
                    "completion_tokens": _i(r[3]) if r else 0,
                    "credit": round(_f(r[4]), 4) if r else 0.0,
                    "failed": fails.get(h, 0),
                })
            return out
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    async def credits_flow(self, days: int = 30, realm: str = "") -> Dict[str, Any]:
        """照抄 manager 的 /stats/credits。

        两个数字是**两套口径**，不能混：
          收入 = task_logs 里 kind='credit' 的余额增加流水（签到/活跃/旅行）；
          支出 = usage_daily.credit（每次调用的真实扣费）。
        qtg 原来把收入硬算成 0（只有支出），所以「积分收支」永远只跌不涨。

        K26/RV05：http 模式读 Manager，失败向上抛；sqlite 模式走本地实现。
        """
        if self._http_reads_enabled():
            return await self._credits_flow_from_manager(days, realm)
        return self._credits_flow_from_sql(days, realm)

    async def _credits_flow_from_manager(self, days: int,
                                        realm: str) -> Dict[str, Any]:
        params = {"days": days}
        if realm:
            params["realm"] = realm
        data = await self._stats_via_http("credits", params)
        _require_fields(data, ("days", "daily", "today", "week", "total"), "credits")
        daily = data.get("daily")
        if not isinstance(daily, list):
            raise manager_api.UpstreamPayloadError(
                f"Manager 的 credits daily 结构异常：{type(daily).__name__}")
        for index, row in enumerate(daily):
            if not isinstance(row, dict):
                raise manager_api.UpstreamPayloadError(
                    f"Manager 的 credits daily 第 {index} 项不是对象")
            _require_fields(row, ("day", "income", "expense", "net"),
                            f"credits daily 第 {index} 项")
        return data

    def _credits_flow_from_sql(self, days: int, realm: str) -> Dict[str, Any]:
        conn = self._db()
        if conn is None:
            return {}
        try:
            import datetime as _dt
            tz = _dt.timezone(_dt.timedelta(hours=8))
            d = max(1, min(365, int(days)))
            now = _dt.datetime.now(tz)
            since = (now.date() - _dt.timedelta(days=d - 1)).isoformat()
            today = now.date().isoformat()
            rf = " AND realm = ?" if realm in ("cn", "global") else ""
            rargs = (realm,) if rf else ()
            expense: Dict[str, float] = {}
            for day, c in conn.execute(
                "SELECT day, COALESCE(SUM(credit),0) FROM usage_daily"
                " WHERE day >= ?" + rf + " GROUP BY day", (since,) + rargs).fetchall():
                expense[str(day)] = float(c or 0)
            income: Dict[str, float] = {}
            try:
                base = _dt.datetime.fromisoformat(since).replace(tzinfo=tz)
                rows = conn.execute(
                    "SELECT date(ts, 'unixepoch', '+8 hours') AS dd,"
                    "       COALESCE(SUM(credits),0)"
                    "  FROM task_logs WHERE kind = 'credit' AND ts >= ?"
                    " GROUP BY dd", (int(base.timestamp()),)).fetchall()
                for dd, c in rows:
                    if dd:
                        income[str(dd)] = float(c or 0)
            except sqlite3.Error:
                income = {}
            daily: List[Dict[str, Any]] = []
            for i in range(d):
                day = (now.date() - _dt.timedelta(days=d - 1 - i)).isoformat()
                a = income.get(day, 0.0)
                b = expense.get(day, 0.0)
                daily.append({"day": day, "income": round(a, 4),
                              "expense": round(b, 4), "net": round(a - b, 4)})
            week_from = (now.date() - _dt.timedelta(days=6)).isoformat()

            def _sum(rows: List[Dict[str, Any]]) -> Dict[str, float]:
                a = sum(r["income"] for r in rows)
                b = sum(r["expense"] for r in rows)
                return {"income": round(a, 4), "expense": round(b, 4),
                        "net": round(a - b, 4)}

            return {
                "days": d,
                "from": daily[0]["day"] if daily else since,
                "to": today,
                "today": _sum([r for r in daily if r["day"] == today]),
                "week": _sum([r for r in daily if r["day"] >= week_from]),
                "total": _sum(daily),
                "daily": daily,
            }
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    @staticmethod
    def _stats_since(days: int) -> str:
        """与 manager 的 _since(days) 同口径：本地时区、含今天。"""
        import datetime as _dt
        tz = _dt.timezone(_dt.timedelta(hours=8))
        d = max(1, min(400, int(days)))
        return (_dt.datetime.now(tz).date() - _dt.timedelta(days=d - 1)).isoformat()

    async def usage_by_model(self, days: int = 30, realm: str = "") -> List[Dict[str, Any]]:
        """照抄 manager 的 /stats/by-model：

        * 排序用 SUM(prompt+completion)，**不是**请求数；
        * 带 cache_hit/miss_tokens；
        * 区间从 `_since(days)` 起（含今天）。
        qtg 原先从 14 天窗口里数请求数排序、且 tokens/credit 恒为 0。

        K26/RV05：http 模式读 Manager，失败向上抛；sqlite 模式走本地实现。
        """
        if self._http_reads_enabled():
            return await self._usage_by_model_from_manager(days, realm)
        return self._usage_by_model_from_sql(days, realm)

    async def _usage_by_model_from_manager(self, days: int,
                                           realm: str) -> List[Dict[str, Any]]:
        params = {"days": days}
        if realm:
            params["realm"] = realm
        rows = await self._stats_via_http("by-model", params)
        for index, row in enumerate(rows):
            _require_fields(row, ("name", "requests", "prompt_tokens",
                                  "completion_tokens", "credit"),
                            f"by-model 第 {index} 项")
        return rows

    def _usage_by_model_from_sql(self, days: int, realm: str) -> List[Dict[str, Any]]:
        conn = self._db()
        if conn is None:
            return []
        try:
            since = self._stats_since(days)
            rf = " AND realm = ?" if realm in ("cn", "global") else ""
            args = (since,) + ((realm,) if rf else ())
            cur = conn.execute(
                "SELECT model AS name, SUM(requests), SUM(prompt_tokens),"
                "       SUM(completion_tokens), COALESCE(SUM(credit),0),"
                "       COALESCE(SUM(cache_hit_tokens),0),"
                "       COALESCE(SUM(cache_miss_tokens),0)"
                "  FROM usage_daily WHERE day >= ?" + rf +
                " GROUP BY model ORDER BY SUM(prompt_tokens + completion_tokens) DESC",
                args,
            )
            return [{
                "name": r[0] or "未知",
                "requests": _i(r[1]),
                "prompt_tokens": _i(r[2]),
                "completion_tokens": _i(r[3]),
                "credit": round(_f(r[4]), 4),
                "cache_hit_tokens": _i(r[5]),
                "cache_miss_tokens": _i(r[6]),
            } for r in cur.fetchall()]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    async def usage_by_key(self, days: int = 30, realm: str = "") -> List[Dict[str, Any]]:
        """照抄 manager 的 /stats/by-key（LEFT JOIN api_keys 取密钥名）。

        K26/RV05：http 模式读 Manager，失败向上抛；sqlite 模式走本地实现。
        """
        if self._http_reads_enabled():
            return await self._usage_by_key_from_manager(days, realm)
        return self._usage_by_key_from_sql(days, realm)

    async def _usage_by_key_from_manager(self, days: int,
                                         realm: str) -> List[Dict[str, Any]]:
        params = {"days": days}
        if realm:
            params["realm"] = realm
        rows = await self._stats_via_http("by-key", params)
        for index, row in enumerate(rows):
            _require_fields(row, ("name", "requests", "prompt_tokens",
                                  "completion_tokens", "credit"),
                            f"by-key 第 {index} 项")
        return rows

    def _usage_by_key_from_sql(self, days: int, realm: str) -> List[Dict[str, Any]]:
        conn = self._db()
        if conn is None:
            return []
        try:
            since = self._stats_since(days)
            rf = " AND u.realm = ?" if realm in ("cn", "global") else ""
            args = (since,) + ((realm,) if rf else ())
            cur = conn.execute(
                "SELECT COALESCE(k.name, u.key_id || '') AS name, SUM(u.requests),"
                "       SUM(u.prompt_tokens), SUM(u.completion_tokens),"
                "       COALESCE(SUM(u.credit),0),"
                "       COALESCE(SUM(u.cache_hit_tokens),0),"
                "       COALESCE(SUM(u.cache_miss_tokens),0)"
                "  FROM usage_daily u LEFT JOIN api_keys k ON k.id = u.key_id"
                " WHERE u.day >= ?" + rf +
                " GROUP BY u.key_id ORDER BY SUM(u.prompt_tokens + u.completion_tokens) DESC",
                args,
            )
            return [{
                "name": r[0] or "未知",
                "requests": _i(r[1]),
                "prompt_tokens": _i(r[2]),
                "completion_tokens": _i(r[3]),
                "credit": round(_f(r[4]), 4),
                "cache_hit_tokens": _i(r[5]),
                "cache_miss_tokens": _i(r[6]),
            } for r in cur.fetchall()]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    async def usage(self, days: int = 14) -> Dict[str, Any]:
        """按天 × 模型聚合 manager.db 的 usage_daily。

        manager.db 里 usage_daily 已经预聚合好了（227 行覆盖几个月），
        比扫 17 万条 request_logs 快得多；错误数单独从 request_logs 数
        status >= 400 的行（只在需要的日期区间内扫）。

        K26 v2（R1-A）：http 模式走 Manager 的 `/api/read-api/usage-matrix`。
        Manager 返回**同一批原始行**（不是二次聚合），两种模式共用 `_build_usage()`
        这段映射，切换时逐位对账一致（见 evidence/r1-k26/parity-sqlite-vs-http.json）。
        失败向上抛，绝不回退 `_db()`。
        """
        days = max(1, min(365, int(days)))
        if self._http_reads_enabled():
            rows, failures = await self._usage_rows_from_manager(days)
            return self._build_usage(days, rows, failures)
        return self._usage_from_sql(days)

    @staticmethod
    def _usage_since(days: int) -> str:
        """与 sqlite 读法同一个窗口口径：本地 +8 时区的「今天 - days」（含当天）。"""
        import datetime as _dt
        tz = _dt.timezone(_dt.timedelta(hours=8))
        return (_dt.datetime.now(tz) - _dt.timedelta(days=days)).date().isoformat()

    def _usage_from_sql(self, days: int) -> Dict[str, Any]:
        days = max(1, min(365, int(days)))
        conn = self._db()
        if conn is None:
            return {"usage": [], "credits_daily": []}
        since = self._usage_since(days)
        try:
            rows = [tuple(r) for r in conn.execute(
                "SELECT day, model, requests, prompt_tokens, completion_tokens,"
                "       credit, realm FROM usage_daily WHERE day >= ?"
                " ORDER BY day",
                (since,),
            ).fetchall()]
            # 错误数：request_logs 里 status 不为 2xx 的（含 status=0 未回状态）
            try:
                failures = {str(d): _i(n) for d, n in conn.execute(
                    "SELECT date(ts, 'unixepoch', '+8 hours') d,"
                    "       SUM(CASE WHEN status >= 400 OR status = 0 THEN 1 ELSE 0 END)"
                    "  FROM request_logs"
                    " WHERE ts >= strftime('%s', ?)"
                    " GROUP BY d",
                    (since + " 00:00:00",),
                ).fetchall() if d is not None}
            except sqlite3.Error:
                failures = {}
            return self._build_usage(days, rows, failures)
        except sqlite3.Error:
            return {"usage": [], "credits_daily": []}
        finally:
            conn.close()

    async def _usage_rows_from_manager(
            self, days: int) -> tuple[List[tuple], Dict[str, int]]:
        """http 通道取原始行 + 日级失败数，形状校验后转成与 sqlite 相同的元组。"""
        await self._ensure_read_capability("usage", min_version=2)
        payload = await manager_api.read_usage_matrix(self.cfg, self._usage_since(days))
        rows: List[tuple] = []
        for index, row in enumerate(payload["rows"]):
            _require_fields(row, ("day", "model", "requests", "prompt_tokens",
                                  "completion_tokens", "credit", "realm"),
                            f"usage-matrix rows 第 {index} 项")
            rows.append((row["day"], row["model"], row["requests"],
                         row["prompt_tokens"], row["completion_tokens"],
                         row["credit"], row["realm"]))
        failures = {str(k): _i(v) for k, v in payload["failures"].items()}
        return rows, failures

    def _build_usage(self, days: int, rows: List[tuple],
                     failures: Dict[str, int]) -> Dict[str, Any]:
        """sqlite / http 共用的映射：把原始行与失败数摊成 QTG 的旧响应形状。"""
        days = max(1, min(365, int(days)))
        by_day: Dict[str, Dict[str, Any]] = {}
        for day, model, req, pt, ct, credit, realm in rows:
            b = by_day.setdefault(str(day), {
                "date": str(day), "requests": 0, "ok": 0, "errors": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
                "credit": 0.0, "models": [], "accounts": [],
                "realm": str(realm or "cn"),
            })
            req_i = _i(req)
            b["requests"] += req_i
            b["ok"] += req_i
            b["prompt_tokens"] += _i(pt)
            b["completion_tokens"] += _i(ct)
            b["credit"] = round(b["credit"] + _f(credit), 4)
            b["models"].append({"name": str(model or ""), "requests": req_i,
                                "ok": req_i, "errors": 0})
        for d, errs in failures.items():
            b = by_day.get(str(d))
            if b:
                b["errors"] = _i(errs)
                b["ok"] = max(0, b["requests"] - b["errors"])
        usage = [by_day[k] for k in sorted(by_day)]
        credits = [
            {"date": u["date"], "total": round(u["credit"], 4),
             "earned": 0.0, "consumed": round(u["credit"], 4)}
            for u in usage
        ]
        return {"usage": usage, "credits_daily": credits}

    async def usage_analytics(self) -> Dict[str, Any]:
        """全表汇总（requests / prompt_tokens / completion_tokens / credits）。

        K26 v2（R1-A）：http 模式走 Manager 的 `/api/read-api/usage-totals`
        （全表、不分时段；`/api/stats/summary` 只有合并后的 total_tokens，
        无法无损表达 analytics）。失败向上抛，绝不回退 `_db()`。
        """
        if not self._http_reads_enabled():
            return self._usage_analytics_from_sql()
        await self._ensure_read_capability("usage", min_version=2)
        totals = await manager_api.read_usage_totals(self.cfg)
        _require_fields(totals, ("requests", "prompt_tokens", "completion_tokens",
                                 "credits"), "usage-totals")
        return self._build_analytics(totals["requests"], totals["prompt_tokens"],
                                     totals["completion_tokens"], totals["credits"])

    @staticmethod
    def _build_analytics(requests: Any, prompt_tokens: Any,
                         completion_tokens: Any, credits: Any) -> Dict[str, Any]:
        """sqlite / http 共用的映射（空表时 SQL 的 SUM 为 NULL，_i/_f 归零）。"""
        return {"summary": {"requests": _i(requests),
                            "prompt_tokens": _i(prompt_tokens),
                            "completion_tokens": _i(completion_tokens),
                            "credits": round(_f(credits), 4)}}

    def _usage_analytics_from_sql(self) -> Dict[str, Any]:
        conn = self._db()
        if conn is None:
            return {}
        try:
            cur = conn.execute(
                "SELECT SUM(requests), SUM(prompt_tokens), SUM(completion_tokens),"
                "       SUM(credit) FROM usage_daily"
            )
            row = cur.fetchone() or (0, 0, 0, 0)
            return self._build_analytics(row[0], row[1], row[2], row[3])
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    # ---------------- 密钥 ----------------
    async def keys(self) -> Dict[str, Any]:
        """manager.db 的 api_keys：只暴露 prefix（前 8 位），key_hash 不出库。

        K26/RV05：http 模式读 Manager 的 /api/keys（同样只含 prefix），失败向上抛；
        sqlite 模式走本地实现。
        """
        if self._http_reads_enabled():
            await self._ensure_read_capability("keys")
            rows = await manager_api.read_api_keys(self.cfg)
            for index, row in enumerate(rows):
                _require_fields(row, ("id", "prefix"), f"密钥列表第 {index} 项")
            return {"data": {"keys": rows}}
        return self._keys_from_sql()

    def _keys_from_sql(self) -> Dict[str, Any]:
        conn = self._db()
        if conn is None:
            return {"data": {"keys": []}}
        try:
            cur = conn.execute(
                "SELECT id, name, prefix, enabled, expires_at, models, quota,"
                "       used_tokens, created_at, last_used_at, realm,"
                "       quota_credit, used_credit FROM api_keys ORDER BY id"
            )
            keys = []
            for (kid, name, prefix, enabled, expires, models, quota,
                 used_tokens, created, last_used, realm, quota_credit, used_credit) in cur.fetchall():
                try:
                    model_list = json.loads(models or "[]")
                except json.JSONDecodeError:
                    model_list = []
                keys.append({
                    "id": _i(kid),
                    "name": str(name or ""),
                    "key": f"{prefix}…",
                    "prefix": str(prefix or ""),
                    "enabled": bool(enabled),
                    "expires_at": _i(expires) or None,
                    "models": model_list,
                    "quota": _i(quota),
                    "used_tokens": _i(used_tokens),
                    "quota_credit": _f(quota_credit),
                    "used_credit": _f(used_credit),
                    "created_at": _i(created),
                    "last_used_at": _i(last_used) or None,
                    "realm": str(realm or ""),
                })
            return {"data": {"keys": keys}}
        except sqlite3.Error:
            return {"data": {"keys": []}}
        finally:
            conn.close()

    # ---------------- 日志 / 监控 ----------------
    async def logs(self, limit: int = 150) -> Dict[str, Any]:
        """请求日志。

        K26/RV05：http 模式读 Manager 的 /api/logs，失败向上抛；sqlite 模式走本地实现。

        分页契约：Manager 该端点的参数是 `page`/`size`（不是 `limit`），所以这里显式
        送 `size`——以前送 `limit` 会被忽略、永远拿默认页大小（R1-A 修正）。
        """
        if self._http_reads_enabled():
            await self._ensure_read_capability("logs")
            data = await manager_api.read_logs(
                self.cfg, {"page": 1, "size": max(1, min(200, int(limit)))})
            if isinstance(data, dict):
                return data
            return {"logs": data, "total": len(data)}
        return self._logs_from_sql(limit)

    def _logs_from_sql(self, limit: int) -> Dict[str, Any]:
        conn = self._db()
        if conn is None:
            return {"data": []}
        try:
            limit = max(1, min(500, int(limit)))
            cur = conn.execute(
                "SELECT ts, model, mapped_model, status, prompt_tokens,"
                "       completion_tokens, latency_ms, first_token_ms, error, ip"
                "  FROM request_logs ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            out = []
            for (ts, model, mapped, status, pt, ct, lat, ft, err, ip) in cur.fetchall():
                out.append({
                    "ts": _i(ts), "model": str(model or ""),
                    "mapped_model": str(mapped or ""), "status": _i(status),
                    "prompt_tokens": _i(pt), "completion_tokens": _i(ct),
                    "latency_ms": _i(lat), "first_token_ms": _i(ft) or None,
                    "error": str(err or "")[:200], "ip": str(ip or ""),
                })
            return {"data": out}
        except sqlite3.Error:
            return {"data": []}
        finally:
            conn.close()

    async def task_logs(self, limit: int = 120) -> Dict[str, Any]:
        """上游自动任务留痕。

        K26/RV05：http 模式读 Manager 的 /api/task-logs，失败向上抛；sqlite 模式走本地实现。
        """
        if self._http_reads_enabled():
            await self._ensure_read_capability("tasks")
            data = await manager_api.read_task_logs(self.cfg,
                                                   {"limit": max(1, int(limit))})
            if isinstance(data, dict):
                return data
            return {"logs": data, "total": len(data)}
        return self._task_logs_from_sql(limit)

    def _task_logs_from_sql(self, limit: int) -> Dict[str, Any]:
        conn = self._db()
        if conn is None:
            return {"data": []}
        try:
            limit = max(1, min(500, int(limit)))
            cur = conn.execute(
                "SELECT * FROM task_logs ORDER BY rowid DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description or []]
            return {"data": [dict(zip(cols, r)) for r in cur.fetchall()]}
        except sqlite3.Error:
            return {"data": []}
        finally:
            conn.close()

    async def audit_logs(self, limit: int = 200) -> Dict[str, Any]:
        """管理端审计日志。

        K26/RV05：http 模式读 Manager 的 /api/audit-logs，失败向上抛；sqlite 模式走本地实现。
        """
        if self._http_reads_enabled():
            await self._ensure_read_capability("audit")
            data = await manager_api.read_audit_logs(self.cfg,
                                                    {"limit": max(1, int(limit))})
            if isinstance(data, dict):
                return data
            return {"logs": data, "total": len(data)}
        return self._audit_logs_from_sql(limit)

    def _audit_logs_from_sql(self, limit: int) -> Dict[str, Any]:
        conn = self._db()
        if conn is None:
            return {"data": []}
        try:
            limit = max(1, min(500, int(limit)))
            cur = conn.execute(
                "SELECT * FROM audit_logs ORDER BY rowid DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description or []]
            return {"data": [dict(zip(cols, r)) for r in cur.fetchall()]}
        except sqlite3.Error:
            return {"data": []}
        finally:
            conn.close()

    # ---------------- 模型 ----------------
    async def models(self) -> Dict[str, Any]:
        """数据面模型表（带 context_length / max_output_tokens / 倍率）。

        K26：http 模式下改读 Manager 的 /api/models（同一份目录、带 realm 过滤），
        失败向上抛；sqlite 模式保持原来的 /v1/models（该方法本来就走 HTTP，不涉及
        直读 DB）。
        """
        if self._http_reads_enabled():
            await self._ensure_read_capability("models")
            data = await manager_api.read_models(self.cfg)
            if isinstance(data, dict):
                return data
            return {"models": data}
        data = await self._json("GET", "/v1/models", required=False)
        return data or {}

    # ---------------- 设置 ----------------
    async def settings(self) -> Dict[str, Any]:
        """面板设置 = workbuddy2api 的 config.json（定时任务窗口等）+ 面板 settings 表。"""
        cfg: Dict[str, Any] = {}
        try:
            cfg = json.loads(Path(self.cfg.wb_config_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
        sched = cfg.get("schedule") or {}
        tasks = [
            {"name": "签到", "key": "checkin", "hours": sched.get("checkin_hours") or [],
             "enabled": bool(sched.get("checkin_enabled"))},
            {"name": "travel", "key": "travel", "hours": sched.get("travel_hours") or [],
             "enabled": bool(sched.get("travel_enabled"))},
            {"name": "活动", "key": "activity", "hours": sched.get("activity_hours") or [],
             "enabled": bool(sched.get("activity_enabled"))},
            {"name": "保活", "key": "keepalive", "hours": sched.get("keepalive_hours") or [],
             "enabled": bool(sched.get("keepalive_enabled"))},
            {"name": "school", "key": "school", "hours": sched.get("school_hours") or [],
             "enabled": bool(sched.get("school_enabled"))},
            {"name": "cat", "key": "cat", "hours": sched.get("cat_hours") or [],
             "enabled": bool(sched.get("cat_enabled"))},
        ]
        # K26：panel 段（settings 表）在 http 模式下改读 Manager；上游 config.json 仍读本地文件
        # （那是 workbuddy2api 容器的配置，不是 Manager 的数据）。
        # RV05：设置面板读不到时**不伪造**——异常向上抛，绝不回退读本地库。
        if self._http_reads_enabled():
            await self._ensure_read_capability("settings")
            panel = await manager_api.read_settings_panel(self.cfg)
            return {'tasks': tasks, 'schedule': sched, 'panel': panel,
                    'source': 'manager-http'}
        return self._settings_from_sql(cfg, sched, tasks)

    def _settings_from_sql(self, cfg: Dict[str, Any], sched: Dict[str, Any],
                           tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        panel: Dict[str, Any] = {}
        conn = self._db()
        if conn is not None:
            try:
                row = conn.execute("SELECT * FROM settings LIMIT 1").fetchone()
                if row is not None:
                    cols = [c[0] for c in conn.execute(
                        "SELECT * FROM settings LIMIT 1").description or []]
                    panel = dict(zip(cols, row))
            except sqlite3.Error:
                panel = {}
            finally:
                conn.close()
        return {
            "scheduler": {"enabled": bool(sched), "mode": "workbuddy2api 内置定时",
                          "tasks": tasks},
            "pool": {"total": len(self._auths())},
            "raw": {"config": cfg, "panel": panel},
        }

    # ---------------- 写操作（本轮明确不支持） ----------------
    async def checkin(self) -> Dict[str, Any]:
        """workbuddy 的签到由 workbuddy2api 自己按 schedule 定时跑。

        面板侧的「手动签到」要调 manager 的 POST /api/accounts/checkin，
        那需要它的登录会话；本轮只做只读聚合，不代它执行写操作。
        """
        return {
            "ok": False,
            "results": [{
                "nickname": "WorkBuddy",
                "ok": False,
                "message": "workbuddy 的签到由 workbuddy2api 按定时任务执行"
                           "（默认 9 点 / 21 点）；qtg 只读合并，不代它触发",
            }],
        }

    async def refresh_account(self, uid: str = "") -> Dict[str, Any]:
        return {"ok": False,
                "error": "workbuddy 的 token 刷新在 workbuddy2api 内部完成，qtg 不接管"}

    async def set_account(self, uid: str, enabled: bool) -> Dict[str, Any]:
        return {"ok": False,
                "error": "启用/禁用 workbuddy 账号请用原面板；qtg 三合一为只读合并"}

    async def add_account(self, name: str, jwt: str) -> Dict[str, Any]:
        return {"ok": False, "error": "新增 workbuddy 账号请在原面板用授权登录"}

    async def save_keys(self, keys: List[Dict[str, Any]],
                        auth_disabled: Optional[bool] = None) -> Dict[str, Any]:
        return {"ok": False, "error": "workbuddy 的 API Key 请在它自己的面板维护"}

    async def oauth_login_url(self) -> Dict[str, Any]:
        return {"ok": False, "error": "workbuddy 授权登录在原面板完成"}

    async def oauth_finish(self, callback_url: str, name: str = "") -> Dict[str, Any]:
        return {"ok": False, "error": "workbuddy 授权登录在原面板完成"}

    async def import_pat(self, pat: str, realm: str = "cn") -> Dict[str, Any]:
        return {"ok": False, "error": "workbuddy 不支持 PAT 导入"}

    async def alerts(self) -> Dict[str, Any]:
        """告警信号：来自 state.json 的冷却/禁用与 manager.db 的高错误率。"""
        items: List[Dict[str, Any]] = []
        state = self._state().get("accounts") or {}
        for uid, st in state.items():
            if not isinstance(st, dict):
                continue
            if st.get("disabled") or st.get("manual_disabled"):
                items.append({"level": "warning", "source": "workbuddy",
                              "title": "账号被禁用", "detail": f"uid={uid[:8]}…"})
            until = str(st.get("until") or "")
            if until and not until.startswith("0001-01-01"):
                items.append({"level": "warning", "source": "workbuddy",
                              "title": "账号冷却中", "detail": f"{uid[:8]}… 直到 {until}"})
            if _i(st.get("consecutive_fails")) >= 3:
                items.append({"level": "error", "source": "workbuddy",
                              "title": "连续失败",
                              "detail": f"{uid[:8]}… 连续 {_i(st.get('consecutive_fails'))} 次"})
        return {"items": items, "total": len(items)}

    async def ack_alerts(self) -> Dict[str, Any]:
        return {"ok": True}

    # ---------------- 健康 ----------------
    async def healthz(self) -> Any:
        try:
            resp = await self.client.get("/healthz", timeout=5.0)
            return {"ok": resp.status_code == 200}
        except Exception:  # noqa: BLE001 - 健康检查不抛
            return {"ok": False}

    async def health(self) -> Dict[str, Any]:
        import time as _t
        start = _t.monotonic()
        up = False
        err = ""
        try:
            resp = await self.client.get("/healthz", timeout=5.0)
            up = resp.status_code == 200
            if not up:
                err = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"[:160]
        return {
            "up": up,
            "latency_ms": int((_t.monotonic() - start) * 1000),
            "status_code": None,
            "error": err,
            # 面板端口单独标注：它挂了不影响数据面
            "panel_url": self.cfg.wb_panel_base,
        }
