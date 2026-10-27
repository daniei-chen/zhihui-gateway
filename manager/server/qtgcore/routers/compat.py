"""WorkBuddy 前端兼容层（/api/*）。

背景：前端直接移植自 WorkBuddy Manager（Next.js + React + motion），
它按自己的契约调用 `/api/*`。本模块把 qtg 的 qoder/trae 数据**映射**成
WorkBuddy 期望的结构，从而不改前端也能跑起来。

约定：
* 读端点**不再默认公开**：`main.py` 的 `_auth_gate` 对全部 `/api/*` 要求
  会话 Cookie 或管理令牌 Bearer，白名单只留 `/api/login`、`/api/logout`、`/api/me`；
* 写端点（login/logout）单独处理；
* 未实现的端点返回**结构正确的空值**，绝不让前端 500。
"""
from __future__ import annotations

import secrets
import datetime as dt
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import manager_api
from ..manager_session import forward_logout, wb_cookie_header
from ..service import all_accounts, build_aggregate, now_iso, summarize, tasks_payload

router = APIRouter(prefix="/api", tags=["compat"])


def _rt(request: Request):
    return request.app.state.rt


# ------------------------------------------------------------------ 认证
class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


# 登录节流（2026-10-23 审计：compat /api/login 原本无任何节流，配合 10 位纯数字
# 弱口令可被本机任意进程爆破；与 auth.py 的 /api/v1/auth/login 同一档位 8 次/分钟）
_LOGIN_WINDOW = 60.0
_LOGIN_MAX = 8
_login_attempts: Dict[str, list] = defaultdict(list)


def _login_throttle(request: Request) -> None:
    ip = (request.headers.get("x-real-ip") or "").strip() or (
        request.client.host if request.client else "?"
    )
    now = time.monotonic()
    q = [t for t in _login_attempts.get(ip, []) if now - t <= _LOGIN_WINDOW]
    if len(q) >= _LOGIN_MAX:
        _login_attempts[ip] = q
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
    q.append(now)
    _login_attempts[ip] = q






# ------------------------------------------------------------------ 账号
def _note_of(rt, uid: str) -> str:
    """账号备注（本端存的，按 uid 关联）——上游账号文件里没有这个字段。"""
    import json as _json
    from pathlib import Path

    try:
        data = _json.loads((Path(rt.settings.data_dir) / "account_notes.json").read_text(encoding="utf-8"))
        return str(data.get(uid) or "") if isinstance(data, dict) else ""
    except Exception:  # noqa: BLE001
        return ""


def _checkin_epoch(a) -> Optional[int]:
    """把账号的签到状态折算成 `checkin_today`（epoch 秒；None = 今天未签）。

    qtg 的 Account 只有布尔 `checked_today` 和字符串 `last_checkin`，而前端
    的 accounts 页需要时间戳：非 null 才渲染「已完成」并计入 pendingCheckin。
    """
    if not bool(getattr(a, "checked_today", False)):
        return None
    tz = dt.timezone(dt.timedelta(hours=8))
    now = dt.datetime.now(tz)
    today0 = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    raw = str(getattr(a, "last_checkin", "") or "").strip()[:19]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            naive = dt.datetime.strptime(raw, fmt)
        except (ValueError, TypeError):
            continue
        ts = int(naive.replace(tzinfo=tz).timestamp())
        # 已知 checked_today=True，但 last_checkin 落在更早的日子
        # （trae 的 last_checkin 是 auth_saved_at）：用今天 0 点，别倒填历史时间。
        return max(ts, today0)
    return today0


def _map_account(rt, a) -> Dict[str, Any]:
    now = time.time()
    expires = a.expires_at or 0
    remain = max(0, int(expires - now)) if expires else 0
    healthy = a.state == "ready"
    return {
        "file": f"{a.platform}:{a.uid}",
        "uid": a.uid,
        "nickname": a.name or a.uid[:8],
        "enterprise_id": a.platform,  # 平台标识，用于顶部平台切换
        "expires_at": expires,
        "is_expired": a.state == "expired",
        "remain_seconds": remain,
        "credits": a.credits_remain,
        "healthy": healthy,
        "disabled": a.state == "disabled",
        "disabled_reason": a.note or None,
        "note": _note_of(rt, a.uid),
        "in_flight": a.in_flight,
        "cooling": a.in_cooldown,
        "cool_remaining_sec": None,
        "weight": 1,
        # 前端 availabilityOf() 的判据：在线 = 在池、上游状态已知、无错误累积。
        # 直接给齐这些字段，避免它落到 unknown / notLoaded 分档。
        # None = 该平台没有池概念（qoder/workbuddy），前端按旧行为当 true
        "in_pool": True if a.in_pool is None else bool(a.in_pool),
        "poolUnknown": a.in_pool is None,
        "success_count": 1,
        "err_total": 0,
        "manual_disabled": False,
        "disabled_by_panel": False,
        "platform": a.platform,
        # realm 是「版本」维度（cn / global），与 platform（平台维度）独立：
        # Qoder 账号本身带 cn/intl；Trae 目前只有国内站，一律算 cn。
        "realm": "global" if str(a.realm).lower() in ("intl", "global") else "cn",
        "state": a.state,
        # 前端 pendingCheckin / 每行「已完成」都读这个字段（null = 今天还没签）
        "checkin_today": _checkin_epoch(a),
        "checked_today": bool(getattr(a, "checked_today", False)),
    }








