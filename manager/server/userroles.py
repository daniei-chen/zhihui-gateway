"""Human roles: full panel reads, with account addition as the only limited write.

Legacy ``admin`` is the super-administrator storage code.

Machine token scopes remain a separate contract; never promote them by username.
"""
from __future__ import annotations

import copy
import re

SUPER_ADMIN = 'admin'
ACCOUNT_ADMIN = 'account_admin'
POLICY_VERSION = 2
HUMAN_ROLES = (SUPER_ADMIN, ACCOUNT_ADMIN)

ACCOUNT_ROUTES = frozenset({
    ('GET', '/api/me'),
    ('GET', '/api/authz/probe'),
    ('GET', '/api/system/internal-session-verify'),
    ('POST', '/api/auth/start'),
    ('GET', '/api/auth/poll'),
    ('POST', '/api/auth/finish'),
    ('POST', '/api/auth/cancel'),
    ('POST', '/api/auth/review'),
})

# Page visibility is independent of write authority. Keep new/credential-export
# endpoints closed until their response and side effects have been reviewed.
PANEL_READ_ROUTES = frozenset({
    '/api/accounts', '/api/status', '/api/models', '/api/model-catalog',
    '/api/accounts/credits-snapshot', '/api/accounts/refresh-plan',
    '/api/accounts/daily-plan', '/api/task-run', '/api/task-claim-schedule',
    '/api/checkin-logs', '/api/task-logs', '/api/logs', '/api/logs/outcomes',
    '/api/logs/attempts', '/api/logs/attempts/billing', '/api/logs/attempts/explain',
    '/api/audit-logs', '/api/keys', '/api/upstreams', '/api/proxies',
    '/api/proxy-routes', '/api/proxy-routes/bindings', '/api/red-packets',
    '/api/settings/upstream', '/api/settings/model-map', '/api/settings/panel',
    '/api/settings/proxy', '/api/settings/github-token', '/api/settings/github-mirror',
    '/api/settings/pg-sync', '/api/settings/pg-sync/status',
    '/api/security/config', '/api/security/rules', '/api/security/logs',
    '/api/users', '/api/tokens', '/api/upstream/reload-state',
    '/api/system/versions', '/api/system/check-update', '/api/system/update-status',
    '/api/system/update-package', '/api/system/changelog',
    '/api/system/config', '/api/system/configuration', '/api/system/drain',
    '/api/operations/health', '/api/budgets/unresolved', '/api/playground/models',
    '/api/engine/overview',
})
PANEL_READ_PATTERNS = (
    r'/api/stats/(?:summary|outcomes|daily|hourly|credits|by-model|by-key|upstream|credit-reconcile)',
    r'/api/accounts/[^/]+/credits',
    r'/api/red-packets/[0-9]+',
    r'/api/v1/(?:overview|accounts|usage|tasks|keys|models|logs|alerts|requests|ops|settings|audit)',
)


def migration(source: dict) -> tuple[dict, int]:
    """One-time explicit migration; preserve credentials and invalidate changed roles."""
    if 'role_policy_version' in source and source.get('role_policy_version') != POLICY_VERSION:
        raise ValueError('不支持的角色策略版本；未修改用户文件')
    result = copy.deepcopy(source)
    users = result.get('users')
    if (not isinstance(users, list) or any(not isinstance(u, dict) for u in users)
            or any(not isinstance(u.get('username'), str) or not u['username'].strip() for u in users)
            or sum(u.get('username') == 'admin' for u in users) != 1
            or len({u.get('username') for u in users}) != len(users)):
        raise ValueError('角色迁移要求唯一的 admin 主账号；未修改用户文件')
    changed = 0
    for user in users:
        sv = user.get('sv', 0)
        if isinstance(sv, bool) or not isinstance(sv, int) or sv < 0:
            raise ValueError('用户会话版本异常；未修改用户文件')
        if source.get('role_policy_version') == POLICY_VERSION:
            if user.get('role') not in HUMAN_ROLES or (user['username'] == 'admin' and user['role'] != SUPER_ADMIN):
                raise ValueError('用户角色策略异常；未修改用户文件')
            continue
        role = SUPER_ADMIN if user.get('username') == 'admin' else ACCOUNT_ADMIN
        if user.get('role') != role:
            user['role'] = role
            user['sv'] = sv + 1
            changed += 1
    result['role_policy_version'] = POLICY_VERSION
    return (source, 0) if source.get('role_policy_version') == POLICY_VERSION else (result, changed)


def account_route_allowed(method: str, path: str) -> bool:
    method, path = method.upper(), path.rstrip('/')
    if (method, path) in ACCOUNT_ROUTES:
        return True
    return method in {'GET', 'HEAD'} and (path in PANEL_READ_ROUTES or any(
        re.fullmatch(pattern, path) for pattern in PANEL_READ_PATTERNS))
