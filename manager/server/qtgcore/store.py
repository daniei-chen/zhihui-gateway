"""数据面持久化：qtg 自己的 API Key、代理请求流水、按密钥用量。

为什么需要独立 store：控制面的 key 是「管理令牌」，数据面的 key 是「用户调用凭证」，
两者生命周期、审计、限流都不同，混用会让权限边界模糊（WorkBuddy 也是分开的）。

用标准库 sqlite3：零依赖、WAL 并发够用、单文件易备份。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

# qtg 进程没有 TZ 环境变量（systemd unit 里只有 PYTHONPATH 等），
# 所以凡是要跟「前端看到的今天」对齐的日界，都显式用 +08:00。
_CST = dt.timezone(dt.timedelta(hours=8))

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS qtg_keys (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    key_hash      TEXT NOT NULL UNIQUE,
    key_prefix    TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    platforms     TEXT NOT NULL DEFAULT 'qoder,trae,workbuddy',
    models        TEXT NOT NULL DEFAULT '',
    daily_limit   INTEGER NOT NULL DEFAULT 0,
    monthly_limit INTEGER NOT NULL DEFAULT 0,
    max_concurrency INTEGER NOT NULL DEFAULT 0,
    ip_allowlist  TEXT NOT NULL DEFAULT '',
    max_ips       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_keys_hash ON qtg_keys(key_hash);
CREATE TABLE IF NOT EXISTS key_ips (
    key_id     TEXT NOT NULL,
    ip         TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (key_id, ip)
);

CREATE TABLE IF NOT EXISTS proxy_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    epoch        REAL NOT NULL,
    key_id       TEXT NOT NULL DEFAULT '',
    key_name     TEXT NOT NULL DEFAULT '',
    platform     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    status       INTEGER NOT NULL DEFAULT 0,
    stream       INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens     INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    ttft_ms      INTEGER NOT NULL DEFAULT 0,
    route_reason TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_req_epoch ON proxy_requests(epoch);
CREATE INDEX IF NOT EXISTS idx_req_key ON proxy_requests(key_id, epoch);
"""