# ------------------------------------------------------------------ 密钥


# ------------------------------------------------------------------ 模型














@router.get("/stats/upstream")
async def compat_stats_upstream(request: Request) -> Dict[str, Any]:
    rt = _rt(request)
    st = rt.store.stats(1)
    return {"requests": st["requests"], "tokens": st["tokens"], "errors": st["errors"],
            "by_platform": st.get("by_platform") or [], "available": True}





@router.get("/stats/credits-flow")
async def compat_credits_flow(request: Request, days: int = Query(default=7), realm: str = Query(default="")) -> Dict[str, Any]:
    rt = _rt(request)
    agg = await build_aggregate(rt)
    daily = []
    import datetime as dt

    n = max(1, min(90, days))
    for i in range(n - 1, -1, -1):
        day = (dt.date.today() - dt.timedelta(days=i)).isoformat()
        daily.append({"day": day, "income": 0.0, "expense": 0.0, "net": 0.0})
    total = {"income": 0.0, "expense": 0.0, "net": 0.0}
    return {
        "days": n,
        "from": daily[0]["day"] if daily else "",
        "to": daily[-1]["day"] if daily else "",
        "today": {"income": 0.0, "expense": 0.0, "net": 0.0},
        "week": {"income": 0.0, "expense": 0.0, "net": 0.0},
        "total": total,
        "daily": daily,
    }


# ------------------------------------------------------------------ 日志
async def _wb_logs(rt, days: int, status: str, page: int, size: int,
                    offset: int) -> Dict[str, Any]:
    """workbuddy 的请求日志（manager.db request_logs，17 万条）。

    它自己就有完整流水，没必要也不应该转存到 qtg 的 store：
    三合一只是把两个来源按平台分开展示。
    """
    import time as _time

    w = getattr(rt, "wb", None)
    if w is None:
        return {"total": 0, "items": [], "page": 1, "size": size}
    try:
        data = await w.logs(500)
    except Exception:  # noqa: BLE001
        return {"total": 0, "items": [], "page": 1, "size": size}
    since = _time.time() - max(1, min(90, days)) * 86400
    rows = [r for r in (data.get("data") or []) if float(r.get("ts") or 0) >= since]
    if status == "ok":
        rows = [r for r in rows if int(r.get("status") or 0) < 400]
    elif status == "error":
        rows = [r for r in rows if int(r.get("status") or 0) >= 400]
    total = len(rows)
    p = max(1, page)
    s = max(1, min(200, size))
    off = offset if offset else (p - 1) * s
    items = []
    for i, r in enumerate(rows[off: off + s]):
        items.append({
            "id": off + i + 1,
            "ts": int(r.get("ts") or 0),
            "key_id": None,
            "key_name": None,
            "ip": r.get("ip") or "",
            "model": r.get("model") or None,
            "mapped_model": r.get("mapped_model") or None,
            "status": int(r.get("status") or 0),
            "prompt_tokens": int(r.get("prompt_tokens") or 0),
            "completion_tokens": int(r.get("completion_tokens") or 0),
            "latency_ms": int(r.get("latency_ms") or 0),
            "ttft_ms": int(r.get("first_token_ms") or 0) or None,
            "stream": False,
            "platform": "workbuddy",
        })
    return {"total": total, "items": items, "page": p, "size": s}


