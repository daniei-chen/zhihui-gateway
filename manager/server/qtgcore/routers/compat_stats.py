"""用量统计兼容层：用 qtg 聚合到的**上游真实 usage** 填 WorkBuddy 的统计页。

旧的 `compat.py` 实现读的是 qtg 自己的代理流水（`store.stats()`）。本部署还没有
代理调用，于是整页 0；而上游 BFF 的用量数据是真实的（qoder `/api/usage`、
trae `/api/usage`）。这里把它映射成前端要的 `StatsSummary` / `UsagePoint` /
`UsageBreakdown` / `CreditsFlow`，并按 `X-QTG-Platform` 只统计当前平台。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

from fastapi import APIRouter, Query, Request

from ..service import build_aggregate, usage_payload

router = APIRouter(prefix="/api", tags=["compat-stats"])

PLATFORMS = ("qoder", "trae", "workbuddy")


def _rt(request: Request):
    return request.app.state.rt


def _platform(request: Request) -> str:
    raw = (request.headers.get("x-qtg-platform") or "").strip().lower()
    return raw if raw in PLATFORMS else "qoder"


def _day_list(days: int) -> List[str]:
    n = max(1, min(90, days))
    return [(dt.date.today() - dt.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _get(obj: Any, key: str, default: Any = 0) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _merge_daily(payload: Any, days: int, platform: str) -> List[Dict[str, Any]]:
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
    if realm not in ("cn", "intl"):
        return True
    try:
        agg = await build_aggregate(_rt(request))
    except Exception:  # noqa: BLE001
        return True
    rows = {
        "qoder": agg.qoder_accounts,
        "trae": agg.trae_accounts,
        "workbuddy": agg.wb_accounts,
    }.get(platform, [])
    for a in rows:
        got = str(getattr(a, "realm", "") or "").strip().lower()
        if got == realm:
            return True
        # 上游不分版本时（Trae 的账号没有 realm 字段）这些账号默认属于国内版；
        # 把它们算到国际版会让国际版凭空多出一份用量。
        if not got and realm == "cn":
            return True
    return False



async def _payload(request: Request, days: int = 14):
    rt = _rt(request)
    agg = await build_aggregate(rt)
    return rt, usage_payload(agg, days)


@router.get("/stats/summary")
async def stats_summary(request: Request, realm: str = Query(default="")) -> Dict[str, Any]:
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
    if not await _realm_available(request, _platform(request), realm):
        return {
            "today_requests": 0, "today_tokens": 0, "today_credit": 0.0,
            "week_credit": 0.0, "total_credit": 0.0,
            "week_requests": 0, "week_tokens": 0,
            "total_requests": 0, "total_tokens": 0,
            "active_keys": 0, "top_model": None,
            "stats_health": {"ok": True, "detail": "该版本下没有账号"},
            "usage_health": {"ok": True, "detail": "", "logs_today": 0},
        }

    rt, payload = await _payload(request, 14)
    platform = _platform(request)
    daily = _merge_daily(payload, 14, platform)
    today = dt.date.today().isoformat()
    week_from = (dt.date.today() - dt.timedelta(days=6)).isoformat()
    today_row = next((d for d in daily if d["day"] == today), None)
    week_rows = [d for d in daily if d["day"] >= week_from]
    by_model = _get(payload, "by_model", []) or []
    top = next((m for m in by_model if _get(m, "platform", platform) == platform), None)
    return {
        "today_requests": int(today_row["requests"]) if today_row else 0,
        "today_tokens": int(today_row["prompt_tokens"] + today_row["completion_tokens"]) if today_row else 0,
        "today_credit": float(today_row["credit"]) if today_row else 0.0,
        "week_credit": round(sum(float(d["credit"]) for d in week_rows), 4),
        "total_credit": round(sum(float(d["credit"]) for d in daily), 4),
        "week_requests": sum(int(d["requests"]) for d in week_rows),
        "week_tokens": sum(int(d["prompt_tokens"] + d["completion_tokens"]) for d in week_rows),
        "total_requests": sum(int(d["requests"]) for d in daily),
        "total_tokens": sum(int(d["prompt_tokens"] + d["completion_tokens"]) for d in daily),
        "active_keys": len([k for k in rt.store.list_keys()
                            if platform in (k.get("platforms") or PLATFORMS)]),
        "top_model": str(_get(top, "model", "")) if top else None,
        "stats_health": {"ok": True, "detail": ""},
        "usage_health": {"ok": True, "detail": "", "logs_today": 0},
    }


@router.get("/stats/daily")
async def stats_daily(request: Request, days: int = Query(default=14),
                      realm: str = Query(default="")) -> List[Dict[str, Any]]:
    if not await _realm_available(request, _platform(request), realm):
        return []
    _, payload = await _payload(request, days)
    return _merge_daily(payload, days, _platform(request))


@router.get("/stats/hourly")
async def stats_hourly(request: Request, day: str = Query(default=""),
                       realm: str = Query(default="")) -> List[Dict[str, Any]]:
    """按小时分桶。

    上游（qoder2 / aiwork）都只给按天聚合、没有小时桶。旧实现直接造 24 个 0 桶，
    界面于是「当日 11 次请求、图上全是 0」——自相矛盾（审计点名）。
    现在改用**本机代理流水**的 epoch 分桶，反映经过 qtg 的真实调用时间。
    """
    # workbuddy：查它自己的 usage_hourly（24 桶固定补零，failed 取 request_logs）。
    # qtg 的流水表里没有 workbuddy 的调用，原来图上永远是 0。
    w = getattr(_rt(request), "wb", None)
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
    # workbuddy：按 manager 的 SQL 直接聚合 usage_daily（排序用 token 总量，
    # 带 cache 字段）。早期实现从 14 天窗口数请求数，数字偏多且 tokens 恒 0。
    w = getattr(_rt(request), "wb", None)
    if _platform(request) == "workbuddy" and w is not None:
        rows = await w.usage_by_model(days, realm)
        if rows:
            return rows
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
        })
    return out


@router.get("/stats/by-key")
async def stats_by_key(request: Request, days: int = Query(default=30),
                       realm: str = Query(default="")) -> List[Dict[str, Any]]:
    """按密钥统计：数据源只有 qtg 自己的代理流水（上游不认识我们的密钥）。

    这里修了两处审计问题：① token 数以前恒 0（SQL 已经算出 `t` 却没返回）；
    ② 以前不按平台过滤，qoder 页面会混进 trae 的密钥用量。
    """
    # workbuddy：按 manager 的 SQL（LEFT JOIN api_keys 取密钥名）。qtg 自己的
    # 流水表里没有 workbuddy 的调用（它不经过 qtg 转发时不会落库），直接查库才对。
    w = getattr(_rt(request), "wb", None)
    if _platform(request) == "workbuddy" and w is not None:
        wb_rows = await w.usage_by_key(days, realm)
        if wb_rows:
            return wb_rows
    rt = _rt(request)
    rows = rt.store.stats(days, _platform(request)).get("by_key") or []
    out: List[Dict[str, Any]] = []
    for r in rows:
        tokens = int(r.get("t") or 0)
        out.append({
            "name": r.get("key_name") or "",
            "requests": int(r.get("c") or 0),
            # 流水只存合计，没有输入/输出拆分：如实放 completion，prompt 留 0
            "prompt_tokens": 0,
            "completion_tokens": tokens,
            "credit": 0,
        })
    return out


@router.get("/stats/credits")
async def stats_credits(request: Request, days: int = Query(default=30),
                        realm: str = Query(default="")) -> Dict[str, Any]:
    # workbuddy：照 manager 的 /stats/credits —— 收入来自 task_logs(kind='credit')，
    # 支出来自 usage_daily.credit。qtg 原来把收入硬算成 0，所以曲线只跌不涨。
    w = getattr(_rt(request), "wb", None)
    if _platform(request) == "workbuddy" and w is not None:
        flow = await w.credits_flow(days, realm)
        if flow:
            return flow
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
