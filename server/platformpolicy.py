"""Server-owned platform selection and per-key platform authorization (K06)."""
from __future__ import annotations

import json
from contextvars import ContextVar
from urllib.parse import quote

# ── 平台属性集中在 `PLATFORM_SPEC`（2026-11-22）──────────────────────────
#
# 此前「有哪些平台」这个事实散在 12 处硬编码元组里，彼此还不一致；加一个平台
# 要逐个找，漏一处就是线上事故（实测漏 `qtgcore/service.py` 会让账号页直接
# `known pool platform required`）。现在只有这一张表，其余各处从它派生。
# 接入清单见 `docs/PLATFORM-ONBOARDING.md`。
from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformSpec:
    """一个平台的静态属性。**不要在任何地方再写死平台名元组。**"""

    name: str
    enabled: bool = False          # 在营：对外枚举/UI 切换器/`/api/platforms` 只认这些
    default_grant: bool = False    # 进 `DEFAULT_PLATFORMS`（代建/回填的保守集合）
    adapter: str = ''              # 引擎运行时属性名 `rt.<adapter>`；空=无独立引擎
    passthrough: bool = False      # 直连自己的上游（`_THREEWAY_SPEC` 的键集合）
    realm_scoped: bool = True      # 受 WorkBuddy cn/global realm 裁剪
    quota_4xx: bool = False        # 上游 4xx 可能是「账号池额度耗尽」
    auth_mode: str = 'callback'    # `callback` 浏览器回跳 / `poll` 轮询上游会话
    oauth_session: bool = False    # 有 OAuth 授权会话台账
    qr_self_driven: bool = False   # 扫码由独立扫码页驱动，台账不重复 poll
    stats_scope: bool = False      # 进「按密钥用量」统计分支
    billing: str = 'none'          # `upstream_credit` = usage 自带 credit；`none` 不计费
    display: str = ''              # 展示名（文案用，不参与判定）


#: **单一真相**。顺序即对外显示顺序，也决定 `_ACCEPTED` 的顺序。
#
# 注意这与 `compat_fixes.UPSTREAM_IDS` 的**编号**是两件事：那里 1/2/3 是外部
# 面板的编号契约（豆包=1、Trae=2、WorkBuddy=3），**不随显示顺序重排**。
PLATFORM_SPEC: dict[str, PlatformSpec] = {
    'workbuddy': PlatformSpec(
        name='workbuddy', enabled=True, default_grant=True,
        adapter='wb', passthrough=False, realm_scoped=True,
        auth_mode='callback', billing='upstream_credit', display='WorkBuddy'),
    'trae': PlatformSpec(
        name='trae', enabled=True, default_grant=True,
        adapter='trae', passthrough=True, realm_scoped=False, quota_4xx=True,
        auth_mode='callback', oauth_session=True, qr_self_driven=True,
        stats_scope=True, display='Trae'),
    'doubaowork': PlatformSpec(
        name='doubaowork', enabled=True, default_grant=True,
        adapter='dbw', passthrough=True, realm_scoped=False, quota_4xx=True,
        auth_mode='callback', qr_self_driven=True, display='Doubao Work'),
    # 「可识别、但不可用」的历史平台：留给存量密钥/旧 users.json/迁移回填，
    # 免得一个已不用的平台让整条鉴权链路 500。
    'qoder': PlatformSpec(
        name='qoder', enabled=False, adapter='qoder', passthrough=True,
        realm_scoped=False, auth_mode='poll', oauth_session=True,
        stats_scope=True, display='Qoder'),
}


def spec(name: str):
    """按名字取平台属性；未知平台返回 None。"""
    return PLATFORM_SPEC.get((name or '').strip().lower())


def passthrough_names() -> tuple:
    """直连上游的平台名（须与 `gateway._THREEWAY_SPEC` 的键一致）。"""
    return tuple(n for n, s in PLATFORM_SPEC.items() if s.passthrough)


def adapter_attr(name: str) -> str:
    """引擎运行时属性名；无独立引擎返回空串。"""
    s = spec(name)
    return s.adapter if s else ''


def is_passthrough(name: str) -> bool:
    s = spec(name)
    return bool(s and s.passthrough)


# **对外枚举**：UI 切换器、`/api/platforms`、`_CAP_MATRIX` 都只认这些。
PLATFORMS = tuple(n for n, s in PLATFORM_SPEC.items() if s.enabled)

# **内部可解析集合**：比 `PLATFORMS` 多一个已摘除的 `qoder`。
#
# 为什么要留它：历史密钥、旧 users.json、迁移回填里的 `platforms` 字段可能
# 还写着 `qoder`。如果 `normalize()` 直接抛 ValueError，那些**存量数据**
# 会让整个鉴权链路 500 —— 一个已经不用了的平台不该把在线服务搞挂。
#
# 留着的语义是「**可识别、但不可用**」：解析能过，真正的请求会因为
# `runtime.qoder is None` 而失败（那是明确的「平台已下线」错误，
# 不是「配置非法」）。
#
# 判断「某个平台是否还在服务」请一律用 `PLATFORMS`，不要用这个。
_ACCEPTED = tuple(PLATFORM_SPEC)