@router.get("/logs")
async def compat_logs(
    request: Request,
    page: int = Query(default=1),
    size: int = Query(default=20),
    days: int = Query(default=7),
    realm: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=0),
    offset: int = Query(default=0),
) -> Dict[str, Any]:
    """请求日志：按平台（X-QTG-Platform）与时间范围过滤，支持前端分页参数。

    数据源是 qtg 自己的代理流水（`proxy_requests`）——只有经过本网关的调用才会
    出现在这里。本部署还没有下游调用时为空是真实情况，不是没实现。
    """
    import time as _time

    rt = _rt(request)
    plat = (request.headers.get("x-qtg-platform") or "").strip().lower()
    if plat == "workbuddy":
        return await _wb_logs(rt, days, status, page, size or limit or 20, offset)
    if plat not in ("qoder", "trae"):
        plat = ""
    rows = rt.store.list_requests(1000, platform=plat)
    since = _time.time() - max(1, min(90, days)) * 86400
    rows = [r for r in rows if float(r.get("epoch") or 0) >= since]
    if status == "ok":
        rows = [r for r in rows if int(r.get("status") or 0) < 400]
    elif status == "error":
        rows = [r for r in rows if int(r.get("status") or 0) >= 400]
    total = len(rows)
    p = max(1, page)
    s = max(1, min(200, size or limit or 20))
    off = offset if offset else (p - 1) * s
    items = []
    for r in rows[off: off + s]:
        items.append({
            "id": r.get("id"),
            "ts": int(r.get("epoch") or 0),
            "key_id": None,
            "key_name": r.get("key_name") or None,
            "ip": "",
            "model": r.get("model") or None,
            "mapped_model": r.get("model") or None,
            "status": int(r.get("status") or 0),
            "prompt_tokens": int(r.get("prompt_tokens") or 0),
            "completion_tokens": int(r.get("completion_tokens") or 0),
            "latency_ms": int(r.get("duration_ms") or 0),
            "ttft_ms": int(r.get("ttft_ms") or 0) or None,
            "stream": bool(r.get("stream")),
            "platform": r.get("platform"),
        })
    return {"total": total, "items": items, "page": p, "size": s}


@router.get("/checkin-logs")
async def compat_checkin_logs(request: Request, realm: str = Query(default="")) -> Dict[str, Any]:
    """签到记录：按平台分开。

    * trae：上游 `/api/checkin` 的历史留痕（按天汇总）；
    * qoder：账号 JSON 里的 `lastCheckin` / 今天是否已签（没有逐条历史）。
    """
    rt = _rt(request)
    agg = await build_aggregate(rt)
    plat = (request.headers.get("x-qtg-platform") or "").strip().lower()
    items: List[Dict[str, Any]] = []
    if plat == "qoder":
        for a in agg.qoder_accounts:
            if not a.last_checkin and not a.checked_today:
                continue
            date = str(a.last_checkin or "")[:10]
            ok = 1 if a.checked_today else 0
            items.append({
                "id": abs(hash(str(a.uid) + date)) % 100000,
                "ts": 0, "uid": a.uid, "nickname": a.name or a.uid[:8],
                "source": "auto", "kind": "checkin",
                "success": bool(ok), "code": None, "date": date,
                "ok": ok, "already": 0, "failed": 0 if ok else 1,
                "message": f"{date} {'已签到' if ok else '未签到'}",
            })
    elif plat == "workbuddy":
        for row in (agg.wb_raw.get("checkin") or []):
            if not isinstance(row, dict):
                continue
            ok_n = int(row.get("ok") or 0)
            items.append({
                "id": abs(hash("wb" + str(row.get("date", "")))) % 100000,
                "ts": 0, "uid": "", "nickname": "",
                "source": "auto", "kind": "checkin",
                "success": ok_n > 0, "code": None,
                "date": row.get("date", ""), "ok": ok_n,
                "already": int(row.get("already") or 0),
                "failed": int(row.get("failed") or 0),
                "message": f"{row.get('date','')} 成功 {ok_n} · 失败 {row.get('failed',0)}",
            })
    else:
        for row in (agg.trae_raw.get("checkin") or []):
            if not isinstance(row, dict):
                continue
            ok_n = int(row.get("ok") or 0)
            items.append({
                "id": abs(hash(row.get("date", ""))) % 100000,
                "ts": 0, "uid": "", "nickname": "",
                "source": "auto", "kind": "checkin",
                "success": ok_n > 0, "code": None,
                "date": row.get("date", ""), "ok": ok_n,
                "already": int(row.get("already") or 0),
                "failed": int(row.get("failed") or 0),
                "message": f"{row.get('date','')} 成功 {ok_n} · 已签 {row.get('already',0)} · 失败 {row.get('failed',0)}",
            })
    return {"total": len(items), "items": items, "local_total": len(items), "auto_total": 0}




# ------------------------------------------------------------------ 安全 / 系统 / 设置



























# ------------------------------------------------------------------ 兜底
# ------------------------------------------------------------------ 任务运行器
# WorkBuddy 的「任务运行」是执行上游脚本；qtg 这边由 Qoder/Trae 各自的调度器负责，
# 因此这些端点如实返回「不可用」而不是假装成功。








