"""Short-lived, request-scoped Manager identity assertions for QTG.

The edge must strip client-supplied assertions and forward this header only after
auth_request succeeds. QTG still authorizes the actual method/path server-side.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import time

from fastapi import HTTPException, Request

from . import config


#: 身份签名密钥的默认位置。
#:
#: ## 为什么需要这个兜底
#:
#: 这个常量在 2026-11-15 之前是**硬编码在路由里**的。当天把它改成「复用本模块的
#: 校验」时，本模块读的是配置项 `QTG_IDENTITY_SECRET_FILE` ——
#: **而那个配置项在部署里从来没设过**（默认空串）。
#:
#: 后果是：管理员密码正确、登录接口返回 200，但 nginx 的鉴权子请求读密钥拿到空值
#: 返回 503，于是**登录成功也进不去面板**。三小时内 19 次 503。
#:
#: 原来的说法是「核过生产上密钥文件都是 0600」——**核的是文件权限，没核配置项的
#: 取值**。文件权限确实对，但那个值根本没被读到。
#:
#: 现在的语义：**配置项优先，缺省回落这个路径**。这样：
#:
#:   · 没设配置的部署（当前生产就是）行为与改动前一致
#:   · 要换位置的部署仍可显式指定
#:   · 两条路都走同一套严格校验，不存在「哪条更宽松」
DEFAULT_IDENTITY_SECRET_FILE = '/opt/workbuddy-manager/engine/secrets/identity_signing.key'


def identity_secret_path() -> str:
    """解析要读的密钥路径：配置项优先，缺省回落默认位置。"""
    configured = (getattr(config, 'QTG_IDENTITY_SECRET_FILE', '') or '').strip()
    return configured or DEFAULT_IDENTITY_SECRET_FILE


def _secret(path: str | None = None) -> str:
    path = path or identity_secret_path()
    if not path:
        return ''
    # 文件不存在时返回 ''，而不是让 `lstat` 抛出去。
    #
    # 这个函数其它每一条失败路径都返回 ''：路径为空、是符号链接、不是常规文件、
    # 太大、权限过宽、内容不合规。**只有「文件不存在」例外，它抛异常。**
    #
    # 调用方都写了 `except OSError`，所以行为上不丢；但接口不一致本身有代价：
    # 任何新增调用方都得知道「这里要 try」，漏了就会把一个「密钥还没部署」的
    # 正常状态变成 500。
    #
    # 2026-11-15 补测试时撞到：期望 '' 却拿到 FileNotFoundError。
    # 修在这里，而不是在测试里绕开。
    try:
        info_lstat = os.lstat(path)
    except FileNotFoundError:
        return ''
    except OSError:
        return ''
    # Reject symlinks on Windows too; O_NOFOLLOW additionally closes the POSIX race.
    if stat.S_ISLNK(info_lstat.st_mode):
        return ''
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        # 从 lstat 到 open 之间文件被删掉的竞态：同样按「没有密钥」处理。
        return ''
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            return ''
        if os.name == 'posix' and info.st_mode & 0o077:
            return ''
        raw = os.read(fd, 4097).decode('utf-8').strip()
        return raw if 32 <= len(raw) <= 4096 and not any(c.isspace() for c in raw) else ''
    finally:
        os.close(fd)


def assertion(request: Request, user: dict) -> str:
    try:
        secret = _secret()
    except (OSError, UnicodeError):
        secret = ''
    if not secret:
        raise HTTPException(status_code=503, detail='身份桥接凭据未就绪')
    token_id = getattr(request.state, 'token_id', None)
    if token_id is not None:
        scope = user.get('scope')
        sub, method = f'manager-token:{token_id}', 'admin_token'
        if scope == 'keys':
            role, scopes = 'internal_service', ['keys:read', 'keys:write']
        elif scope == 'admin':
            role, scopes = 'admin', ['*']
        else:
            role, scopes = 'viewer', ['read']
    else:
        sub, method = str(user.get('username') or ''), 'edge_session'
        role = 'admin' if user.get('role') == 'admin' else 'viewer'
        scopes = ['*'] if role == 'admin' else ['read']
    if not sub:
        raise HTTPException(status_code=401, detail='未登录')
    now = int(time.time())
    payload = {'sub': sub, 'role': role, 'scopes': scopes, 'auth_method': method,
               'aud': 'qtg', 'iss': 'manager', 'iat': now, 'exp': now + 60,
               'jti': secrets.token_hex(16)}
    body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).decode().rstrip('=')
    key = hashlib.sha256(b'qtg-identity-v1\0' + secret.encode()).digest()
    signature = base64.urlsafe_b64encode(hmac.new(key, body.encode(), hashlib.sha256).digest()).decode().rstrip('=')
    return f'v1.{body}.{signature}'
