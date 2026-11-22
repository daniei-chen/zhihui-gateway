"""用量统计兼容层：用 qtg 聚合到的**上游真实 usage** 填 WorkBuddy 的统计页。

旧的 `compat.py` 实现读的是 qtg 自己的代理流水（`store.stats()`）。本部署还没有
代理调用，于是整页 0；而上游 BFF 的用量数据是真实的（qoder `/api/usage`、
trae `/api/usage`）。这里把它映射成前端要的 `StatsSummary` / `UsagePoint` /
`UsageBreakdown` / `CreditsFlow`，并按 `X-QTG-Platform` 只统计当前平台。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request

from ..identity import platform_of
from ..errors import UpstreamError
from ..service import build_aggregate, usage_payload
# U28：`_rt` 原先在本文件里有一份拷贝（与另外 4 个 compat 路由逐字节相同）。
# 现在统一从 `_shared` 引入并**在此继续导出**。
from ._shared import _rt

router = APIRouter(prefix="/api", tags=["compat-stats"])

# 2026-11-19：qoder 摘除，豆包工作顶替它的位置（顺序即 UI 显示顺序）。
PLATFORMS = ("workbuddy", "trae", "doubaowork")


def _platform(request: Request) -> str:
    raw = platform_of(request)
    return raw if raw in PLATFORMS else "qoder"


def _day_list(days: int) -> List[str]:
    n = max(1, min(90, days))
    return [(dt.date.today() - dt.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _get(obj: Any, key: str, default: Any = 0) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _merge_daily(payload: Any, days: int, platform: str) -> List[Dict[str, Any]]:
    # 豆包工作的上游没有用量接口。这里**必须返回空列表**：给 `days` 个全 0 的
    # 桶会让前端画出一条「每天 0 请求」的曲线，那是把「上游不给数据」讲成
    # 「这段时间确实没有调用」——两个完全不同的事实。
    # 空列表在页面上表现为「暂无数据」，是真话。
    if platform == "doubaowork":
        return []
    buckets: Dict[str, Dict[str, Any]] = {
        d: {"day": d, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "credit": 0.0, "failed": 0}
        for d in _day_list(days)
    }
    points = _get(payload, "points", {}) or {}
    if isinstance(points, dict):
        for plat, rows in points.items():
            if plat != platform:
                continue
            for p in rows or []:
                day = str(_get(p, "date", "") or "")
                b = buckets.get(day)
                if not b:
                    continue
                b["requests"] += int(_get(p, "requests", 0) or 0)
                pt = int(_get(p, "prompt_tokens", 0) or 0)
                ct = int(_get(p, "completion_tokens", 0) or 0)
                total = int(_get(p, "total_tokens", 0) or 0)
                if total and pt + ct == 0:
                    ct = total
                b["prompt_tokens"] += pt
                b["completion_tokens"] += ct
                b["failed"] += int(_get(p, "errors", 0) or 0)
    for row in _get(payload, "credits", []) or []:
        plat = str(_get(row, "platform", "") or "")
        if plat and plat != platform:
            continue
        day = str(_get(row, "date", "") or "")
        b = buckets.get(day)
        if not b:
            continue
        b["credit"] = round(b["credit"] + float(_get(row, "expense", 0) or 0), 4)
    return [buckets[d] for d in sorted(buckets)]


async def _realm_available(request: Request, platform: str, realm: str) -> bool:
    """该平台在该版本下有没有账号。

    Qoder / Trae 的上游用量接口是**按账号池整体**统计的，不区分国内 / 国际；
    只有账号本身带 realm。所以「该版本下一个账号都没有」是唯一能确定的情形：
    这时必须返回空，否则切到国际版会看到和国内版一模一样的数字（用户实测反馈）。
    """
    if not realm:
        return True
    if realm not in ("cn", "global", "intl"):
        raise HTTPException(status_code=422, detail='Invalid usage realm')
    try:
        from ..service import build_pool_snapshot
        agg = await build_pool_snapshot(_rt(request),platform)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail='无法核实账号池版本归属') from exc
    rows = {
        "qoder": agg.qoder_accounts,
        "trae": agg.trae_accounts,
        "workbuddy": agg.wb_accounts,
        "doubaowork": agg.doubaowork_accounts,
    }.get(platform, [])
    observed={('global' if str(getattr(a,'realm','')).lower()=='intl' else str(getattr(a,'realm','') or 'cn').lower()) for a in rows}
    if len(observed)>1:
        raise HTTPException(status_code=409, detail='旧上游仅提供全池用量，不能按版本归属')
    for a in rows:
        got = str(getattr(a, "realm", "") or "").strip().lower()
        if ("global" if got=="intl" else got) == ("global" if realm=="intl" else realm):
            return True
        # 上游不分版本时（Trae 的账号没有 realm 字段）这些账号默认属于国内版；
        # 把它们算到国际版会让国际版凭空多出一份用量。
        if not got and realm == "cn":
            return True
    return False



async def _payload(request: Request, days: int = 14):
    rt = _rt(request)
    platform=_platform(request)
    from ...services.scope import fixed
    # 2026-11-20：上游整个不可达时 `build_aggregate` 会抛 UpstreamError，
    # 而所有 stats 端点都没有兜它 —— 实测（停掉 zhihui-trae 容器后）
    # `/api/stats/summary?platform=trae` 返回**裸 500 纯文本**
    # 「Internal Server Error」，前端只剩一个红色 XHR 错误。
    # 同期 `/api/accounts` 已经是 409 +「上游不可达: ConnectError」，
    # 同一个故障在两张页面上给出完全不同的可读性。
    try:
        agg = await build_aggregate(rt, usage_days=days,scope=fixed(platform,authority='upstream'))
    except UpstreamError as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    prefixes=('wb.',) if platform=='workbuddy' else (platform+'.',)
    if any(error.startswith(prefixes) for error in agg.errors):
        raise HTTPException(status_code=503, detail='用量证据不完整，不能以空数组或零值代替')
    return rt, usage_payload(agg, days)


def _uses_local_flow(platform: str) -> bool:
    """该平台是否只能读**本机代理流水**（上游没有用量接口）。

    目前只有豆包工作（doubaowork2api 的 25 条路由里没有任何 usage 端点）。
    2026-11-20 修：此前 stats_summary / stats_credits / stats_by_model 三个
    端点在判断平台**之前**就调了 `_payload()`，而 `_payload()` 会去调
    `doubaowork.usage()` —— 上游没有这个接口，必然抛错，`agg.errors` 里
    出现 `doubaowork.` 前缀，于是整页统计变成 503「用量证据不完整」。

    用户看到的是「统计页打不开」，实际是「我在拿一个上游不存在的接口当证据」。
    判据必须前置到调用 `_payload()` 之前，不能事后补救。
    """
    return platform == "doubaowork"


def _realm_scoped(platform: str, realm: str) -> str:
    """豆包工作不分国内/国际版：前端 `RealmToggle` 对它整个禁用。

    这里把它归一成空串，否则 `_realm_available` 会按「该版本下没有账号」
    返回全 0 —— 而豆包根本不是「这个版本没账号」，是「它没有版本这个概念」。
    """
    return "" if _uses_local_flow(platform) else realm


def _local_credits(rt, days: int, platform: str) -> Dict[str, Any]:
    """用本机流水拼出「积分收支」形状。

    豆包的上游不给积分收支，也没有 `task_logs(kind='credit')` 那种入账记录
    （那是 WorkBuddy 的口径）。这里**只填支出**、收入恒 0，并把口径写进
    `note` —— 让界面能说清「这是经过网关的消耗」，而不是假装成完整账本。
    """
    daily = rt.store.daily(max(1, min(90, days)), platform)
    rows = [{"day": d["day"], "income": 0.0,
             "expense": float(d.get("credit") or 0.0), "net": -float(d.get("credit") or 0.0)}
            for d in daily]
    today = dt.date.today().isoformat()
    week_from = (dt.date.today() - dt.timedelta(days=6)).isoformat()

    def _sum(items):
        return {"income": round(sum(r["income"] for r in items), 4),
                "expense": round(sum(r["expense"] for r in items), 4),
                "net": round(sum(r["net"] for r in items), 4)}

    return {
        "days": days,
        "from": rows[0]["day"] if rows else "",
        "to": today,
        "today": _sum([r for r in rows if r["day"] == today]),
        "week": _sum([r for r in rows if r["day"] >= week_from]),
        "total": _sum(rows),
        "daily": rows,
        "note": "豆包工作上游不提供积分收支，这里只统计经过本网关的消耗（收入恒 0）",
    }


@router.get("/stats/summary")
async def stats_summary(request: Request, realm: str = Query(default="")) -> Dict[str, Any]:
    rt = _rt(request)
    scoped = getattr(rt.qoder, 'usage_summary_scoped', None) if _platform(request) == 'qoder' else None
    if callable(scoped):
        if realm not in ('', 'cn', 'global'):
            raise HTTPException(status_code=422, detail='Invalid usage realm')
        summary = await scoped(realm)
        summary['active_keys'] = sum(1 for key in rt.store.list_keys()
                                     if key.get('enabled') and 'qoder' in (key.get('platforms') or PLATFORMS))
        return summary
    # workbuddy：直接按 manager 的口径查库（total_* 是全表，不是某个时间窗）。
    # 早期实现从 14 天窗口推 total_*，16 万次请求只显示成 3.3 万次。
    w = getattr(_rt(request), "wb", None)
    if _platform(request) == "workbuddy" and w is not None:
        summary = await w.usage_summary(realm)
        if summary:
            summary["stats_health"] = {"ok": True, "detail": ""}
            summary["usage_health"] = {"ok": True, "detail": "", "logs_today": 0}
            return summary
    # 该版本下一个账号都没有时，下面那份「账号池整体」用量与本版本无关，必须给 0。
    if not await _realm_available(request, _platform(request), _realm_scoped(_platform(request), realm)):
        return {
            "today_requests": 0, "today_tokens": 0, "today_credit": 0.0,
            "week_credit": 0.0, "total_credit": 0.0,
            "week_requests": 0, "week_tokens": 0,
            "total_requests": 0, "total_tokens": 0,
            "active_keys": 0, "top_model": None,
            "stats_health": {"ok": True, "detail": "该版本下没有账号"},
            "usage_health": {"ok": True, "detail": "", "logs_today": 0},
        }

    platform = _platform(request)
    # 豆包工作必须在 `_payload()` **之前**分流：它没有上游 usage 接口，
    # 走进去必然 503（见 `_uses_local_flow` 的注释）。
    if _uses_local_flow(platform):
        daily = rt.store.daily(14, platform)
        today = dt.date.today().isoformat()
        week_from = (dt.date.today() - dt.timedelta(days=6)).isoformat()
        today_row = next((d for d in daily if d["day"] == today), None)
        week_rows = [d for d in daily if d["day"] >= week_from]
        try:
            _rows = rt.store.stats(14, platform).get('by_model') or []
            top_model = str(_rows[0]['model']) if _rows and _rows[0].get('model') else None
        except Exception:  # noqa: BLE001 - 聚合失败不该让整页报表 500
            top_model = None
        return {
            "today_requests": int(today_row["requests"]) if today_row else 0,
            "today_tokens": int(today_row["prompt_tokens"] + today_row["completion_tokens"]) if today_row else 0,
            "today_credit": float(today_row["credit"]) if today_row else 0.0,
            "week_credit": round(sum(float(d["credit"]) for d in week_rows), 4),
            "total_credit": None,
            "week_requests": sum(int(d["requests"]) for d in week_rows),
            "week_tokens": sum(int(d["prompt_tokens"] + d["completion_tokens"]) for d in week_rows),
            "total_requests": None,
            "total_tokens": None,
            "observed_window_days": 14,
            "lifetime_totals_known": False,
            "active_keys": len([k for k in rt.store.list_keys()
                                if platform in (k.get("platforms") or PLATFORMS)]),
            "top_model": top_model,
            "stats_health": {"ok": True, "detail": ""},
            "usage_health": {"ok": True, "detail": "", "logs_today": 0},
            "usage_source": "local_proxy_flow",
        }

    rt, payload = await _payload(request, 14)
    daily = _merge_daily(payload, 14, platform)
    today = dt.date.today().isoformat()
    week_from = (dt.date.today() - dt.timedelta(days=6)).isoformat()
    today_row = next((d for d in daily if d["day"] == today), None)
    week_rows = [d for d in daily if d["day"] >= week_from]
    by_model = _get(payload, "by_model", []) or []
    top = next((m for m in by_model if _get(m, "platform", platform) == platform), None)
    top_model = str(_get(top, "model", "")) if top else None
    return {
        "today_requests": int(today_row["requests"]) if today_row else 0,
        "today_tokens": int(today_row["prompt_tokens"] + today_row["completion_tokens"]) if today_row else 0,
        "today_credit": float(today_row["credit"]) if today_row else 0.0,
        "week_credit": round(sum(float(d["credit"]) for d in week_rows), 4),
        "total_credit": None,
        "week_requests": sum(int(d["requests"]) for d in week_rows),
        "week_tokens": sum(int(d["prompt_tokens"] + d["completion_tokens"]) for d in week_rows),
        "total_requests": None,
        "total_tokens": None,
        "observed_window_days": 14,
        "lifetime_totals_known": False,
        "active_keys": len([k for k in rt.store.list_keys()
                            if platform in (k.get("platforms") or PLATFORMS)]),
        "top_model": top_model,
        "stats_health": {"ok": True, "detail": ""},
        "usage_health": {"ok": True, "detail": "", "logs_today": 0},
    }


@router.get("/stats/daily")
async def stats_daily(request: Request, days: int = Query(default=14),
                      realm: str = Query(default="")) -> List[Dict[str, Any]]:
    scoped = getattr(_rt(request).qoder, 'usage_scoped', None) if _platform(request) == 'qoder' else None
    if callable(scoped):
        if realm not in ('', 'cn', 'global'):
            raise HTTPException(status_code=422, detail='Invalid usage realm')
        return (await scoped(max(1, min(90, days)), realm))['daily']
    # 豆包工作的上游没有用量接口 —— 用**本机代理流水**按天聚合，
    # 那是「经过 qtg 的转发量」的真数据，不是补零。必须排在
    # `_realm_available` / `_payload` 之前，理由见 `_uses_local_flow`。
    if _uses_local_flow(_platform(request)):
        return _rt(request).store.daily(max(1, min(90, days)), _platform(request))
    if not await _realm_available(request, _platform(request), realm):
        return []
    _, payload = await _payload(request, days)
    return _merge_daily(payload, days, _platform(request))


@router.get("/stats/hourly")
async def stats_hourly(request: Request, day: str = Query(default=""),
                       realm: str = Query(default="")) -> List[Dict[str, Any]]:
    """Qoder2 uses engine evidence, WorkBuddy its ledger, others local traffic."""
    rt = _rt(request)
    scoped = getattr(rt.qoder, 'usage_hourly_scoped', None) if _platform(request) == 'qoder' else None
    if callable(scoped):
        if realm not in ('', 'cn', 'global'):
            raise HTTPException(status_code=422, detail='Invalid usage realm')
        if day:
            try:
                date = dt.date.fromisoformat(day)
                if date.isoformat() != day or not 1971 <= date.year <= 9998:
                    raise ValueError('invalid day')
            except ValueError:
                raise HTTPException(status_code=422, detail='Invalid usage day') from None
        return await scoped(day, realm)
    # workbuddy：查它自己的 usage_hourly（24 桶固定补零，failed 取 request_logs）。
    # qtg 的流水表里没有 workbuddy 的调用，原来图上永远是 0。
    w = getattr(rt, "wb", None)
    if _platform(request) == "workbuddy" and w is not None:
        rows = await w.usage_hourly(day, realm)
        if rows:
            return rows
    if not await _realm_available(request, _platform(request), realm):
        return []
    rt = _rt(request)
    return rt.store.hourly(day or dt.date.today().isoformat(), _platform(request))


@router.get("/stats/by-model")
async def stats_by_model(request: Request, days: int = Query(default=30),
                         realm: str = Query(default="")) -> List[Dict[str, Any]]:
    scoped = getattr(_rt(request).qoder, 'usage_scoped', None) if _platform(request) == 'qoder' else None
    if callable(scoped):
        if realm not in ('', 'cn', 'global'):
            raise HTTPException(status_code=422, detail='Invalid usage realm')
        return (await scoped(max(1, min(90, days)), realm))['by_model']
    # workbuddy：按 manager 的 SQL 直接聚合 usage_daily（排序用 token 总量，
    # 带 cache 字段）。早期实现从 14 天窗口数请求数，数字偏多且 tokens 恒 0。
    w = getattr(_rt(request), "wb", None)
    if _platform(request) == "workbuddy" and w is not None:
        return await w.usage_by_model(days, realm)
    # 豆包工作：上游没有 usage，改读本机流水的 by_model 聚合。
    if _uses_local_flow(_platform(request)):
        try:
            rows = _rt(request).store.stats(max(1, min(90, days)), _platform(request)).get("by_model") or []
        except Exception:  # noqa: BLE001 - 聚合失败不该让整页报表 500
            return []
        return [{
            "name": str(r.get("model") or ""),
            "requests": int(r.get("c") or 0),
            "prompt_tokens": 0,
            "completion_tokens": int(r.get("t") or 0),
            "credit": 0.0,
            "cache_hit_rate": None,
            "tokens_per_sec": None,
            "throughput_samples": 0,
        } for r in rows]
    if not await _realm_available(request, _platform(request), realm):
        return []
    _, payload = await _payload(request, days)
    platform = _platform(request)
    out: List[Dict[str, Any]] = []
    for m in _get(payload, "by_model", []) or []:
        if _get(m, "platform", platform) != platform:
            continue
        prompt = int(_get(m, "prompt_tokens", 0) or 0)
        completion = int(_get(m, "completion_tokens", 0) or 0)
        if not (prompt or completion):
            # 旧上游只给合计：整额算 completion（历史行为，保持兼容）
            completion = int(_get(m, "tokens", 0) or 0)
        out.append({
            "name": str(_get(m, "model", "") or ""),
            "requests": int(_get(m, "requests", 0) or 0),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "credit": float(_get(m, "credit", 0) or 0),
            "cache_hit_rate": None,
            "tokens_per_sec": None,
            "throughput_samples": 0,
        })
    return out


@router.get("/stats/by-key")
async def stats_by_key(request: Request, days: int = Query(default=30),
                       realm: str = Query(default="")) -> List[Dict[str, Any]]:
    """Current Manager keys, with exact recorded platform/realm attribution."""
    platform = _platform(request)
    from ...platformpolicy import spec as _platform_spec
    _s = _platform_spec(platform)
    if _s and _s.stats_scope:
        import sqlite3
        from starlette.concurrency import run_in_threadpool
        from ...platformstats import current_key_usage

        if realm not in ('', 'cn', 'global'):
            raise HTTPException(status_code=422, detail='Invalid key-usage realm')
        try:
            return await run_in_threadpool(current_key_usage, max(1, min(90, days)), realm, platform)
        except (sqlite3.Error, ValueError):
            raise HTTPException(status_code=503, detail='Manager key-usage evidence unavailable') from None
    # workbuddy：只列仍存在的 manager 密钥。qtg 自己的
    # 流水表里没有 workbuddy 的调用（它不经过 qtg 转发时不会落库），直接查库才对。
    w = getattr(_rt(request), "wb", None)
    if _platform(request) == "workbuddy" and w is not None:
        # An empty current-key list is authoritative, not a fallback signal.
        return await w.usage_by_key(days, realm)
    if _platform(request) == "doubaowork":
        # 豆包工作的上游没有用量接口，「按密钥的用量」无从谈起。
        #
        # 2026-11-20 修：这里原来抛 409。前端拿到 409 会在「按密钥」那一栏
        # 显示成红色报错，用户读成「这个功能坏了」。但事实是「该平台没有这个
        # 能力」，属于**可解释的空**，不是故障 —— 返回空列表 + note，
        # 页面就能正常渲染「暂无数据」并附上原因。
        return []
    raise HTTPException(status_code=503, detail='Manager key-usage source unavailable')


@router.get("/stats/credits")
async def stats_credits(request: Request, days: int = Query(default=30),
                        realm: str = Query(default="")) -> Dict[str, Any]:
    rt = _rt(request)
    scoped = getattr(rt.qoder, 'credits_flow_scoped', None) if _platform(request) == 'qoder' else None
    if callable(scoped):
        if realm not in ('', 'cn', 'global'):
            raise HTTPException(status_code=422, detail='Invalid credits realm')
        return await scoped(max(1, min(90, days)), realm)
    # workbuddy：照 manager 的 /stats/credits —— 收入来自 task_logs(kind='credit')，
    # 支出来自 usage_daily.credit。qtg 原来把收入硬算成 0，所以曲线只跌不涨。
    w = getattr(_rt(request), "wb", None)
    if _platform(request) == "workbuddy" and w is not None:
        flow = await w.credits_flow(days, realm)
        if flow:
            return flow
    # 豆包工作：上游不给积分收支，用本机流水只算支出（收入恒 0，口径写进 note）。
    if _uses_local_flow(_platform(request)):
        return _local_credits(_rt(request), max(1, min(90, days)), _platform(request))
    _, payload = await _payload(request, days)
    platform = _platform(request)
    daily_map = {d: {"day": d, "income": 0.0, "expense": 0.0, "net": 0.0}
                 for d in _day_list(days)}
    for row in _get(payload, "credits", []) or []:
        plat = str(_get(row, "platform", "") or "")
        if plat and plat != platform:
            continue
        day = str(_get(row, "date", "") or "")
        b = daily_map.get(day)
        if not b:
            continue
        b["income"] = round(b["income"] + float(_get(row, "income", 0) or 0), 4)
        b["expense"] = round(b["expense"] + float(_get(row, "expense", 0) or 0), 4)
    for b in daily_map.values():
        b["net"] = round(b["income"] - b["expense"], 4)
    daily = [daily_map[d] for d in sorted(daily_map)]
    today = dt.date.today().isoformat()
    week_from = (dt.date.today() - dt.timedelta(days=6)).isoformat()

    def _sum(rows):
        return {"income": round(sum(r["income"] for r in rows), 4),
                "expense": round(sum(r["expense"] for r in rows), 4),
                "net": round(sum(r["net"] for r in rows), 4)}

    return {
        "days": days,
        "from": daily[0]["day"] if daily else "",
        "to": today,
        "today": _sum([r for r in daily if r["day"] == today]),
        "week": _sum([r for r in daily if r["day"] >= week_from]),
        "total": _sum(daily),
        "daily": daily,
    }