@router.get("/accounts/daily-plan")
async def compat_daily_plan(request: Request) -> Dict[str, Any]:
    """DailyPlan：四个子配置，每个都是 {enabled, hour, last_run}。

    本部署的签到由 Qoder/Trae 自己的调度器负责，这里如实返回「未启用」，
    但**结构必须完整**——前端会直接读 dailyPlan.checkin.enabled。
    """
    task = {"enabled": False, "hour": 21, "last_run": ""}
    return {"checkin": dict(task), "compensation": dict(task), "claim": dict(task), "full": dict(task)}


@router.get("/accounts/refresh-plan")
async def compat_refresh_plan2(request: Request) -> Dict[str, Any]:
    return {"enabled": False, "last_run": None, "next_run": None, "items": []}




def _platform_rows(request: Request, agg) -> tuple[str, list]:
    """按 `X-QTG-Platform` 过滤账号行；header 缺失时返回全部（旧调用方兼容）。

    批量动作（刷新积分 / 刷新令牌 / 签到 / 计划执行）必须走这里：否则在 Qoder
    平台点「刷新积分」会把 Trae 的账号也刷一遍——这正是「还是两个一起签到」的根因。
    """
    plat = (request.headers.get("x-qtg-platform") or "").strip().lower()
    if plat == "qoder":
        return plat, list(agg.qoder_accounts)
    if plat == "trae":
        return plat, list(agg.trae_accounts)
    if plat == "workbuddy":
        return plat, list(agg.wb_accounts)
    return "", all_accounts(agg)


def _res_ok(res) -> bool:
    """上游写操作的成败判断。

    trae/aiwork 返回 `{"ok": bool}`，qoder2 返回 `{"ok": bool}` 或空 dict。
    上游没明确表态时按「成功」处理，保持旧行为；一旦它说了 ok:false，
    就必须如实返回失败——否则界面会显示一个没发生的成功。
    """
    if not isinstance(res, dict):
        return False
    if "ok" in res:
        return bool(res["ok"])
    if "success" in res:
        return bool(res["success"])
    return True


def _adapter_for(rt, platform: str):
    """按平台取适配器。返回 None 表示该平台未启用（如 QTG_WB_ENGINE=off）。"""
    if platform == "qoder":
        return rt.qoder
    if platform == "trae":
        return rt.trae
    if platform == "workbuddy":
        return getattr(rt, "wb", None)
    return None


def _split_file(file: str) -> tuple[str, str]:
    """`platform:uid` 拆成 (platform, uid)。"""
    platform, _, uid = file.partition(":")
    return platform or "qoder", uid


@router.get("/accounts/{file:path}")
async def compat_account_detail(request: Request, file: str) -> Dict[str, Any]:
    rt = _rt(request)
    agg = await build_aggregate(rt)
    for a in all_accounts(agg):
        if f"{a.platform}:{a.uid}" == file:
            return _map_account(rt, a)
    return {}



@router.post("/accounts/{file:path}/checkin")
async def compat_account_checkin(request: Request, file: str) -> Dict[str, Any]:
    rt = _rt(request)
    platform, uid = _split_file(file)
    if platform == "qoder":
        res = await rt.qoder.checkin()
        # 上游的 checkin 是**整池**动作（旧 BFF 只有全量签到接口），这里必须按 uid
        # 过滤出本账号的结果。过滤不到时**不能回退到 results[0]** —— 那会把别的号
        # 的签到结果当成这个号的（审计点名的问题）。
        rows = [r for r in (res.get("results") or []) if str(r.get("uid")) == uid]
        if not rows:
            return {
                "code": 0,
                "ok": False,
                "message": "上游未返回该账号的签到结果（可能今日已签或 uid 不匹配）",
                "detail": {},
            }
        hit = rows[0]
        return {"code": 1 if hit.get("ok") else 0, "ok": bool(hit.get("ok")),
                "message": f"签到{'成功' if hit.get('ok') else '失败'}", "detail": hit}
    if platform == "workbuddy":
        w = getattr(rt, "wb", None)
        if w is None:
            return {"code": 0, "ok": False, "already": False,
                    "message": "workbuddy 平台未启用（QTG_WB_ENGINE=off）", "detail": {}}
        rows = _batch_results(await w.checkin())
        hit = rows[0] if rows else {"ok": False, "message": "上游未返回签到结果"}
        return {"code": 0, "ok": False, "already": bool(hit.get("already")),
                "message": hit.get("message") or "", "detail": hit}
    res = await rt.trae.checkin()
    rows = _batch_results(res)
    if not rows:
        return {"code": 0, "ok": False, "message": "上游未返回签到结果", "detail": {}}
    hit = rows[0]
    return {"code": 1 if hit.get("ok") else 0, "ok": bool(hit.get("ok")),
            "already": bool(hit.get("already")), "message": hit.get("message") or "",
            "detail": hit}



