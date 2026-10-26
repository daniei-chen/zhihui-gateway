"""健康检查与控制面端点：/healthz /readyz /metrics。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import metrics
from ..config import get_settings
from ..models import HealthResponse
from ..service import health_payload

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    """只证明进程活着（不做上游探测，避免探活风暴）。"""
    return {"ok": True, "service": "qtg", "role": "qoder-trae-control-plane"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """就绪探针：进程活着 + 上游可达性（带 30s 缓存，避免打爆上游）。"""
    rt = request.app.state.rt
    from ..service import build_aggregate

    try:
        agg = await build_aggregate(rt)
        payload = health_payload(agg)
        return JSONResponse({'ok': bool(payload.overall)},
                            status_code=200 if payload.overall else 503)
    except Exception:  # noqa: BLE001 - details belong to authenticated diagnostics
        return JSONResponse({'ok': False}, status_code=503)


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus() -> str:
    return metrics.render()


@router.get("/api/v1/health", response_model=HealthResponse)
async def health_detail(request: Request) -> HealthResponse:
    rt = request.app.state.rt
    from ..service import build_aggregate

    agg = await build_aggregate(rt)
    return health_payload(agg)
