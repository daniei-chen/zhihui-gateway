"""能力表、部分失败语义与「三层成功」判定（K29）。

## 三个层次不能混

QTG 里同一个动作可能给出完全不同形状的「成功」，此前前端只看 HTTP 状态码或只看
`ok` 字段，导致几类假成功：

| 现象 | HTTP | 信封 | 业务 |
|---|---|---|---|
| `/api/login` 密码错误 | 200 | `{ok:false}` | 失败 |
| `/api/keys/check-models`（旧实现） | 200 | `{checked:true}` | 其实没校验 |
| compat 未实现端点 | 501 | `{available:false,code:not_implemented}` | 不支持 |
| 上游 BFF 挂了 | 502 | `{error:{code:upstream_error}}` | 失败 |
| 部分平台成功 | 200 | `{ok:true, sources:[…一个 error…]}` | 部分失败 |

所以本模块把判定拆成 `http_ok / envelope_ok / business_ok` 三个布尔值，只有三者
同时为真才算成功；并把「不支持 / 暂不可用」写成显式能力状态，而不是让前端把
「没实现」渲染成「暂无数据」。

## 能力状态

* `real`：有真实实现，会读/写权威状态；
* `readonly`：能读，但写被明确拒绝（或本部署只开放读）；
* `unsupported`：本部署明确不提供该能力，服务端用 409/410 明确拒绝并给理由；
* `degraded`：依赖缺失或已知缺陷导致暂不可用（例如实现是桩、上游未配置）。

`unsupported`/`degraded` 的行必须带 `error`（可读理由）和 `evidence`（源码位置），
不允许出现「表格说支持、代码是桩」的情况；发现不一致时以代码为准并改表格。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TZ = datetime.now().astimezone().tzinfo


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
STATE_REAL = "real"
STATE_READONLY = "readonly"
STATE_UNSUPPORTED = "unsupported"
STATE_DEGRADED = "degraded"

STATE_LABELS = {
    STATE_REAL: "真实",
    STATE_READONLY: "只读",
    STATE_UNSUPPORTED: "不支持",
    STATE_DEGRADED: "暂不可用",
}


@dataclass(frozen=True)
class Capability:
    """一条能力声明。`locked=True` 表示服务端会拒绝（不是只在前端隐藏）。"""

    id: str
    title: str
    state: str
    platforms: Tuple[str, ...]
    endpoints: Tuple[str, ...]
    evidence: str
    source: str = "源码核对"
    error: str = ""
    notes: str = ""
    locked: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "state_label": STATE_LABELS.get(self.state, self.state),
            "platforms": list(self.platforms),
            "endpoints": list(self.endpoints),
            "evidence": self.evidence,
            "source": self.source,
            "error": self.error,
            "notes": self.notes,
            "locked": self.locked,
        }


_ALL = ("qoder", "trae", "workbuddy")
_QT = ("qoder", "trae")

CAPABILITIES: Tuple[Capability, ...] = (
    Capability(
        "auth.login.compat", "兼容登录（password=管理令牌）", STATE_REAL, _ALL,
        ("POST /api/login",), "qtg/app/qtg/routers/compat.py:61",
        notes="失败时 HTTP 200 + {ok:false}（与 /api/v1/auth/login 的 401 不一致，"
              "见 capabilities.auth.login.native 的 notes）",
    ),
    Capability(
        "auth.login.native", "原生登录（只收 password）", STATE_REAL, _ALL,
        ("POST /api/v1/auth/login",), "qtg/app/qtg/routers/auth.py:57",
        notes="失败返回 401 + 错误信封；与兼容端的 200/ok:false 属两套契约",
    ),
    Capability(
        "auth.logout", "退出登录（同时失效 Manager 会话）", STATE_REAL, _ALL,
        ("POST /api/logout", "POST /api/v1/auth/logout"),
        "qtg/app/qtg/routers/compat.py:77、routers/auth.py:85",
        notes="Manager 不可达时降级为 degraded=True，仍删本地会话（K03）",
    ),
    Capability(
        "identity.me", "身份探测 /api/me", STATE_DEGRADED, _QT,
        ("GET /api/me",), "qtg/app/qtg/routers/compat.py:103",
        error="兼容旧令牌能通过全局闸门，但 compat_me 只比对主令牌 → 旧令牌会拿到 401",
        notes="contracts README §4-10 已登记；修复需统一 verify_password，未在本轮改动",
    ),
    Capability(
        "keys.list", "密钥列表（Manager 权威）", STATE_REAL, _ALL,
        ("GET /api/keys",), "qtg/app/qtg/routers/compat_fixes.py:623",
        notes="Manager 不可达时报 5xx，不返回空列表冒充「没有密钥」",
    ),
    Capability(
        "keys.create", "创建密钥（转发 Manager）", STATE_REAL, _ALL,
        ("POST /api/keys",), "qtg/app/qtg/routers/compat_fixes.py:662",
        notes="QTG 不落地推理密钥权威（K01-S2）",
    ),
    Capability(
        "keys.mutate", "改/删/重置用量", STATE_REAL, _ALL,
        ("PATCH /api/keys/*", "DELETE /api/keys/*", "POST /api/keys/*/reset-usage"),
        "qtg/app/qtg/routers/compat.py:1290-1343",
    ),
    Capability(
        "keys.export", "导出客户端配置（仅结构）", STATE_READONLY, _ALL,
        ("POST /api/keys/export",), "qtg/app/qtg/routers/compat_fixes.py:729",
        notes="不返回明文密钥；base URL 语义见 contracts README §4-1（K01 断点）",
    ),
    Capability(
        "keys.import_local", "本机导入客户端配置", STATE_UNSUPPORTED, _ALL,
        ("POST /api/keys/import-local",), "qtg/app/qtg/routers/compat_fixes.py:723",
        error="本部署不写本机客户端配置，服务端恒定 409", locked=True,
    ),
    Capability(
        "keys.check_models", "模型白名单校验", STATE_DEGRADED, _ALL,
        ("POST /api/keys/check-models",), "qtg/app/qtg/routers/compat.py:1285",
        error="QTG 不做模型白名单校验：此前返回 {checked:true} 属假成功，"
              "现已改为 available/checked=false + 理由",
        notes="前端应把 checked=false 渲染成「未校验」，不能显示「校验通过」",
    ),
    Capability(
        "accounts.read", "账号池读取", STATE_REAL, _ALL,
        ("GET /api/accounts", "GET /api/status"), "qtg/app/qtg/routers/compat.py:208",
    ),
    Capability(
        "accounts.write", "账号池写（启停/签到/刷新）", STATE_REAL, _QT,
        ("POST /api/accounts/*/checkin", "PUT /api/accounts/*/disabled",
         "POST /api/accounts/checkin-all", "POST /api/accounts/refresh-credits"),
        "qtg/app/qtg/routers/compat.py:966-1054、1372-1462",
        notes="权重/分组/强制冷却/代理绑定等 WorkBuddy 专有字段恒定 409（见 accounts.write.wb_only）",
    ),
    Capability(
        "accounts.write.wb_only", "账号权重/代理/移动/强制冷却", STATE_UNSUPPORTED, _QT,
        ("PUT /api/accounts/*/weight", "PUT /api/accounts/*/proxy",
         "POST /api/accounts/*/move", "POST /api/accounts/*/force-cooling"),
        "qtg/app/qtg/routers/compat_accounts_ext.py:180-278",
        error="QTG 没有这些概念（无代理池/无权重/无冷却策略表），服务端 409 明确拒绝",
        locked=True,
    ),
    Capability(
        "playground.chat", "测试台对话（直连上游 SSE）", STATE_REAL, _QT,
        ("POST /api/playground/chat",), "qtg/app/qtg/routers/compat.py:1076",
        notes="不写 proxy_requests、不记审计、不校验 key 额度（K27 待办，属真实缺陷）",
    ),
    Capability(
        "security.ip_guard", "IP 管控（规则 + 中间件）", STATE_REAL, _ALL,
        ("GET/POST /api/security/config", "POST/DELETE /api/security/rules"),
        "qtg/app/qtg/routers/ip_guard.py:111-201、254-284",
        notes="启用前校验当前来源会被放行，避免把管理员锁在门外（409）",
    ),
    Capability(
        "security.sessions_revoke", "吊销全部会话", STATE_REAL, _ALL,
        ("POST /api/sessions/revoke",), "qtg/app/qtg/routers/compat_settings.py:68",
        notes="现状 Bearer 即可调用（比 Manager 严的 require_session_admin 宽）；"
              "本轮由 authz 矩阵收紧为「只允许真人会话」",
    ),
    Capability(
        "users.multi", "多用户管理", STATE_UNSUPPORTED, _QT,
        ("GET/POST/PATCH/DELETE /api/users*",), "qtg/app/qtg/routers/compat_settings.py:43-63",
        error="本部署单管理员（口令即令牌），用户管理恒定 409", locked=True,
    ),
    Capability(
        "tokens.manage", "管理面 API Token 管理", STATE_UNSUPPORTED, _QT,
        ("GET/POST/PATCH/DELETE /api/tokens*",),
        "qtg/app/qtg/routers/compat_settings.py:81-100",
        error="本部署不提供管理面 API Token（列表恒空、写操作 409）", locked=True,
    ),
    Capability(
        "proxy_routes", "出口代理池/线路", STATE_UNSUPPORTED, _QT,
        ("GET/POST/PUT/DELETE /api/proxy-routes*", "/api/settings/proxy*"),
        "qtg/app/qtg/routers/compat_settings.py:106-176",
        error="本部署不做出口代理池，写操作 409；读接口返回空结构", locked=True,
    ),
    Capability(
        "red_packets", "红包/抽奖/领取", STATE_UNSUPPORTED, _QT,
        ("GET/POST /api/red-packets*", "GET/POST /api/claim/*"),
        "qtg/app/qtg/routers/compat_fixes.py:772-800",
        error="红包是 WorkBuddy 的营销功能，QTG 平台没有 → 410 明确说明", locked=True,
    ),
    Capability(
        "pg_sync", "PostgreSQL 同步/导出/恢复", STATE_UNSUPPORTED, _ALL,
        ("GET/POST /api/settings/pg-sync*",),
        "qtg/app/qtg/routers/compat_settings.py:274-302",
        error="本部署不支持 PG 同步（409）", locked=True,
    ),
    Capability(
        "system.update", "在线更新（检查/上传/安装/回退）", STATE_UNSUPPORTED, _QT,
        ("GET/POST/DELETE /api/system/update*",),
        "qtg/app/qtg/routers/compat_settings.py:305-367",
        error="本部署用 rsync + systemctl 发布，不做在线更新（409）", locked=True,
    ),
    Capability(
        "metrics.prometheus", "Prometheus 指标 /metrics", STATE_READONLY, _ALL,
        ("GET /metrics",), "qtg/app/qtg/main.py:130、routers/health.py:35",
        notes="在 _PROTECTED_EXTRA 内 → 需凭证；与 test_api 期望的「公开」冲突，"
              "本轮不动行为，只如实登记（并写进 K30 路由表备注）",
    ),
    Capability(
        "data_plane.chat", "数据面推理（sk-qtg- 密钥）", STATE_REAL, _QT,
        ("POST /v1/chat/completions", "POST /v1/responses", "POST /v1/messages"),
        "qtg/app/qtg/routers/proxy.py:355-560、routers/anthropic.py:175",
        notes="控制面凭证不能用于数据面；推理密钥不能用于控制面（K05 边界）",
    ),
    Capability(
        "authz.probe", "专用认证/授权探针（Nginx auth_request）", STATE_REAL, _ALL,
        ("GET /api/v1/authz/probe",), "qtg/app/qtg/routers/authz.py",
        notes="轻量：不读业务状态、不写业务账本、不打上游；供边缘 auth_request 调用",
    ),
    Capability(
        "capabilities.table", "能力表（本端点）", STATE_REAL, _ALL,
        ("GET /api/v1/capabilities",), "qtg/app/qtg/capabilities.py",
    ),
)


def capability_index() -> Dict[str, Capability]:
    return {c.id: c for c in CAPABILITIES}


def _status_counts() -> Dict[str, int]:
    counts = {state: 0 for state in STATE_LABELS}
    for cap in CAPABILITIES:
        counts[cap.state] = counts.get(cap.state, 0) + 1
    return counts


def capabilities_payload(*, sources: Optional[Sequence[dict]] = None) -> dict:
    """能力表 + 生成时间 + 可选的实时来源状态（部分失败语义）。"""
    return {
        "generated_at": now_iso(),
        "states": STATE_LABELS,
        "counts": _status_counts(),
        "capabilities": [c.as_dict() for c in CAPABILITIES],
        "sources": list(sources or []),
        "envelope_ok": True,
        "notes": "本表由源码核对生成；unsupported/degraded 行必须带 error 与 evidence。",
    }


# ---------------------------------------------------------------------------
# 部分失败语义
# ---------------------------------------------------------------------------
def source_status(source: str, ok: bool, *, error: str = "",
                  at: Optional[str] = None, detail: Optional[dict] = None,
                  duration_ms: Optional[float] = None) -> dict:
    """一个数据来源的状态：来源 / 时间 / 错误（K29 要求三者齐全）。"""
    out = {
        "source": source,
        "ok": bool(ok),
        "at": at or now_iso(),
        "error": "" if ok else (error or "unknown error"),
        "detail": detail or {},
    }
    if duration_ms is not None:
        out["duration_ms"] = round(float(duration_ms), 2)
    return out


def partial_report(data: Any, sources: Iterable[dict], *,
                   envelope_ok: bool = True) -> dict:
    """把「部分失败」表达成结构化信封。

    `ok` 只有在**所有**来源都成功时才为真；`partial` 表示「有的成了、有的没成」。
    前端必须据此显示「部分数据缺失」，不能用 200 当成全部成功。
    """
    items = list(sources)
    ok_count = sum(1 for s in items if s.get("ok"))
    all_ok = ok_count == len(items) and bool(items)
    return {
        "ok": all_ok,
        "envelope_ok": envelope_ok,
        "partial": 0 < ok_count < len(items),
        "generated_at": now_iso(),
        "sources": items,
        "sources_ok": ok_count,
        "sources_total": len(items),
        "data": data,
    }


# ---------------------------------------------------------------------------
# 三层成功判定
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    http_ok: bool
    envelope_ok: bool
    business_ok: bool
    reason: str = ""
    status_code: int = 0

    @property
    def success(self) -> bool:
        return self.http_ok and self.envelope_ok and self.business_ok

    def as_dict(self) -> dict:
        return {
            "http_ok": self.http_ok,
            "envelope_ok": self.envelope_ok,
            "business_ok": self.business_ok,
            "success": self.success,
            "status_code": self.status_code,
            "reason": self.reason,
        }


_IMPLICIT_FAIL_CODES = frozenset({
    "not_implemented", "unauthorized", "forbidden", "upstream_error",
    "upstream_timeout", "config_missing", "validation_error",
})


def classify(status_code: int, body: Any = None, *, explicit_flags: bool = True) -> Verdict:
    """把一次 HTTP 响应拆成三层。

    设计要点（对齐 K29 的「严格区分」）：

    * **http_ok** 只看状态码 2xx；
    * **envelope_ok** 看信封里有没有显式失败标记：`ok:false` / `success:false` /
      `available:false` / `error` 非空 / 已知失败 code；非 dict 体（例如 /metrics 的
      明文）视为信封成立（它本来就没有信封语义）；
    * **business_ok** 在信封成立的前提下再排除 `partial:true`、`errors` 非空；
    * `409/503` 只代表**这次**业务拒绝，绝不据此推断「永久桩」——能力状态必须来自
      `CAPABILITIES` 的人工核对（K29 明确禁止用 AST 分支推断能力）。
    """
    http_ok = 200 <= int(status_code) < 300
    if not explicit_flags:
        return Verdict(http_ok, http_ok, http_ok, "显式关闭信封判定", int(status_code))
    if body is None:
        return Verdict(http_ok, http_ok, http_ok, "无响应体", int(status_code))
    if not isinstance(body, (dict, list)):
        return Verdict(http_ok, True, http_ok, "非结构化响应体（无信封语义）", int(status_code))

    envelope_ok = True
    business_ok = True
    reasons: List[str] = []
    if isinstance(body, dict):
        if "ok" in body and body.get("ok") is False:
            envelope_ok = False
            reasons.append("ok=false")
        if "success" in body and body.get("success") is False:
            envelope_ok = False
            reasons.append("success=false")
        if "available" in body and body.get("available") is False:
            envelope_ok = False
            reasons.append("available=false")
        error = body.get("error")
        if error:
            envelope_ok = False
            code = error.get("code") if isinstance(error, dict) else str(error)
            reasons.append(f"error={code}")
            if code in _IMPLICIT_FAIL_CODES:
                reasons.append(f"code:{code}")
        code_field = body.get("code")
        if isinstance(code_field, str) and code_field in _IMPLICIT_FAIL_CODES:
            envelope_ok = False
            reasons.append(f"code:{code_field}")
        if body.get("partial") is True:
            business_ok = False
            reasons.append("partial=true")
        errors = body.get("errors")
        if isinstance(errors, (list, tuple)) and len(errors) > 0:
            business_ok = False
            reasons.append(f"errors={len(errors)}")
    reason = ", ".join(reasons) or "ok"
    return Verdict(http_ok, envelope_ok, business_ok and envelope_ok, reason, int(status_code))


def capability_state_from_status(status_code: int) -> str:
    """仅用于**提示**，不用于判定能力：409/410/501 多半是「不支持」。

    真正的能力状态必须来自 `CAPABILITIES` 的人工核对（K29 禁止 AST 推断）。
    """
    if status_code in (410, 501):
        return STATE_UNSUPPORTED
    if status_code in (409, 503):
        return STATE_DEGRADED
    return STATE_REAL
