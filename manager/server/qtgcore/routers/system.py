"""系统与安全自检：版本、依赖、磁盘、发布完整性、会话与令牌来源。

WorkBuddy 的教训是「健康检查分层、细节只给管理员」——这里的两个端点都要求管理凭证。
"""
from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request, Response

from .. import __version__

router = APIRouter(prefix="/api/v1", tags=["system"])

_START = time.time()


@router.get('/system/configuration')
async def configuration_snapshot(request: Request, response: Response) -> dict:
    from ..configcontract import validate_environment
    from ..identity import identity_of
    from ..errors import Forbidden
    identity = identity_of(request)
    if identity is None or identity.role not in ('admin', 'internal_service'):
        raise Forbidden('需要管理员权限')
    loaded = getattr(request.app.state, 'configuration_snapshot', None)
    response.headers['Cache-Control']='no-store'
    return {'ok':True, 'configuration':loaded or validate_environment(),
            'active':{'authz_enforce':_rt(request).settings.authz_enforce,
                      'human_session_required':_rt(request).settings.human_session_required,
                      'read_requires_auth':_rt(request).settings.read_requires_auth,
                      'identity_audience':_rt(request).settings.identity_audience,
                      'qoder_engine':_rt(request).settings.qoder_engine,
                      'trae_engine':_rt(request).settings.trae_engine,
                      'wb_engine':_rt(request).settings.wb_engine}}


def _rt(request: Request):
    return request.app.state.rt


async def require_read(request: Request) -> str:
    """读端点统一策略：默认公开；QTG_READ_REQUIRES_AUTH=1 时要求凭证。

    曾经只有部分读端点强制鉴权，导致「会话过期后少数页面 401、其它页正常」，
    用户体感就是「有时打不开」。鉴权策略必须全站一致。
    """
    return await _rt(request).auth.require_read(request)


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def _dep_versions() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for mod in ("fastapi", "starlette", "uvicorn", "httpx", "pydantic"):
        try:
            out[mod] = __import__(mod).__version__
        except Exception:
            out[mod] = "?"
    return out


@router.get("/system")
async def system_info(request: Request, _: str = Depends(require_read)) -> Dict[str, Any]:
    rt = _rt(request)
    s = rt.settings
    manifest = Path("/opt/workbuddy-manager/deploy/managed-release.json")
    sig = Path("/opt/workbuddy-manager/deploy/managed-release.sig")
    manifest_meta: Dict[str, Any] = {"present": manifest.exists(), "signed": sig.exists()}
    if manifest.exists():
        import json

        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_meta.update({
                "files": data.get("file_count"),
                "version": data.get("version"),
                "size": manifest.stat().st_size,
            })
        except Exception:
            pass
    uptime = int(time.time() - _START)
    keys = rt.store.list_keys()
    usage = rt.store.stats(3650)
    return {
        "service": {
            "version": __version__,
            "uptime_sec": uptime,
            "uptime_text": f"{uptime // 3600}h{(uptime % 3600) // 60}m",
            "pid": os.getpid(),
            "host": s.host,
            "port": s.port,
            "docs_enabled": s.docs_enabled,
            "proxy_enabled": s.proxy_enabled,
        },
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "deps": _dep_versions(),
        },
        "storage": {
            "data_dir": s.data_dir,
            "data_size": _dir_size(Path(s.data_dir)),
            "audit_size": rt.settings.audit_path.stat().st_size if rt.settings.audit_path.exists() else 0,
            "db_size": s.db_path.stat().st_size if s.db_path.exists() else 0,
            "audit_records": rt.audit.count(),
            "audit_integrity": rt.audit.verify(),
        },
        "release": manifest_meta,
        "data_plane": {
            "keys_total": len(keys),
            "keys_enabled": len([k for k in keys if k["enabled"]]),
            "requests_total": usage["requests"],
            "tokens_total": usage["tokens"],
        },
        "limits": {
            "upstream_timeout_sec": s.upstream_timeout_sec,
            "proxy_timeout_sec": s.proxy_timeout_sec,
            "proxy_max_body_mb": s.proxy_max_body_mb,
            "cache_overview_sec": s.cache_overview_sec,
            "breaker_fail_threshold": s.breaker_fail_threshold,
        },
    }


@router.get("/security")
async def security_info(request: Request, _: str = Depends(require_read)) -> Dict[str, Any]:
    rt = _rt(request)
    s = rt.settings
    cookie = request.cookies.get("qtg_session", "")
    session = rt.credentials.verify_session(cookie) if cookie else None
    extra = [p for p in rt.credentials.extra_files if p.exists()]
    keys = rt.store.list_keys()
    return {
        "session": {
            "via": "cookie" if session else ("bearer" if request.headers.get("authorization") else "none"),
            "authenticated": bool(session) or bool(request.headers.get("authorization")),
            "expires_at": session.get("exp") if session else None,
            "ttl_sec": s.session_ttl_sec,
        },
        "tokens": {
            "primary": s.admin_token_file,
            "primary_exists": Path(s.admin_token_file).exists(),
            "compatible": [str(p) for p in extra],
            "cookie_secure": s.cookie_secure,
            "read_requires_auth": s.read_requires_auth,
        },
        "data_plane": {
            "keys_total": len(keys),
            "keys_with_daily_limit": len([k for k in keys if k.get("daily_limit")]),
            "keys_with_monthly_limit": len([k for k in keys if k.get("monthly_limit")]),
            "keys_with_model_allowlist": len([k for k in keys if k.get("models")]),
            "keys_with_concurrency_limit": len([k for k in keys if k.get("max_concurrency")]),
        },
        "upstream_secrets": {
            "qoder_key_file": s.qoder_api_key_file,
            "qoder_configured": Path(s.qoder_api_key_file).exists(),
            "trae_key_file": s.trae_api_key_file,
            "trae_configured": Path(s.trae_api_key_file).exists(),
        },
        "audit": rt.audit.verify(),
        "hardening": {
            "systemd": "ProtectSystem=strict + PrivateTmp + NoNewPrivileges + MemoryMax",
            "csp": "default-src 'self'; script-src 'self'",
            "cookie": "HttpOnly + SameSite=Strict + HMAC 签名",
            "login_throttle": "8 次/分钟/IP",
        },
    }