_KEY_PREFIX = "sk-qtg-"


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def hash_key(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


@dataclass
class KeyRow:
    id: str
    name: str
    key_prefix: str
    enabled: bool
    platforms: List[str]
    models: List[str]
    daily_limit: int
    created_at: str
    last_used_at: str
    note: str = ""

    monthly_limit: int = 0
    max_concurrency: int = 0
    ip_allowlist: List[str] = field(default_factory=list)
    max_ips: int = 0
    # 过期时间（unix 秒；0 = 永不过期）。以前前端填了会被静默丢弃（审计 P1）。
    expires_at: int = 0

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "masked": f"{self.key_prefix}****{self.id[:4]}",
            "enabled": self.enabled,
            "platforms": self.platforms,
            "models": self.models,
            "daily_limit": self.daily_limit,
            "monthly_limit": self.monthly_limit,
            "max_concurrency": self.max_concurrency,
            "ip_allowlist": self.ip_allowlist,
            "max_ips": self.max_ips,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "note": self.note,
            "expires_at": self.expires_at or None,
        }


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            # sqlite3.Connection.__exit__ commits/rolls back but does not close.
            # Keep the transaction semantics and always release the descriptor.
            with conn:
                yield conn
        finally:
            conn.close()

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            # 轻量迁移：老库补齐新列
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(qtg_keys)")}
            for name, ddl in (
                ("monthly_limit", "ALTER TABLE qtg_keys ADD COLUMN monthly_limit INTEGER NOT NULL DEFAULT 0"),
                ("max_concurrency", "ALTER TABLE qtg_keys ADD COLUMN max_concurrency INTEGER NOT NULL DEFAULT 0"),
                ("ip_allowlist", "ALTER TABLE qtg_keys ADD COLUMN ip_allowlist TEXT NOT NULL DEFAULT ''"),
                ("max_ips", "ALTER TABLE qtg_keys ADD COLUMN max_ips INTEGER NOT NULL DEFAULT 0"),
                ("expires_at", "ALTER TABLE qtg_keys ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in cols:
                    conn.execute(ddl)

    # ---------------- 密钥 ----------------
    def create_key(
        self,
        name: str,
        *,
        platforms: Optional[List[str]] = None,
        models: Optional[List[str]] = None,
        daily_limit: int = 0,
        monthly_limit: int = 0,
        max_concurrency: int = 0,
        note: str = "",
        ip_allowlist: Optional[List[str]] = None,
        max_ips: Optional[int] = None,
        expires_at: int = 0,
    ) -> Dict[str, Any]:
        plain = _KEY_PREFIX + secrets.token_hex(20)
        kid = secrets.token_hex(8)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO qtg_keys(id,name,key_hash,key_prefix,enabled,platforms,models,daily_limit,"
                "monthly_limit,max_concurrency,ip_allowlist,max_ips,expires_at,created_at,note)"
                " VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)",
                (
                    kid, name.strip()[:64] or "未命名", hash_key(plain), plain[:12],
                    ",".join(platforms or ["qoder", "trae", "workbuddy"]),
                    ",".join(models or []), int(daily_limit or 0),
                    int(monthly_limit or 0), int(max_concurrency or 0),
                    ",".join(ip_allowlist or []), int(max_ips or 0), int(expires_at or 0),
                    now_iso(), note[:200],
                ),
            )
        return {"id": kid, "key": plain, "name": name}

    def verify_key(self, plain: str) -> Optional[KeyRow]:
        if not plain or not plain.startswith(_KEY_PREFIX):
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM qtg_keys WHERE key_hash=? AND enabled=1", (hash_key(plain),)
            ).fetchone()
        return self._row_to_key(row) if row else None

    def touch_key(self, kid: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE qtg_keys SET last_used_at=? WHERE id=?", (now_iso(), kid))

    def list_keys(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM qtg_keys ORDER BY created_at DESC").fetchall()
        return [self._row_to_key(r).public() for r in rows]

    def set_key_enabled(self, kid: str, enabled: bool) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE qtg_keys SET enabled=? WHERE id=?", (1 if enabled else 0, kid))
            return cur.rowcount == 1

    def delete_key(self, kid: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM qtg_keys WHERE id=?", (kid,))
            return cur.rowcount == 1


    def update_key(
        self,
        kid: str,
        *,
        name: Optional[str] = None,
        models: Optional[List[str]] = None,
        daily_limit: Optional[int] = None,
        monthly_limit: Optional[int] = None,
        max_concurrency: Optional[int] = None,
        ip_allowlist: Optional[List[str]] = None,
        max_ips: Optional[int] = None,
    ) -> bool:
        """更新数据面密钥的可变字段（PATCH /api/keys/{id} 用）。"""
        sets: List[str] = []
        args: List[Any] = []
        if name is not None:
            sets.append("name=?")
            args.append(name.strip()[:64] or "未命名")
        if models is not None:
            sets.append("models=?")
            args.append(",".join(models))
        if daily_limit is not None:
            sets.append("daily_limit=?")
            args.append(int(daily_limit))
        if monthly_limit is not None:
            sets.append("monthly_limit=?")
            args.append(int(monthly_limit))
        if max_concurrency is not None:
            sets.append("max_concurrency=?")
            args.append(int(max_concurrency))
        if max_ips is not None:
            sets.append("max_ips=?")
            args.append(int(max_ips))
        if ip_allowlist is not None:
            sets.append("ip_allowlist=?")
            args.append(",".join(ip_allowlist))
        if not sets:
            return True
        args.append(kid)
        with self._conn() as conn:
            cur = conn.execute(f"UPDATE qtg_keys SET {', '.join(sets)} WHERE id=?", args)
            return cur.rowcount == 1
    def register_key_ip(self, kid: str, ip: str, max_ips: int = 0) -> bool:
        """登记密钥来源 IP；返回 False = 新 IP 超过 max_ips 上限。

        只统计最近 24h 出现过的 IP：一个密钥换了网络（或手机热点）不该永久占位。
        """
        now = time.time()
        since = now - 86400
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM key_ips WHERE key_id=? AND ip=? AND last_seen>=?",
                (kid, ip, since),
            ).fetchone()
            if row:
                conn.execute("UPDATE key_ips SET last_seen=? WHERE key_id=? AND ip=?",
                             (now, kid, ip))
                return True
            if max_ips:
                count = conn.execute(
                    "SELECT COUNT(*) c FROM key_ips WHERE key_id=? AND last_seen>=?",
                    (kid, since),
                ).fetchone()["c"]
                if int(count) >= int(max_ips):
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO key_ips(key_id, ip, first_seen, last_seen) VALUES(?,?,?,?)",
                (kid, ip, now, now),
            )
            return True

    @staticmethod
    def _row_to_key(row: sqlite3.Row) -> KeyRow:
        return KeyRow(
            id=row["id"], name=row["name"], key_prefix=row["key_prefix"],
            enabled=bool(row["enabled"]),
            platforms=[p for p in (row["platforms"] or "").split(",") if p],
            models=[m for m in (row["models"] or "").split(",") if m],
            daily_limit=int(row["daily_limit"] or 0),
            monthly_limit=int(row["monthly_limit"] or 0),
            max_concurrency=int(row["max_concurrency"] or 0),
            ip_allowlist=[x for x in (row["ip_allowlist"] or "").split(",") if x],
            max_ips=int(row["max_ips"] or 0),
            created_at=row["created_at"], last_used_at=row["last_used_at"],
            note=row["note"],
            expires_at=int(row["expires_at"] or 0) if "expires_at" in row.keys() else 0,
        )

    # ---------------- 请求流水 ----------------
    def record_request(
        self,
        *,
        key: Optional[KeyRow],
        platform: str,
        model: str,
        status: int,
        stream: bool = False,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        duration_ms: int = 0,
        ttft_ms: int = 0,
        route_reason: str = "",
        error: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO proxy_requests(ts,epoch,key_id,key_name,platform,model,status,stream,"
                "prompt_tokens,completion_tokens,cached_tokens,duration_ms,ttft_ms,route_reason,error)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    now_iso(), time.time(), key.id if key else "", key.name if key else "",
                    platform, model[:120], int(status), 1 if stream else 0,
                    int(prompt_tokens), int(completion_tokens), int(cached_tokens),
                    int(duration_ms), int(ttft_ms), route_reason[:80], error[:300],
                ),
            )

    def list_requests(self, limit: int = 200, *, platform: str = "", key_id: str = "") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM proxy_requests WHERE 1=1"
        args: List[Any] = []
        if platform:
            sql += " AND platform=?"
            args.append(platform)
        if key_id:
            sql += " AND key_id=?"
            args.append(key_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(1000, int(limit))))
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def stats(self, days: int = 1, platform: str = "") -> Dict[str, Any]:
        """本机代理流水的聚合。`platform` 非空时只统计该平台（平台隔离要求）。"""
        since = time.time() - max(1, days) * 86400
        where = "WHERE epoch>=?" + (" AND platform=?" if platform else "")
        args: List[Any] = [since] + ([platform] if platform else [])
        with self._conn() as conn:
            total = conn.execute(
                "SELECT count(*) c, sum(prompt_tokens+completion_tokens) t, sum(status>=400) e"
                f" FROM proxy_requests {where}", args
            ).fetchone()
            by_platform = conn.execute(
                "SELECT platform, count(*) c, sum(prompt_tokens+completion_tokens) t"
                f" FROM proxy_requests {where} GROUP BY platform", args
            ).fetchall()
            by_key = conn.execute(
                "SELECT key_name, count(*) c, sum(prompt_tokens+completion_tokens) t"
                f" FROM proxy_requests {where} GROUP BY key_name ORDER BY c DESC LIMIT 20", args
            ).fetchall()
            by_model = conn.execute(
                "SELECT model, count(*) c, sum(prompt_tokens+completion_tokens) t, avg(duration_ms) d"
                f" FROM proxy_requests {where} GROUP BY model ORDER BY c DESC LIMIT 20", args
            ).fetchall()
        return {
            "window_days": days,
            "requests": int(total["c"] or 0),
            "tokens": int(total["t"] or 0),
            "errors": int(total["e"] or 0),
            "by_platform": [dict(r) for r in by_platform],
            "by_key": [dict(r) for r in by_key],
            "by_model": [dict(r) for r in by_model],
        }

    def hourly(self, day: str = "", platform: str = "") -> List[Dict[str, Any]]:
        """按小时分桶的真实用量（数据源是本机 `proxy_requests` 的 epoch）。

        为什么用本机流水：上游（qoder2 / aiwork）都只给按天聚合，没有小时桶。
        旧实现直接造 24 个 0 桶，界面于是「当日 11 次请求、图上全是 0」——
        自相矛盾。这里用 qtg 自己记的真实时间戳，至少能反映**经过 qtg 的流量**。

        时区：qtg 进程没有设置 TZ（systemd unit 里只有 PYTHONPATH 等），所以
        `time.mktime`/`strftime` 会按 **UTC** 算日界，和前端（service.py 固定 +8）
        差 8 小时。这里统一用固定 +08:00。
        """
        if not day:
            day = (dt.datetime.now(_CST)).strftime("%Y-%m-%d")
        try:
            start = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_CST).timestamp()
        except ValueError:
            start = dt.datetime.now(_CST).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
        end = start + 86400
        sql = (
            "SELECT epoch, prompt_tokens, completion_tokens, status FROM proxy_requests"
            " WHERE epoch>=? AND epoch<?"
        )
        params: List[Any] = [start, end]
        if platform:
            sql += " AND platform=?"
            params.append(platform)
        buckets = {h: {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                       "failed": 0} for h in range(24)}
        try:
            with self._conn() as conn:
                for row in conn.execute(sql, params).fetchall():
                    h = int((float(row["epoch"] or 0) - start) // 3600)
                    if not (0 <= h <= 23):
                        continue
                    b = buckets[h]
                    b["requests"] += 1
                    b["prompt_tokens"] += int(row["prompt_tokens"] or 0)
                    b["completion_tokens"] += int(row["completion_tokens"] or 0)
                    if int(row["status"] or 0) >= 400:
                        b["failed"] += 1
        except sqlite3.Error:
            pass
        return [
            {"day": day, "hour": h, **buckets[h],
             "total_tokens": buckets[h]["prompt_tokens"] + buckets[h]["completion_tokens"],
             "credit": 0}
            for h in range(24)
        ]

    def key_usage_today(self, key_id: str) -> int:
        """当日该 key 已用 token（用于 daily_limit 判断）。"""
        day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        with self._conn() as conn:
            row = conn.execute(
                "SELECT coalesce(sum(prompt_tokens+completion_tokens),0) t FROM proxy_requests"
                " WHERE key_id=? AND epoch>=?", (key_id, day_start)
            ).fetchone()
        return int(row["t"] or 0)

    def key_usage_month(self, key_id: str) -> int:
        """当月该 key 已用 token。"""
        import datetime as _dt
        now = _dt.datetime.now()
        start = _dt.datetime(now.year, now.month, 1).timestamp()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT coalesce(sum(prompt_tokens+completion_tokens),0) t FROM proxy_requests"
                " WHERE key_id=? AND epoch>=?", (key_id, start)
            ).fetchone()
        return int(row["t"] or 0)

    def prune(self, keep_days: int = 30) -> int:
        cutoff = time.time() - keep_days * 86400
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM proxy_requests WHERE epoch<?", (cutoff,))
        self._init()
        return cur.rowcount
