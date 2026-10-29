"""Qoder / Trae 控制面（原独立服务 qtg 并入 manager 后的读端点）。

并入要点
--------
* 原来这些端点跑在独立进程 qtg(:8795)，用 qtg 自己的 admin_token / 旁路密码文件鉴权；
* 并入后**统一走 manager 的登录态**（security.current_user），不再有第二套凭据；
* 数据仍来自 qtgcore（adapters 直连 qoder2:8799 / trae2:8080），不复制账号数据；
* runtime 懒加载：qoder/trae 侧不可用只影响本组端点，绝不影响 workbuddy 反代。
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import security
from ..qtgcore import service as qservice
from ..qtgcore.config import load_settings
from ..qtgcore.runtime import Runtime

router = APIRouter(prefix='/api/v1', tags=['qtg-panel'])

_lock = threading.Lock()
_rt: Runtime | None = None
_rt_lock = None


async def get_rt() -> Runtime:
    """Lazily build and START the qtg runtime.

    The adapters wrap a shared httpx.AsyncClient and refuse to serve before
    `Runtime.startup()` runs ("adapter 尚未启动"), so the
    startup hook must be awaited exactly once.
    """
    global _rt, _rt_lock
    if _rt is not None:
        return _rt
    if _rt_lock is None:
        _rt_lock = asyncio.Lock()
    async with _rt_lock:
        if _rt is None:
            rt = Runtime(load_settings())
            await rt.startup()
            _rt = rt
    return _rt


async def _rt_or_503() -> Runtime:
    try:
        return await get_rt()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503,
                            detail=f'qoder/trae 控制面不可用：{exc}') from exc


def _panel_scope(request: Request, *, authority: str = 'upstream'):
    """面板端点的统一作用域（U17）。

    这些端点**不按平台取数**（聚合里三平台数据都在），所以客户端带
    `X-QTG-Platform` 时会被显式降级：`platform_honored=false` +
    `degraded=['platform_not_routable']`，而不是静默返回同一份数据。
    """
    from ..services.scope import for_request
    return for_request(request, authority=authority, platform_routed=False)


@router.get('/overview')
async def overview(refresh: bool = Query(default=False),
                   user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    agg = await qservice.build_aggregate(rt, force=refresh)
    return qservice.overview_payload(agg, qservice.systemd_timers(), qservice.random_state())


@router.get('/accounts')
async def accounts(refresh: bool = Query(default=False),
                   user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    agg = await qservice.build_aggregate(rt, force=refresh)
    return qservice.accounts_payload(agg)


@router.get('/usage')
async def usage(days: int = Query(default=14, ge=1, le=90),
                user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    agg = await qservice.build_aggregate(rt)
    return qservice.usage_payload(agg, days)


@router.get('/tasks')
async def tasks(user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    agg = await qservice.build_aggregate(rt)
    return qservice.tasks_payload(agg, qservice.systemd_timers())


@router.get('/keys')
async def keys(user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    qs = await rt.qoder.settings()
    tk = await rt.trae.keys()
    return qservice.keys_payload(qs or {}, tk or {})


@router.get('/models')
async def models(user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    from ..qtgcore.cache import cache
    from ..services.scope import fixed, scoped_key
    # U17：两个平台的模型目录**各自一个 key**，且 key 里显式带 platform 与
    # config_generation —— 平台之间不会互相覆盖，配置变更也不会命中旧目录。
    qm = await cache.get_or_set(
        scoped_key('qoder.models', fixed('qoder', authority='upstream')),
        rt.settings.cache_models_sec, rt.qoder.models)
    tm = await cache.get_or_set(
        scoped_key('trae.models', fixed('trae', authority='upstream')),
        rt.settings.cache_models_sec, rt.trae.models)
    return qservice.models_payload(qm or {}, tm or {})


@router.get('/logs')
async def logs(user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    ql = await rt.qoder.logs()
    tl = await rt.trae.logs()
    return qservice.logs_payload(ql or {}, tl or {})


@router.get('/alerts')
async def alerts(user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    qa = await rt.qoder.alerts()
    ts = await rt.trae.settings()
    return qservice.alerts_payload(qa or {}, ts or {})


@router.get('/requests')
async def requests_rows(days: int = Query(default=3, ge=1, le=30),
                        user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    qu = await rt.qoder.usage(days)
    tu = await rt.trae.usage(days)
    return qservice.requests_payload(qu or {}, tu or {}, days)


@router.get('/ops')
async def ops(user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    audit_meta = {'records': rt.audit.count(), 'integrity': rt.audit.verify()}
    return qservice.ops_payload(
        rt.settings,
        Path(rt.settings.limits_file),
        Path(rt.settings.notify_config_file),
        {'trae': rt.settings.backup_glob_trae, 'qoder': rt.settings.backup_glob_qoder},
        audit_meta,
    )


@router.get('/settings')
async def settings(request: Request,
                   user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    qs = await rt.qoder.settings()
    ts = await rt.trae.settings()
    from ..qtgcore import __version__ as qtgcore_version
    from ..qtgcore.cache import cache
    meta = {
        # U17：面板的作用域元数据（四个维度 + 请求的平台是否被采纳）。
        'scope': _panel_scope(request, authority='upstream').metadata(),
        'version': qtgcore_version,
        'read_requires_auth': rt.settings.read_requires_auth,
        'cache': cache.stats(),
        'upstreams': {'qoder': rt.settings.qoder_base, 'trae': rt.settings.trae_base},
        'data_dir': str(rt.settings.data_dir),
        'features': ['unified-contract', 'manager-session-auth', 'hmac-audit-chain',
                     'prometheus-metrics', 'single-flight-cache', 'circuit-breaker'],
    }
    return qservice.settings_payload(qs or {}, ts or {}, meta)


@router.get('/audit')
async def audit(request: Request,
                limit: int = Query(default=100, ge=1, le=1000),
                user: dict = Depends(security.current_user)):
    rt = await _rt_or_503()
    payload = {'records': rt.audit.tail(limit), 'integrity': rt.audit.verify()}
    from ..services.scope import attach
    return attach(payload, _panel_scope(request, authority='upstream'))


@router.post('/cache/invalidate')
async def invalidate_cache(request: Request,
                           prefix: str = Query(default=''),
                           user: dict = Depends(security.current_user),
                           _origin: None = Depends(security.require_same_origin)):
    """清进程内缓存。

    U19：身份保持 `current_user`（既有策略：不碰凭据、不写持久业务数据，见
    test_api_tokens 的 ANY_LOGGED_IN 清单），但**补上同源校验** —— 它是本组
    唯一一个只要求"已登录"的写端点，跨站页面否则能借会话把缓存打空（放大上游
    读取压力）。经 admin/session 闸门的写端点已由 `security` 内部统一校验，
    这里因为不走那些闸门，所以显式声明。
    """
    rt = await _rt_or_503()
    from ..qtgcore.cache import cache
    n = cache.invalidate(prefix)
    payload = {'ok': True, 'invalidated': n}
    from ..services.scope import attach
    return attach(payload, _panel_scope(request, authority='derived'))