# 未显式声明平台时的**保守**集合：给「系统代建」路径（红包等）与历史迁移回填用。
#
# 注意它与 `normalize(None)` **不是一回事**，两者作用域不同：
#   normalize(None)   → list(PLATFORMS)    「不传 = 不限制 = 全部」，是既有 API 契约
#   DEFAULT_PLATFORMS                      「代建/回填必须显式使用」
#
# 新平台绝不允许自动进入这里；而任何系统代建路径都必须**显式**传本常量，
# 否则新增平台就会静默生效——红包密钥正是这种路径（`redpacket.py` 已显式传）。
# 从 `PLATFORM_SPEC` 派生：只有 enabled 且 default_grant 的才进。
DEFAULT_PLATFORMS = tuple(n for n, s in PLATFORM_SPEC.items()
                          if s.enabled and s.default_grant)
current_platform: ContextVar[str] = ContextVar('forced_platform', default='')


class PlatformConflict(ValueError):
    def __init__(self, message, code='platform_conflict'):
        super().__init__(message)
        self.code = code


def normalize(values, *, default=True):
    if values is None and default:
        # 不传 = 不限制 = 当前在服务的全部平台（**不含已摘除的 qoder**）
        return list(PLATFORMS)
    if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) for v in values):
        raise ValueError('platforms must be an array of platform names')
    # 用 `_ACCEPTED` 而不是 `PLATFORMS`：存量密钥里可能还写着 qoder，
    # 那是「可识别但不可用」，不该让鉴权链路 500。
    if any(v not in _ACCEPTED for v in values):
        raise ValueError('platforms contains an unknown platform')
    return [p for p in _ACCEPTED if p in values]


def stored_policy(raw):
    try:
        return normalize(json.loads(raw) if isinstance(raw, str) else raw, default=False), True
    except (ValueError, TypeError):
        return [], False


def split_model(model):
    if isinstance(model, str) and '/' in model:
        prefix, rest = model.split('/', 1)
        if prefix.lower() in _ACCEPTED and rest:
            return prefix.lower(), rest
    return '', model


def select(path, header='', model=None):
    explicit = ''
    for p in _ACCEPTED:
        base = f'/{p}/v1'
        if path == base or path.startswith(base + '/'):
            explicit = p
            break
    hint = (header or '').strip().lower()
    # `_ACCEPTED` 而非 `PLATFORMS`：`X-QTG-Platform: qoder` 属于「已知但已下线」，
    # 应该走到下游被明确拒绝（runtime.qoder is None），而不是在这里报
    # 「Unknown」——那条错误会让排障方向变成「是不是拼错了平台名」。
    if hint and hint not in _ACCEPTED:
        raise PlatformConflict('Unknown X-QTG-Platform', 'invalid_platform')
    model_platform, _ = split_model(model)
    sources = [v for v in (explicit, hint, model_platform) if v]
    if len(set(sources)) > 1:
        raise PlatformConflict('Platform path, header and model prefix disagree')
    return next(iter(sources), 'workbuddy')


def bind_request(request, model=None):
    path = request.scope.get('zhihui.original_path', request.url.path)
    chosen = select(path, request.headers.get('x-qtg-platform', ''), model)
    current_platform.set(chosen if chosen != 'workbuddy' else '')
    request.state.requested_platform = chosen
    return chosen


class PlatformRoutingMiddleware:
    def __init__(self, app, base_path=''):
        self.app = app
        self.base_path = (base_path or '').rstrip('/')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        original = scope.get('path', '')
        path = original
        if self.base_path and (path == self.base_path or path.startswith(self.base_path + '/')):
            path = path[len(self.base_path):] or '/'
        prefixes = ('/v1', '/v2') + tuple(f'/{p}/v1' for p in _ACCEPTED)
        is_data = any(path == b or path.startswith(b + '/') for b in prefixes)
        token = current_platform.set('')
        try:
            if is_data:
                hint = next((v.decode('latin-1') for k, v in scope.get('headers', [])
                             if k.lower() == b'x-qtg-platform'), '')
                try:
                    chosen = select(path, hint)
                except PlatformConflict as exc:
                    from starlette.responses import JSONResponse
                    await JSONResponse({'error': {'message': str(exc), 'code': exc.code,
                                                  'type': 'invalid_request_error'}},
                                       status_code=400)(scope, receive, send)
                    return
                scope['zhihui.original_path'] = path
                current_platform.set(chosen if chosen != 'workbuddy' else '')
                # 前缀改写也要认 `_ACCEPTED`：`/qoder/v1/models` 是存量调用方
                # 可能还在用的路径，改写成 `/v1/models` 之后由下游给出
                # 「平台已下线」的明确错误，而不是 404 让人以为路由写错了。
                for p in _ACCEPTED:
                    base = f'/{p}/v1'
                    if path == base or path.startswith(base + '/'):
                        new_path = '/v1' + path[len(base):]
                        if self.base_path and original.startswith(self.base_path + '/'):
                            new_path = self.base_path + new_path
                        scope['path'] = new_path
                        scope['raw_path'] = quote(new_path, safe='/:@').encode('ascii')
                        break
            await self.app(scope, receive, send)
        finally:
            current_platform.reset(token)
