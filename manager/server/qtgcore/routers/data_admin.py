"""数据面管理：qtg API Key 的增删改查、请求流水与统计、上游凭据配置。

与数据面（proxy.py）分离：这里全部要求管理凭证，属于控制面。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, Field

from ..cache import cache
from ..models import ActionResult

router = APIRouter(tags=["data-admin"])

Platform = Literal["qoder", "trae"]


def _rt(request: Request):
    return request.app.state.rt


async def require_admin(request: Request) -> str:
    """写操作：必须是管理凭证。"""
    rt = _rt(request)
    actor = await rt.auth.require_admin(request)
    rt.auth.check_origin(request)
    return actor


async def require_read(request: Request) -> str:
    """读操作：与 /api/v1/* 其它读端点保持一致。

    之前这里错用了 require_admin，导致「会话 cookie 一失效，只有流水页 401、
    其它页照常」，用户表现为「打不开」。读端点的鉴权策略必须全站统一。
    """
    return await _rt(request).auth.require_read(request)


def _audit(request: Request, action: str, target: str, result: str = "ok", detail: Optional[dict] = None) -> None:
    rt = _rt(request)
    rt.record_audit(
        action=action, actor="admin", target=target, result=result, detail=detail or {},
        ip=request.headers.get("x-real-ip") or (request.client.host if request.client else ""),
    )


# ---------------------------------------------------------------- 读
@router.get("/api/v1/qtg-keys")
async def list_qtg_keys(request: Request, _: str = Depends(require_read)) -> dict:
    rt = _rt(request)
    return {"keys": rt.store.list_keys(), "auth_hint": "客户端使用 Authorization: Bearer sk-qtg-..."}


@router.get("/api/v1/proxy-requests")
async def list_proxy_requests(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    platform: str = Query(default=""),
    key_id: str = Query(default=""),
    _: str = Depends(require_read),
) -> dict:
    rt = _rt(request)
    return {"requests": rt.store.list_requests(limit, platform=platform, key_id=key_id)}


@router.get("/api/v1/proxy-stats")
async def proxy_stats(request: Request, days: int = Query(default=1, ge=1, le=90), _: str = Depends(require_read)) -> dict:
    rt = _rt(request)
    return rt.store.stats(days)


@router.get("/api/v1/data-plane")
async def data_plane_status(request: Request, _: str = Depends(require_read)) -> dict:
    """数据面自检：是否启用、两枚上游 key 是否就绪、模型数。"""
    rt = _rt(request)
    from .proxy import _catalog, _upstream_key

    cat = await _catalog(rt)
    return {
        "enabled": rt.settings.proxy_enabled,
        "base_url": f"http://{rt.settings.host}:{rt.settings.port}/v1",
        "upstreams": {
            p: {
                "configured": bool(_upstream_key(rt, p)),
                "models": len(cat.get(p) or []),
                "key_file": rt.settings.qoder_api_key_file if p == "qoder" else rt.settings.trae_api_key_file,
            }
            for p in ("qoder", "trae")
        },
        "keys": len(rt.store.list_keys()),
        "requests_total": rt.store.stats(3650)["requests"],
    }


# ---------------------------------------------------------------- 写
class CreateKeyRequest(BaseModel):
    name: str = Field(default="", max_length=64)
    platforms: List[Platform] = Field(default_factory=lambda: ["qoder", "trae"])
    models: List[str] = Field(default_factory=list)
    daily_limit: int = Field(default=0, ge=0)
    monthly_limit: int = Field(default=0, ge=0)
    max_concurrency: int = Field(default=0, ge=0, le=64)
    note: str = Field(default="", max_length=200)


class UpstreamKeyRequest(BaseModel):
    platform: Platform
    key: str = Field(min_length=1, max_length=4096)


@router.post("/api/v1/actions/qtg-keys", response_model=ActionResult)
async def create_qtg_key(request: Request, body: CreateKeyRequest, _: str = Depends(require_admin)) -> ActionResult:
    rt = _rt(request)
    created = rt.store.create_key(
        body.name, platforms=list(body.platforms), models=body.models,
        daily_limit=body.daily_limit, monthly_limit=body.monthly_limit,
        max_concurrency=body.max_concurrency, note=body.note,
    )
    _audit(request, "qtg_key_create", created["id"], detail={"name": body.name, "platforms": body.platforms})
    return ActionResult(
        ok=True, action="qtg_key_create", target=created["id"], message="密钥已创建（只显示一次）",
        data=created,
    )


@router.post("/api/v1/actions/qtg-keys/{kid}/{op}", response_model=ActionResult)
async def mutate_qtg_key(
    request: Request, kid: str, op: Literal["enable", "disable", "delete"], _: str = Depends(require_admin)
) -> ActionResult:
    rt = _rt(request)
    if op == "delete":
        ok = rt.store.delete_key(kid)
    else:
        ok = rt.store.set_key_enabled(kid, op == "enable")
    _audit(request, f"qtg_key_{op}", kid, result="ok" if ok else "not_found")
    return ActionResult(ok=ok, action=f"qtg_key_{op}", target=kid,
                        message="已执行" if ok else "未找到该密钥")


@router.post("/api/v1/actions/upstream-keys", response_model=ActionResult)
async def save_upstream_key(request: Request, body: UpstreamKeyRequest, _: str = Depends(require_admin)) -> ActionResult:
    rt = _rt(request)
    path = Path(rt.settings.qoder_api_key_file if body.platform == "qoder" else rt.settings.trae_api_key_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body.key.strip(), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        _audit(request, "upstream_key", body.platform, "error", {"error": str(exc)[:200]})
        return ActionResult(ok=False, action="upstream_key", target=body.platform,
                            message=f"写入失败：{exc}")
    cache.invalidate("proxy.")
    _audit(request, "upstream_key", body.platform, detail={"length": len(body.key)})
    return ActionResult(ok=True, action="upstream_key", target=body.platform,
                        message=f"{body.platform} 上游密钥已保存（0600）")


@router.post("/api/v1/actions/proxy-prune", response_model=ActionResult)
async def prune_requests(request: Request, keep_days: int = Query(default=30, ge=1, le=365), _: str = Depends(require_admin)) -> ActionResult:
    rt = _rt(request)
    n = rt.store.prune(keep_days)
    _audit(request, "proxy_prune", f"keep={keep_days}", detail={"deleted": n})
    return ActionResult(ok=True, action="proxy_prune", target=str(keep_days),
                        message=f"已清理 {n} 条流水", data={"deleted": n})


# ------------------------------------------------------------------ 自描述
@router.get("/api/v1/meta")
async def meta(request: Request) -> Dict[str, Any]:
    """自描述：版本 + 分组端点清单（API.md 契约）。"""
    from .. import __version__

    return {
        "version": __version__,
        "endpoints": ["read", "actions", "proxy", "data_admin", "system", "compat"],
    }
