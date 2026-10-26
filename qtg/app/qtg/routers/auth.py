"""登录 / 会话 / 限速。

与旧实现的关键差异：
* 登录失败按 IP 限速（旧实现可无限爆破面板密码）；
* 会话 Cookie 带 HttpOnly + SameSite=Strict + 签名 + 过期；
* 登录成功/失败都写审计（旧实现只写上游日志）。
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ..errors import RateLimited, Unauthorized
from ..manager_session import forward_logout, wb_cookie_header
from ..security import Auth

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_WINDOW = 60.0
_MAX_ATTEMPTS = 8
_attempts: Dict[str, Deque[float]] = defaultdict(deque)


def _throttle(ip: str) -> None:
    now = time.monotonic()
    q = _attempts[ip]
    while q and now - q[0] > _WINDOW:
        q.popleft()
    if len(q) >= _MAX_ATTEMPTS:
        raise RateLimited(f"登录尝试过于频繁（{_MAX_ATTEMPTS} 次/分钟），请稍后再试")
    q.append(now)


def _client_ip(request: Request) -> str:
    xri = request.headers.get("x-real-ip") or ""
    if xri:
        return xri.strip()
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class LoginResponse(BaseModel):
    ok: bool
    expires_in: int


@router.post("/login", response_model=LoginResponse)
async def login(request: Request, response: Response, body: LoginRequest) -> LoginResponse:
    rt = request.app.state.rt
    ip = _client_ip(request)
    _throttle(ip)
    if not rt.credentials.verify_password((body.password or "").strip()):
        rt.record_audit(action="login", actor=f"ip:{ip}", result="denied", detail={"reason": "bad_password"}, ip=ip)
        raise Unauthorized("密码错误")
    token = rt.credentials.issue_session(rt.settings.session_ttl_sec)
    secure = rt.settings.cookie_secure
    is_https = (
        request.url.scheme == "https"
        or (request.headers.get("x-forwarded-proto", "").lower() == "https")
    )
    use_secure = secure == "always" or (secure == "auto" and is_https)
    response.set_cookie(
        Auth.cookie_name(),
        token,
        max_age=rt.settings.session_ttl_sec,
        httponly=True,
        samesite="strict",
        secure=use_secure,
        path="/",
    )
    rt.record_audit(action="login", actor=f"ip:{ip}", result="ok", ip=ip)
    return LoginResponse(ok=True, expires_in=rt.settings.session_ttl_sec)


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    """退出：删 qtg_session，并让 Manager 的 wb_session 一并失效（K03）。

    与 compat 层同一个缺陷：只删本地会话会让浏览器里的 wb_session 继续有效。
    Manager 不可达时仍删本地会话，但响应标记 degraded。
    """
    rt = request.app.state.rt
    cookie_header = wb_cookie_header(request.cookies)
    degraded = False
    if cookie_header:
        ok, set_cookies = await forward_logout(rt.settings.wb_panel_base, cookie_header)
        degraded = not ok
        for header in set_cookies:
            response.headers.append("set-cookie", header)
    response.delete_cookie(Auth.cookie_name(), path="/")
    rt.record_audit(
        action="logout",
        actor="session",
        result="degraded" if degraded else "ok",
        ip=_client_ip(request),
        detail={"manager_forwarded": bool(cookie_header), "degraded": degraded},
    )
    return {"ok": True, "degraded": True} if degraded else {"ok": True}


@router.get("/session")
async def session(request: Request) -> dict:
    rt = request.app.state.rt
    token = request.cookies.get(Auth.cookie_name(), "")
    payload = rt.credentials.verify_session(token) if token else None
    return {
        "authenticated": bool(payload),
        "expires_at": payload.get("exp") if payload else None,
        "read_requires_auth": rt.settings.read_requires_auth,
    }
