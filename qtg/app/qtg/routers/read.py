"""只读端点（统一契约 `/api/v1/*`）。

全部共享 `build_aggregate()` 的同一份归一化快照，因此：
* 页面上 5 个卡片和 8 个标签页看到的是同一次上游采样，不会互相矛盾；
* 上游调用量与页面端点数解耦（旧实现是端点 × 5 次上游调用）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from .. import metrics
from ..models import (
    AccountsResponse,
    AlertsResponse,
    AuditResponse,
    KeysResponse,
    LogsResponse,
    ModelsResponse,
    OpsResponse,
    OverviewResponse,
    RequestsResponse,
    SettingsResponse,
    TasksResponse,
    UsageResponse,
)
from ..service import (
    accounts_payload,
    alerts_payload,
    build_aggregate,
    keys_payload,
    logs_payload,
    models_payload,
    ops_payload,
    overview_payload,
    random_state,
    requests_payload,
    settings_payload,
    systemd_timers,
    tasks_payload,
    usage_payload,
)

router = APIRouter(prefix="/api/v1", tags=["read"])


def _rt(request: Request):
    return request.app.state.rt


@router.get("/overview", response_model=OverviewResponse)
async def overview(request: Request, refresh: bool = Query(default=False)) -> OverviewResponse:
    rt = _rt(request)
    agg = await build_aggregate(rt, force=refresh)
    return overview_payload(agg, systemd_timers(), random_state())


@router.get("/accounts", response_model=AccountsResponse)
async def accounts(request: Request, refresh: bool = Query(default=False)) -> AccountsResponse:
    rt = _rt(request)
    agg = await build_aggregate(rt, force=refresh)
    return accounts_payload(agg)


@router.get("/usage", response_model=UsageResponse)
async def usage(request: Request, days: int = Query(default=14, ge=1, le=90)) -> UsageResponse:
    rt = _rt(request)
    agg = await build_aggregate(rt)
    return usage_payload(agg, days)


@router.get("/tasks", response_model=TasksResponse)
async def tasks(request: Request) -> TasksResponse:
    rt = _rt(request)
    agg = await build_aggregate(rt)
    return tasks_payload(agg, systemd_timers())


@router.get("/keys", response_model=KeysResponse)
async def keys(request: Request) -> KeysResponse:
    rt = _rt(request)
    qs = await rt.qoder.settings()
    tk = await rt.trae.keys()
    return keys_payload(qs, tk)


@router.get("/models", response_model=ModelsResponse)
async def models(request: Request) -> ModelsResponse:
    rt = _rt(request)
    from ..cache import cache

    qm = await cache.get_or_set("qoder.models", rt.settings.cache_models_sec, rt.qoder.models)
    tm = await cache.get_or_set("trae.models", rt.settings.cache_models_sec, rt.trae.models)
    return models_payload(qm or {}, tm or {})


@router.get("/logs", response_model=LogsResponse)
async def logs(request: Request) -> LogsResponse:
    rt = _rt(request)
    ql = await rt.qoder.logs()
    tl = await rt.trae.logs()
    return logs_payload(ql or {}, tl or {})


@router.get("/alerts", response_model=AlertsResponse)
async def alerts(request: Request) -> AlertsResponse:
    rt = _rt(request)
    qa = await rt.qoder.alerts()
    ts = await rt.trae.settings()
    return alerts_payload(qa or {}, ts or {})


@router.get("/requests", response_model=RequestsResponse)
async def requests_rows(
    request: Request, days: int = Query(default=3, ge=1, le=30)
) -> RequestsResponse:
    rt = _rt(request)
    qu = await rt.qoder.usage(days)
    tu = await rt.trae.usage(days)
    return requests_payload(qu or {}, tu or {}, days)


@router.get("/ops", response_model=OpsResponse)
async def ops(request: Request) -> OpsResponse:
    rt = _rt(request)
    from pathlib import Path

    audit_meta = {
        "records": rt.audit.count(),
        "integrity": rt.audit.verify(),
    }
    return ops_payload(
        rt.settings,
        Path(rt.settings.limits_file),
        Path(rt.settings.notify_config_file),
        {
            "trae": rt.settings.backup_glob_trae,
            "qoder": rt.settings.backup_glob_qoder,
        },
        audit_meta,
    )


@router.get("/settings", response_model=SettingsResponse)
async def settings(request: Request) -> SettingsResponse:
    rt = _rt(request)
    qs = await rt.qoder.settings()
    ts = await rt.trae.settings()
    qtg = {
        "version": __import__("qtg").__version__,
        "read_requires_auth": rt.settings.read_requires_auth,
        "cache": _cache_stats(),
        "upstreams": {"qoder": rt.settings.qoder_base, "trae": rt.settings.trae_base},
        "data_dir": rt.settings.data_dir,
        "features": [
            "unified-contract",
            "session-auth",
            "hmac-audit-chain",
            "prometheus-metrics",
            "single-flight-cache",
            "circuit-breaker",
        ],
    }
    return settings_payload(qs or {}, ts or {}, qtg)


@router.get("/audit", response_model=AuditResponse)
async def audit(request: Request, limit: int = Query(default=100, ge=1, le=1000)) -> AuditResponse:
    rt = _rt(request)
    return AuditResponse(records=rt.audit.tail(limit), integrity=rt.audit.verify())


def _cache_stats() -> dict:
    from ..cache import cache

    return cache.stats()


@router.post("/cache/invalidate")
async def invalidate_cache(request: Request, prefix: str = Query(default="")) -> dict:
    from ..cache import cache

    rt = _rt(request)
    await rt.auth.require_admin(request)
    n = cache.invalidate(prefix)
    metrics.inc("qtg_actions_total", {"action": "cache_invalidate", "result": "ok"})
    return {"ok": True, "invalidated": n}
