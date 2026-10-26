"""应用装配。

`create_app()` 是唯一入口，便于测试用 `create_app(settings)` 注入隔离配置
（旧 preview-agent 在 import 时就绑定端口，无法测试）。
"""
from __future__ import annotations

import logging
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, authz, metrics
from .config import Settings, get_settings
from .errors import Forbidden, install_handlers
from .identity import IdentityRejected, audit_actor, audit_context, audit_detail
from .routers import (actions, anthropic, auth, compat, compat_accounts_ext, compat_fixes, compat_settings,
                        compat_stats, ip_guard,
                        data_admin, health, proxy, read, system)
from .routers import authz as authz_probe
from .routers import capabilities as capabilities_router
from .runtime import Runtime
from .service import now_iso

log = logging.getLogger("qtg")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings_injected = settings is not None
    settings = settings or get_settings()
    _setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .configcontract import validate_environment, with_effective_values
        environment = validate_environment()
        app.state.configuration_snapshot = with_effective_values(environment,
            {'QTG_'+key.upper():value for key,value in asdict(settings).items()},
            source='explicit_settings_object' if settings_injected else 'resolved_service_settings')
        rt = Runtime(settings)
        app.state.rt = rt
        await rt.startup()
        # 每日随机签到调度（2026-10-23 从 /opt/signin-random 迁入，见 checkin_scheduler.py）
        _sched_task = None
        try:
            from .checkin_scheduler import start as _start_checkin
            _sched_task = _start_checkin(rt)
        except Exception:  # noqa: BLE001
            pass
        try:
            yield
        finally:
            try:
                if _sched_task is not None:
                    _sched_task.cancel()
                    try:
                        await _sched_task
                    except asyncio.CancelledError:
                        pass
            finally:
                await rt.shutdown()

    app = FastAPI(
        title="Qoder / Trae 统一网关控制面",
        version=__version__,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    install_handlers(app)

    # ---- 中间件 ----
    @app.middleware("http")
    async def _observe(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            metrics.inc(
                "qtg_requests_total",
                {"path": _route_of(request), "status": "500"},
            )
            raise
        elapsed = time.perf_counter() - started
        metrics.inc(
            "qtg_requests_total",
            {"path": _route_of(request), "status": str(response.status_code)},
        )
        metrics.observe(
            "qtg_request_duration_seconds",
            elapsed,
            {"path": _route_of(request)},
        )
        response.headers["x-request-id"] = rid
        response.headers["x-qtg-version"] = __version__
        # 缓存策略：HTML 绝不缓存（否则旧 HTML 会引用错误的资源路径），
        # 静态资源走 no-cache + ETag（换版本立即生效，未变则 304）。
        path = request.url.path
        if path.startswith("/assets/") or path.endswith((".css", ".js", ".svg", ".woff2")):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"

        # 安全头（旧面板完全缺失）
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if not _is_static(request.url.path):
            response.headers.setdefault(
                "Content-Security-Policy",
                # 与 Manager 一致：显式 object-src 'none'；QTG 面板没有内联脚本
                # （script-src 不带 unsafe-inline），也没有表单提交面，仍写
                # form-action 收敛以防将来加表单时漏掉。
                "default-src 'self'; img-src 'self' data: blob:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "connect-src 'self'; object-src 'none'; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            )
        return response

    # ---- 认证闸门（P0） ----
    # 兼容层（compat_*）大量接口是「形状适配」，历史上直接暴露在 /api/ 下：
    # 可读密钥列表、改账号、清代理流水、吊销会话。统一要求登录态：
    #   1) 浏览器会话 Cookie（/api/login 签发，HttpOnly + SameSite=Strict）
    #   2) 管理令牌 Bearer（verify.sh / 运维脚本用）
    #   3) 边缘内部凭据（X-QTG-Internal-Auth，仅可信对端，见 identity.py）
    # 白名单只留登录/登出/身份探测，其余一律 401。
    _AUTH_EXEMPT = frozenset({
        "/api/login", "/api/logout", "/api/me",
        "/api/v1/auth/login", "/api/v1/auth/session", "/api/v1/auth/logout",
    })

    # 非 /api 前缀但同样敏感的端点（2026-10-23 审计：/metrics 泄露端点清单，/readyz 回显上游 URL）
    _PROTECTED_EXTRA = frozenset({"/metrics", "/readyz"})

    def _client_ip(request: Request) -> str:
        return request.headers.get("x-real-ip") or (
            request.client.host if request.client else ""
        )

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        """认证 + 授权（K04/K05）。

        与旧版差别：
        * 认证结果统一成 `Identity`（sub/role/scopes/auth_method/aud/exp），挂在
          `request.state.identity` 上，后续 handler/审计复用同一份断言；
        * 伪造身份头、非可信来源的内部凭据、推理密钥冒充控制面凭证 → 401 并记审计，
          **不再静默忽略**；
        * 授权交给 `authz.decide()`（角色×动作×平台矩阵），拒绝时 403 并记审计。
        """
        path = request.url.path
        if not (path.startswith("/api/") or path in _PROTECTED_EXTRA) or path in _AUTH_EXEMPT:
            return await call_next(request)
        # K04：公开业务走**显式白名单**（authz 矩阵里 public=True 的行），
        # 例如登录/退出/健康检查/`/api/claim/*`（本部署恒 410，无状态）。
        # 白名单是一条条登记出来的，不是"前缀排除"。
        public_rule = authz.find_rule(request.method, path)
        if public_rule is not None and public_rule.public:
            return await call_next(request)
        rt = getattr(request.app.state, "rt", None)
        if rt is None:  # lifespan 之前（几乎不会发生）
            return await call_next(request)
        try:
            ident = rt.identity.resolve(request)
        except IdentityRejected as exc:
            rt.record_audit(
                action="identity_rejected", actor="anonymous",
                target=f"{request.method} {path}", result="denied",
                detail={"code": exc.code, "reason": exc.message,
                        "internal_auth_present": bool(request.headers.get("x-qtg-internal-auth")),
                        "identity_header_present": bool(request.headers.get("x-qtg-identity")),
                        "origin": request.headers.get("origin", "")[:160]},
                ip=_client_ip(request),
            )
            return JSONResponse(
                {"detail": exc.message, "code": exc.code, "reason": exc.code},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.identity = ident
        if ident is None:
            return JSONResponse(
                {"detail": "unauthorized", "code": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if request.headers.get('x-qtg-identity-required') == '1' and not rt.settings.authz_enforce:
            return JSONResponse({'detail': '身份桥接要求服务端授权已启用',
                                 'code': 'edge_authz_disabled'}, status_code=503)
        if rt.settings.authz_enforce:
            decision = authz.decide(
                ident, request, human_session_required=rt.settings.human_session_required
            )
            if not decision.allowed:
                detail = {"decision": decision.as_dict(), "reason": decision.reason}
                detail.update(audit_detail(ident))
                rt.record_audit(
                    action="authz_denied", actor=audit_actor(ident),
                    target=f"{request.method} {path}", result="denied",
                    detail=detail, ip=_client_ip(request),
                )
                return JSONResponse(
                    {
                        "detail": decision.reason,
                        "code": decision.code,
                        "decision": decision.as_dict(),
                        "actor": audit_actor(ident),
                    },
                    status_code=403,
                )
        with audit_context(ident, request.headers.get("x-request-id", "")):
            return await call_next(request)

    # ---- 路由 ----
    # 注意注册顺序（K30）：`compat` 里有一条 `/api/{path:path}` 兜底，它会把
    # 之后注册的所有 `/api/v1/**` 新路由吞掉（返回 404/501）。所以新路由必须
    # 排在 compat **之前**，与 `qtg/routeaudit.py` 的 REGISTRATION_ORDER 一致。
    app.include_router(ip_guard.router)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(read.router)
    app.include_router(actions.router)
    app.include_router(proxy.router)
    app.include_router(anthropic.router)
    app.include_router(data_admin.router)
    app.include_router(system.router)
    app.include_router(compat_fixes.router)
    app.include_router(compat_stats.router)
    app.include_router(compat_settings.router)
    app.include_router(compat_accounts_ext.router)
    app.include_router(authz_probe.router)
    app.include_router(capabilities_router.router)
    app.include_router(compat.router)
    ip_guard.install(app)


    # ---- 静态前端 ----
    static_dir = Path(settings.static_dir)
    index = static_dir / "index.html"
    if static_dir.exists():
        # 原生 ESM 前端（旧）
        if (static_dir / "assets").exists():
            app.mount("/assets", StaticFiles(directory=str(static_dir / "assets"), check_dir=False), name="assets")
        # Next.js 静态导出（多页：每页一个 index.html）——必须单独挂 _next
        if (static_dir / "_next").exists():
            app.mount("/_next", StaticFiles(directory=str(static_dir / "_next"), check_dir=False), name="_next")

        @app.get("/", include_in_schema=False)
        async def _index():
            if index.exists():
                return FileResponse(index)
            return JSONResponse({"error": {"code": "no_ui", "message": "前端未部署"}}, status_code=404)

        @app.get("/favicon.svg", include_in_schema=False)
        async def _favicon():
            p = static_dir / "favicon.svg"
            if p.exists():
                return FileResponse(p)
            return JSONResponse({}, status_code=404)

        @app.get("/{path:path}", include_in_schema=False)
        async def _spa(path: str):
            # 只对非 API 路径做 SPA 回退，避免把 404 的 API 也变成 HTML
            if path.startswith(("api/", "metrics", "healthz", "readyz", "docs", "openapi.json")):
                return JSONResponse({"error": {"code": "not_found", "message": "not found"}}, status_code=404)
            # Next.js App Router 客户端导航会取预生成的 RSC payload（*.txt）：
            #   /dashboard.txt  → dashboard/index.txt
            #   /dashboard/index.txt → dashboard/index.txt
            # 若落到 SPA fallback 会返回 HTML，React 解析即报 "Connection closed" 白屏。
            if path.endswith(".txt"):
                stem = path[:-4]
                for cand_txt in (
                    static_dir / (stem + ".txt"),
                    static_dir / stem / "index.txt",
                ):
                    if cand_txt.is_file():
                        return FileResponse(cand_txt, media_type="text/plain; charset=utf-8")
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "rsc payload not found"}},
                    status_code=404,
                )

            candidate = static_dir / path
            # 目录穿越防护（P0）：`GET /..%2f..%2fetc%2fpasswd` 曾被 Starlette
            # 解成 `../..`，`static_dir / path` 直接逃出根目录 → 任意文件读取。
            # 母本 WorkBuddy 在 server/main.py 用 resolve()+包含性校验修过同一个洞，
            # 这里照抄同一套判据（不要依赖 nginx 归一化，否则直连 8795 仍然可读）。
            try:
                _root = static_dir.resolve()
                _resolved = candidate.resolve()
            except OSError:
                return JSONResponse(
                    {"error": {"code": "forbidden", "message": "path escapes static dir"}},
                    status_code=403,
                )
            if _resolved != _root and _root not in _resolved.parents:
                return JSONResponse(
                    {"error": {"code": "forbidden", "message": "path escapes static dir"}},
                    status_code=403,
                )
            # 目录 → 目录下 index.html（Next 的 /dashboard/ 形态）
            if candidate.is_dir():
                dir_index = candidate / "index.html"
                if dir_index.is_file():
                    return FileResponse(dir_index)
            if candidate.is_file():
                return FileResponse(candidate)
            # 允许省略结尾斜杠：/dashboard → /dashboard/index.html
            alt = static_dir / (path + ".html")
            if alt.is_file():
                return FileResponse(alt)
            alt_dir = static_dir / path / "index.html"
            if alt_dir.is_file():
                return FileResponse(alt_dir)
            if index.exists():
                return FileResponse(index)
            return JSONResponse({"error": {"code": "not_found", "message": "not found"}}, status_code=404)


    @app.middleware("http")
    async def _csrf_gate(request: Request, call_next):
        """浏览器会话下的状态改变请求必须通过严格的 Origin/Referer 校验（K07 修复本体）。

        2026-10-23 审计确认的漏洞链：
        Nginx 给**所有** `/api/` 浏览器请求注入 `Authorization: Bearer <内部令牌>`，
        而旧闸门「见 Bearer 就跳过 Origin 校验」→ 浏览器写请求实际没有 CSRF 保护
        （跨站表单能自动带上 Cookie，边缘又替它补上内部 Bearer）。

        修复原理见 `identity.py`：**看有没有浏览器会话 Cookie，而不是看有没有 Bearer**。
        Bearer 可能是服务器自己替浏览器补上的，不能作为豁免信号；Cookie 才是攻击者
        无法阻止浏览器自动携带的环境凭证。校验时把来源拆成 (scheme, host, port)
        全等比较（旧实现是 `host not in source` 子串判断，相似域名即可绕过）。
        """
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            path = request.url.path
            if path.startswith("/api/") and path not in _AUTH_EXEMPT:
                rt = getattr(request.app.state, "rt", None)
                if rt is not None:
                    auth = rt.auth
                    ambient = auth.ambient_session(request)
                    try:
                        if ambient:
                            # 浏览器上下文：无论有没有 Bearer/内部凭据，都必须校验来源
                            auth.check_origin(request, strict=rt.settings.csrf_strict_origin)
                        elif request.headers.get("origin"):
                            # 无 Cookie 但有 Origin：多半是「别的前端在跨站调用」。
                            # 机器调用不应带 Origin；带了对不上就按伪造拒绝（可白名单）。
                            auth.check_origin_machine(request, strict=rt.settings.csrf_strict_origin)
                    except Forbidden as exc:
                        reason = getattr(exc, "code", "csrf_forbidden")
                        rt.record_audit(
                            action="csrf_rejected", actor="anonymous",
                            target=f"{request.method} {path}", result="denied",
                            detail={"reason": reason, "message": str(exc)[:200],
                                    "ambient_cookie": ambient,
                                    "origin": (request.headers.get("origin") or "")[:160],
                                    "referer": (request.headers.get("referer") or "")[:160]},
                            ip=_client_ip(request),
                        )
                        return JSONResponse(
                            {
                                "detail": str(exc) or "forbidden",
                                "code": "csrf_forbidden",
                                "reason": reason,
                            },
                            status_code=403,
                        )
        return await call_next(request)

    # N36 修复：安全头必须由**最外层**中间件补齐。
    #
    # 中间件按注册顺序"后进先出"包裹，`_observe` 最早注册 → 最内层。它在自己的
    # 返回值上设头，但 `_auth_gate` / `_csrf_gate` 拒绝时是**直接 return
    # JSONResponse**，根本不经过内层，于是 401/403 响应一个安全头都没有。
    # 实测：GET /api/v1/meta 无凭据 → 401，X-Frame-Options 等全部缺失。
    #
    # 这里用 setdefault 只补缺失项，不覆盖内层已设的值（CSP 对静态资源的跳过、
    # x-request-id 等仍以内层结果为准）。
    _SECURITY_HEADERS = (
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "same-origin"),
        ("X-Frame-Options", "DENY"),
        ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
    )

    @app.middleware("http")
    async def _security_headers_outer(request: Request, call_next):
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS:
            response.headers.setdefault(name, value)
        if not _is_static(request.url.path):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "connect-src 'self'; object-src 'none'; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            )
        return response
    return app


def _route_of(request: Request) -> str:
    """用路由模板而不是原始 path 做指标标签，避免标签爆炸（/accounts/<uuid>）。"""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    p = request.url.path
    if p.startswith("/api/v1/accounts/"):
        return "/api/v1/accounts/{...}"
    return p[:80]


def _is_static(path: str) -> bool:
    return path.startswith("/assets/")


app = create_app()
