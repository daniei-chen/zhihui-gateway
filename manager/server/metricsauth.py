"""Authenticate Prometheus without granting a collector management authority."""
from __future__ import annotations

import os
import secrets
import stat

from fastapi import Request

from . import config, security


def _collector_secret() -> str:
    if config.METRICS_TOKEN_FILE:
        fd = os.open(config.METRICS_TOKEN_FILE,
                     os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return ''
            if os.name == 'posix' and info.st_mode & 0o077:
                return ''
            value = os.read(fd, 4097).decode('utf-8').strip()
        finally:
            os.close(fd)
    else:
        value = config.METRICS_TOKEN.strip()
    # Short/missing/malformed credentials fail closed; no loopback bypass.
    return value if (value.startswith('wbm_') and 32 <= len(value) <= 4096
                     and not any(c.isspace() for c in value)) else ''


def require_metrics(request: Request) -> dict:
    authorization = request.headers.get('authorization', '')
    supplied = authorization[7:].strip() if authorization.lower().startswith('bearer ') else ''
    try:
        configured = _collector_secret()
    except (OSError, UnicodeError):
        configured = ''
    if configured and supplied and secrets.compare_digest(configured.encode(), supplied.encode()):
        actor = {'username': 'collector:metrics', 'role': 'metrics', 'scope': 'metrics'}
        request.state.audit_actor = actor['username']
        return actor
    # Existing session/admin API tokens remain useful for manual diagnosis.
    # A collector secret is not registered in tokensvc and cannot pass this path.
    return security.require_admin(security.current_user(request))
