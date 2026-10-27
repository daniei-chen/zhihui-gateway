"""IP 管控（真实生效）：规则存储 + ASGI 中间件 + 访问日志。

数据文件：`<data_dir>/ip_rules.json`

```json
{"enabled": false, "mode": "whitelist",
 "rules": [{"id": 1, "kind": "allow", "cidr": "1.2.3.0/24", "note": "", "created_at": 0}]}
```

* **whitelist**：只放行命中 allow 规则的来源 IP；
* **blacklist**：拒绝命中 deny 规则的来源 IP；
* 回环地址（127.0.0.1 / ::1）在 whitelist 模式下**始终放行**——否则本机健康检查
  与直连 8795 的运维命令会被自己锁死；
* 启用前校验「当前请求来源 IP 会被放行」，不满足直接 409，避免把管理员关在门外；
* 用纯 ASGI 中间件（不是 BaseHTTPMiddleware），不缓冲 SSE 响应。
"""
from __future__ import annotations

import ipaddress
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api", tags=["ip-guard"])

_LOCK = threading.Lock()
_LOGS: List[Dict[str, Any]] = []          # 最近的拦截记录（环形缓冲）
_LOG_MAX = 500


def _path(request: Request) -> Path:
    return Path(request.app.state.rt.settings.data_dir) / "ip_rules.json"


def _load(request: Request) -> Dict[str, Any]:
    p = _path(request)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("enabled", False)
            data.setdefault("mode", "whitelist")
            data.setdefault("rules", [])
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"enabled": False, "mode": "whitelist", "rules": []}


def _save(request: Request, data: Dict[str, Any]) -> None:
    p = _path(request)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _client_ip(request: Request) -> str:
    xr = (request.headers.get("x-real-ip") or "").strip()
    if xr:
        return xr
    xf = (request.headers.get("x-forwarded-for") or "").strip()
    if xf:
        return xf.split(",")[0].strip()
    return request.client.host if request.client else ""


def _match(rule_cidr: str, ip: str) -> bool:
    try:
        net = ipaddress.ip_network(rule_cidr, strict=False)
        addr = ipaddress.ip_address(ip)
        return addr in net
    except Exception:  # noqa: BLE001
        return False


def _verdict(ip: str, data: Dict[str, Any]) -> Optional[bool]:
    """返回 True = 放行，False = 拒绝，None = 没有明确结论（走默认策略）。"""
    if ip in ("127.0.0.1", "::1"):
        return True
    allow = [r for r in data.get("rules") or [] if r.get("kind") == "allow"]
    deny = [r for r in data.get("rules") or [] if r.get("kind") == "deny"]
    if data.get("mode") == "blacklist":
        return not any(_match(r.get("cidr", ""), ip) for r in deny)
    return any(_match(r.get("cidr", ""), ip) for r in allow)


def _push_log(ip: str, path: str, ua: str, reason: str) -> None:
    with _LOCK:
        _LOGS.append({"id": len(_LOGS) + 1, "ts": int(time.time()), "ip": ip,
                      "path": path[:200], "blocked": True, "ua": ua[:200], "reason": reason})
        del _LOGS[:-_LOG_MAX]


def _check(request: Request) -> Tuple[bool, str]:
    data = _load(request)
    if not data.get("enabled"):
        return True, ""
    ip = _client_ip(request)
    ok = _verdict(ip, data)
    if ok is False:
        return False, "ip_not_allowed"
    return True, ""


# ------------------------------------------------------------------ 管理接口
@router.get("/security/config")
async def compat_security_config_get(request: Request) -> Dict[str, Any]:
    data = _load(request)
    return {"enabled": bool(data.get("enabled")), "mode": data.get("mode") or "whitelist"}


