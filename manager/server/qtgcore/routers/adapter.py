"""Manager 专用适配器契约（U3）：把引擎侧真实数据以稳定结构暴露给统一控制面。

## 为什么需要这一层

QTG 的 `compat_*` 路由是给**旧前端与上游 BFF** 用的形状（WorkBuddy 的
`StatsSummary` / `UsagePoint` 等）。Manager 要成为唯一入口，不能去消费这些
前端形状，也不该反过来被 QTG 的前端形状牵着走。所以这里定义一份**只给
Manager 用**的契约：

* 版本号 + 能力声明先行（`/api/v1/adapter/contract`），调用方按声明决定能否用；
* 每族数据一个端点，结构扁平、字段含义固定（平台 / 计数 / 口径）；
* 只读：本契约不提供任何写操作，写仍走 Manager 自己的权威路径。

## 鉴权

`ROLE_INTERNAL`（`X-QTG-Internal-Auth` 常量时间比对）或 admin。不允许匿名，
也不接受会话 Cookie——机器到机器的调用必须显式携带内部令牌。

## 数据来源

全部复用 `service.build_aggregate()` 已经聚合好的引擎快照（qoder2 / trae /
workbuddy BFF），不在本层重新探测上游，避免双倍打上游。
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Query, Request

from ..service import all_accounts, build_aggregate, health_payload, usage_payload

router = APIRouter(prefix="/api/v1/adapter", tags=["adapter"])

CONTRACT_VERSION = 1

#: 能力声明：调用方必须按此判断可用性，不可用时报错而不是猜。
CAPABILITIES: Dict[str, Any] = {
    "accounts": {"available": True, "unit": "per_platform", "pagination": False},
    "models": {"available": True, "unit": "per_platform"},
    "usage": {"available": True, "unit": "tokens_and_credit", "days_max": 90},
    "health": {"available": True, "unit": "upstream_probe"},
    "write_actions": {"available": False,
                      "note": "写操作仍由 Manager 权威路径处理，本契约只读"},
}


def _rt(request: Request):
    return request.app.state.rt


@router.get("/contract")
async def contract(request: Request) -> Dict[str, Any]:
    """契约版本与能力声明。调用方必须先取这一份再决定用哪些端点。"""
    return {
        "contract_version": CONTRACT_VERSION,
        "service": "qtg",
        "capabilities": CAPABILITIES,
        "auth": "internal_service_token_or_admin",
        "read_only": True,
        "data_source": "engine_aggregate",
    }


@router.get("/health")
async def health(request: Request) -> Dict[str, Any]:
    """上游可达性（与 /readyz 同源，但结构稳定、不含内部诊断细节）。"""
    agg = await build_aggregate(_rt(request))
    payload = health_payload(agg)
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": payload.generated_at,
        "overall": payload.overall,
        "upstreams": [{"name": h.name, "up": bool(h.up),
                       "latency_ms": getattr(h, "latency_ms", None)}
                      for h in payload.upstreams],
    }


@router.get("/accounts")
async def accounts(request: Request) -> Dict[str, Any]:
    """引擎侧账号池（含运行态），按平台分组。"""
    agg = await build_aggregate(_rt(request))
    grouped: Dict[str, List[Dict[str, Any]]] = {"qoder": [], "trae": [], "workbuddy": []}
    for account in all_accounts(agg):
        row = account.model_dump() if hasattr(account, "model_dump") else dict(account)
        platform = str(row.get("platform") or "")
        if platform not in grouped:
            grouped.setdefault(platform, [])
        grouped[platform].append({
            "uid": row.get("uid"),
            "name": row.get("name") or "",
            "realm": row.get("realm") or "",
            "enabled": bool(row.get("enabled", True)),
            "state": row.get("state") or "unknown",
            "healthy": (row.get("state") == "ready"),
            "credits_remain": row.get("credits_remain"),
            "expires_iso": row.get("expires_iso"),
            "days_left": row.get("days_left"),
            "checked_today": bool(row.get("checked_today")),
            "in_flight": row.get("in_flight"),
        })
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": getattr(agg, "generated_at", None),
        "platforms": grouped,
        "totals": {name: len(rows) for name, rows in grouped.items()},
    }


@router.get("/models")
async def models(request: Request) -> Dict[str, Any]:
    """各平台可用模型清单（引擎侧目录，非 Manager 白名单）。

    注意数据来源与其它端点不同：模型目录**不在** `build_aggregate()` 里，它由
    `compat_fixes._load_models()` 从各引擎的 `/v1/models` 现取（底层有缓存）。
    2026-10-26 的初版这里误用了 `getattr(agg, "<platform>_models")`，而 Aggregate
    没有这些字段，于是恒返回空数组——契约声明 available 却没有数据，是典型的
    "静默空"。修复后与 `/api/models` 走同一来源，避免两条路径给出不同答案。
    """
    from .compat_fixes import _load_models

    rt = _rt(request)
    loaded = await _load_models(rt)
    catalog: Dict[str, List[str]] = {}
    for platform in ("qoder", "trae", "workbuddy"):
        ids: List[str] = []
        for item in loaded.get(platform) or []:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("model") or item.get("name")
                if mid:
                    ids.append(str(mid))
        catalog[platform] = sorted(set(ids))
    body: Dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "models": catalog,
        "totals": {k: len(v) for k, v in catalog.items()},
    }
    # 上游取不到时如实带出原因，别让调用方把"引擎故障"读成"引擎上没有模型"。
    errors = loaded.get("errors") or []
    if errors:
        body["errors"] = errors
    return body


@router.get("/usage")
async def usage(request: Request, days: int = Query(default=14)) -> Dict[str, Any]:
    """引擎侧真实用量（token 与 credit），按平台给出总量与日桶。"""
    agg = await build_aggregate(_rt(request))
    window = max(1, min(90, days))
    payload = usage_payload(agg, window)
    raw = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
    platforms: Dict[str, Any] = {}
    for name in ("qoder", "trae", "workbuddy"):
        rows = raw.get(name) or raw.get(f"{name}_usage") or []
        total = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "credit": 0.0}
        points = []
        for point in rows:
            row = point.model_dump() if hasattr(point, "model_dump") else dict(point)
            requests = int(row.get("requests") or 0)
            prompt = int(row.get("prompt_tokens") or 0)
            completion = int(row.get("completion_tokens") or 0)
            credit = float(row.get("credit") or 0.0)
            total["requests"] += requests
            total["prompt_tokens"] += prompt
            total["completion_tokens"] += completion
            total["credit"] += credit
            points.append({"day": row.get("day") or row.get("date"),
                           "requests": requests, "prompt_tokens": prompt,
                           "completion_tokens": completion, "credit": credit})
        platforms[name] = {"total": total, "points": points}
    return {
        "contract_version": CONTRACT_VERSION,
        "days": window,
        "platforms": platforms,
        "note": "engine-side usage; Manager ledger totals are a separate authority",
    }
