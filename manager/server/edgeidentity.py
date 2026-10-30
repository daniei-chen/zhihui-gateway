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


def _secret() -> str:
    path = config.QTG_IDENTITY_SECRET_FILE
    if not path:
        return ''
    # Reject symlinks on Windows too; O_NOFOLLOW additionally closes the POSIX race.
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