@router.post("/accounts/{file:path}/credits")
async def compat_account_credits(request: Request, file: str) -> Dict[str, Any]:
    rt = _rt(request)
    platform, uid = _split_file(file)
    agg = await build_aggregate(rt, force=True)
    for a in all_accounts(agg):
        if a.platform == platform and a.uid == uid:
            return {"ok": True, "credits": a.credits_remain, "cached": False, "message": ""}
    return {"ok": False, "credits": None, "cached": False, "message": "未找到该账号"}



@router.post("/accounts/{file:path}/refresh")
async def compat_account_refresh(request: Request, file: str) -> Dict[str, Any]:
    rt = _rt(request)
    platform, uid = _split_file(file)
    adapter = _adapter_for(rt, platform)
    if adapter is None:
        return {"ok": False, "message": f"{platform} 平台未启用", "detail": {}}
    res = await adapter.refresh_account(uid)
    # 不能恒回 ok:true —— aiwork 的 `refresh_jwt` 在「JWT 还没到期」时会返回
    # ok:false + 原因，前端要能把这句原文显示出来（审计点名的「恒回 OK」问题）。
    ok = bool(res.get("ok", True)) if isinstance(res, dict) else True
    message = "已刷新" if ok else str((res or {}).get("error") or "上游未刷新")
    return {"ok": ok, "message": message, "detail": res}



@router.put("/accounts/{file:path}/disabled")
async def compat_account_disabled(request: Request, file: str) -> Dict[str, Any]:
    rt = _rt(request)
    platform, uid = _split_file(file)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    disabled = bool(body.get("disabled"))
    adapter = _adapter_for(rt, platform)
    if adapter is None:
        return {"success": False, "disabled": not disabled,
                "message": f"{platform} 平台未启用（QTG_WB_ENGINE=off）"}
    res = await adapter.set_account(uid, not disabled)
    if not _res_ok(res):
        msg = str((res or {}).get("error") or (res or {}).get("message")
                  or "上游未接受这次变更")
        return {"success": False, "disabled": not disabled, "message": msg}
    return {"success": True, "disabled": disabled}



@router.delete("/accounts/{file:path}")
async def compat_account_delete(request: Request, file: str) -> Dict[str, Any]:
    return {"success": False, "message": "上游 BFF 未提供删除账号接口；请在 Qoder/Trae 面板的账号页操作"}





@router.post("/playground/chat")
async def compat_playground_chat(request: Request):
    """测试台对话：用管理端登录态直接转发到上游（不走数据面密钥），SSE 原样透传。"""
    rt = _rt(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    model = str(body.get("model") or "")
    messages = body.get("messages") or []

    from .proxy import _base, _catalog, _upstream_key

    hdr = (request.headers.get("x-qtg-platform") or "").strip().lower()
    platform, upstream_model = (hdr if hdr in ("qoder", "trae") else "qoder"), model
    if "/" in model:
        head, _, rest = model.partition("/")
        if head in ("qoder", "trae") and rest:
            platform, upstream_model = head, rest
    else:
        cat = await _catalog(rt)
        for plat, ids in (cat or {}).items():
            if model in ids:
                platform = plat
                break

    token = _upstream_key(rt, platform)
    if not token:
        from ..errors import ConfigMissing

        raise ConfigMissing(
            f"未配置 {platform} 上游 API Key：请在「密钥 → 上游凭据」填写", code="missing_upstream_key"
        )

    payload = {
        "model": upstream_model,
        "messages": messages,
        "stream": True,
    }
    client = httpx.AsyncClient(timeout=rt.settings.proxy_timeout_sec)
    req = client.build_request(
        "POST",
        _base(rt, platform) + "/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
    )
    resp = await client.send(req, stream=True)
    if resp.status_code >= 400:
        err = await resp.aread()
        await resp.aclose()
        await client.aclose()
        return Response(content=err, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))

    async def gen():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass
            await client.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



# ------------------------------------------------------------------ 日志 / 统计动作
@router.post("/logs/clear")
async def compat_logs_clear(request: Request) -> Dict[str, Any]:
    rt = _rt(request)
    try:
        n = rt.store.prune(0)
    except Exception:  # noqa: BLE001
        n = 0
    return {"ok": True, "cleared": n, "message": f"已清理 {n} 条请求日志"}


@router.post("/stats/repair-usage")
async def compat_stats_repair_usage(request: Request) -> Dict[str, Any]:
    """按请求日志修复用量统计。

    qtg 的用量统计是**实时聚合**（每次读取都从 proxy_requests 现算），
    不存在会漂移的汇总表，因此 repaired 恒为 0，前端会提示「无需修复」。
    """
    rt = _rt(request)
    st = rt.store.stats(3650)
    return {
        "repaired": 0,
        "requests": int(st.get("requests") or 0),
        "tokens": int(st.get("tokens") or st.get("total_tokens") or 0),
    }


