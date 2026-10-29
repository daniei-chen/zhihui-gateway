"""U17：统一作用域上下文（platform + realm + source_authority + config_generation）。

## 为什么需要这个模块

审计 U17：平台、版本（realm）、数据权威来源、以及「配置第几代」此前散在四处 ——

* `platformpolicy` 管平台绑定（路径前缀 / `X-QTG-Platform` / 模型名前缀）；
* `db.realm_of_model` 管版本（`global:` 前缀 → global，其余 → cn）；
* 每个 router 自己在注释里说明「这份数据来自上游还是本地库」；
* 缓存 key 则是 `'aggregate'` / `'qoder.models'` 这种**不含作用域**的字面量。

后果有两条，都是"看起来正常、实际上不可验证"：

1. **缓存 key 看不出作用域**：配置改了（上游 config.json、模型映射）缓存 key
   不变，仍然命中旧条目 —— 用户看到的是"改了不生效"；也无法从 key 判断这条
   缓存属于哪个平台/版本。
2. **平台提示可能被静默忽略**：客户端带 `X-QTG-Platform: qoder` 请求一个不在
   平台路由白名单里的路径时，`main._qtg_platform_route` 不会改写路径，请求落到
   管理端原生 handler，返回**本地库**数据 —— 而调用方以为自己拿到的是 qoder
   的数据。没有任何字段能区分这两种情况。

本模块给出唯一的作用域来源：`Scope`（四维 + 「请求了什么 / 采纳了吗 / 为什么
没采纳」）、唯一的缓存 key 构造器 `scoped_key()`、配置指纹
`config_generation()`，以及把请求解析成作用域的 `for_request()`。

**冲突与降级**：能确定的显式带上；不能采纳的**显式降级**并写进响应元数据
（`degraded` 列表），绝不静默取其一。真正自相矛盾的输入（未知平台、非法
realm）抛 `ScopeConflict`，由调用方转成 400 + 稳定 code。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from .. import platformpolicy

#: 不限定平台/版本时的取值（响应里显式写 `all`，而不是省略字段）。
PLATFORM_ALL = 'all'
REALM_ALL = 'all'
REALMS = ('cn', 'global')

#: 数据权威来源。`mixed` = 一次响应里混了多个平台的权威数据（聚合端点）。
AUTHORITIES = ('local_db', 'upstream', 'derived', 'mixed')

#: 显式降级原因（写进响应元数据的 `degraded` 数组）。
DEGRADED_PLATFORM_NOT_ROUTABLE = 'platform_not_routable'


class ScopeConflict(ValueError):
    """作用域输入非法或自相矛盾；调用方转成 400 + 稳定 `code`。"""

    def __init__(self, message: str, code: str = 'scope_conflict'):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Scope:
    """一次读请求的作用域。四个维度都会进缓存 key 与响应元数据。"""

    platform: str = PLATFORM_ALL
    realm: str = REALM_ALL
    source_authority: str = 'local_db'
    config_generation: str = ''
    #: 客户端**显式请求**的平台（`X-QTG-Platform` 或 `?platform=`）；空 = 没指定。
    requested_platform: str = ''
    #: 请求的平台是否真的决定了数据来源。False 表示已降级（见 `degraded`）。
    honored: bool = True
    degraded: tuple[str, ...] = ()

    def cache_key(self, name: str) -> str:
        """缓存 key：`<name>|p=<platform>|r=<realm>|g=<config_generation>`。

        **前缀语义保留**：`name` 在最前面，所以 `cache.invalidate('aggregate')`
        仍然能清掉所有代/所有作用域的同名条目（既有 `/cache/invalidate?prefix=`
        行为不变）。
        """
        return f'{name}|p={self.platform}|r={self.realm}|g={self.config_generation}'

    def metadata(self) -> dict:
        """响应元数据（`payload['scope']`）。字段名固定，便于客户端与测试断言。"""
        out: dict[str, Any] = {
            'platform': self.platform,
            'realm': self.realm,
            'source_authority': self.source_authority,
            'config_generation': self.config_generation,
            'platform_requested': self.requested_platform or None,
            'platform_honored': self.honored,
        }
        if self.degraded:
            out['degraded'] = list(self.degraded)
        return out

    def with_authority(self, authority: str) -> 'Scope':
        if authority not in AUTHORITIES:
            raise ScopeConflict(f'未知的数据权威来源：{authority!r}', 'invalid_authority')
        return Scope(platform=self.platform, realm=self.realm,
                     source_authority=authority,
                     config_generation=self.config_generation,
                     requested_platform=self.requested_platform,
                     honored=self.honored, degraded=self.degraded)


# ── 配置指纹（config_generation）─────────────────────────────────────────
#
# 组成：所有"能改变缓存内容"的输入。刻意**只 stat 不读文件内容**：这个函数会被
# 每个带缓存的读请求调用一次，读一遍 config.json 不值得。
_GEN_TTL = 1.0
_gen_lock = threading.Lock()
_gen_state: dict[str, Any] = {'at': 0.0, 'value': '', 'bump': 0}


def _upstream_config_fingerprint() -> str:
    try:
        from .. import config
        st = os.stat(config.UPSTREAM_CONFIG)
        return f'{st.st_mtime_ns}:{st.st_size}'
    except (OSError, AttributeError, TypeError):
        return 'missing'


def _model_map_fingerprint() -> str:
    """模型别名映射：改变"模型名 → 上游模型/版本"的解析结果。"""
    try:
        from .. import db
        raw = db.get_setting('model_map', {}) or {}
        text = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]
    except Exception:  # noqa: BLE001 - 指纹算不出来不该让读请求失败
        return 'unavailable'


def config_generation(*, force: bool = False) -> str:
    """当前配置指纹（12 位 hex）。

    进缓存 key：配置一变，key 就变，旧条目自然不再命中（等价于"改了立刻生效"）。
    进响应元数据：调用方能判断"我看到的这份数据是哪一代配置下的"。

    `force=True` 跳过 1 秒 memo（测试与显式失效路径用）。
    """
    now = time.monotonic()
    with _gen_lock:
        if not force and _gen_state['value'] and now - _gen_state['at'] < _GEN_TTL:
            return str(_gen_state['value'])
        parts = [
            _upstream_config_fingerprint(),
            _model_map_fingerprint(),
            ','.join(platformpolicy.PLATFORMS),
            str(_gen_state['bump']),
        ]
        value = hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()[:12]
        _gen_state['at'] = now
        _gen_state['value'] = value
        return value


def bump_generation() -> str:
    """显式推进一代（配置保存路径、测试用）。返回新指纹。"""
    with _gen_lock:
        _gen_state['bump'] = int(_gen_state['bump']) + 1
        _gen_state['at'] = 0.0
        _gen_state['value'] = ''
    return config_generation(force=True)


def reset_generation() -> None:
    """把指纹计数器归零（**仅供测试**；生产不要调用）。"""
    with _gen_lock:
        _gen_state.update({'at': 0.0, 'value': '', 'bump': 0})


# ── 输入解析（冲突显式化）────────────────────────────────────────────────
def resolve_realm(value: object) -> str:
    """把请求里的 realm 参数归一；空 = 不限定（`all`），非法 = 显式 400。

    改动前 `routers/stats.py` 等处的写法是 `realm in ('cn','global')` 才过滤，
    其余值**静默退化成不过滤** —— 客户端写 `realm=global ` 或拼错成 `glboal`
    会拿到"全部版本"的数据而毫无提示。这里统一成显式报错（`invalid_realm`）。
    """
    text = str(value or '').strip().lower()
    if not text:
        return REALM_ALL
    if text not in REALMS:
        raise ScopeConflict(
            f'realm 只接受 {" / ".join(REALMS)}（收到 {text[:16]!r}）', 'invalid_realm')
    return text


def requested_platform(request) -> str:
    """客户端显式请求的平台：`X-QTG-Platform` 优先，其次 `?platform=`。

    与 `main._qtg_platform_route` 的取值顺序一致（header 优先、query 兜底），
    所以两处不会得出不同结论。
    """
    raw = (request.headers.get('x-qtg-platform') or '').strip().lower()
    if not raw:
        try:
            raw = (request.query_params.get('platform') or '').strip().lower()
        except Exception:  # noqa: BLE001 - 没有 query 的调用方（纯 ASGI 测试）
            raw = ''
    if not raw:
        return ''
    if raw not in platformpolicy.PLATFORMS:
        raise ScopeConflict(f'未知平台：{raw[:24]!r}', 'invalid_platform')
    return raw


def for_request(request, *, authority: str = 'local_db', realm: object = None,
                platform_routed: bool = False) -> Scope:
    """把请求解析成 `Scope`。

    `authority`：本端点的数据权威来源（`local_db` / `upstream` / `derived` /
    `mixed`）。`platform_routed`：调用方是否**真的**按平台取数（`__qtg` 兼容层
    是；管理端原生 handler 不是）。当客户端显式请求了平台、而本端点并不按平台
    取数时，`honored=False` 且 `degraded=['platform_not_routable']` ——
    这就是"显式降级"：调用方能看出"我要的平台没被采纳"，而不是拿到本地库数据
    却以为它是平台数据。
    """
    requested = requested_platform(request)
    resolved_realm = resolve_realm(realm)
    if authority not in AUTHORITIES:
        raise ScopeConflict(f'未知的数据权威来源：{authority!r}', 'invalid_authority')
    degraded: list[str] = []
    honored = True
    if requested:
        honored = bool(platform_routed)
        if not honored:
            degraded.append(DEGRADED_PLATFORM_NOT_ROUTABLE)
    return Scope(platform=requested or PLATFORM_ALL,
                 realm=resolved_realm,
                 source_authority=authority,
                 config_generation=config_generation(),
                 requested_platform=requested,
                 honored=honored,
                 degraded=tuple(degraded))


def fixed(platform: str = PLATFORM_ALL, realm: str = REALM_ALL, *,
          authority: str = 'local_db') -> Scope:
    """给"与请求无关"的调用点（缓存预热、后台任务、测试）用的固定作用域。"""
    if platform != PLATFORM_ALL and platform not in platformpolicy.PLATFORMS:
        raise ScopeConflict(f'未知平台：{platform!r}', 'invalid_platform')
    if realm != REALM_ALL and realm not in REALMS:
        raise ScopeConflict(f'未知 realm：{realm!r}', 'invalid_realm')
    if authority not in AUTHORITIES:
        raise ScopeConflict(f'未知的数据权威来源：{authority!r}', 'invalid_authority')
    return Scope(platform=platform, realm=realm, source_authority=authority,
                 config_generation=config_generation())


def scoped_key(name: str, scope: Scope | None = None) -> str:
    """缓存 key 的唯一构造器。`scope=None` = 用"全平台/全版本"的固定作用域。"""
    return (scope or fixed(authority='mixed')).cache_key(name)


def attach(payload: object, scope: Scope) -> object:
    """把作用域元数据挂到**字典**响应上（Pydantic 模型原样返回，见 notes）。"""
    if isinstance(payload, dict):
        payload['scope'] = scope.metadata()
    return payload


def apply_headers(response, scope: Scope) -> None:
    """把同一份作用域写进响应头（body 形状被冻结的端点靠这个表达契约）。"""
    try:
        response.headers['X-Scope-Platform'] = scope.platform
        response.headers['X-Scope-Realm'] = scope.realm
        response.headers['X-Scope-Authority'] = scope.source_authority
        response.headers['X-Scope-Config-Generation'] = scope.config_generation
        if scope.degraded:
            response.headers['X-Scope-Degraded'] = ','.join(scope.degraded)
    except Exception:  # noqa: BLE001 - 头部写入失败不该让读请求失败
        pass
