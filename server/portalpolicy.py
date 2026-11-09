"""Immutable customer data-plane policy, independent of editable key settings."""
from __future__ import annotations

import hmac
import os

from . import portalstore as store

SOURCE = 'portal-v1'
MODELS = ('deepseek-v4-flash', 'deepseek-v4.1-flash', 'deepseek-v4-pro')


def enabled():
    # Startup opt-in only; adding portal routes must never enable dispatch by itself.
    return os.environ.get('WB_PORTAL_PUBLIC_POOL_ENABLED') == '1'


def is_portal(key):
    return key.get('source') == SOURCE


def model_allowed(model):
    return isinstance(model, str) and model.removeprefix('cn:') in MODELS


def identity(key, conn):
    store.require_not_quarantined(conn)
    row = conn.execute('SELECT l.owner_id,l.key_hash,k.revoked,k.owner_sv,u.sv,u.enabled '
        'FROM portal_key_links l JOIN portal_keys k ON k.key_id=l.key_id '
        'JOIN portal_users u ON u.id=l.owner_id WHERE l.key_id=?', (key['id'],)).fetchone()
    if (not row or row['revoked'] or not row['enabled'] or row['owner_sv'] != row['sv']
            or not hmac.compare_digest(row['key_hash'], key.get('credential_hash', ''))):
        raise ValueError('用户密钥或用户授权已失效')
    return row['owner_id']


def problem(key, model=None, mapped=None, platform='workbuddy', *, listing=False):
    if not is_portal(key):
        return None
    if platform != 'workbuddy' or key.get('upstream_id') is not None:
        return '用户密钥仅限默认 WorkBuddy 国内公共池'
    try:
        with store.transaction() as conn:
            identity(key, conn)
    except Exception:
        return '用户密钥归属或账本不可用，未放行调用'
    if not listing and not enabled():
        return '用户公共池调用尚未启用；密钥有效，可读取模型目录，暂不能发起计费调用'
    if not listing and (not model_allowed(model) or not model_allowed(mapped or model)):
        return '用户端仅允许 DeepSeek 国内模型，映射目标也必须在白名单内'
    return None