@router.post("/stats/rebuild-usage")
async def compat_stats_rebuild_usage(request: Request) -> Dict[str, Any]:
    rt = _rt(request)
    st = rt.store.stats(3650)
    n = int(st.get("requests") or 0)
    return {"rows_before": n, "rows_after": n, "requests_delta": 0, "tokens_delta": 0}


@router.post("/stats/repair")
async def compat_stats_repair_legacy(request: Request) -> Dict[str, Any]:
    return await compat_stats_repair_usage(request)


@router.post("/stats/rebuild")
async def compat_stats_rebuild_legacy(request: Request) -> Dict[str, Any]:
    return await compat_stats_rebuild_usage(request)


# ------------------------------------------------------------------ 上游重启（明确不支持）




# ------------------------------------------------------------------ 数据面密钥写操作
async def _body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _key_row(rt: Any, index: Any) -> Optional[Dict[str, Any]]:
    """按密钥 **id** 取（不再按列表序号）。

    历史坑（2026-10-22 事故）：旧实现是 `keys[index - 1]`，而 `list_keys()` 按
    `created_at DESC` 排序 —— 删掉一个之后后面的整体前移，连续删两次就会删到
    没打算删的那一个。现在优先按密钥自身 id 精确匹配；纯数字入参仍按序号兼容
    旧前端，但只在新列表里存在该项时才生效。
    """
    keys = rt.store.list_keys()
    token = str(index)
    for k in keys:
        if str(k.get("id")) == token:
            return k
    if token.isdigit():
        i = int(token)
        if 1 <= i <= len(keys):
            return keys[i - 1]
    return None


def _key_out(rt: Any, index: int, k: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": k.get("id") or (index + 1),
        "seq": index + 1,
        "name": k.get("name") or "",
        "prefix": (k.get("masked") or "")[:12],
        "enabled": bool(k.get("enabled")),
        # 有效期现在是真的（库里有 expires_at 列，数据面会校验）；老密钥是 None
        "expires_at": k.get("expires_at") or None,
        "max_ips": k.get("max_ips") or 0,
        "ip_allowlist": k.get("ip_allowlist") or [],
        "models": k.get("models") or [],
        "realm": "",
        "quota": None,
        "used_tokens": 0,
        "quota_credit": 0,
        "used_credit": 0,
        "created_at": k.get("created_at", ""),
        "daily_limit": int(k.get("daily_limit") or 0),
        "monthly_limit": int(k.get("monthly_limit") or 0),
        "max_concurrency": int(k.get("max_concurrency") or 0),
        "qoder_key_id": k.get("id"),
        "upstream_id": None,
    }












# ------------------------------------------------------------------ 账号批量动作（结构对齐前端）
def _batch_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in (payload.get("results") or []):
        if not isinstance(r, dict):
            continue
        status = str(r.get("status") or "").lower()
        # already 的判定：trae 看 checkin_results.status；qoder2 上游没有 status，
        # 但给了 earned_credit / claimed —— 赚到 0 且没领取就是「今日已领」。
        already = status == "already" or bool(r.get("already"))
        if not already and status != "success":
            earned = r.get("earned_credit")
            if bool(r.get("ok")) and earned is not None and float(earned or 0) == 0:
                already = True
        out.append({
            "nickname": str(r.get("nickname") or r.get("name") or r.get("uid") or ""),
            "uid": str(r.get("uid") or ""),
            "ok": bool(r.get("ok")),
            "message": str(r.get("message") or r.get("msg") or ""),
            "code": r.get("code"),
            "status": status,
            "already": already,
            "earned_credit": r.get("earned_credit"),
            "next_available_at": r.get("next_available_at"),
            "next_available_note": r.get("next_available_note"),
        })
    return out


