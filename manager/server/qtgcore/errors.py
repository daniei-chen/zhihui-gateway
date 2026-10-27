"""统一错误模型。

对外错误体固定为 OpenAI 风格，便于调用方复用已有客户端代码：

    {"error": {"code": "...", "message": "...", "type": "..."}}

内部异常一律从 `QtgError` 派生，并在 `install_handlers()` 中集中翻译，
避免每个路由各自 try/except 导致漏掉某一种异常。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class QtgError(Exception):
    """所有受控错误的基类。"""

    status_code = 500
    code = "internal_error"
    type = "api_error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code

    def to_body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "type": self.type}}


class BadRequest(QtgError):
    status_code = 400
    code = "bad_request"
    type = "invalid_request_error"


class Unauthorized(QtgError):
    status_code = 401
    code = "unauthorized"
    type = "authentication_error"


class Forbidden(QtgError):
    status_code = 403
    code = "forbidden"
    type = "permission_error"


class NotFound(QtgError):
    status_code = 404
    code = "not_found"
    type = "invalid_request_error"


class UpstreamError(QtgError):
    """上游控制面（BFF）不可用或返回了不可解析的内容。

    2026-10-25（RV05）：`upstream_status` 保存**上游**返回的 HTTP 状态码（非 HTTP
    失败为 None），与 `status_code`（QTG 自己回给调用方的响应码）分开。四种失败
    ——资源不存在(404)、未授权(401/403)、载荷不可用、上游不可达——必须能被调用方
    区分，不能全部挤进一个含糊的返回值（历史上 404 被翻译成 None，见
    UpstreamNotFound 的说明）。
    """

    status_code = 502
    code = "upstream_error"
    type = "api_error"

    def __init__(self, message: str, *, code: str | None = None,
                 status_code: int | None = None,
                 upstream_status: int | None = None):
        super().__init__(message, code=code, status_code=status_code)
        self.upstream_status = upstream_status

    @property
    def status(self) -> int | None:
        """上游 HTTP 状态码别名：契约里写作「保留 code/status 属性」。"""
        return self.upstream_status


class UpstreamTimeout(UpstreamError):
    status_code = 504
    code = "upstream_timeout"


class UpstreamNotFound(UpstreamError):
    """上游明确回答 404：**资源不存在**。

    404 不是「没有数据」，更不是「换一条通道再读一次」的许可。只有显式声明
    `allow_not_found=True` 的写调用（删除/更新不存在的 key）才把 404 映射成 None；
    读路径一律抛本异常。
    """

    code = "upstream_not_found"


class UpstreamUnauthorized(UpstreamError):
    """上游 401/403：服务凭据无效、过期或 scope 不足（与 404/5xx 可区分）。

    `token_purpose` 记录是哪种服务凭据出的问题（read=K26 只读令牌 /
    keys=K01-S2 密钥管理令牌）；运维据此直接知道该换哪个文件，无需翻日志。
    """

    code = "upstream_unauthorized"

    def __init__(self, message: str, *, code: str | None = None,
                 status_code: int | None = None,
                 upstream_status: int | None = None,
                 token_purpose: str | None = None):
        super().__init__(message, code=code, status_code=status_code,
                         upstream_status=upstream_status)
        self.token_purpose = token_purpose


class UpstreamPayloadError(UpstreamError):
    """上游有响应，但内容不可用：非 JSON、空 body、JSON null、结构不符。"""

    code = "upstream_payload_error"


# 注：R0 曾用 `ReadChannelNotMigrated`（501）显式拒绝"尚未迁移到 http"的读方法
# （usage / usage_analytics）。R1-A 把这两个方法也迁到 Manager v2 的 usage 族后，
# **已无读方法走这条路径**，该异常类型随之移除：保留一个永远不抛的 501 只会让台账
# 与代码不一致。将来若再出现"未迁移读取"，应当新增显式错误，而不是回退 SQLite。


class BreakerOpen(UpstreamError):
    status_code = 503
    code = "circuit_open"


class ConfigMissing(QtgError):
    """本端配置缺失（例如未填上游 API Key）：不是上游故障，503 更准确。"""

    status_code = 503
    code = "config_missing"
    type = "api_error"


class RateLimited(QtgError):
    status_code = 429
    code = "rate_limited"
    type = "rate_limit_error"


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(QtgError)
    async def _qtg(_: Request, exc: QtgError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "请求参数不合法",
                    "type": "invalid_request_error",
                    "details": exc.errors()[:8],
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail), "type": "api_error"}},
        )
