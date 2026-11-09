"""鉴权与会话。

三种凭证，按可信度从高到低：

1. `Authorization: Bearer <admin_token>` —— 给脚本/CI/其他服务用；token 存文件，权限 0600。
2. 会话 Cookie（HttpOnly + SameSite=Strict + HMAC-SHA256 签名）—— 给浏览器面板用。
3. 无凭证 —— 默认只能读（`QTG_READ_REQUIRES_AUTH=1` 时连读也要凭证）。

关键取舍：**所有写操作必须凭证**，不接受「本机来源即信任」。
旧 preview-agent 的教训是 6 个写端点裸奔，其中 `/api/actions/account/add`
可以注入账号凭据、`/api/actions/notify` 可以改外发 webhook。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

from fastapi import Request

from .errors import Forbidden, Unauthorized

_COOKIE = "qtg_session"
# 浏览器会自动携带的会话 Cookie：只要出现任意一个，就说明这是「浏览器上下文」，
# 写请求必须过 CSRF 校验（K07）。`wb_session` 是 Manager 的会话，浏览器同样自动带。
AMBIENT_COOKIES = ("qtg_session", "wb_session")
_CLOCK_SKEW = 60


def _peer_ip(request: Request) -> str:
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "")


class CredentialStore:
    """从文件读取并缓存凭证；文件缺失时按需自动生成（仅限 session secret）。"""

    def __init__(self, token_file: Path, secret_file: Path, extra_files: Optional[list[str]] = None,
                 revoke_file: Optional[Path] = None,
                 internal_service_token_file: Optional[Path] = None,
                 identity_signing_key_file: Optional[Path] = None) -> None:
        self.token_file = token_file
        self.secret_file = secret_file
        self.extra_files = [Path(f) for f in (extra_files or []) if f]
        self._token: Optional[str] = None
        self._secret: Optional[bytes] = None
        self._mtimes: dict[str, float] = {}
        self._revoke: Optional[int] = None
        self.revoke_file = revoke_file
        # 边缘/内部服务凭据：可选。文件缺失或为空 = 内部服务身份关闭（默认安全）。
        self.internal_service_token_file = internal_service_token_file
        self._internal: Optional[str] = None
        # R3-L5：双值过渡的候选列表（<path>.next 在前、<path> 在后），按 mtime 失效。
        self._internal_list: Optional[list[str]] = None
        self.identity_signing_key_file = identity_signing_key_file

    def identity_signing_key(self) -> str:
        """Read the separate, protected assertion key; invalid configuration disables it.

        No fallback to the service token: its holder must not mint human identities.
        Read by descriptor each time so atomic rotation is visible without caching.
        """
        path = self.identity_signing_key_file
        if path is None:
            return ''
        try:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                return ''
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                    return ''
                if os.name == 'posix' and info.st_mode & 0o077:
                    return ''
                raw = os.read(fd, 4097).decode('utf-8').strip()
            finally:
                os.close(fd)
            if not 32 <= len(raw) <= 4096 or any(c.isspace() for c in raw):
                return ''
            # R3-L5：内部凭据可能是「current + .next」双值，任何一个相同都不允许
            for internal in self.internal_service_tokens():
                if internal and hmac.compare_digest(raw.encode(), internal.encode()):
                    return ''
            return raw
        except (OSError, UnicodeError):
            return ''

    # ---- 边缘服务凭据（K05，R3-L5 支持双值过渡）----
    INTERNAL_NEXT_SUFFIX = ".next"

    def internal_service_tokens(self) -> list[str]:
        """返回**当前可接受**的内部凭据列表（`<path>.next` 在前，`<path>` 在后）。

        轮换流程：把新值写到 `<path>.next`（原子写）→ 两边都被接受 → 改 Nginx
        include 为新值 → nginx -t + reload → 删除 `<path>.next`。过渡窗口内新旧
        两枚都可用，所以 reload 前后都不会出现 401 空窗。

        顺序：next 在前只是"优先展示/日志里标谁"，**不是信任级差异**——两枚都接受。
        文件缺失/为空一律跳过（不抛异常：内部凭据本来就是可选开关）。
        """
        if self.internal_service_token_file is None:
            return []
        path = str(self.internal_service_token_file)
        next_path = path + self.INTERNAL_NEXT_SUFFIX
        mtime_key = f"{self._mtime(path)}|{self._mtime(next_path)}"
        if self._mtimes.get("internal") != mtime_key or self._internal_list is None:
            tokens: list[str] = []
            for candidate in (next_path, path):
                raw = _read(candidate)
                if raw and raw not in tokens:
                    tokens.append(raw)
            self._internal_list = tokens
            self._mtimes["internal"] = mtime_key
        return list(self._internal_list)

    def internal_service_token(self) -> str:
        """返回内部服务凭据（首选值）；未配置时返回空串（不抛异常，可选开关）。

        为什么单独一枚令牌、单独一个头：
        * 它是**机器身份**，与浏览器能自动携带的 Cookie/Authorization 必须可区分；
        * 运维令牌（admin_token）留在 Authorization 里给脚本用，内部凭据放
          `X-QTG-Internal-Auth`，Nginx 负责在入口清掉客户端伪造的同名值；
        * 两枚令牌不共用一个文件，泄露其一不会同时拿到另一类身份。

        R3-L5 起支持 `<path>.next` 双值：`internal_service_tokens()` 返回全部可接受值，
        本方法保留为"首选值"的兼容入口。
        """
        tokens = self.internal_service_tokens()
        return tokens[0] if tokens else ""

    # ---- 管理 token ----
    def token(self) -> str:
        path = str(self.token_file)
        mtime = self._mtime(path)
        if self._token is None or self._mtimes.get(path) != mtime:
            raw = _read(path)
            if not raw:
                raise Unauthorized(
                    f"管理令牌未配置：{self.token_file} 不存在或为空", code="no_admin_token"
                )
            self._token = raw
            self._mtimes[path] = mtime
        return self._token

    # ---- 会话签名密钥 ----
    SECRET_PREVIOUS_SUFFIX = ".previous"

    def previous_secret(self) -> Optional[bytes]:
        """轮换过渡期的**旧**会话签名密钥（`<secret_file>.previous`）。

        R3-L5：轮换会话密钥不可能「瞬时切换」——已签发的 Cookie 是用旧密钥签的。
        流程：① 把当前 secret 原子复制为 `<secret_file>.previous`；② 用新值原子替换
        `<secret_file>`；③ 过渡期结束（≥ 会话总寿命或业务允许的最长会话）删除
        `.previous`。本方法只在**验证失败后**才被调用，签发永远用当前 secret。
        """
        path = str(self.secret_file) + self.SECRET_PREVIOUS_SUFFIX
        raw = _read(path)
        return raw.encode("utf-8") if raw else None

    def secret(self) -> bytes:
        path = str(self.secret_file)
        mtime = self._mtime(path)
        if self._secret is None or self._mtimes.get(path) != mtime:
            raw = _read(path)
            if not raw:
                raw = secrets.token_hex(32)
                try:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    Path(path).write_text(raw, encoding="utf-8")
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            self._secret = raw.encode("utf-8")
            self._mtimes[path] = mtime
        return self._secret

    @staticmethod
    def _mtime(path: str) -> float:
        try:
            return os.stat(path).st_mtime
        except OSError:
            return 0.0

    def verify_password(self, candidate: str) -> bool:
        """接受：qtg 管理令牌、以及兼容配置的旧面板令牌/面板密码。

        升级后用户手上往往只有旧令牌，强行只认新令牌会造成「进不去」。
        额外文件同样要求 0600 且属于本机 root，可信边界不变。
        """
        candidate = (candidate or "").strip()
        try:
            want = self.token()
        except Unauthorized:
            want = ""
        if want and hmac.compare_digest(candidate.encode(), want.encode()):
            return True
        for path in self.extra_files:
            try:
                extra = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if extra and hmac.compare_digest(candidate.encode(), extra.encode()):
                return True
        return False

    # ---- 会话令牌 ----
    def issue_session(self, ttl: int, now: Optional[float] = None, *,
                      sub: str = "admin", role: str = "admin",
                      auth_method: str = "session_cookie",
                      scopes: Optional[Iterable[str]] = None,
                      aud: str = "qtg") -> str:
        """签发会话。保留 `sub=admin` 默认值，兼容既有测试与旧客户端。

        K05 要求身份断言保留 sub/role/scopes/auth_method/aud/exp：这里把它们
        直接写进会话载荷，`IdentityResolver` 读取时按同一套规则校验。
        """
        ts = int(now if now is not None else time.time())
        payload = {
            "sub": sub,
            "role": role,
            "scopes": sorted(scopes or []),
            "auth_method": auth_method,
            "aud": aud,
            "iat": ts,
            "exp": ts + ttl,
            "n": secrets.token_hex(8),
        }
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64(hmac.new(self.secret(), body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def _verify_signature(self, body: str, sig: str, secret: bytes) -> bool:
        expect = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
        return hmac.compare_digest(sig, expect)

    def verify_session(self, token: str, now: Optional[float] = None) -> Optional[dict]:
        """验签会话。R3-L5：先当前 secret，失败后**仅在存在 `.previous` 时**再试旧值。

        这样会话密钥轮换有过渡窗口：改密钥后旧 Cookie 仍可验，直到删除 `.previous`；
        签发（`issue_session`）永远用当前 secret，不会把流量钉在旧值上。
        """
        if not token or "." not in token:
            return None
        body, sig = token.rsplit(".", 1)
        if not self._verify_signature(body, sig, self.secret()):
            previous = self.previous_secret()
            if not previous or not self._verify_signature(body, sig, previous):
                return None
        try:
            payload = json.loads(_b64d(body))
        except Exception:
            return None
        ts = now if now is not None else time.time()
        if float(payload.get("exp", 0)) + _CLOCK_SKEW < ts:
            return None
        if float(payload.get("iat", 0)) < self.revoke_epoch():
            return None
        return payload


    def revoke_epoch(self) -> int:
        """当前吊销纪元：iat 早于它的会话一律失效。"""
        if not self.revoke_file:
            return 0
        path = str(self.revoke_file)
        mtime = self._mtime(path)
        if self._revoke is None or self._mtimes.get("revoke") != mtime:
            try:
                self._revoke = int(_read(path) or 0)
            except ValueError:
                self._revoke = 0
            self._mtimes["revoke"] = mtime
        return int(self._revoke or 0)

    def revoke_sessions(self) -> int:
        """吊销此前签发的全部会话（不换签名密钥，只把纪元推到「当前 + 1 秒」）。"""
        epoch = int(time.time()) + 1
        self._revoke = epoch
        if self.revoke_file:
            p = Path(self.revoke_file)
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(str(epoch), encoding="utf-8")
                os.chmod(p, 0o600)
                self._mtimes["revoke"] = self._mtime(str(p))
            except OSError:
                pass
        return epoch


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def split_csv(value: str) -> list[str]:
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def peer_is_trusted(peer: str, trusted: Iterable[str]) -> bool:
    """判断对端是否可信边缘（IP 精确匹配或 CIDR 命中）。"""
    peer = (peer or "").strip()
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        # 非 IP 形态（例如测试客户端的 "testclient"）：按字面量比对，便于显式配置
        return any(peer == str(t).strip() for t in trusted)
    for item in trusted:
        text = str(item).strip()
        if not text:
            continue
        try:
            if "/" in text:
                if addr in ipaddress.ip_network(text, strict=False):
                    return True
            elif addr == ipaddress.ip_address(text):
                return True
        except ValueError:
            continue
    return False


def parse_origin(value: str) -> Optional[tuple[str, str, int]]:
    """把 Origin/Referer 归一化成 (scheme, host, port)；非法返回 None。

    必须自己拆，不能用 `host in source` 这类子串判断：
    `"testserver" in "https://testserver.evil.example"` 为真（相似域名绕过），
    `"testserver" in "https://evil.example/?h=testserver"` 也为真。
    """
    raw = (value or "").strip()
    if not raw or raw.lower() == "null":
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    if parts.username or parts.password:
        return None          # userinfo 形态一律拒绝（https://host@evil）
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, host.lower().rstrip("."), int(port))


class Auth:
    """鉴权依赖集合；由 main.py 注入到路由。"""

    def __init__(self, store: CredentialStore, *, read_requires_auth: bool, ttl: int,
                 allowed_origins: str = "", trusted_proxies: Iterable[str] = ()) -> None:
        self.store = store
        self.read_requires_auth = read_requires_auth
        self.ttl = ttl
        self.trusted_proxies = [str(p).strip() for p in trusted_proxies if str(p).strip()]
        self.allowed_origins = {
            norm for norm in (parse_origin(o) for o in split_csv(allowed_origins)) if norm
        }

    # ---- 解析 ----
    @staticmethod
    def bearer(request: Request) -> str:
        raw = request.headers.get("authorization", "")
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return ""

    def cookie(self, request: Request) -> str:
        return request.cookies.get(_COOKIE, "")

    def _is_admin(self, request: Request) -> bool:
        # K05 修复：认证已在 `_auth_gate` 统一解析成 Identity（含内部服务身份）。
        # 这里优先复用它，避免"闸门认可、业务依赖不认可"的分裂——内部服务调用
        # 曾在闸门通过、到 require_admin 又 401（两处各有一套判定）。
        ident = getattr(request.state, "identity", None)
        if ident is not None:
            return getattr(ident, "role", "") in ("admin", "internal_service")
        tok = self.bearer(request)
        if tok:
            # 必须复用 verify_password：它同时认主令牌与兼容令牌（旧面板令牌等）。
            # 之前这里直接比对主令牌，导致「登录能进、API 调用 401」的不一致。
            if self.store.verify_password(tok):
                return True
        sess = self.cookie(request)
        if sess and self.store.verify_session(sess):
            return True
        return False

    # ---- FastAPI 依赖 ----
    async def require_admin(self, request: Request) -> str:
        """写操作/敏感读：必须是 admin 凭证。"""
        if not self._is_admin(request):
            raise Unauthorized("需要管理凭证：Authorization: Bearer <token> 或先登录面板")
        return "admin"

    async def require_read(self, request: Request) -> str:
        ident = getattr(request.state, "identity", None)
        authenticated_reader = ident is not None and ident.role in (
            "admin", "viewer", "readonly", "internal_service")
        if self.read_requires_auth and not (authenticated_reader or self._is_admin(request)):
            raise Unauthorized("读取需要登录凭证")
        return "reader"

    # ---- CSRF（K07）----
    @staticmethod
    def ambient_session(request: Request) -> str:
        """返回浏览器会自动携带的会话 Cookie 名；没有则空串。

        这是 CSRF 判定的**唯一**依据：浏览器跨站表单会自动带 Cookie，但不会
        自动带 Authorization，所以「请求里有 Bearer」不能豁免 Origin 校验。
        """
        for name in AMBIENT_COOKIES:
            if request.cookies.get(name):
                return name
        return ""

    def trusted_peer(self, request: Request) -> bool:
        """请求是否来自可信边缘（可信反代/回环）。"""
        return peer_is_trusted(_peer_ip(request), self.trusted_proxies)

    def expected_origin(self, request: Request) -> Optional[tuple[str, str, int]]:
        """从 Host + （可信边缘时的）X-Forwarded-Proto 推出期望来源。"""
        host_header = (request.headers.get("host") or "").strip()
        if not host_header:
            return None
        scheme = request.url.scheme or "http"
        if peer_is_trusted(_peer_ip(request), self.trusted_proxies):
            xfp = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
            if xfp in ("http", "https"):
                scheme = xfp
        return parse_origin(f"{scheme}://{host_header}")

    def check_origin(self, request: Request, *, strict: bool = True) -> None:
        """Cookie 会话下的状态改变请求必须做严格 Origin/Referer 校验。

        与旧实现的差别（旧实现是 K07 的漏洞本体）：
        1. 旧实现见 `Authorization: Bearer` 就 return —— 而 Nginx 会给浏览器请求
           注入内部 Bearer，于是浏览器写请求全部跳过 CSRF 保护；
        2. 旧实现用 `host not in source` 子串判断 —— 相似域名
           (`testserver.evil.example`) 与拼在 query 里的 host 都能绕过；
        3. 旧实现不看 scheme/port —— http↔https、80↔8080 混用都能过。

        新规则：**有没有浏览器会话 Cookie** 决定要不要校验，Bearer 不参与豁免；
        校验时把来源拆成 (scheme, host, port) 三元组做全等比较。
        """
        cookie_name = self.ambient_session(request)
        if not cookie_name:
            # 纯机器调用：没有浏览器会自动携带的凭证，不存在 CSRF 语义。
            return
        origins = [o for o in request.headers.getlist("origin") if o.strip()]
        if len(origins) > 1:
            raise Forbidden("请求带有多个 Origin 头，按伪造处理", code="csrf_multiple_origin")
        origin = origins[0].strip() if origins else ""
        referer = (request.headers.get("referer") or "").strip()
        source = origin or referer
        if not source:
            raise Forbidden(
                f"浏览器会话（{cookie_name}）的写操作必须带 Origin/Referer",
                code="csrf_origin_missing",
            )
        if not strict:
            # 仅用于排障：仍然解析，但不做比对（默认不可达，见 QTG_CSRF_STRICT_ORIGIN）
            return
        expected = self.expected_origin(request)
        if expected is None:
            raise Forbidden("无法确定本站来源（缺少或非法的 Host 头）", code="csrf_host_missing")
        got = parse_origin(source)
        if got is None:
            raise Forbidden(f"来源格式非法，拒绝：{source[:120]}", code="csrf_origin_malformed")
        if got == expected or got in self.allowed_origins:
            return
        raise Forbidden(
            f"请求来源与本站不一致：{source[:120]} != {expected[0]}://{expected[1]}:{expected[2]}",
            code="csrf_origin_mismatch",
        )

    def check_origin_machine(self, request: Request, *, strict: bool = True) -> None:
        """无 Cookie 但带 Origin 的请求：机器调用不该带 Origin，带了对不上就拒绝。

        这一条是纵深防御：即便有人把内部凭据塞进浏览器上下文（例如被注入的前端
        脚本），跨站来源也无法借内部凭据执行写操作。`QTG_ALLOWED_ORIGINS` 可显式豁免。
        """
        origin = (request.headers.get("origin") or "").strip()
        if not origin or not strict:
            return
        expected = self.expected_origin(request)
        got = parse_origin(origin)
        if expected is None or got is None or got == expected or got in self.allowed_origins:
            return
        raise Forbidden(
            f"无会话的机器调用带了外部 Origin：{origin[:120]}",
            code="csrf_cross_origin_machine",
        )

    # ---- Cookie helpers ----
    @staticmethod
    def cookie_name() -> str:
        return _COOKIE