@router.post("/accounts/checkin-all")
async def compat_checkin_all(request: Request) -> Dict[str, Any]:
    rt = _rt(request)
    plat = (request.headers.get("x-qtg-platform") or "").strip().lower()
    results: List[Dict[str, Any]] = []
    if plat in ("", "qoder"):
        try:
            results += _batch_results(await rt.qoder.checkin())
        except Exception as exc:  # noqa: BLE001
            results.append({"nickname": "Qoder", "ok": False, "message": str(exc)[:160], "code": None})
    if plat in ("", "trae"):
        try:
            results += _batch_results(await rt.trae.checkin())
        except Exception as exc:  # noqa: BLE001
            results.append({"nickname": "Trae", "ok": False, "message": str(exc)[:160], "code": None})
    if plat == "workbuddy":
        w = getattr(rt, "wb", None)
        if w is None:
            results.append({"nickname": "WorkBuddy", "ok": False,
                            "message": "workbuddy 平台未启用（QTG_WB_ENGINE=off）", "code": None})
        else:
            try:
                results += _batch_results(await w.checkin())
            except Exception as exc:  # noqa: BLE001
                results.append({"nickname": "WorkBuddy", "ok": False,
                                "message": str(exc)[:160], "code": None})
    # 前端契约（lib/api.ts checkinAll）：total 只算「本次真正发起签到」的账号，
    # 今天已签的进 already、不适用的进 skipped —— 它们既不算成功也不算失败。
    already_rows = [r for r in results if r.get("already")]
    fresh_rows = [r for r in results if not r.get("already")]
    ok = len([r for r in fresh_rows if r["ok"]])
    failed = len(fresh_rows) - ok
    if not fresh_rows and already_rows:
        msg = f"今日已签到（{len(already_rows)} 个账号），无需重复签到"
        note = next((str(r.get("next_available_note") or "") for r in already_rows
                     if r.get("next_available_note")), "")
        if note:
            msg += f"；下次可签：{note}"
    else:
        msg = f"签到完成：新签成功 {ok} / {len(fresh_rows)}"
        if already_rows:
            msg += f"，今日已签 {len(already_rows)}"
        if failed:
            msg += f"，失败 {failed}"
    return {"total": len(fresh_rows), "succeeded": ok, "already": len(already_rows),
            "skipped": 0, "results": results, "message": msg}


@router.post("/accounts/refresh-tokens")
async def compat_refresh_tokens(request: Request) -> Dict[str, Any]:
    rt = _rt(request)
    agg = await build_aggregate(rt)
    plat, rows = _platform_rows(request, agg)
    results: List[Dict[str, Any]] = []
    failed: List[str] = []
    for a in rows:
        file = f"{a.platform}:{a.uid}"
        adapter = rt.trae if a.platform == "trae" else rt.qoder
        try:
            await adapter.refresh_account(a.uid)
            results.append({"file": file, "nickname": a.name or a.uid, "uid": a.uid,
                            "ok": True, "message": "已刷新"})
        except Exception as exc:  # noqa: BLE001
            results.append({"file": file, "nickname": a.name or a.uid, "uid": a.uid,
                            "ok": False, "message": str(exc)[:160]})
            failed.append(file)
    ok = len([r for r in results if r["ok"]])
    return {"total": len(results), "succeeded": ok, "failed": failed,
            "results": results, "reload_triggered": False}


@router.post("/accounts/refresh-credits")
async def compat_refresh_credits(request: Request, force: str = Query(default="true")) -> Dict[str, Any]:
    rt = _rt(request)
    agg = await build_aggregate(rt, force=True)
    plat, rows = _platform_rows(request, agg)
    credits: Dict[str, Optional[float]] = {}
    failed: List[str] = []
    for a in rows:
        if a.credits_remain is None:
            failed.append(f"{a.platform}:{a.uid}")
        else:
            credits[a.uid] = a.credits_remain
    return {
        "total": len(rows),
        "succeeded": len(credits),
        "credits": credits,
        "meta": {},
        "failed": failed,
    }


@router.get("/accounts/export")
async def compat_accounts_export(request: Request) -> Response:
    """导出账号清单（**不含任何凭据**，只给运维对账用）。"""
    import json as _json

    rt = _rt(request)
    agg = await build_aggregate(rt)
    payload = [
        {"platform": a.platform, "uid": a.uid, "name": a.name, "realm": a.realm,
         "state": a.state, "credits": a.credits_remain, "expires_iso": a.expires_iso}
        for a in all_accounts(agg)
    ]
    return Response(
        content=_json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="accounts.json"'},
    )


@router.post("/accounts/upload")
async def compat_accounts_upload(request: Request) -> Dict[str, Any]:
    return {
        "ok": False,
        "uploaded": [], "added": [], "overwritten": [],
        "rejected": [], "failed": [],
        "message": "账号导入需通过「添加账号」的 OAuth 授权流程；文件导入暂未开放",
    }


# ------------------------------------------------------------------ 计划任务（保存 + 立即执行）
def _plans_path(rt: Any):
    from pathlib import Path

    return Path(rt.settings.data_dir) / "compat-plans.json"


