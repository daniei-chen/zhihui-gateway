"""服务层：把两个上游适配器的数据收敛成统一领域模型。

这一层是本次升级的核心价值点之一。旧 preview-agent 是「每个端点各写一遍
聚合」，导致同一份上游数据在一次请求里被重复拉 5 次、且各端点的字段口径
不一致（例：同一个账号在 /api/accounts 和 /api/overview 里 name 可能不同）。

这里改成：**一个聚合构建器 + 一份缓存**，所有端点和它共享同一份归一化结果。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import glob
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import metrics
from .cache import cache
from .config import Settings
from .errors import UpstreamError
from .models import (
    Account,
    AccountSummary,
    AccountsResponse,
    Alert,
    AlertsResponse,
    ActionResult,
    AuditResponse,
    BackupEntry,
    HealthResponse,
    HealthStatus,
    KeyInfo,
    KeysResponse,
    LogEntry,
    LogsResponse,
    ModelInfo,
    ModelsResponse,
    OpsResponse,
    OverviewResponse,
    RequestRow,
    RequestsResponse,
    SchedulerInfo,
    SettingsResponse,
    StatCard,
    TaskInfo,
    TasksResponse,
    UsageByModel,
    UsagePoint,
    UsageResponse,
)
from .security import CredentialStore

TZ = dt.timezone(dt.timedelta(hours=8))
LOW_CREDIT_THRESHOLD = 100.0
EXPIRING_SOON_DAYS = 3
# 上游用 9999-12-31 表示「永久额度」，不能按真实到期日展示
_FOREVER = 253402214400


# ---------------------------------------------------------------- 工具
def now_iso() -> str:
    return dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _iso(epoch: Any) -> Optional[str]:
    try:
        ts = float(epoch)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or ts >= _FOREVER:
        return None
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S")


def _days_left(epoch: Any) -> Optional[int]:
    try:
        ts = float(epoch)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or ts >= _FOREVER:
        return None
    return int((ts - time.time()) // 86400)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _month_day_to_date(label: str, today: Optional[dt.date] = None) -> Optional[str]:
    """'10/9' → '2026-10-09'（按当前年份，跨年时回退一年）。"""
    try:
        m, d = str(label).split("/")
        today = today or dt.datetime.now(TZ).date()
        cand = dt.date(today.year, int(m), int(d))
        if cand > today + dt.timedelta(days=1):
            cand = dt.date(today.year - 1, int(m), int(d))
        return cand.isoformat()
    except Exception:
        return None


async def _safe(coro, default=None, errors: Optional[List[str]] = None, label: str = ""):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - 聚合场景下单点失败不应拖垮整页
        if errors is not None:
            errors.append(f"{label or 'upstream'}: {str(exc)[:200]}")
        return default


# ---------------------------------------------------------------- 映射
def map_qoder_account(raw: Dict[str, Any]) -> Account:
    credits = raw.get("credits") or {}
    remain = _f(credits.get("remain"))
    total = _f(credits.get("size"))
    used = _f(credits.get("used"))
    expires_at = raw.get("expiresAt") or credits.get("expires_at")
    dleft = _days_left(expires_at)
    cooldown = bool(raw.get("inCooldown"))
    enabled = bool(raw.get("enabled", True))
    expired = bool(expires_at) and float(expires_at) <= time.time()  # 秒级判断，不用整数天
    if not enabled:
        state = "disabled"
    elif cooldown:
        state = "cooling"
    elif expired:
        state = "expired"
    elif remain < LOW_CREDIT_THRESHOLD:
        state = "low"
    else:
        state = "ready"
    return Account(
        platform="qoder",
        uid=str(raw.get("uid") or ""),
        name=str(raw.get("nickname") or raw.get("uid") or "")[:64],
        realm=str(raw.get("realm") or ""),
        enabled=enabled,
        state=state,  # type: ignore[arg-type]
        credits_remain=round(remain, 4),
        credits_total=round(total, 4),
        credits_used=round(used, 4),
        expires_at=_i(expires_at) or None,
        expires_iso=_iso(expires_at),
        days_left=dleft,
        checked_today=bool(raw.get("canCheckin") is False or raw.get("lastCheckin")),
        last_checkin=str(raw.get("lastCheckin") or "") or None,
        plan=str(raw.get("plan") or ""),
        in_flight=_i(raw.get("inFlight")) if raw.get("inFlight") is not None else None,
        in_flight_cap=_i(raw.get("inFlightCap")) if raw.get("inFlightCap") is not None else None,
        in_cooldown=cooldown,
        errors=_i(raw.get("softStreak")),
        note=str(raw.get("lastError") or "")[:160],
    )


def map_wb_account(raw: Dict[str, Any]) -> Account:
    """workbuddy 账号（`auths/*.json` 与 `state.json` 合并后的一行）。

    两边字段互补：凭证文件给 uid/nickname/expiresAt/realm，
    state.json 给 credits/disabled/until/success_count/err_total。
    """
    now = time.time()
    expires = _i(raw.get("expires_at"))
    credits = _f(raw.get("credits"))
    until = str(raw.get("until") or "")
    cooling = bool(until) and not until.startswith("0001-01-01")
    disabled = bool(raw.get("disabled")) or bool(raw.get("manual_disabled"))
    expires_ts = expires if expires > 0 else None
    dleft = _days_left(expires_ts)
    expired = bool(expires_ts) and float(expires_ts) <= now
    known = bool(raw.get("credits_known", True))
    if disabled:
        state = "disabled"
    elif cooling:
        state = "cooling"
    elif expired:
        state = "expired"
    elif known and credits < LOW_CREDIT_THRESHOLD:
        state = "low"
    else:
        state = "ready"

    def _iso_of(ts: Any) -> Optional[str]:
        try:
            v = float(ts)
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(v))

    return Account(
        platform="workbuddy",
        uid=str(raw.get("uid") or ""),
        name=str(raw.get("nickname") or raw.get("uid") or "")[:64],
        realm=str(raw.get("realm") or "cn").lower(),
        enabled=not disabled,
        state=state,  # type: ignore[arg-type]
        credits_remain=round(credits, 4),
        credits_total=round(credits + _f(raw.get("credits_expiring")), 4),
        credits_used=0.0,
        expires_at=expires_ts,
        expires_iso=_iso_of(expires_ts),
        days_left=dleft,
        checked_today=bool(raw.get("checked_today")),
        last_checkin=_iso_of(raw.get("last_checkin")),
        in_cooldown=cooling,
        cooldown_until=until if cooling else None,
        errors=_i(raw.get("err_total")),
        note=str(raw.get("last_error") or "")[:160],
    )


def map_trae_account(raw: Dict[str, Any]) -> Account:
    remain = _f(raw.get("remaining_credits"))
    if remain == 0.0 and raw.get("general_credits") is not None:
        remain = _f(raw.get("general_credits"))
    total = _f(raw.get("total_credits")) or remain
    cooldown_until = raw.get("cooldown_until")
    expires_at = raw.get("credits_expire_at")
    dleft = _days_left(expires_at)
    enabled = not bool(raw.get("disabled"))
    cooling = bool(cooldown_until) and str(cooldown_until) not in ("", "None")
    expired = bool(expires_at) and float(expires_at) <= time.time()  # 秒级判断
    if not enabled:
        state = "disabled"
    elif cooling:
        state = "cooling"
    elif expired:
        state = "expired"
    elif remain < LOW_CREDIT_THRESHOLD:
        state = "low"
    else:
        state = "ready"
    return Account(
        platform="trae",
        uid=str(raw.get("user_id") or raw.get("uid") or ""),
        name=str(raw.get("name") or raw.get("user_id") or "")[:64],
        realm=str(raw.get("realm") or "cn").lower(),
        enabled=enabled,
        state=state,  # type: ignore[arg-type]
        credits_remain=round(remain, 4),
        credits_total=round(total, 4),
        credits_used=round(max(0.0, total - remain), 4),
        expires_at=_i(expires_at) or None,
        expires_iso=_iso(expires_at),
        days_left=dleft,
        checked_today=bool(raw.get("checked_today")),
        last_checkin=str(raw.get("auth_saved_at") or "") or None,
        group=str(raw.get("group_id") or "") or None,
        in_cooldown=cooling,
        cooldown_until=str(cooldown_until or "") or None,
        errors=_i(raw.get("refresh_token_fails")),
        note=str(raw.get("cooldown_reason") or "")[:160],
    )


def summarize(accounts: Iterable[Account]) -> AccountSummary:
    rows = list(accounts)
    s = AccountSummary(total=len(rows))
    s.credits = round(sum(a.credits_remain for a in rows), 2)
    for a in rows:
        if a.state == "ready":
            s.ready += 1
        elif a.state == "cooling":
            s.cooling += 1
        elif a.state == "disabled":
            s.disabled += 1
        elif a.state == "low":
            s.low += 1
        if a.days_left is not None and 0 <= a.days_left <= EXPIRING_SOON_DAYS:
            s.expiring_soon += 1
    return s


# ---------------------------------------------------------------- 聚合器
class Aggregate:
    """一次请求内共享的归一化快照。"""

    def __init__(self) -> None:
        self.qoder_accounts: List[Account] = []
        self.trae_accounts: List[Account] = []
        self.qoder_raw: Dict[str, Any] = {}
        self.trae_raw: Dict[str, Any] = {}
        self.qoder_usage: Dict[str, Any] = {}
        self.qoder_analytics: Dict[str, Any] = {}
        self.qoder_flow: Dict[str, Any] = {}
        self.qoder_scheduler: Dict[str, Any] = {}
        self.trae_usage: Dict[str, Any] = {}
        self.trae_settings: Dict[str, Any] = {}
        # workbuddy（三合一第三平台，只读）
        self.wb_accounts: List[Account] = []
        self.wb_raw: Dict[str, Any] = {}
        self.wb_usage: Dict[str, Any] = {}
        self.wb_settings: Dict[str, Any] = {}
        self.health: List[HealthStatus] = []
        self.errors: List[str] = []


async def build_aggregate(rt: Any, *, force: bool = False) -> Aggregate:
    """并行拉取两平台的核心数据；单点失败只记录不抛出。"""
    ttl = rt.settings.cache_overview_sec

    async def _build() -> Aggregate:
        agg = Aggregate()
        q, t = rt.qoder, rt.trae

        days = 14
        errs = agg.errors
        results = await asyncio.gather(
            _safe(q.accounts(), {}, errs, "qoder.accounts"),
            _safe(q.usage(days), {}, errs, "qoder.usage"),
            _safe(q.usage_analytics(), {}, errs, "qoder.analytics"),
            _safe(q.credits_flow(7), {}, errs, "qoder.credits"),
            _safe(q.scheduler(), {}, errs, "qoder.scheduler"),
            _safe(t.overview(), {}, errs, "trae.overview"),
            _safe(t.usage(days), {}, errs, "trae.usage"),
            _safe(t.settings(), {}, errs, "trae.settings"),
            _safe(q.health(), {}, None, ""),
            _safe(t.health(), {}, None, ""),
            return_exceptions=False,
        )
        (
            agg.qoder_raw,
            agg.qoder_usage,
            agg.qoder_analytics,
            agg.qoder_flow,
            agg.qoder_scheduler,
            agg.trae_raw,
            agg.trae_usage,
            agg.trae_settings,
            qh,
            th,
        ) = results

        for raw in (agg.qoder_raw.get("accounts") or []):
            agg.qoder_accounts.append(map_qoder_account(raw))
        for raw in (agg.trae_raw.get("accounts") or []):
            agg.trae_accounts.append(map_trae_account(raw))

        # workbuddy：只读聚合，engine=off 时整段跳过（w 为 None）
        w = getattr(rt, "wb", None)
        wh: Dict[str, Any] = {}
        if w is not None:
            agg.wb_raw = await _safe(w.overview(), {}, errs, "wb.overview")
            agg.wb_usage = await _safe(w.usage(days), {}, errs, "wb.usage")
            agg.wb_settings = await _safe(w.settings(), {}, errs, "wb.settings")
            wh = await _safe(w.health(), {}, None, "")
            for raw in (agg.wb_raw.get("accounts") or []):
                agg.wb_accounts.append(map_wb_account(raw))

        # 两个上游都不可达时明确失败，绝不返回「全 0 的健康面板」
        q_up = bool((qh or {}).get("up"))
        t_up = bool((th or {}).get("up"))
        if not q_up and not t_up:
            detail = "; ".join(agg.errors[:3]) or "两个上游 BFF 均不可达"
            raise UpstreamError(f"上游控制面不可用：{detail}")

        # 健康条目的名字与地址跟随**实际在用的适配器**：qoder 数据面可能是
        # 旧 Go BFF（qoder-bff / :8791），也可能是 qoder2 引擎（:8799），
        # 写死会让「设置 → 服务信息」显示成没在用的那个。
        q_name = str(getattr(rt.qoder, "name", "qoder"))
        t_name = str(getattr(rt.trae, "name", "trae"))
        q_url = str(getattr(rt.qoder, "base_url", rt.settings.qoder_base))
        t_url = str(getattr(rt.trae, "base_url", rt.settings.trae_base))
        agg.health = [
            HealthStatus(
                name=q_name,
                url=q_url,
                up=bool((qh or {}).get("up")),
                latency_ms=(qh or {}).get("latency_ms"),
                error=str((qh or {}).get("last_error") or "")[:200],
            ),
            HealthStatus(
                name=t_name,
                url=t_url,
                up=bool((th or {}).get("up")),
                latency_ms=(th or {}).get("latency_ms"),
                error=str((th or {}).get("last_error") or "")[:200],
            ),
        ]
        if w is not None:
            agg.health.append(HealthStatus(
                name=str(getattr(w, "name", "workbuddy")),
                url=str(getattr(w, "base_url", rt.settings.wb_api_base)),
                up=bool(wh.get("up")),
                latency_ms=wh.get("latency_ms"),
                error=str(wh.get("error") or "")[:200],
            ))
        _apply_pool_flags(agg)
        return agg

    try:
        return await cache.get_or_set("aggregate", ttl, _build, force=force)
    except UpstreamError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(f"聚合上游数据失败: {str(exc)[:200]}") from exc


def _apply_pool_flags(agg: Aggregate) -> None:
    """把上游真实的「在池」状态写到账号上。

    * trae：aiwork 的 `pool_list.enabled_uids` 才是权威——池空的时候
      数据面会直接 503，界面不该显示「在池」。
    * workbuddy：workbuddy2api 自己按 state.json 的 disabled 调度，
      没有单独的池列表，`in_pool` 留 None。
    * qoder：qoder2 的池是它的内部概念，qtg 只拿到账号列表，留 None。
    """
    pool = (agg.trae_settings.get("pool") or {}) if agg.trae_settings else {}
    enabled = pool.get("enabled_uids")
    if isinstance(enabled, list):
        ids = {str(x) for x in enabled}
        for a in agg.trae_accounts:
            a.in_pool = a.uid in ids


def all_accounts(agg: Aggregate) -> List[Account]:
    """三平台账号合集。所有「全量」语义的调用点都走这里，
    避免每加一个平台都要全局搜 `qoder + trae`。"""
    return list(agg.qoder_accounts) + list(agg.trae_accounts) + list(agg.wb_accounts)


def accounts_payload(agg: Aggregate) -> AccountsResponse:
    return AccountsResponse(
        generated_at=now_iso(),
        qoder=agg.qoder_accounts,
        trae=agg.trae_accounts,
        workbuddy=agg.wb_accounts,
        summary={
            "qoder": summarize(agg.qoder_accounts),
            "trae": summarize(agg.trae_accounts),
            "workbuddy": summarize(agg.wb_accounts),
            "all": summarize(agg.qoder_accounts + agg.trae_accounts + agg.wb_accounts),
        },
    )


def usage_payload(agg: Aggregate, days: int = 14) -> UsageResponse:
    q_points: List[UsagePoint] = []
    for b in agg.qoder_usage.get("buckets") or []:
        date = _month_day_to_date(str(b.get("label") or ""))
        if not date:
            continue
        tokens = _i(b.get("tokens"))
        # qoder2 引擎的 buckets 带 prompt/completion 拆分（旧 BFF 只有合计）。
        # 有拆分就用拆分，没有才退回「总量全部算 completion」——后者会让
        # 「输入/输出占比」永远显示 0:100，是审计点名的失真项。
        prompt = _i(b.get("prompt_tokens"))
        completion = _i(b.get("completion_tokens"))
        if prompt or completion:
            total = prompt + completion
        else:
            total = tokens
            completion = tokens
        q_points.append(
            UsagePoint(
                date=date,
                requests=_i(b.get("requests")),
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                errors=_i(b.get("errors")),
            )
        )
    t_points: List[UsagePoint] = []
    for u in agg.trae_usage.get("usage") or []:
        prompt = _i(u.get("prompt_tokens"))
        completion = _i(u.get("completion_tokens"))
        t_points.append(
            UsagePoint(
                date=str(u.get("date") or ""),
                requests=_i(u.get("requests") or u.get("ok")),
                ok=_i(u.get("ok")),
                errors=_i(u.get("errors")),
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
            )
        )

    w_points: List[UsagePoint] = []
    w_model_map: Dict[str, Dict[str, float]] = {}
    for u in agg.wb_usage.get("usage") or []:
        prompt = _i(u.get("prompt_tokens"))
        completion = _i(u.get("completion_tokens"))
        w_points.append(
            UsagePoint(
                date=str(u.get("date") or ""),
                requests=_i(u.get("requests")),
                ok=_i(u.get("ok")),
                errors=_i(u.get("errors")),
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
            )
        )
        for m in u.get("models") or []:
            name = str(m.get("name") or "")
            b = w_model_map.setdefault(name, {"requests": 0.0, "errors": 0.0})
            b["requests"] += _i(m.get("requests"))
            b["errors"] += _i(m.get("errors"))

    by_model: List[UsageByModel] = []
    for m in agg.qoder_usage.get("by_model") or []:
        # qoder2 的 by_model 带 prompt/completion 拆分（旧 BFF 只有合计）
        pt = _i(m.get("prompt_tokens"))
        ct = _i(m.get("completion_tokens"))
        by_model.append(
            UsageByModel(
                platform="qoder",
                model=str(m.get("model") or ""),
                requests=_i(m.get("requests")),
                tokens=_i(m.get("tokens")) or (pt + ct),
                credit=round(_f(m.get("credit")), 4),
                prompt_tokens=pt,
                completion_tokens=ct,
            )
        )
    # Trae 的 by-model 藏在 usage[].models 里，按天展开后聚合
    trae_models: Dict[str, Dict[str, float]] = {}
    for u in agg.trae_usage.get("usage") or []:
        for m in u.get("models") or []:
            name = str(m.get("name") or "")
            bucket = trae_models.setdefault(name, {"requests": 0, "errors": 0})
            bucket["requests"] += _i(m.get("requests") or m.get("ok"))
            bucket["errors"] += _i(m.get("errors"))
    for name, bucket in sorted(trae_models.items(), key=lambda kv: -kv[1]["requests"]):
        by_model.append(
            UsageByModel(
                platform="trae",
                model=name,
                requests=int(bucket["requests"]),
            )
        )
    for name, bucket in sorted(w_model_map.items(), key=lambda kv: -kv[1]["requests"]):
        by_model.append(
            UsageByModel(
                platform="workbuddy",
                model=name,
                requests=int(bucket["requests"]),
            )
        )

    credits: List[Dict[str, Any]] = []
    for d in agg.qoder_flow.get("daily") or []:
        credits.append(
            {
                "platform": "qoder",
                "date": str(d.get("day") or ""),
                "income": round(_f(d.get("income")), 4),
                "expense": round(_f(d.get("expense")), 4),
            }
        )
    for d in agg.trae_usage.get("credits_daily") or []:
        credits.append(
            {
                "platform": "trae",
                "date": str(d.get("date") or ""),
                "income": round(_f(d.get("earned")), 4),
                "expense": round(_f(d.get("consumed")), 4),
            }
        )
    for d in agg.wb_usage.get("credits_daily") or []:
        credits.append(
            {
                "platform": "workbuddy",
                "date": str(d.get("date") or ""),
                "income": round(_f(d.get("earned")), 4),
                "expense": round(_f(d.get("consumed")), 4),
            }
        )
    credits.sort(key=lambda r: r["date"])

    today = str(dt.datetime.now(TZ).date())
    today_row = (agg.qoder_analytics.get("summary") or {}).get("today") or {}
    w_today = [p for p in w_points if p.date == today]
    today_req = (
        _i(today_row.get("requests"))
        + sum(p.requests for p in t_points if p.date == today)
        + sum(p.requests for p in w_today)
    )
    totals = {
        "today_requests": today_req,
        "today_errors": _i(today_row.get("errors")),
        "today_prompt_tokens": _i(today_row.get("prompt_tokens")),
        "today_completion_tokens": _i(today_row.get("completion_tokens")),
        "window_tokens": (sum(p.total_tokens for p in q_points)
                          + sum(p.total_tokens for p in t_points)
                          + sum(p.total_tokens for p in w_points)),
    }
    return UsageResponse(
        generated_at=now_iso(),
        days=days,
        points={"qoder": q_points, "trae": t_points, "workbuddy": w_points},
        by_model=by_model,
        credits=credits,
        totals=totals,
    )


def _scheduler_from(raw: Dict[str, Any], platform: str) -> SchedulerInfo:
    tasks = raw.get("tasks") or raw.get("logs") or []
    if not isinstance(tasks, list):
        tasks = []
    return SchedulerInfo(
        platform=platform,  # type: ignore[arg-type]
        enabled=raw.get("enabled") if isinstance(raw.get("enabled"), bool) else None,
        mode=str(raw.get("mode") or raw.get("mode_cn") or ""),
        next_run=raw.get("next_run_time") or raw.get("next_run"),
        last_run=raw.get("last_run_time") or raw.get("last_run"),
        tasks=[t if isinstance(t, dict) else {"raw": str(t)} for t in tasks[:40]],
    )


def _wb_scheduler(agg: Aggregate) -> Dict[str, Any]:
    """workbuddy2api 的定时任务来自它自己的 config.json（schedule 段）。

    它没有「下次运行时间」概念（每小时轮询判断 current_hour in hours），
    所以 next_run 为空，任务行里用 schedule 展示每天几点。
    """
    raw = (agg.wb_settings.get("scheduler") or {}) if agg.wb_settings else {}
    tasks = []
    for t in (raw.get("tasks") or []):
        if not isinstance(t, dict):
            continue
        hours = t.get("hours") or []
        hh = ", ".join("%02d:00" % int(h) for h in hours
                       if isinstance(h, (int, float))) or "—"
        tasks.append({
            "name": str(t.get("name") or t.get("key") or ""),
            "hours": hours,
            "schedule": ("每天 " + hh) if hours else "未配置",
            "enabled": bool(t.get("enabled")),
            "platform": "workbuddy",
        })
    return {"enabled": bool(raw.get("enabled")), "mode": str(raw.get("mode") or ""),
            "tasks": tasks}


def tasks_payload(agg: Aggregate, systemd_timers: List[str]) -> TasksResponse:
    trae_sched_raw = (agg.trae_settings.get("scheduler") or {}) if agg.trae_settings else {}
    return TasksResponse(
        qoder=_scheduler_from(agg.qoder_scheduler, "qoder"),
        trae=_scheduler_from(trae_sched_raw, "trae"),
        workbuddy=_scheduler_from(_wb_scheduler(agg), "workbuddy"),
        systemd=[
            TaskInfo(name=t, platform="systemd", schedule="systemd-timer", status="active")
            for t in systemd_timers
        ],
    )


def health_payload(agg: Aggregate) -> HealthResponse:
    return HealthResponse(
        generated_at=now_iso(),
        upstreams=agg.health,
        overall=all(h.up for h in agg.health),
    )


def overview_payload(agg: Aggregate, systemd_timers: List[str], random_state: Dict[str, Any]) -> OverviewResponse:
    accounts = accounts_payload(agg)
    usage = usage_payload(agg, 14)
    tasks = tasks_payload(agg, systemd_timers)
    health = health_payload(agg)

    all_accts = accounts.qoder + accounts.trae + accounts.workbuddy
    summary = summarize(all_accts)
    q_credits = summarize(accounts.qoder).credits
    t_credits = summarize(accounts.trae).credits
    w_credits = summarize(accounts.workbuddy).credits
    valid = summary.ready
    stats = [
        StatCard(
            key="accounts",
            label="账号总数",
            value=str(summary.total),
            sub=f"Qoder {len(accounts.qoder)} · Trae {len(accounts.trae)}"
                f" · WB {len(accounts.workbuddy)}",
        ),
        StatCard(
            key="ready",
            label="有效期内",
            value=str(valid),
            sub="全部正常" if valid == summary.total else f"冷却 {summary.cooling} · 停用 {summary.disabled}",
            tone="" if valid == summary.total else "warn",
        ),
        StatCard(
            key="expiring",
            label="即将过期",
            value=str(summary.expiring_soon),
            sub=f"{EXPIRING_SOON_DAYS} 天内到期积分" if summary.expiring_soon else "暂无风险",
            tone="bad" if summary.expiring_soon else "",
        ),
        StatCard(
            key="credits",
            label="积分余额",
            value=_fmt_num(q_credits + t_credits + w_credits),
            sub=f"Qoder {_fmt_num(q_credits)} · Trae {_fmt_num(t_credits)}"
                f" · WB {_fmt_num(w_credits)}",
            tone="bad" if summary.low else "",
        ),
        StatCard(
            key="tokens",
            label="14 天 Token",
            value=_fmt_num(usage.totals.get("window_tokens", 0)),
            sub=f"今日请求 {usage.totals.get('today_requests', 0)}",
        ),
    ]
    # 失败趋势：Trae 按天有 errors；Qoder 的 buckets 无 errors，用今日值单列。
    trae_fail = [int(p.errors) for p in usage.points.get("trae", [])]
    wb_fail = [int(p.errors) for p in usage.points.get("workbuddy", [])]
    qoder_today_err = int(usage.totals.get("today_errors") or 0)
    failure_total = int(sum(trae_fail)) + int(sum(wb_fail)) + qoder_today_err
    checkin_log = []
    for row in (agg.trae_raw.get("checkin") or []):
        if isinstance(row, dict):
            checkin_log.append({
                "date": row.get("date") or "",
                "ok": int(row.get("ok") or 0),
                "already": int(row.get("already") or 0),
                "failed": int(row.get("failed") or 0),
            })
    checkin_log.sort(key=lambda r: r["date"], reverse=True)

    return OverviewResponse(
        generated_at=now_iso(),
        degraded=bool(agg.errors),
        errors=list(agg.errors),
        checkin_log=checkin_log[:7],
        failures=trae_fail,
        failure_total=failure_total,
        sticky_sessions=1 if tasks.qoder.enabled else 0,
        storage_mode="local-file",
        stats=stats,
        accounts=accounts,
        usage=usage,
        tasks=tasks,
        health=health,
        qoder_scheduler=tasks.qoder,
        random_checkin=random_state,
    )


def _fmt_num(v: Any) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if abs(n) >= 10_000:
        return f"{n/1000:.1f}k"
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.2f}"


# ---------------------------------------------------------------- 直接聚合（非 overview 端点）
def keys_payload(qoder_settings: Dict[str, Any], trae_keys: Dict[str, Any]) -> KeysResponse:
    q: List[KeyInfo] = []
    for k in qoder_settings.get("api_keys") or []:
        q.append(
            KeyInfo(
                platform="qoder",
                id=str(k.get("id") or ""),
                name=str(k.get("name") or ""),
                masked=str(k.get("masked") or ""),
                enabled=bool(k.get("enabled", True)),
                realm=str(k.get("realm") or ""),
                source=str(k.get("source") or ""),
                created_at=str(k.get("created_at") or ""),
            )
        )
    t: List[KeyInfo] = []
    data = trae_keys.get("data") or trae_keys
    for k in (data.get("keys") if isinstance(data, dict) else []) or []:
        t.append(
            KeyInfo(
                platform="trae",
                id=str(k.get("id") or k.get("key_id") or ""),
                name=str(k.get("name") or ""),
                masked=str(k.get("masked") or ("***" if k.get("key") else "")),
                enabled=bool(k.get("enabled", True)),
                daily_limit=_i(k.get("daily_limit")) if k.get("daily_limit") is not None else None,
                created_at=str(k.get("created_at") or ""),
            )
        )
    return KeysResponse(qoder=q, trae=t)


def models_payload(qoder_models: Dict[str, Any], trae_models: Dict[str, Any]) -> ModelsResponse:
    q: List[ModelInfo] = []
    for m in qoder_models.get("data") or []:
        caps = m.get("capabilities") or {}
        q.append(
            ModelInfo(
                platform="qoder",
                id=str(m.get("id") or ""),
                name=str(m.get("name") or m.get("id") or ""),
                context_length=_i(m.get("context_length")) or None,
                max_tokens=_i(m.get("max_tokens")) or None,
                vision=bool(m.get("supports_vision") or caps.get("vision")),
                tool_calls=bool(m.get("supports_tool_calls") or caps.get("tool_calls")),
                reasoning=bool(m.get("supports_reasoning") or caps.get("reasoning")),
                enabled=bool(m.get("enabled", True)),
            )
        )
    t: List[ModelInfo] = []
    for m in trae_models.get("data") or []:
        t.append(
            ModelInfo(
                platform="trae",
                id=str(m.get("id") or ""),
                name=str(m.get("display") or m.get("id") or ""),
                context_length=_i(m.get("context_length")) or None,
                max_tokens=_i(m.get("max_tokens")) or None,
                vision=bool(m.get("supports_image")),
            )
        )
    return ModelsResponse(qoder=q, trae=t)


def logs_payload(qoder_logs: Dict[str, Any], trae_logs: Dict[str, Any]) -> LogsResponse:
    q: List[LogEntry] = []
    for row in (qoder_logs.get("logs") or [])[:300]:
        q.append(
            LogEntry(
                platform="qoder",
                ts=str(row.get("ts") or row.get("time") or ""),
                level=str(row.get("level") or ""),
                message=str(row.get("msg") or row.get("message") or row.get("detail") or "")[:300],
            )
        )
    t: List[LogEntry] = []
    data = trae_logs.get("data") or []
    for row in (data if isinstance(data, list) else [])[:300]:
        if not isinstance(row, dict):
            continue
        t.append(
            LogEntry(
                platform="trae",
                ts=str(row.get("time") or row.get("ts") or ""),
                level=str(row.get("level") or ""),
                message=str(row.get("message") or row.get("msg") or "")[:300],
            )
        )
    return LogsResponse(qoder=q, trae=t)


def alerts_payload(qoder_alerts: Dict[str, Any], trae_settings: Dict[str, Any]) -> AlertsResponse:
    rows: List[Alert] = []
    for a in qoder_alerts.get("alerts") or []:
        rows.append(
            Alert(
                id=str(a.get("id") or ""),
                ts=str(a.get("time") or ""),
                platform="qoder",
                severity=str(a.get("level") or "info"),
                title=str(a.get("title") or "")[:160],
                detail=str(a.get("detail") or "")[:400],
                acked=bool(a.get("acked")),
            )
        )
    rows.sort(key=lambda x: x.ts, reverse=True)
    return AlertsResponse(alerts=rows[:200])


def requests_payload(qoder_usage: Dict[str, Any], trae_usage: Dict[str, Any], days: int) -> RequestsResponse:
    rows: List[RequestRow] = []
    for r in (qoder_usage.get("rows") or [])[-300:]:
        prompt = _i(r.get("prompt_tokens"))
        completion = _i(r.get("completion_tokens"))
        rows.append(
            RequestRow(
                ts=str(r.get("iso") or ""),
                platform="qoder",
                model=str(r.get("model") or ""),
                key_id=str(r.get("key_id") or ""),
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=_i(r.get("total_tokens")) or prompt + completion,
                credit=_f(r.get("credit")),
                elapsed_ms=_i(r.get("elapsed_ms")) or None,
                ttft_ms=_i(r.get("ttft_ms")) or None,
                stream=bool(r.get("stream")),
            )
        )
    for u in (trae_usage.get("usage") or [])[-60:]:
        prompt = _i(u.get("prompt_tokens"))
        completion = _i(u.get("completion_tokens"))
        rows.append(
            RequestRow(
                ts=str(u.get("date") or ""),
                platform="trae",
                model="(当日聚合)",
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                elapsed_ms=_i(u.get("avg_duration_ms")) or None,
                ttft_ms=_i(u.get("avg_ttfb_ms")) or None,
            )
        )
    rows.sort(key=lambda x: x.ts, reverse=True)
    return RequestsResponse(rows=rows[:300])


def random_state(state_dir: str = "/var/lib/signin-random") -> Dict[str, Any]:
    day = dt.datetime.now(TZ).date().isoformat()
    path = os.path.join(state_dir, f"state-{day}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
    except Exception:
        return {"targets": {}, "done": {}}
    targets = {
        k: dt.datetime.fromtimestamp(float(v), TZ).strftime("%H:%M:%S")
        for k, v in (st.get("targets") or {}).items()
        if isinstance(v, (int, float))
    }
    return {"targets": targets, "done": st.get("done") or {}}


def systemd_timers(names: Tuple[str, ...] = ("signin-random", "healthcheck", "trae-expiry", "trae-limits")) -> List[str]:
    import subprocess

    try:
        proc = subprocess.run(
            ["systemctl", "list-timers", "--all", "--no-pager"],
            capture_output=True, text=True, timeout=8,
        )
    except Exception:
        return []
    out = []
    for line in (proc.stdout or "").splitlines():
        if any(n in line for n in names):
            parts = line.split()
            if parts:
                out.append(parts[-2] if len(parts) >= 2 else line.strip())
    return out


def ops_payload(settings: Settings, limits_file: Path, notify_file: Path, backup_globs: Dict[str, str], audit_meta: Dict[str, Any]) -> OpsResponse:
    limits: Dict[str, Any] = {}
    try:
        limits = json.loads(Path(limits_file).read_text(encoding="utf-8"))
    except Exception:
        limits = {}
    notify: Dict[str, Any] = {}
    try:
        cfg = json.loads(Path(notify_file).read_text(encoding="utf-8"))
        notify = {k: cfg.get(k) for k in ("enabled", "bark_url", "serverchan_sendkey", "webhook_url")}
        for k in ("bark_url", "serverchan_sendkey", "webhook_url"):
            if notify.get(k):
                notify[k] = "***"
    except Exception:
        notify = {}
    backups: Dict[str, List[BackupEntry]] = {}
    for name, pattern in backup_globs.items():
        files = [f for f in glob.glob(pattern) if not f.endswith(".sha256")]
        files.sort(key=os.path.getmtime, reverse=True)
        backups[name] = [
            BackupEntry(
                name=os.path.basename(f),
                size=os.path.getsize(f),
                mtime=time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(f))),
            )
            for f in files[:8]
        ]
    return OpsResponse(
        generated_at=now_iso(),
        limits=limits,
        notify=notify,
        backups=backups,
        timers=systemd_timers(),
        audit=audit_meta,
    )


def settings_payload(
    qoder_settings: Dict[str, Any],
    trae_settings: Dict[str, Any],
    qtg: Dict[str, Any],
) -> SettingsResponse:
    q = {
        "api_keys": qoder_settings.get("api_keys") or [],
        "limits": qoder_settings.get("limits") or {},
        "auth_required": qoder_settings.get("auth_required"),
        "api_key_masked": qoder_settings.get("api_key_masked"),
        "version": qoder_settings.get("version"),
    }
    t = {
        "keys": ((trae_settings.get("data") or {}) if isinstance(trae_settings.get("data"), dict) else {}),
        "ip_allowlist": trae_settings.get("ip_allowlist") or {},
        "notify": trae_settings.get("notify") or {},
        "scheduler": trae_settings.get("scheduler") or {},
        "settings": trae_settings.get("settings") or {},
    }
    return SettingsResponse(generated_at=now_iso(), qoder=q, trae=t, qtg=qtg)
