"""专用认证 / 授权探针（K04 第二半）。

## 为什么不能再用业务端点当探针

现状：Nginx `auth_request` 指向 Manager 的 `/api/settings/github-mirror`（一个
`require_admin` 的业务端点）。副作用是：

* viewer 访问**只读**页面也被整条 `/api/` 拦掉（403），而 QTG 侧其实允许读；
* 该端点的语义（"能不能看 github 镜像设置"）与"能不能写账号池"根本不是一回事，
  改业务端点权限会顺带改掉全站入口；
* 它要读业务配置才能回答，探针本身变重（高频子请求放大成本）。

本文件给的探针是**只回答身份与授权**的轻量接口：

* 不读业务状态、不写业务账本、不打上游；
* 结论放在 HTTP 状态码：200 允许 / 401 无身份 / 403 有身份但不允许；
* 响应体给 `identity.assertion()`（sub/role/scopes/auth_method/aud/exp）与
  `decision`（命中哪条契约、理由码），因此边缘与审计都能解释"为什么"。

## 顺序：先授权，后换探针

`main.py` 的 `_authz_gate` 对真实请求执行同一套矩阵。只有在服务端授权已经生效、
且 `/api/v1/authz/matrix` 与 `contracts/roles-actions-platforms.csv` 对得上之后，
才允许把 Nginx 的 `auth_request` 从业务端点切到本探针 —— 见
`design/qtg/K04-先授权后换探针.md`；实际可核验的边缘候选是
`ops/nginx/qtg-identity-bridge.conf`。Manager 探针只认证，QTG 在真实动作上授权。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .. import authz
from ..identity import audit_actor, audit_detail, identity_of
from ..service import now_iso

router = APIRouter(prefix="/api/v1/authz", tags=["authz"])


def _settings(request: Request):
    return request.app.state.rt.settings


def _probe_args(request: Request) -> dict:
    """从请求头/查询参数取"要判定的目标请求"。"""
    header = request.headers
    return {
        "action": (header.get("x-qtg-probe-action") or request.query_params.get("action") or "").strip(),
        "platform": (header.get("x-qtg-probe-platform") or request.query_params.get("platform") or "").strip(),
        "method": (header.get("x-qtg-probe-method") or request.query_params.get("method") or "").strip(),
        "path": (header.get("x-qtg-probe-path") or request.query_params.get("path") or "").strip(),
    }


def _audit(request: Request, identity, decision, *, via: str) -> None:
    rt = request.app.state.rt
    detail = {
        "via": via,
        "decision": decision.as_dict(),
        "request_id": request.headers.get("x-request-id", ""),
    }
    detail.update(audit_detail(identity))
    rt.record_audit(
        action="authz_probe",
        actor=audit_actor(identity),
        target=f"{request.method} {request.url.path}",
        result="ok" if decision.allowed else "denied",
        detail=detail,
        ip=request.headers.get("x-real-ip") or (request.client.host if request.client else ""),
    )


@router.get("/probe")
async def probe(request: Request) -> JSONResponse:
    """轻量探针：返回 200/401/403 + 身份断言 + 授权理由。

    匿名请求由全局闸门先返回 401（本 handler 只在已认证时执行），这里仍显式处理
    `identity is None`，以便闸门未来调整顺序时语义不变（fail-closed）。
    """
    identity = identity_of(request)
    args = _probe_args(request)
    settings = _settings(request)
    decision = authz.decide(
        identity, request,
        human_session_required=settings.human_session_required,
        action=args["action"] or None,
        platform=args["platform"] or None,
        target_method=args["method"] or None,
        target_path=args["path"] or None,
    )
    # 探针本身是只读接口，允许所有已认证身份调用（用它来回答"能不能"）；
    # 但如果调用方显式声明了目标动作，就必须按目标的授权结论返回。
    _audit(request, identity, decision, via="probe")
    body = authz.probe_payload(identity, decision,
                               request_id=request.headers.get("x-request-id", ""))
    if identity is None:
        return JSONResponse(status_code=401, content=body)
    if not decision.allowed:
        return JSONResponse(status_code=403, content=body)
    return JSONResponse(status_code=200, content=body)


@router.get("/identity")
async def identity_view(request: Request) -> dict:
    """当前请求的身份断言（K05 验收：sub/role/scopes/auth_method/aud/exp 全在）。"""
    identity = identity_of(request)
    settings = _settings(request)
    decision = authz.decide(identity, request,
                            human_session_required=settings.human_session_required)
    return {
        "ok": decision.allowed,
        "generated_at": now_iso(),
        "identity": identity.assertion() if identity else None,
        "audience": settings.identity_audience,
        "decision": decision.as_dict(),
        "amr": ["cookie"] if identity and identity.auth_method == "session_cookie"
        else ([identity.auth_method] if identity else []),
    }


@router.get("/matrix")
async def matrix(request: Request,
                 action: Optional[str] = Query(default=None, description="只看某个动作")) -> dict:
    """角色×动作×平台授权矩阵（与 contracts 表逐行核对用）。"""
    payload = authz.matrix_payload()
    if action:
        payload["rows"] = [r for r in payload["rows"] if r["action"] == action]
        payload["counts"]["rows"] = len(payload["rows"])
    payload["generated_at"] = now_iso()
    return payload
