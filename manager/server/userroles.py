"""Human roles. Legacy ``admin`` is the super-administrator storage code.

Machine token scopes remain a separate contract; never promote them by username.
"""
from __future__ import annotations

import copy

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
    return (method.upper(), path.rstrip('/')) in ACCOUNT_ROUTES