@router.post("/security/config")
async def compat_security_config_save(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    data = _load(request)
    enabled = bool(body.get("enabled", data.get("enabled")))
    mode = str(body.get("mode") or data.get("mode") or "whitelist")
    if mode not in ("whitelist", "blacklist"):
        return JSONResponse(status_code=400, content={"detail": "mode 只能是 whitelist 或 blacklist"})
    if enabled:
        ip = _client_ip(request)
        probe = {"enabled": True, "mode": mode, "rules": data.get("rules") or []}
        if _verdict(ip, probe) is False:
            return JSONResponse(status_code=409, content={
                "detail": f"当前来源 IP {ip} 不会被{mode}放行；请先添加放行规则再启用，"
                          "否则启用后本面板会立刻无法访问"})
    data["enabled"] = enabled
    data["mode"] = mode
    _save(request, data)
    request.app.state.rt.record_audit(action="ip_guard_config", actor="compat", result="ok",
                                     detail={"enabled": enabled, "mode": mode})
    return {"enabled": enabled, "mode": mode}


@router.get("/security/rules")
async def compat_security_rules_list(request: Request) -> List[Dict[str, Any]]:
    return list(_load(request).get("rules") or [])


@router.post("/security/rules")
async def compat_security_rules_add(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    kind = str(body.get("kind") or "deny")
    cidr = str(body.get("cidr") or "").strip()
    note = str(body.get("note") or "")[:120]
    if kind not in ("allow", "deny"):
        return JSONResponse(status_code=400, content={"detail": "kind 只能是 allow 或 deny"})
    try:
        ipaddress.ip_network(cidr, strict=False)
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"detail": f"不是合法的 IP 或 CIDR：{cidr}"})
    data = _load(request)
    rules = data.get("rules") or []
    rid = max([int(r.get("id") or 0) for r in rules] + [0]) + 1
    rule = {"id": rid, "kind": kind, "cidr": cidr, "note": note, "created_at": int(time.time())}
    rules.append(rule)
    data["rules"] = rules
    _save(request, data)
    request.app.state.rt.record_audit(action="ip_rule_add", actor="compat", result="ok",
                                     detail={"kind": kind, "cidr": cidr})
    return rule


@router.delete("/security/rules/{rid}")
async def compat_security_rules_delete(request: Request, rid: int) -> Dict[str, Any]:
    data = _load(request)
    rules = data.get("rules") or []
    keep = [r for r in rules if int(r.get("id") or 0) != rid]
    if len(keep) == len(rules):
        return JSONResponse(status_code=404, content={"detail": "没有这条规则"})
    data["rules"] = keep
    _save(request, data)
    return {"ok": True}


@router.get("/security/logs")
async def compat_security_logs(request: Request, limit: int = Query(default=200)) -> List[Dict[str, Any]]:
    with _LOCK:
        return list(_LOGS[-max(1, min(limit, _LOG_MAX)):])[::-1]


@router.post("/security/logs/clear")
async def compat_security_logs_clear(request: Request) -> Dict[str, Any]:
    with _LOCK:
        _LOGS.clear()
    return {"ok": True}


# ------------------------------------------------------------------ 中间件
_APP = None
_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": None}
_SKIP_PATHS = {"/healthz", "/metrics", "/favicon.ico"}
_SKIP_PREFIXES = ("/assets/", "/_next/", "/__qtg-legacy/")


def _load_file(p: Path) -> Dict[str, Any]:
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"enabled": False, "mode": "whitelist", "rules": []}
    with _CACHE_LOCK:
        if _CACHE["path"] == str(p) and _CACHE["mtime"] == mtime and _CACHE["data"]:
            return _CACHE["data"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("enabled", False)
    data.setdefault("mode", "whitelist")
    data.setdefault("rules", [])
    with _CACHE_LOCK:
        _CACHE.update(path=str(p), mtime=mtime, data=data)
    return data


def _scope_ip(scope: Dict[str, Any]) -> str:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    xr = (headers.get("x-real-ip") or "").strip()
    if xr:
        return xr
    xf = (headers.get("x-forwarded-for") or "").strip()
    if xf:
        return xf.split(",")[0].strip()
    client = scope.get("client") or ("", 0)
    return client[0] or ""


def _scope_ua(scope: Dict[str, Any]) -> str:
    for k, v in scope.get("headers") or []:
        if k.decode("latin-1").lower() == "user-agent":
            return v.decode("latin-1")
    return ""


class IpGuardMiddleware:
    """纯 ASGI 中间件（不缓冲流式响应）。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path") or ""
        if path in _SKIP_PATHS or path.startswith(_SKIP_PREFIXES):
            return await self.app(scope, receive, send)
        rt = getattr(_APP.state, "rt", None) if _APP is not None else None
        if rt is None:
            return await self.app(scope, receive, send)
        data = _load_file(Path(rt.settings.data_dir) / "ip_rules.json")
        if not data.get("enabled"):
            return await self.app(scope, receive, send)
        ip = _scope_ip(scope)
        if _verdict(ip, data) is False:
            _push_log(ip, path, _scope_ua(scope), "ip_not_allowed")
            resp = JSONResponse(status_code=403, content={
                "detail": "当前 IP 不在访问白名单内（IP 管控已启用）"})
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


def install(app) -> None:
    global _APP
    _APP = app
    app.add_middleware(IpGuardMiddleware)