def _read_plans(rt: Any) -> Dict[str, Any]:
    import json as _json

    try:
        data = _json.loads(_plans_path(rt).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_plans(rt: Any, data: Dict[str, Any]) -> None:
    import json as _json
    import os

    p = _plans_path(rt)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


@router.put("/accounts/refresh-plan")
async def compat_refresh_plan_save(request: Request) -> Dict[str, Any]:
    rt = _rt(request)
    body = await _body(request)
    plans = _read_plans(rt)
    cur = plans.get("refresh") or {}
    enabled = bool(body.get("enabled", cur.get("enabled", False)))
    interval = int(body.get("interval_days") or cur.get("interval_days") or 7)
    hour = int(body["hour"]) if body.get("hour") is not None else int(cur.get("hour") or 3)
    cur = {"enabled": enabled, "interval_days": interval, "hour": hour,
           "last_run": cur.get("last_run"), "next_run": cur.get("next_run")}
    plans["refresh"] = cur
    _write_plans(rt, plans)
    return {
        "enabled": enabled, "interval_days": interval, "hour": hour,
        "last_run": cur.get("last_run"), "next_run": cur.get("next_run"), "items": [],
    }


@router.post("/accounts/refresh-plan/run")
async def compat_refresh_plan_run(request: Request) -> Dict[str, Any]:
    rt = _rt(request)
    agg = await build_aggregate(rt)
    plat, rows = _platform_rows(request, agg)
    checked = renewed = failed_n = 0
    for a in rows:
        checked += 1
        adapter = rt.trae if a.platform == "trae" else rt.qoder
        try:
            await adapter.refresh_account(a.uid)
            renewed += 1
        except Exception:  # noqa: BLE001
            failed_n += 1
    now = int(time.time())
    plans = _read_plans(rt)
    rp = plans.setdefault("refresh", {})
    rp["last_run"] = now
    rp["next_run"] = (now + int(rp.get("interval_days") or 7) * 86400
                      if rp.get("enabled") else None)
    _write_plans(rt, plans)
    return {"checked": checked, "renewed": renewed, "failed": failed_n, "skipped": 0,
            "last_run": now, "next_run": rp.get("next_run")}


# ------------------------------------------------------------------ 任务 / 签到日志




@router.post("/checkin-logs/clear")
async def compat_checkin_logs_clear(request: Request) -> Dict[str, Any]:
    return JSONResponse(
        status_code=409,
        content={"detail": "签到记录来自上游，本端没有可删除的存储，因此无法清空"},
    )


@router.put("/accounts/daily-plan")
async def compat_daily_plan_save(request: Request) -> Dict[str, Any]:
    rt = _rt(request)
    body = await _body(request)
    plans = _read_plans(rt)
    saved: Dict[str, Any] = {}
    for key in ("checkin", "compensation", "claim", "full"):
        cur = (plans.get("daily") or {}).get(key) or {}
        item = body.get(key) if isinstance(body.get(key), dict) else {}
        saved[key] = {
            "enabled": bool(item.get("enabled", cur.get("enabled", False))),
            "hour": int(item.get("hour", cur.get("hour", 21)) or 21),
            "last_run": str(cur.get("last_run") or ""),
        }
    plans["daily"] = saved
    _write_plans(rt, plans)
    return saved


@router.post("/accounts/daily-plan/run")
async def compat_daily_plan_run(request: Request) -> Dict[str, Any]:
    rt = _rt(request)
    body = await _body(request)
    task = str(body.get("task") or "checkin")
    plans = _read_plans(rt)
    daily = plans.setdefault("daily", {})
    if task == "checkin":
        res = await compat_checkin_all(request)
        ok, msg = True, str(res.get("message") or "签到完成")
    elif task == "claim":
        try:
            await rt.trae.checkin()   # 上游把签到与领奖合并在同一个动作里
            ok, msg = True, "已向上游发起领奖（Trae 签到动作内含领奖）"
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"领奖失败：{str(exc)[:120]}"
    else:
        ok = False
        msg = ("本部署不执行该动作：它需要向上游发送活跃上报，有风控风险"
               "（见 HANDOVER 第 5 节 P1）")
    item = daily.setdefault(task, {"enabled": False, "hour": 21, "last_run": ""})
    item["last_run"] = time.strftime("%Y-%m-%d-%H")
    _write_plans(rt, plans)
    plan: Dict[str, Any] = {}
    for key in ("checkin", "compensation", "claim", "full"):
        c = daily.get(key) or {"enabled": False, "hour": 21, "last_run": ""}
        plan[key] = {"enabled": bool(c.get("enabled")), "hour": int(c.get("hour") or 21),
                     "last_run": str(c.get("last_run") or "")}
    return {"ok": ok, "message": msg, "task": task, "plan": plan}





# ------------------------------------------------------------------ 兜底
# 未实现的 GET 以前一律 `return {}`，前端把「没实现」渲染成「暂无数据」——
# 用户分不清是「真的没有」还是「这个功能没做」（审计点名的首要工程问题）。
# 现在如实返回 501 + `available:false`，前端的错误态会显示这句话。
