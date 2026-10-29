"""角色 × 动作 × 平台授权矩阵（K04）。

## 为什么要有这个模块

`contracts/roles-actions-platforms.csv` 把 8 类身份 × 45 组动作 × 三平台捋清楚了，
结论是**QTG 这一侧几乎没有角色概念**：全局闸门只分「已登录 / 未登录」，viewer 一旦
拿到（未来可能出现的）会话就能调 compat 的 90 多个写端点；管理令牌能调「吊销会话」
这类本该只给真人使用的高危动作；推理密钥与控制面凭证没有明确边界。

所以 K04 的第一半（本模块）先把授权矩阵落到代码里：
* 每个动作声明它允许哪些角色、适用哪些平台、是否要求真人会话、是否公开；
* 决策函数给出「允许/拒绝 + 可审计的理由码」；
* 矩阵可导出成 JSON 供 `/api/v1/authz/matrix` 与审计核对。

第二半（`routers/authz.py` 的 `/api/v1/authz/probe`）才是把 Nginx 的
`auth_request` 从「业务端点 `require_admin`」换到专用轻量探针 —— 顺序不能反：
**先有服务端授权，再换探针**。若先换探针，探针就是唯一闸门，而探针的语义与业务
端点不一致时会出现「探针放行、业务拒绝」或更糟的「探针拒绝、业务照跑」。

## 平台维度

QTG 控制面（`/api/**`）本身与平台无关（一个进程管 qoder/trae/workbuddy），表中
`platform="三平台"`；数据面（`/v1/**`）按平台区分，`platform="qoder/trae"`。
请求所属平台从 `X-Platform` 头、`platform` 查询参数或路径前缀推断，推断不出按
「三平台」处理。平台不适用的动作返回 `platform_not_applicable`（不是 403 权限问题，
便于前端显示「本平台无此能力」）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import Request

from .identity import Identity

# ---- 角色 ----
ROLE_ANONYMOUS = "anonymous"
ROLE_VIEWER = "viewer"
ROLE_READONLY = "readonly"
ROLE_ADMIN = "admin"
ROLE_INTERNAL = "internal_service"
ROLE_INFERENCE = "inference_key"

ROLE_LABELS = {
    ROLE_ANONYMOUS: "匿名（无凭据）",
    ROLE_VIEWER: "只读会话（viewer）",
    ROLE_READONLY: "只读管理 Token（scope=readonly）",
    ROLE_ADMIN: "管理员（会话或 admin Token）",
    ROLE_INTERNAL: "内部服务（边缘/自动化）",
    ROLE_INFERENCE: "推理密钥（数据面）",
}

# 控制面写操作的默认允许角色（QTG 单管理员部署的现状基线）
_WRITE_ROLES = frozenset({ROLE_ADMIN, ROLE_INTERNAL})
_READ_ROLES = frozenset({ROLE_ADMIN, ROLE_VIEWER, ROLE_READONLY, ROLE_INTERNAL})
_PUBLIC_ROLES = frozenset({ROLE_ANONYMOUS, ROLE_VIEWER, ROLE_READONLY, ROLE_ADMIN, ROLE_INTERNAL})

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class Rule:
    """一行授权契约。

    字段与 `contracts/roles-actions-platforms.csv` 对齐：
    `action` = action_id，`platform` = 平台列，`evidence` = 本树定位到的文件:行号，
    `allow` 是**本候选实现的目标角色集**（不是现状），现状见 `contracts` 表。
    """

    action: str
    title: str
    methods: frozenset
    paths: Tuple[str, ...]
    allow: frozenset
    platform: str = "三平台"
    human_only: bool = False
    public: bool = False
    evidence: str = ""
    notes: str = ""
    #: 该动作**额外要求**的认证方式集合；空 = 不限制。
    #:
    #: 与 `human_only` 互补：`human_only` 是"必须真人会话"，本字段是"必须是指定
    #: 的机器身份"。用于机器到机器通道——那些端点不该接受浏览器会话 Cookie，
    #: 哪怕调用者角色是 admin。
    auth_methods: frozenset = frozenset()

    def matches(self, method: str, path: str) -> bool:
        if method not in self.methods:
            return False
        return any(_match(pattern, path) for pattern in self.paths)


@dataclass(frozen=True)
class Decision:
    """一次授权决策：允许/拒绝 + 理由码 + 命中的契约行。"""

    allowed: bool
    code: str
    reason: str
    action: str = ""
    platform: str = "三平台"
    human_only: bool = False
    rule_title: str = ""

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "action": self.action,
            "platform": self.platform,
            "human_only": self.human_only,
            "rule": self.rule_title,
        }


def _to_regex(pattern: str) -> re.Pattern:
    """把人类可读的路径模式转成正则：`*` 匹配任意字符，其余字面量。"""
    out: List[str] = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$")


_PATTERN_CACHE: Dict[str, re.Pattern] = {}


def _match(pattern: str, path: str) -> bool:
    rx = _PATTERN_CACHE.get(pattern)
    if rx is None:
        rx = _to_regex(pattern)
        _PATTERN_CACHE[pattern] = rx
    return bool(rx.match(path))


# ---------------------------------------------------------------------------
# 公开白名单（K04：公开业务显式白名单，避免「直接放开 QTG 写权限」）
# ---------------------------------------------------------------------------
PUBLIC_RULES: Tuple[Rule, ...] = (
    Rule(
        action="A01-Q1", title="登录（QTG 兼容端：password=管理令牌）", methods=frozenset({"POST"}),
        paths=("/api/login",),
        allow=_PUBLIC_ROLES, public=True,
        evidence="qtg/app/qtg/routers/compat.py:61",
        notes="失败信封是 200 {ok:false}（不是 401），K29 能力表已登记不一致",
    ),
    Rule(
        action="A01-Q2", title="登录（QTG 原生端，只收 password）", methods=frozenset({"POST"}),
        paths=("/api/v1/auth/login",),
        allow=_PUBLIC_ROLES, public=True,
        evidence="qtg/app/qtg/routers/auth.py:57",
        notes="失败返回 401 + 错误信封",
    ),
    Rule(
        action="A02", title="退出登录", methods=frozenset({"POST"}),
        paths=("/api/logout", "/api/v1/auth/logout"),
        allow=_PUBLIC_ROLES, public=True,
        evidence="qtg/app/qtg/routers/compat.py:77、routers/auth.py:85",
        notes="会话可能已过期，退出必须仍可执行（幂等）",
    ),
    Rule(
        action="A03", title="身份探测 me/session", methods=frozenset({"GET"}),
        paths=("/api/me", "/api/v1/auth/session"),
        allow=_PUBLIC_ROLES, public=True,
        evidence="qtg/app/qtg/routers/compat.py:103、routers/auth.py:111",
        notes="匿名返回 401 信封（由 handler 决定），不能是闸门 401 之外的行为差异",
    ),
    Rule(
        action="A40", title="健康/就绪探针", methods=frozenset({"GET"}),
        paths=("/healthz", "/readyz", "/api/healthz", "/api/readyz"),
        allow=_PUBLIC_ROLES, public=True,
        evidence="qtg/app/qtg/routers/health.py:15-33",
        notes="/readyz 回显上游 URL，生产 Nginx 已单独收敛（见候选配置）",
    ),
    Rule(
        action="A23", title="公开领取红包链接（本部署恒 410）", methods=frozenset({"GET", "POST"}),
        paths=("/api/claim/*",),
        allow=_PUBLIC_ROLES, public=True, platform="qoder/trae",
        evidence="qtg/app/qtg/routers/compat_fixes.py:792-799",
        notes="handler 是常量 410（无状态、无副作用）：公开可达才能让前端显示"
              "「本部署没有红包」，而不是被 401 伪装成「未登录」（K29 不支持项要可解释）",
    ),
)


# ---------------------------------------------------------------------------
# 契约矩阵（QTG 控制面；证据均为本执行树行号）
# ---------------------------------------------------------------------------
RULES: Tuple[Rule, ...] = PUBLIC_RULES + (
    Rule(action='A43', title='非敏感配置生效快照', methods=frozenset({'GET'}),
         paths=('/api/v1/system/configuration',), allow=frozenset({ROLE_ADMIN, ROLE_INTERNAL}),
         evidence='qtg/app/qtg/routers/system.py:configuration_snapshot'),
    Rule(
        action="A05", title="吊销全部会话", methods=frozenset({"POST"}),
        paths=("/api/sessions/revoke",), allow=frozenset({ROLE_ADMIN}),
        human_only=True,
        evidence="qtg/app/qtg/routers/compat_settings.py:68",
        notes="现状 Bearer 即可调用（比 Manager 的 require_session_admin 宽）；"
              "目标：只允许真人会话，Token 一律 403",
    ),
    Rule(
        action="A06", title="密钥列表读取（元数据，无明文）", methods=frozenset({"GET"}),
        paths=("/api/keys", "/api/v1/keys"), allow=_READ_ROLES,
        evidence="qtg/app/qtg/routers/compat_fixes.py:623、routers/read.py:80",
        notes="读端点对 viewer 只读开放（K04「viewer 只读可达」）；写仍走 A07/A08",
    ),
    Rule(
        action="A15", title="账号池/状态读取", methods=frozenset({"GET"}),
        paths=("/api/accounts", "/api/status", "/api/v1/accounts",
               "/api/v1/overview", "/api/v1/usage"),
        allow=_READ_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:208、routers/read.py:52-88",
    ),
    Rule(
        action="A30", title="模型目录读取", methods=frozenset({"GET"}),
        paths=("/api/model-catalog", "/api/models", "/api/v1/models"),
        allow=_READ_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:334-375、compat_fixes.py:415-520",
    ),
    Rule(
        action="A31", title="请求/签到/任务日志读取", methods=frozenset({"GET"}),
        paths=("/api/logs", "/api/checkin-logs", "/api/task-logs", "/api/v1/logs",
               "/api/v1/requests"),
        allow=_READ_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:529-642、routers/read.py:98-124",
    ),
    Rule(
        action="A07", title="创建密钥", methods=frozenset({"POST"}),
        paths=("/api/keys", "/api/v1/actions/keys"),
        allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat_fixes.py:662、routers/actions.py:227",
        notes="键的权威在 Manager（K01-S2）；QTG 只转发",
    ),
    Rule(
        action="A07-Q1", title="创建 QTG 数据面密钥", methods=frozenset({"POST"}),
        paths=("/api/v1/actions/qtg-keys",), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/data_admin.py:115",
    ),
    Rule(
        action="A08", title="修改密钥", methods=frozenset({"PATCH"}),
        paths=("/api/keys/*",), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:1290",
    ),
    Rule(
        action="A09", title="删除密钥", methods=frozenset({"DELETE"}),
        paths=("/api/keys/*",), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:1313",
    ),
    Rule(
        action="A10", title="重置密钥用量", methods=frozenset({"POST"}),
        paths=("/api/keys/*/reset-usage",), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:1329",
        notes="QTG 侧恒定 409（用量按流水统计，不可重置）",
    ),
    Rule(
        action="A11", title="导出客户端配置", methods=frozenset({"POST"}),
        paths=("/api/keys/export",), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat_fixes.py:729",
    ),
    Rule(
        action="A12", title="本机导入客户端配置", methods=frozenset({"POST"}),
        paths=("/api/keys/import-local",), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat_fixes.py:723",
        notes="本部署恒定 409（不写本机客户端配置）",
    ),
    Rule(
        action="A13", title="模型白名单校验", methods=frozenset({"POST"}),
        paths=("/api/keys/check-models",), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:1285",
        notes="桩实现：固定回 checked=true（contracts README §4-8 已登记误报）",
    ),
    Rule(
        action="A14", title="管理 Token 增删改", methods=frozenset({"POST", "PATCH", "DELETE"}),
        paths=("/api/tokens", "/api/tokens/*"), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat_settings.py:90-100",
        notes="本部署写操作恒定 409（不支持管理面 API Token）",
    ),
    Rule(
        action="A16", title="账号池写（启停/权重/代理/备注/冷却/移动/删除）",
        methods=frozenset({"PUT", "POST", "DELETE"}),
        paths=("/api/accounts/*",), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:1034-1057、compat_accounts_ext.py:157-278",
        notes="QTG 无账号写权限的部分恒定 409",
    ),
    Rule(
        action="A17", title="账号导入/导出", methods=frozenset({"POST", "GET"}),
        paths=("/api/accounts/upload", "/api/accounts/export"), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:1464-1493",
    ),
    Rule(
        action="A18", title="扫码/授权登录 start/poll/finish",
        methods=frozenset({"POST", "GET"}),
        paths=("/api/auth/start", "/api/auth/poll", "/api/auth/finish"),
        allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat_accounts_ext.py:49-108",
    ),
    Rule(
        action="A19", title="签到（单账号/全量）", methods=frozenset({"POST"}),
        paths=("/api/accounts/*/checkin", "/api/accounts/checkin-all"),
        allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:966、1372",
        notes="部分失败在 results 内（K29 部分失败语义）",
    ),
    Rule(
        action="A20", title="成长任务 task-run", methods=frozenset({"POST", "GET"}),
        paths=("/api/task-run", "/api/task-run/stop"), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:831-857",
    ),
    Rule(
        action="A21", title="清理日志（签到/任务/请求）", methods=frozenset({"POST"}),
        paths=("/api/checkin-logs/clear", "/api/task-logs/clear", "/api/logs/clear"),
        allow=frozenset({ROLE_ADMIN}), human_only=True,
        evidence="qtg/app/qtg/routers/compat.py:1148、1580、1588",
        notes="抹痕迹类动作只对真人会话开放（对齐 Manager require_session_admin）",
    ),
    Rule(
        action="A22", title="红包创建/吊销（本部署 410）", methods=frozenset({"POST"}),
        paths=("/api/red-packets", "/api/red-packets/*"),
        allow=_WRITE_ROLES, platform="qoder/trae",
        evidence="qtg/app/qtg/routers/compat_fixes.py:782-790",
        notes="本部署没有红包能力，正确做法是 410 + 说明（不是伪造成功）",
    ),
    Rule(
        action="A24", title="设置写（上游/模型映射/GitHub/upstash）",
        methods=frozenset({"POST", "PUT"}),
        paths=("/api/settings/*",), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat_settings.py:197-260",
        notes="model-map 是真实写且无审计（contracts README §4-7）",
    ),
    Rule(
        action="A25", title="出口代理/代理线路写", methods=frozenset({"POST", "PUT", "DELETE"}),
        paths=("/api/proxy-routes", "/api/proxy-routes/*", "/api/settings/proxy*"),
        allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat_settings.py:122-176",
        notes="本部署写操作恒定 409",
    ),
    Rule(
        action="A26", title="安全策略/IP 规则写",
        methods=frozenset({"POST", "PUT", "DELETE"}),
        paths=("/api/security/config", "/api/security/rules", "/api/security/rules/*"),
        allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/ip_guard.py:117-201",
    ),
    Rule(
        action="A27", title="PG 同步（备份/恢复）", methods=frozenset({"POST"}),
        paths=("/api/settings/pg-sync", "/api/settings/pg-sync/*"),
        allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat_settings.py:284-299",
        notes="本部署恒定 409",
    ),
    Rule(
        action="A28", title="系统更新（检查/上传/安装/回退）",
        methods=frozenset({"POST", "DELETE"}),
        paths=("/api/system/update", "/api/system/update/*", "/api/system/update-package",
               "/api/system/update-status"),
        allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat_settings.py:318-352",
        notes="本部署恒定 409（不做在线更新）",
    ),
    Rule(
        action="A29", title="用户管理（增/改角色/删）",
        methods=frozenset({"POST", "PATCH", "DELETE"}),
        paths=("/api/users", "/api/users/*"), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat_settings.py:53-63",
        notes="本部署恒定 409（不支持多用户）",
    ),
    Rule(
        action="A32", title="统计重建/修复", methods=frozenset({"POST"}),
        paths=("/api/stats/repair*", "/api/stats/rebuild*"), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:1158-1191",
    ),
    Rule(
        action="A41", title="重启上游/服务", methods=frozenset({"POST"}),
        paths=("/api/restart", "/api/upstream/restart"), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/compat.py:1193-1207",
        notes="本部署软拒绝（200 ok=false + 说明）",
    ),
    Rule(
        action="A42", title="QTG 动作面（checkin/账号动作/限额/通知/备份/oauth）",
        methods=frozenset({"GET", "POST"}),
        paths=("/api/v1/actions/*",), allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/actions.py:140-376",
        notes="actions.py 全部 Depends(require_admin)，角色粒度由本矩阵统一表达",
    ),
    Rule(
        action="A42", title="QTG 数据面管理（上游密钥/流水清理）",
        methods=frozenset({"POST"}),
        paths=("/api/v1/actions/upstream-keys", "/api/v1/actions/proxy-prune",
               "/api/v1/actions/qtg-keys/*"),
        allow=_WRITE_ROLES,
        evidence="qtg/app/qtg/routers/data_admin.py:130-170",
    ),
    Rule(
        action="A33", title="审计日志读取", methods=frozenset({"GET"}),
        paths=("/api/audit-logs", "/api/v1/audit"),
        allow=frozenset({ROLE_ADMIN, ROLE_INTERNAL}),
        evidence="qtg/app/qtg/routers/compat.py:643、routers/read.py:168",
        notes="审计日志含全部管理动作，最小权限：只给管理员/内部服务；"
              "viewer 读取审计在生产属敏感操作（contracts 表 A33 对 viewer 记「拒绝」）",
    ),
    Rule(
        action="A34", title="Prometheus 指标", methods=frozenset({"GET"}),
        paths=("/metrics",), allow=frozenset({ROLE_ADMIN, ROLE_INTERNAL}),
        evidence="qtg/app/qtg/main.py:130、routers/health.py:35",
        notes="现状在 _PROTECTED_EXTRA 内 → 需凭证；与 test_api 的「公开」期望冲突，"
              "K29 能力表如实登记为「需凭证」，不为了让测试变绿而放开",
    ),
    Rule(
        action="A37", title="推理调用（数据面）", methods=frozenset({"POST"}),
        paths=("/v1/chat/completions", "/v1/responses", "/v1/messages"),
        allow=frozenset({ROLE_INFERENCE}), platform="qoder/trae",
        evidence="qtg/app/qtg/routers/proxy.py:355、routers/anthropic.py:175",
        notes="数据面不在 /api 闸门内，由 proxy 自行校验 sk-qtg- 密钥；"
              "控制面凭证不得用于数据面",
    ),
    Rule(
        action="A38", title="模型列表（数据面）", methods=frozenset({"GET"}),
        paths=("/v1/models",), allow=frozenset({ROLE_INFERENCE}), platform="qoder/trae",
        evidence="qtg/app/qtg/routers/proxy.py:325",
    ),
    # U3：Manager 专用适配器契约。机器到机器的只读通道，不接受会话 Cookie。
    Rule(
        action="A60", title="引擎数据适配器契约（Manager 专用，只读）",
        methods=frozenset({"GET"}),
        paths=("/api/v1/adapter/contract", "/api/v1/adapter/health",
               "/api/v1/adapter/accounts", "/api/v1/adapter/models",
               "/api/v1/adapter/usage"),
        allow=frozenset({ROLE_INTERNAL, ROLE_ADMIN}), platform="三平台",
        evidence="qtg/app/qtg/routers/adapter.py:1-190",
        # 机器到机器通道：admin 角色还不够，认证方式必须是机器身份。
        # 2026-10-26 之前这里只判角色，admin 的浏览器会话也能调；与文档
        # 「不接受会话 Cookie」矛盾。现按文档收紧，并由
        # tests/test_adapter_contract.py::test_admin_session_denied 锁定。
        auth_methods=frozenset({"internal_service", "admin_token"}),
        notes="只给统一控制面消费的引擎侧数据（账号运行态/模型/用量/健康）；"
              "只读，写操作仍由 Manager 权威路径处理；"
              "不允许匿名、也不接受会话 Cookie——只认内部服务令牌或 admin API token",
    ),
)


def platform_of(request: Request) -> str:
    """推断请求所属平台；推断不出返回「三平台」。

    2026-10-27 R4 收敛：state/请求头的读取统一走 identity.platform_of——
    qtgcore 内从此只有那一处读 `x-qtg-platform`（中间件已把 query 入口
    归一化进 state）。query/path 兜底保留：那是「URL 自身携带平台」的
    独立语义，不属于请求头兼容层。
    """
    from .identity import platform_of as _identity_platform_of
    header = _identity_platform_of(request)
    if header in ("qoder", "trae", "workbuddy"):
        return header
    query = (request.query_params.get("platform") or "").strip().lower()
    if query in ("qoder", "trae", "workbuddy"):
        return query
    path = request.url.path
    for name in ("qoder", "trae", "workbuddy"):
        if path.startswith(f"/{name}/"):
            return name
    return "三平台"


def _platform_compatible(rule: Rule, platform: str) -> bool:
    if rule.platform == "三平台" or platform == "三平台":
        return True
    allowed = {p.strip() for p in rule.platform.replace("（", "(").split("/") if p.strip()}
    return platform in allowed or rule.platform.startswith(platform)


def find_rule(method: str, path: str) -> Optional[Rule]:
    for rule in RULES:
        if rule.matches(method, path):
            return rule
    return None


def _scope_allows(identity: Identity, method: str, path: str, rule: Optional[Rule]) -> bool:
    """Scopes restrict roles; they never grant a role or a human session.

    Empty local sessions and the legacy bare service credential keep their role
    ceiling. Edge assertions must carry explicit, recognized scopes.
    """
    scopes = identity.scopes
    if not scopes:
        return identity.auth_method == "session_cookie"
    if "*" in scopes or "service" in scopes:
        return True
    introspection = ("/api/v1/authz/identity", "/api/v1/authz/probe", "/api/v1/authz/matrix")
    recognized = {"read", "readonly", "keys:read", "keys:write"}
    if not scopes.issubset(recognized):
        return False
    if method in {"GET", "HEAD"} and path in introspection:
        return True
    if method in {"GET", "HEAD"} and ("read" in scopes or "readonly" in scopes):
        return (not path.startswith("/api/v1/actions/")
                and (rule is None or ROLE_VIEWER in rule.allow))
    if method in {"GET", "HEAD"} and "keys:read" in scopes:
        return path in {"/api/keys", "/api/v1/keys"}
    if "keys:write" in scopes:
        if method == "POST" and path in {"/api/keys", "/api/v1/actions/keys"}:
            return True
        if method in {"PATCH", "DELETE"} and re.fullmatch(r"/api/keys/[0-9]+", path):
            return True
        if method == "POST" and re.fullmatch(r"/api/keys/[0-9]+/reset-usage", path):
            return True
    return False


def decide(identity: Optional[Identity], request: Request, *,
           human_session_required: bool = True,
           action: Optional[str] = None,
           platform: Optional[str] = None,
           target_method: Optional[str] = None,
           target_path: Optional[str] = None) -> Decision:
    """给出授权结论。

    探针（Nginx `auth_request`）可以显式指定 `action` / `platform` /
    `target_method` / `target_path` —— 探针请求本身永远是 GET，被判定的是它
    想访问的那条真实请求。
    """
    probe = bool(target_method or target_path or action)
    method = (target_method or request.method).upper()
    path = (target_path or request.url.path).rstrip('/') or '/'
    role = identity.role if identity else ROLE_ANONYMOUS
    plat = platform or platform_of(request)
    rule = find_rule(method, path)
    if rule is None and action:
        rule = next((r for r in RULES if r.action == action), None)
        if rule is not None and not target_path:
            # An action-only probe must evaluate that action, not the read-only
            # /authz/probe URL (which would bypass the action's scope ceiling).
            method = (target_method or sorted(rule.methods)[0]).upper()
            path = rule.paths[0]
    if rule is not None and platform is None and plat == '三平台' and rule.platform != "三平台":
        plat = rule.platform

    # 1) 公开白名单
    if rule is not None and rule.public:
        return Decision(True, "public", "公开白名单（登录/退出/身份探测/健康检查）",
                        rule.action, plat, rule.human_only, rule.title)

    # 2) 匿名
    if identity is None:
        return Decision(False, "unauthenticated", "未携带任何可采信凭据",
                        rule.action if rule else "", plat,
                        bool(rule and rule.human_only), rule.title if rule else "")

    if not _scope_allows(identity, method, path, rule):
        return Decision(False, "scope_denied", "当前凭据的作用域不允许该操作",
                        rule.action if rule else "", plat,
                        bool(rule and rule.human_only), rule.title if rule else "")

    # 3) 平台不适用
    if rule is not None and not _platform_compatible(rule, plat):
        return Decision(False, "platform_not_applicable",
                        f"该动作不适用于平台 {plat}",
                        rule.action, plat, rule.human_only, rule.title)

    # 4) 命中契约行：按角色集判定
    if rule is not None:
        if role not in rule.allow:
            code = "role_write_denied" if method in _WRITE_METHODS else "role_read_denied"
            return Decision(False, code,
                            f"角色 {role} 不允许执行动作 {rule.action}（{rule.title}）",
                            rule.action, plat, rule.human_only, rule.title)
        if rule.human_only and human_session_required and not identity.is_human_session:
            return Decision(False, "human_session_required",
                            f"动作 {rule.action}（{rule.title}）只允许真人会话，"
                            f"当前登录方式为 {identity.auth_method}",
                            rule.action, plat, rule.human_only, rule.title)
        # 机器到机器通道：角色对了还不够，认证方式也必须是约定的那几种。
        # 否则一个 admin 会话（浏览器）就能驱动只该由服务调用的端点。
        if rule.auth_methods:
            actual = identity.auth_method if identity is not None else "anonymous"
            if actual not in rule.auth_methods:
                return Decision(
                    False, "auth_method_not_allowed",
                    f"动作 {rule.action}（{rule.title}）只接受 "
                    f"{'/'.join(sorted(rule.auth_methods))}，当前为 {actual}",
                    rule.action, plat, rule.human_only, rule.title)
        return Decision(True, "rule_allowed",
                        f"命中契约 {rule.action}（{rule.title}）：角色 {role} 允许",
                        rule.action, plat, rule.human_only, rule.title)

    # 5) 未命中：写操作只给管理员/内部服务，读操作放开给所有已登录角色
    if method in _WRITE_METHODS:
        allowed = role in _WRITE_ROLES
        return Decision(allowed,
                        "default_authenticated_write" if allowed else "role_write_denied",
                        f"未命中契约行的写操作：角色 {role} "
                        f"{'允许' if allowed else '不允许'}（默认策略）",
                        "", plat, False, "")
    allowed = role in _READ_ROLES
    return Decision(allowed, "default_read" if allowed else "role_read_denied",
                    f"未命中契约行的读操作：角色 {role} "
                    f"{'允许' if allowed else '不允许'}（默认策略）", "", plat, False, "")


def rules_for(action: str) -> Sequence[Rule]:
    return tuple(r for r in RULES if r.action == action)


def matrix_payload() -> dict:
    """导出矩阵（供 `/api/v1/authz/matrix` 与测试/报告核对）。"""
    rows = []
    for rule in RULES:
        rows.append({
            "action": rule.action,
            "title": rule.title,
            "methods": sorted(rule.methods),
            "paths": list(rule.paths),
            "platform": rule.platform,
            "allow": sorted(rule.allow),
            "human_only": rule.human_only,
            "public": rule.public,
            "evidence": rule.evidence,
            "notes": rule.notes,
        })
    by_role: Dict[str, List[str]] = {role: [] for role in ROLE_LABELS}
    for rule in RULES:
        for role_name in rule.allow:
            by_role.setdefault(role_name, []).append(f"{rule.action}:{'/'.join(sorted(rule.methods))}")
    return {
        "generated_by": "qtg.authz.matrix_payload",
        "roles": ROLE_LABELS,
        "rows": rows,
        "role_index": by_role,
        "counts": {
            "rows": len(rows),
            "public": sum(1 for r in RULES if r.public),
            "human_only": sum(1 for r in RULES if r.human_only),
            "actions": len({r.action for r in RULES}),
        },
    }


def probe_payload(identity: Optional[Identity], decision: Decision,
                  *, request_id: str = "") -> dict:
    """专用认证/授权探针的响应体（K04）。

    刻意保持极小：不读业务状态、不写业务账本、不打上游，供 Nginx
    `auth_request` 高频调用。鉴权结论放在 HTTP 状态码（200/401/403），
    这里的 `ok` 只是同一结论的信封表达，二者必须一致（K29 三层判定）。
    """
    body = {
        "ok": decision.allowed,
        "probe": "qtg-authz-v1",
        "request_id": request_id,
        "decision": decision.as_dict(),
    }
    if identity is not None:
        # 只回断言字段，不回任何 token；aud/exp 让调用方可以判断「能信任多久」
        body["identity"] = identity.assertion()
        body["envelope_ok"] = True
    else:
        body["envelope_ok"] = False
    return body
