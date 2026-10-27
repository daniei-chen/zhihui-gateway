"""内部身份断言与凭证分类（K05 / K04 / K07 的公共底座）。

## 为什么需要这个模块

旧形态下，QTG 只认两种东西：会话 Cookie 和 `Authorization: Bearer <管理令牌>`。
上线 Nginx 后为了「让浏览器请求能过 QTG 闸门」，Nginx 给**所有** `/api/` 请求注入
一枚静态 Bearer。于是产生两个安全问题：

1. **CSRF 失效（K07）**：请求里总有 `Authorization: Bearer`，`_csrf_gate` 见 Bearer
   就跳过 Origin 校验；而浏览器跨站表单会自动带上 Cookie —— 于是「内部 Bearer +
   浏览器 Cookie」把 CSRF 保护整个抵掉了。
   根因不是「判断写错了」，而是**用错了判据**：Bearer 是攻击者可以「让服务器替它
   加上」的东西，Cookie 才是浏览器会自动携带的环境凭证。CSRF 保护必须绑定后者。

2. **共享身份（K05）**：注入的是同一枚静态令牌，QTG 无法知道请求背后是
   viewer 还是 admin、是真人会话还是 CI 脚本，审计只能记 `actor=admin`。

## 本模块提供的区分

| 凭证来源 | 头 / Cookie | 能否被外部伪造 | 用途 |
|---|---|---|---|
| `session_cookie` | `Cookie: qtg_session=` | 不能（HttpOnly + HMAC 签名） | 浏览器面板 |
| `edge_session` | Manager 签名的 `X-QTG-Identity` | 独立身份签名密钥 + 可信对端 | 边缘转发的真实用户身份 |
| `internal_service` | 仅 `X-QTG-Internal-Auth` | 不能（同上） | 边缘/自动化的机器调用 |
| `admin_token` | `Authorization: Bearer` | 需知道令牌 | 运维脚本/CI |
| `signed identity` | `X-QTG-Identity: v1.<body>.<sig>` | 不能（HMAC 签名 + 可信对端） | 会签名的中间件 |

### 为什么不能「见 Bearer 就跳过 Origin 校验」

因为 Bearer 可以是**服务器自己注入**的：Nginx 的 `proxy_set_header Authorization`
会把客户端值覆盖成内部凭据。QTG 看到 Bearer 时，无法区分「这是远处的脚本直接
带进来的」还是「这台边缘刚刚替浏览器补上的」。让一个「可能是替浏览器补的」东西
去豁免 CSRF，等于把保护交给了攻击者可控的信号。判定必须看**浏览器环境凭证**
（会话 Cookie）是否存在 —— 浏览器会自动带它，攻击者无法阻止。

### 为什么外部伪造同名字段过不了

* `Authorization`：候选 Nginx 配置对该头显式 `proxy_set_header ... ""` 或注入固定值，
  客户端值根本到不了 QTG；即便直连 8795，伪造值也过不了 `verify_password`。
* `X-QTG-Internal-Auth`：额外要求 (a) 对端在 `QTG_TRUSTED_EDGE_PEERS` 内，
  (b) 与 `internal_service_token` 常量时间比对相等。两条任一不满足即 401。
* 明文 `X-QTG-*` 用户字段一律拒绝。内部服务凭据只代表机器，不能靠这些字段
  声称自己是某个人；旧候选的明文字段传递方式已撤回。
* 明文身份头 `X-User` / `X-Role` / `X-Ingress-*` / `X-Auth-Request-*`：从不采信，
  出现即拒（它们是最常见的「网关信任上游头」翻车点）。
* `X-QTG-Identity` 签名断言：HMAC-SHA256（使用独立身份密钥，禁止复用服务令牌），并校验
  aud / exp / role / auth_method；外部构造的签名对不上。

所有拒绝都抛 `IdentityRejected`（401），`code` 字段给出可审计的原因。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Optional

from fastapi import Request

from .errors import Unauthorized
from .security import (AMBIENT_COOKIES, CredentialStore, parse_origin,
                       peer_is_trusted, split_csv)

_AUDIT_IDENTITY: ContextVar = ContextVar('qtg_audit_identity', default=None)


@contextmanager
def audit_context(identity: 'Identity', request_id: str):
    token = _AUDIT_IDENTITY.set((identity, request_id))
    try:
        yield
    finally:
        _AUDIT_IDENTITY.reset(token)


def current_audit_identity():
    return _AUDIT_IDENTITY.get()

# ---- 头名常量 ----
INTERNAL_AUTH_HEADER = "X-QTG-Internal-Auth"
IDENTITY_HEADER = "X-QTG-Identity"
# 边缘转发的身份字段（由 Nginx 从 auth_request 子响应里取，覆盖写入）
EDGE_IDENTITY_HEADERS: Dict[str, str] = {
    "x-qtg-sub": "sub",
    "x-qtg-role": "role",
    "x-qtg-scopes": "scopes",
    "x-qtg-auth-method": "auth_method",
    "x-qtg-aud": "aud",
    "x-qtg-exp": "exp",
    "x-qtg-iss": "iss",
    "x-qtg-jti": "jti",
}
# 常见的「明文身份头」：从不采信，出现即视为伪造
SPOOF_MARKERS = (
    "x-user", "x-role", "x-roles", "x-scopes", "x-forwarded-user",
    "x-remote-user", "x-auth-request-user", "x-auth-request-role",
    "x-ingress-user", "x-ingress-role", "x-wb-user", "x-wb-role",
)

ROLES = frozenset({"admin", "viewer", "readonly", "internal_service", "inference_key"})
AUTH_METHODS = frozenset({
    "session_cookie", "edge_session", "internal_service", "admin_token", "inference_key",
})
_INFERENCE_KEY_PREFIXES = ("sk-qtg-", "wbk-", "wbk_")


class IdentityRejected(Unauthorized):
    """身份不可采信：默认 401，带 `code` 便于审计与前端提示。"""

    def __init__(self, message: str, *, code: str = "identity_rejected") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Identity:
    """已核验的身份断言。

    K05 要求保留 sub/role/scopes/auth_method/aud/exp —— 这六项是必填，
    `assertion()` 用于审计与响应；任何 token/密钥都不出现在这里。
    """

    sub: str
    role: str
    scopes: frozenset = field(default_factory=frozenset)
    auth_method: str = "session_cookie"
    aud: str = "qtg"
    exp: int = 0
    iat: int = 0
    iss: str = "qtg"
    jti: str = ""

    def assertion(self) -> dict:
        return {
            "sub": self.sub,
            "role": self.role,
            "scopes": sorted(self.scopes),
            "auth_method": self.auth_method,
            "aud": self.aud,
            "exp": self.exp,
            "iss": self.iss,
        }

    def payload(self) -> dict:
        out = self.assertion()
        out["iat"] = self.iat
        out["jti"] = self.jti
        return out

    @property
    def is_human_session(self) -> bool:
        """真人会话：只有浏览器 Cookie 会话算（内部服务/令牌都不算）。"""
        return self.auth_method in ("session_cookie", "edge_session")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _derive_key(secret: str, purpose: str) -> bytes:
    """从内部凭据派生用途隔离的签名密钥（一个密钥不直接当两用）。"""
    return hashlib.sha256(f"qtg-{purpose}-v1\x00".encode() + secret.encode()).digest()


class IdentityResolver:
    """把请求解析成 Identity；失败一律 fail-closed。"""

    def __init__(self, credentials: CredentialStore, *, audience: str = "qtg",
                 max_ttl: int = 12 * 3600, trusted_peers: Iterable[str] = (),
                 clock=time.time) -> None:
        self.credentials = credentials
        self.audience = audience
        self.max_ttl = max_ttl
        self.trusted_peers = [str(p).strip() for p in trusted_peers if str(p).strip()]
        self.clock = clock

    # ---- 基础判定 ----
    def trusted_peer(self, request: Request) -> bool:
        client = getattr(request, "client", None)
        peer = str(getattr(client, "host", "") or "")
        return peer_is_trusted(peer, self.trusted_peers)

    def internal_token_ok(self, candidate: str) -> bool:
        """内部 Bearer 校验：**接受 current 与 `.next` 双值**（R3-L5）。

        轮换时先写 `<path>.next`，两边同时接受，改完 Nginx include 再 reload，
        整个窗口内新旧请求都 200；最后删 `.next` 收口。用 `any(...)` 逐个
        `compare_digest`（常量时间比较，不因为多一个候选而泄露长度/前缀信息）。
        """
        if not candidate:
            return False
        tokens = self.credentials.internal_service_tokens()
        if not tokens:
            return False
        value = candidate.strip().encode()
        return any(hmac.compare_digest(value, want.encode()) for want in tokens)

    def ambient_session(self, request: Request) -> str:
        for name in AMBIENT_COOKIES:
            if request.cookies.get(name):
                return name
        return ""

    # ---- 签名断言（会签名的边缘/中间件用） ----
    def sign(self, identity: Identity) -> str:
        secret = self.credentials.identity_signing_key()
        if not secret:
            raise IdentityRejected("独立身份签名凭据未就绪", code="identity_signing_disabled")
        body = _b64(json.dumps(identity.payload(), separators=(",", ":"),
                               sort_keys=True).encode())
        sig = _b64(hmac.new(_derive_key(secret, "identity"), body.encode(),
                            hashlib.sha256).digest())
        return f"v1.{body}.{sig}"

    def verify(self, value: str) -> Identity:
        parts = (value or "").strip().split(".")
        if len(parts) != 3 or parts[0] != "v1":
            raise IdentityRejected("身份断言格式非法", code="identity_malformed")
        secret = self.credentials.identity_signing_key()
        if not secret:
            raise IdentityRejected("独立身份签名凭据未就绪",
                                   code="identity_signing_disabled")
        body, sig = parts[1], parts[2]
        expect = _b64(hmac.new(_derive_key(secret, "identity"), body.encode(),
                               hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            raise IdentityRejected("身份断言签名不匹配（疑似伪造）", code="identity_tampered")
        try:
            payload = json.loads(_b64d(body))
        except Exception as exc:  # noqa: BLE001
            raise IdentityRejected(f"身份断言无法解析: {exc}", code="identity_malformed") from exc
        if not isinstance(payload, dict):
            raise IdentityRejected("身份断言必须是对象", code="identity_malformed")
        return self._from_mapping(payload, source="identity_signature")

    # ---- 主入口 ----
    def resolve(self, request: Request) -> Optional[Identity]:
        """解析身份；None = 匿名。任何不可采信的迹象都抛 IdentityRejected。

        解析顺序：
        1. 经独立签名验证的边缘身份：Manager 每次重新核对当前用户/角色与凭据；
        2. QTG 本地签名会话：仅在没有边缘身份时使用；
        3. 仅内部凭据：机器调用，身份降级为 `internal_service`；
        4. 运维管理令牌（Authorization Bearer）；
        5. 都没有 → 匿名。

        边缘身份必须优先于旧 QTG Cookie，否则 Manager 降权后仍会使用旧本地 admin。
        但不能让「没有用户断言的内部凭据」优先：旧 Nginx 会替浏览器注入内部凭据，
        若内部凭据优先，真实用户的角色/登录方式会被抹平成 `internal_service`
        —— 那正是 K05 说的"真实用户与登录方式丢失"。同理，绝不能在持有 Cookie
        时退化成内部服务身份（viewer 会被当成服务身份，权限反而变大）。
        """
        trusted = self.trusted_peer(request)
        internal_header = (request.headers.get(INTERNAL_AUTH_HEADER) or "").strip()
        identity_header = (request.headers.get(IDENTITY_HEADER) or "").strip()
        edge_fields = {
            key: (request.headers.get(header) or "").strip()
            for header, key in EDGE_IDENTITY_HEADERS.items()
            if (request.headers.get(header) or "").strip()
        }
        spoof = [h for h in SPOOF_MARKERS if (request.headers.get(h) or "").strip()]
        internal_ok = bool(internal_header) and self.internal_token_ok(internal_header)

        # Reject unknown identity markers immediately; legacy edge fields are
        # also rejected below, even with a valid service token.
        if spoof or (edge_fields and not (trusted and internal_ok)):
            raise IdentityRejected(
                "请求携带了未经签名/非可信来源的身份头，按伪造处理",
                code="spoofed_identity_header",
            )

        # Any explicitly supplied edge credential must validate; a local cookie
        # must never hide a bad assertion or a misconfigured bridge.
        if internal_header:
            if not trusted:
                raise IdentityRejected("内部凭据来自非可信对端", code="identity_untrusted_source")
            if not internal_ok:
                raise IdentityRejected("内部凭据不正确", code="bad_internal_token")

        # 2) Manager authoritative assertion, including token auth method/scopes.
        if identity_header:
            if not trusted:
                raise IdentityRejected("身份断言来自非可信对端", code="identity_untrusted_source")
            if edge_fields:
                raise IdentityRejected("不能同时携带两种边缘身份", code="identity_ambiguous")
            return self.verify(identity_header)
        if edge_fields:
            # A machine credential authenticates a service, not an arbitrary
            # human claimed in unsigned headers. Only Manager assertions may
            # carry a user's role/authentication method across the edge.
            raise IdentityRejected("用户身份必须使用独立签名断言", code="identity_signature_required")
        if request.headers.get('x-qtg-identity-required') == '1':
            raise IdentityRejected("边缘未提供已核验的用户身份", code="edge_identity_required")

        # 3) Local standalone sessions retain compatibility when no edge identity exists.
        cookie = request.cookies.get("qtg_session", "")
        if cookie:
            payload = self.credentials.verify_session(cookie, now=self.clock())
            if payload:
                return self._from_session_payload(payload)

        # 4) 边缘服务凭据（+ 可选身份字段）
        if internal_header:
            if self.ambient_session(request):
                raise IdentityRejected("浏览器请求缺少已核验的用户身份", code="edge_identity_required")
            return self._internal_identity()

        # 5) 运维管理令牌
        bearer = self._bearer(request)
        if bearer:
            if bearer.lower().startswith(_INFERENCE_KEY_PREFIXES):
                # 推理密钥是数据面凭证，不能当控制面身份（K05 边界之一）
                raise IdentityRejected("推理密钥不能用作控制面凭证",
                                       code="inference_key_not_control_plane")
            if self.credentials.verify_password(bearer):
                now = int(self.clock())
                return Identity(
                    sub="admin-token", role="admin", scopes=frozenset({"*"}),
                    auth_method="admin_token", aud=self.audience,
                    exp=now + 300, iat=now, iss="qtg",
                )
        return None

    # ---- 分类（供 CSRF 网关使用，不抛异常） ----
    def classify(self, request: Request) -> str:
        """粗分类：`browser_session` / `machine` / `anonymous`。

        只看「浏览器会自动携带凭证」这一性质，不参与鉴权结论。
        """
        if self.ambient_session(request):
            return "browser_session"
        if (request.headers.get(INTERNAL_AUTH_HEADER) or "").strip():
            return "machine"
        if self._bearer(request) or (request.headers.get(IDENTITY_HEADER) or "").strip():
            return "machine"
        return "anonymous"

    # ---- 内部实现 ----
    @staticmethod
    def _bearer(request: Request) -> str:
        raw = request.headers.get("authorization", "") or ""
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return ""

    def _now(self) -> int:
        return int(self.clock())

    def _check_exp(self, exp: object, *, code: str = "identity_expired") -> int:
        if exp is None or exp == "":
            raise IdentityRejected("身份缺少 exp（不接受无过期的身份断言）",
                                   code="identity_missing_exp")
        try:
            exp_i = int(float(exp))
        except (TypeError, ValueError) as exc:
            raise IdentityRejected("身份 exp 不是合法时间戳", code="identity_bad_exp") from exc
        now = self._now()
        if exp_i + 60 < now:
            raise IdentityRejected("身份已过期", code=code)
        if exp_i - now > self.max_ttl:
            raise IdentityRejected(
                f"身份有效期超过上限 {self.max_ttl}s（拒绝无限期委派）",
                code="identity_exp_too_long",
            )
        return exp_i

    def _check_aud(self, aud: object) -> str:
        if not aud:
            raise IdentityRejected("身份缺少 aud", code="identity_missing_aud")
        aud_s = str(aud)
        if aud_s != self.audience:
            raise IdentityRejected(f"身份受众不匹配：{aud_s} != {self.audience}",
                                   code="identity_wrong_audience")
        return aud_s

    def _check_role(self, role: object) -> str:
        role_s = str(role or "")
        if role_s not in ROLES:
            raise IdentityRejected(f"未知角色：{role_s or '(空)'}", code="identity_bad_role")
        return role_s

    def _check_auth_method(self, method: object) -> str:
        method_s = str(method or "")
        if method_s not in AUTH_METHODS:
            raise IdentityRejected(f"未知登录方式：{method_s or '(空)'}",
                                   code="identity_bad_auth_method")
        return method_s

    @staticmethod
    def _scopes(raw: object) -> frozenset:
        if isinstance(raw, str):
            items = [s for s in raw.replace(";", ",").replace(" ", ",").split(",") if s]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            items = [str(s) for s in raw]
        else:
            items = []
        return frozenset(s.strip() for s in items if s and s.strip())

    def _from_mapping(self, payload: Mapping[str, object], *, source: str) -> Identity:
        sub = str(payload.get("sub") or "").strip()
        if not sub:
            raise IdentityRejected("身份缺少 sub", code="identity_missing_sub")
        role = self._check_role(payload.get("role"))
        method = self._check_auth_method(payload.get("auth_method"))
        aud = self._check_aud(payload.get("aud"))
        exp = self._check_exp(payload.get("exp"))
        scopes = self._scopes(payload.get("scopes"))
        try:
            iat = int(float(payload.get("iat") or 0))
        except (TypeError, ValueError):
            iat = 0
        identity = Identity(
            sub=sub, role=role, scopes=scopes, auth_method=method, aud=aud,
            exp=exp, iat=iat, iss=str(payload.get("iss") or source),
            jti=str(payload.get("jti") or ""),
        )
        if method == "inference_key":
            raise IdentityRejected("推理密钥身份不能用于控制面",
                                   code="inference_key_not_control_plane")
        return identity

    def _from_session_payload(self, payload: Mapping[str, object]) -> Identity:
        """会话 Cookie → 身份。

        兼容旧会话（旧载荷只有 sub/iat/exp/n）：缺省视为 admin/session_cookie，
        与升级前一致；但 aud 若存在就必须匹配。
        """
        sub = str(payload.get("sub") or "admin")
        role = str(payload.get("role") or "admin")
        if role not in ROLES:
            raise IdentityRejected(f"会话内角色非法：{role}", code="identity_bad_role")
        aud = str(payload.get("aud") or self.audience)
        if aud != self.audience:
            raise IdentityRejected("会话受众不匹配", code="identity_wrong_audience")
        try:
            exp = int(float(payload.get("exp") or 0))
        except (TypeError, ValueError):
            exp = 0
        identity = Identity(
            sub=sub, role=role, scopes=self._scopes(payload.get("scopes")),
            auth_method=str(payload.get("auth_method") or "session_cookie"),
            aud=aud, exp=exp, iat=int(float(payload.get("iat") or 0)),
            iss=str(payload.get("iss") or "qtg-session"),
        )
        if identity.auth_method not in AUTH_METHODS:
            raise IdentityRejected("会话登录方式非法", code="identity_bad_auth_method")
        return identity

    def _internal_identity(self) -> Identity:
        """仅持内部凭据的机器调用：请求级身份，exp 只活 60 秒。"""
        now = self._now()
        return Identity(
            sub="edge-service", role="internal_service",
            scopes=frozenset({"service"}), auth_method="internal_service",
            aud=self.audience, exp=now + 60, iat=now, iss="edge",
        )


def identity_of(request: Request) -> Optional[Identity]:
    """从 request.state 取已解析身份（闸门解析一次，全链路复用）。"""
    return getattr(request.state, "identity", None)


def audit_actor(identity: Optional[Identity]) -> str:
    """审计里的 actor：把「谁」写到最具体的一级（K05 可归因）。"""
    if identity is None:
        return "anonymous"
    return f"{identity.role}:{identity.sub}"


def audit_detail(identity: Optional[Identity]) -> dict:
    """审计 detail 里的身份断言（只含断言字段，不含任何 token）。"""
    return {"identity": identity.assertion() if identity else None}


__all__ = [
    "AUTH_METHODS", "AMBIENT_COOKIES", "EDGE_IDENTITY_HEADERS", "IDENTITY_HEADER",
    "INTERNAL_AUTH_HEADER", "Identity", "IdentityRejected", "IdentityResolver",
    "ROLES", "SPOOF_MARKERS", "audit_actor", "audit_detail", "identity_of",
    "parse_origin", "split_csv",
]
