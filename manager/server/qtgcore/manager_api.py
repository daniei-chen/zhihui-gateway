"""QTG → Manager 的内部 API 客户端（K01-S2 / K26）。

QTG 不再拥有公开推理密钥的权威：页面上的列表/创建/修改/吊销都转成对 Manager 的调用。
这里只放"服务身份 + HTTP + 错误翻译"这一层，字段映射留在调用方，便于分别测试。

两种服务凭据（R1-A：读写分离，最小权限）
---------------------------------------
| 用途 | 配置项 | 文件默认路径 | Manager scope | 允许的端点 |
|---|---|---|---|---|
| 只读读取（K26 全部 read_*） | `manager_read_token_file` | `/opt/workbuddy-manager/engine/secrets/manager_read_token` | `read` | GET 只读族（stats/logs/tasks/checkin/audit/accounts/models/settings/keys/read-api） |
| 密钥管理写（K01-S2） | `manager_service_token_file` | `/opt/workbuddy-manager/engine/secrets/manager_service_token` | `keys` | `/api/keys` 的 GET/POST/PATCH/DELETE/reset-usage |

**两者都是显式传参**（`_request(..., purpose=...)` / `_headers(..., purpose=...)`），
缺失都抛 `ConfigMissing` 且消息能区分是哪一种；**不得互相回退**，也不允许用
scope=keys 的令牌兼任读取（那会把"密钥管理"权限带进读通道）。

错误语义（2026-10-25，RV05 修复 + R1-A 扩展）
----------------------------------------------
历史实现把 HTTP 404 统一翻译成 `None`，本意是服务"删除/更新一个不存在的 key"这种
写语义；但读路径（`read_api_version` / `read_stats` / logs / accounts …）拿到同一个
`None`，调用方（WorkBuddyAdapter）又用它表示"当前应该走 sqlite"，于是 http 模式下
上游 404 会**静默回退直读 Manager 的 SQLite**，无库时返回 `{}` —— 把"读不到"伪装成
"没有数据"。现在：

* `_request` **默认严格**：4xx/5xx（含 404）、超时、连接失败、非 JSON、空 body、
  JSON `null` 全部抛异常，且类型可区分（`UpstreamNotFound` / `UpstreamUnauthorized`
  / `UpstreamPayloadError` / `UpstreamTimeout` / `UpstreamError`）；
* 只有显式传 `allow_not_found=True` 的**写**调用（`delete_key` / `update_key` /
  `reset_usage`：目标 key 不存在是业务可回答的结果）才把 404 映射成 `None`；
* 结构异常（非 dict/list、缺必需字段）由各函数用 `_as_dict` / `_as_list` 显式抛
  `UpstreamPayloadError`，不会退化成空值；
* 唯一接受"空对象 `{}`"的读端点是 `/api/settings/panel`（"没配置"是合法状态，
  Manager 侧有测试钉住）；其它端点回 `{}` 仍按结构异常处理；
* 没有"回退另一条通道"这种可能：这里只有 HTTP 一条通道。

读取契约版本（K26 v2，R1-A）
----------------------------
`KNOWN_READ_API_VERSIONS` 是 QTG 认识的契约版本集。适配器在真正读之前会探测一次
`/api/read-api/version` 并按族校验能力；**未知版本或缺失能力必须显式失败**，
不得按旧语义猜（见 WorkBuddyAdapter._ensure_read_capability）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from .errors import (
    ConfigMissing,
    UpstreamError,
    UpstreamNotFound,
    UpstreamPayloadError,
    UpstreamTimeout,
    UpstreamUnauthorized,
)

log = logging.getLogger("qtg.manager_api")

# QTG 认识的读取契约版本。v1：探测 + 既有只读族；v2：新增 usage 族（按天×模型 /
# 全表拆分）与审计日志对只读令牌开放。Manager 升版本时这里要显式跟改——否则
# QTG 会在"未知版本"上明确失败而不是按猜的语义继续跑。
KNOWN_READ_API_VERSIONS = frozenset({1, 2})
# 各读取族进入契约的最低版本（缺失 = 1）。
READ_FAMILY_MIN_VERSION = {"usage": 2}

_TOKEN_LABEL = {
    "read": "只读服务凭据（scope=read）",
    "keys": "密钥管理服务凭据（scope=keys）",
}
_TOKEN_FIELD = {
    "read": "manager_read_token_file",
    "keys": "manager_service_token_file",
}
_TOKEN_HINT = {
    "read": "请按 K26 部署说明创建 scope=read 的 token；它只用于只读读取，"
            "不得用 scope=keys 的令牌兼任（最小权限）",
    "keys": "请按 K01-S2 部署说明创建 scope=keys 的 token；它只用于 /api/keys 的"
            "管理写操作",
}


def read_service_token(path: str, *, purpose: str = "keys",
                       slot: str | None = None) -> str:
    """读取一个 Manager 服务凭据文件；不存在/为空都明确报错。

    `purpose` 只影响**错误消息与提示**（read / keys 两种凭据必须能被运维区分），
    不做"另一种用途的令牌兜底"——那会把最小权限设计悄悄破坏掉。
    `slot` 只用于说明读的是 `current` 还是 `.next`（R3-L5 双凭据过渡）。
    """
    label = _TOKEN_LABEL.get(purpose, "服务凭据")
    hint = _TOKEN_HINT.get(purpose, "")
    where = f"{path}（slot={slot}）" if slot else path
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigMissing(
            f"缺少 Manager {label} 文件 {where}"
            f"（{_TOKEN_FIELD.get(purpose, 'token')}；{hint}）"
        ) from exc
    token = raw.strip()
    if not token:
        raise ConfigMissing(f"Manager {label} 文件为空：{where}（{hint}）")
    return token


def token_path(settings, purpose: str) -> str:
    """按用途取凭据文件路径；配置项缺失（空串/None）也明确报错。"""
    field = _TOKEN_FIELD.get(purpose)
    if field is None:
        raise ValueError(f"未知的 Manager 凭据用途: {purpose!r}（只支持 read / keys）")
    path = str(getattr(settings, field, "") or "")
    if not path:
        raise ConfigMissing(
            f"未配置 {field}（Manager {_TOKEN_LABEL[purpose]} 的文件路径；"
            f"{_TOKEN_HINT[purpose]}）")
    return path


# R3-L5：双凭据过渡的文件后缀。`<path>.next` = 待生效的新值。
TOKEN_NEXT_SUFFIX = ".next"


def token_candidates(settings, purpose: str) -> list[tuple[str, str]]:
    """按轮换优先级返回候选令牌文件：`[("next", <path>.next), ("current", <path>)]`。

    为什么 **next 优先**：
    * 轮换流程是「写 `<path>.next` → 验证 → 提升为 current → 删旧值」。`next` 一旦
      出现就代表**已通过 stage 验证的待生效新值**，优先使用它，运维才能在验证通过后
      立刻在 Manager 侧撤销旧令牌，不必等 QTG 重启或等一个"切换时刻"；
    * 若 `next` 实际不可用（手滑写成坏值/权限不对），`_request` **只在 401/403 时**
      回退 current，稳态不会丢请求；代价只是那一次多一次往返（并记一条 warning）。
    * 不缓存文件内容：轮换要求"改完立刻生效"；原先就是每请求读一次 current，
      多读一个可能不存在的 `.next` 开销可忽略。
    """
    current = token_path(settings, purpose)
    return [("next", current + TOKEN_NEXT_SUFFIX), ("current", current)]


def read_purpose_token(settings, purpose: str, *, slot: str | None = None) -> str:
    """按用途读取对应凭据（read → 只读令牌；keys → 密钥管理令牌）。

    * `slot=None`：按 `token_candidates` 顺序取第一个可读且非空的值；
    * `slot="next"/"current"`：只读该槽位（不存在/为空即 ConfigMissing）。
    两个槽位都缺失时报**一条**明确的 ConfigMissing，同时给出两个路径。
    """
    candidates = token_candidates(settings, purpose)
    if slot is not None:
        for name, path in candidates:
            if name == slot:
                return read_service_token(path, purpose=purpose, slot=name)
        raise ValueError(f"未知的令牌槽位: {slot!r}（只支持 next / current）")
    for name, path in candidates:
        try:
            return read_service_token(path, purpose=purpose, slot=name)
        except ConfigMissing:
            continue
    field = _TOKEN_FIELD.get(purpose, "token")
    label = _TOKEN_LABEL.get(purpose, "服务凭据")
    paths = " 与 ".join(path for _slot, path in candidates)
    raise ConfigMissing(
        f"缺少 Manager {label}：{paths} 都不存在或为空"
        f"（{field}；{_TOKEN_HINT.get(purpose, '')}；"
        "双凭据过渡期间可只写 <path>.next，提升后由轮换工具落为 current）")


def _headers(settings, request_id: str = "", *, purpose: str,
             token: str | None = None, slot: str = "") -> dict[str, str]:
    """内部 API 请求头。`purpose` 必填：读与写用的是**不同**的 service token。

    `token` 由调用方显式传入（`_request` 轮换时逐槽位尝试）；不传则取当前首选值。
    `x-qtg-token-slot` 只用于可观测性（Manager 不读它），便于轮换期间与审计侧
    核对"这一请求用的是 next 还是 current"。
    """
    if token is None:
        token = read_purpose_token(settings, purpose)
    headers = {
        "authorization": f"Bearer {token}",
        "x-internal-actor": "qtg-service",
        "x-qtg-token-purpose": purpose,
    }
    if slot:
        headers["x-qtg-token-slot"] = slot
    if request_id:
        headers["x-request-id"] = request_id
    return headers


def _base(settings) -> str:
    base = (getattr(settings, "wb_panel_base", "") or "").rstrip("/")
    if not base:
        raise ConfigMissing("未配置 Manager 地址（wb_panel_base）")
    return base


def _error_message(resp: httpx.Response) -> str:
    """从上游错误体里取 message（取不到就空串，绝不猜）。"""
    try:
        body = resp.json()
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or "")
    if isinstance(body, dict):
        return str(body.get("detail") or "")
    return ""


def _client(timeout: float) -> httpx.AsyncClient:
    """构造 HTTP 客户端。

    单独抽出来是为了给测试一个**单一接缝**：回归测试用
    `httpx.MockTransport` 替换它，从而真实走 `_request` 的状态码/载荷翻译逻辑，
    而不需要起端口、也不影响生产路径。
    """
    return httpx.AsyncClient(timeout=timeout)


def _as_dict(data: Any, what: str) -> dict:
    """结构校验：必须是非空 JSON 对象（`{}` / `null` 都不算"有数据"）。"""
    if not isinstance(data, dict):
        raise UpstreamPayloadError(
            f"Manager 的{what}结构异常：期望 JSON 对象，实际是 {type(data).__name__}")
    if not data:
        raise UpstreamPayloadError(f"Manager 的{what}结构异常：返回了空对象 {{}}")
    return data


def _as_list(data: Any, what: str) -> list:
    """结构校验：必须是 JSON 数组，且每个元素都是对象。"""
    if not isinstance(data, list):
        raise UpstreamPayloadError(
            f"Manager 的{what}结构异常：期望 JSON 数组，实际是 {type(data).__name__}")
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise UpstreamPayloadError(
                f"Manager 的{what}结构异常：第 {index} 项不是对象"
                f"（{type(item).__name__}）")
    return data


_LOG_LIST_KEYS = ("items", "data", "logs", "rows")


def _log_payload(data: Any, what: str) -> Any:
    """日志族载荷校验：接受"带分页信息的对象"或"纯数组"，但里面必须有日志数组。"""
    if isinstance(data, list):
        return _as_list(data, what)
    if not isinstance(data, dict):
        raise UpstreamPayloadError(
            f"Manager 的{what}结构异常：期望对象或数组，实际是 {type(data).__name__}")
    if not data:
        raise UpstreamPayloadError(f"Manager 的{what}结构异常：返回了空对象 {{}}")
    for key in _LOG_LIST_KEYS:
        if key in data:
            _as_list(data[key], f"{what}的 {key} 字段")
            return data
    raise UpstreamPayloadError(
        f"Manager 的{what}结构异常：对象里没有 {'/'.join(_LOG_LIST_KEYS)} 数组字段")


async def _request(
    settings,
    method: str,
    path: str,
    *,
    purpose: str,
    payload: dict | None = None,
    params: dict | None = None,
    request_id: str = "",
    allow_not_found: bool = False,
    allow_empty_object: bool = False,
) -> Any:
    """内部 API 请求。**默认严格**：任何"读不到"都抛异常，绝不返回含糊的 None。

    `purpose` 必填（read / keys）：读端点送 `scope=read` 令牌，写端点送
    `scope=keys` 令牌，两者不得互相回退（最小权限）。

    `allow_not_found=True` 是本模块唯一的"404 → None"入口，只能由**写**调用显式
    声明（目标资源不存在是业务可回答的结果）。读路径一律不传：404 是"资源不存在"，
    与"没有数据"是两件事，更不能变成"换条通道再读一次"的许可。

    `allow_empty_object=True` 只给 `/api/settings/panel` 这类"空表 = 没配置"
    合法状态的端点；其它端点回 200 + `{}` 仍按结构异常处理。
    """
    url = _base(settings) + path
    timeout = float(getattr(settings, "manager_api_timeout_sec", 10.0) or 10.0)

    # R3-L5 双凭据过渡：next 优先、current 兜底。只有 401/403（凭据本身不被接受）
    # 才换下一个候选；超时/连接失败/5xx 换令牌也不会变好，直接按原语义抛出。
    candidates = token_candidates(settings, purpose)
    tried: list[dict[str, Any]] = []
    last_unauthorized: UpstreamUnauthorized | None = None
    resp: httpx.Response | None = None
    used_slot = ""
    for index, (slot, candidate_path) in enumerate(candidates):
        try:
            token = read_service_token(candidate_path, purpose=purpose, slot=slot)
        except ConfigMissing as exc:
            tried.append({"slot": slot, "result": "missing", "detail": str(exc)[:160]})
            continue
        headers = _headers(settings, request_id, purpose=purpose, token=token,
                           slot=slot)
        try:
            async with _client(timeout) as client:
                resp = await client.request(
                    method, url, json=payload, params=params, headers=headers
                )
        except httpx.TimeoutException as exc:
            log.warning("manager api timeout: %s %s: %s", method, path, exc)
            raise UpstreamTimeout(
                f"Manager 内部 API 超时：{method} {path}（{purpose} 通道，"
                f"token_slot={slot}）"
            ) from exc
        except httpx.HTTPError as exc:
            # ConnectError/ReadError/RemoteProtocolError…：连接失败与超时是两种事故，
            # 调用方要能分开处理（超时可重试，连接拒绝通常意味着配置/进程问题）。
            log.warning("manager api unreachable: %s %s: %s", method, path, exc)
            raise UpstreamError(
                f"Manager 内部 API 连接失败：{method} {path}（{type(exc).__name__}）",
                code="upstream_unreachable",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 其它网络层错误统一翻译
            log.warning("manager api unreachable: %s %s: %s", method, path, exc)
            raise UpstreamError(
                f"Manager 内部 API 不可用：{method} {path}（{type(exc).__name__}）",
                code="upstream_unreachable",
            ) from exc

        tried.append({"slot": slot, "status": resp.status_code})
        if resp.status_code in (401, 403):
            message = _error_message(resp)
            label_ = _TOKEN_LABEL.get(purpose, "服务凭据")
            reason = ("令牌无效或已过期" if resp.status_code == 401
                      else "该令牌的权限不足（scope 不含此端点）")
            last_unauthorized = UpstreamUnauthorized(
                f"QTG 未被 Manager 授权：{method} {path} → HTTP {resp.status_code}"
                f"（{label_} {reason}；token_slot={slot}）"
                + (f"；上游说明：{message}" if message else ""),
                upstream_status=resp.status_code,
                token_purpose=purpose,
            )
            last_unauthorized.token_slots_tried = [dict(item) for item in tried]
            if index + 1 < len(candidates):
                log.warning(
                    "manager api token slot %s rejected (%s %s -> %s)，回退下一个候选",
                    slot, method, path, resp.status_code)
                continue
            raise last_unauthorized
        used_slot = slot
        break

    if resp is None:
        # 两个槽位都读不到（文件不存在/为空）：明确失败，并列出两个路径。
        read_purpose_token(settings, purpose)  # 复用它的 ConfigMissing 文案（会抛）
        raise ConfigMissing(  # pragma: no cover - 防御式兜底
            f"缺少 Manager {_TOKEN_LABEL.get(purpose, '服务凭据')}："
            f"{' 与 '.join(path for _slot, path in candidates)} 都不可读")

    status = resp.status_code
    label = _TOKEN_LABEL.get(purpose, "服务凭据")
    if status in (401, 403):
        message = _error_message(resp)
        log.warning("manager api unauthorized: %s %s -> %s (purpose=%s)",
                    method, path, status, purpose)
        # 401/403 的两种成因必须可分：令牌本身无效（401）与令牌 scope 不足（403）。
        reason = ("令牌无效或已过期" if status == 401
                  else "该令牌的权限不足（scope 不含此端点）")
        raise UpstreamUnauthorized(
            f"QTG 未被 Manager 授权：{method} {path} → HTTP {status}"
            f"（{label} {reason}）"
            + (f"；上游说明：{message}" if message else ""),
            upstream_status=status,
            token_purpose=purpose,
        )
    if status == 404:
        if allow_not_found:
            # 唯一的 404 → None 入口：只有声明了该语义的**写**调用会走到这里。
            return None
        message = _error_message(resp)
        raise UpstreamNotFound(
            f"Manager 未找到资源：{method} {path} → HTTP 404"
            + (f"（上游说明：{message}）" if message else ""),
            upstream_status=status,
        )
    if status == 204:
        # 204 是"成功且无 body"的语义（删除类端点可能这样回），不是空数据。
        return {}
    if status >= 400:
        message = _error_message(resp)
        raise UpstreamError(
            f"Manager 拒绝请求（{method} {path} → HTTP {status}）"
            + (f"：{message}" if message else ""),
            code="upstream_server_error" if status >= 500 else "upstream_rejected",
            upstream_status=status,
        )
    if not resp.content:
        raise UpstreamPayloadError(
            f"Manager 返回空 body：{method} {path} → HTTP {status}",
            upstream_status=status,
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise UpstreamPayloadError(
            f"Manager 返回非 JSON 响应：{method} {path} → HTTP {status}",
            upstream_status=status,
        ) from exc
    if data is None:
        # 200 + JSON null 曾是 404 之外的第二条静默回退路径：None 会被调用方当成
        # "该走 sqlite"。这里必须显式失败。
        raise UpstreamPayloadError(
            f"Manager 返回 JSON null：{method} {path} → HTTP {status}",
            upstream_status=status,
        )
    if data == {} and not allow_empty_object:
        # 空对象不是"没有数据"：本次迁移用到的每个读端点都会回非空字段
        # （唯一例外是 settings/panel：空表 = 没配置，由 allow_empty_object 放行）。
        # 允许空数组（"这个区间没有日志"是合法业务结果）。
        raise UpstreamPayloadError(
            f"Manager 返回空对象 {{}}：{method} {path} → HTTP {status}",
            upstream_status=status,
        )
    return data


async def list_keys(settings, *, request_id: str = "") -> list[dict]:
    """密钥列表：**只读**，走 read 令牌（Manager 的 scope=read 白名单允许 GET /api/keys）。"""
    data = await _request(settings, "GET", "/api/keys", purpose="read",
                          request_id=request_id)
    return _as_list(data, "密钥列表")


async def create_key(settings, payload: dict[str, Any], *, request_id: str = "") -> dict:
    data = await _request(settings, "POST", "/api/keys", purpose="keys",
                          payload=payload, request_id=request_id)
    return _as_dict(data, "创建密钥响应")


async def update_key(settings, key_id: int, patch: dict, *,
                     request_id: str = "") -> dict | None:
    """更新密钥：目标不存在（上游 404）返回 None —— 这是**显式声明**的写语义。"""
    data = await _request(
        settings, "PATCH", f"/api/keys/{key_id}", payload=patch, purpose="keys",
        request_id=request_id, allow_not_found=True,
    )
    if data is None:
        return None
    return _as_dict(data, "更新密钥响应")


async def delete_key(settings, key_id: int, *, request_id: str = "") -> bool:
    """删除密钥：不存在（上游 404）返回 False；其余失败一律抛错。

    只有这里和 update_key 用 `allow_not_found=True`：Manager 对"不存在的 key"回
    404，而"删除/更新不存在的 key"本身是业务可回答的结果。无条件 True 会把
    "没删成"报成成功（K29/N21：与 Manager 自身及 PATCH 的 404 语义对齐）。
    """
    data = await _request(
        settings, "DELETE", f"/api/keys/{key_id}", purpose="keys",
        request_id=request_id, allow_not_found=True,
    )
    return data is not None


async def reset_usage(settings, key_id: int, *, request_id: str = "") -> bool:
    """重置用量：目标不存在（上游 404）返回 False，其余失败抛错。

    与 delete/update 同一族：Manager 对不存在的 key 回 404，QTG 的既有对外契约是
    200 + `{"ok": false}`（K29 的逐动作错误契约在 R1 收敛），因此这里显式声明
    `allow_not_found=True`，而不是让 404 变成一个上游故障。
    """
    data = await _request(
        settings, "POST", f"/api/keys/{key_id}/reset-usage", purpose="keys",
        request_id=request_id, allow_not_found=True,
    )
    if data is None:
        return False
    body = _as_dict(data, "重置用量响应")
    return bool(body.get("ok", True))


def manager_request_id(request) -> str:
    """沿用 QTG 的 request id，便于两边审计关联。"""
    return str(getattr(request.state, "request_id", "") or "")


# ---------------------------------------------------------------- K26 只读通道
# 下面这组函数把「读 Manager 数据」从「只读打开它的 SQLite」改成「带**只读**服务
# 凭据（scope=read）的 HTTP 读取」。契约由 Manager 的 /api/read-api/version 声明
# （见设计文档 design/qtg/K26-read-api-migration-assessment.md）。
#
# **关键约束**：
#   1. HTTP 不可用时**不得**回退直读 DB（那会让"共享文件系统"这个前提悄悄复活，
#      而且在容器化部署里根本不成立）。调用方必须处理抛出的异常；
#   2. 读通道只用 `purpose="read"` 的令牌——绝不用 scope=keys 的密钥管理令牌兜底；
#   3. 返回结构按族校验，形状不对显式抛 UpstreamPayloadError。

_READ = "read"


async def read_api_version(settings, *, request_id: str = "") -> dict:
    """探测读取契约版本与能力表（读通道，scope=read）。

    返回结构见 Manager `routers/read_api.py`：`version` / `capabilities` /
    `timezone` / `credit_unit` / `timestamp_unit` / `pagination`。
    """
    data = await _request(settings, "GET", "/api/read-api/version", purpose=_READ,
                          request_id=request_id)
    return _as_dict(data, "读取契约探测响应")


async def read_api_health(settings, *, request_id: str = "") -> dict:
    data = await _request(settings, "GET", "/api/read-api/health", purpose=_READ,
                          request_id=request_id)
    return _as_dict(data, "读取通道健康响应")


def supports(version_info: dict, capability: str) -> bool:
    """能力表里该族是否可用（缺失一律视为不可用——不做乐观假设）。"""
    caps = version_info.get("capabilities") if isinstance(version_info, dict) else None
    if not isinstance(caps, dict):
        return False
    return bool(caps.get(capability))


# 统计族端点的期望结构：缺失/形状不对一律当结构异常（显式抛错，不当空值）。
_STATS_DICT_FAMILIES = frozenset({"summary", "credits", "upstream"})
_STATS_LIST_FAMILIES = frozenset({"daily", "hourly", "by-model", "by-key"})


async def read_stats(settings, name: str, params: dict | None = None,
                     *, request_id: str = "") -> Any:
    """读取一个统计族端点（summary/daily/hourly/credits/by-model/by-key/upstream）。

    `name` 限定在白名单内：这个函数只服务统计读取，不接受任意路径（避免变成
    通用代理后被误用于写端点）。返回结构按族校验：summary/credits/upstream 是
    对象，daily/hourly/by-model/by-key 是对象数组。
    """
    if name not in _STATS_DICT_FAMILIES | _STATS_LIST_FAMILIES:
        raise ValueError(f"unsupported stats endpoint: {name}")
    data = await _request(settings, "GET", f"/api/stats/{name}", purpose=_READ,
                          request_id=request_id, params=params)
    what = f"统计 {name} 响应"
    if name in _STATS_DICT_FAMILIES:
        return _as_dict(data, what)
    return _as_list(data, what)


async def read_logs(settings, params: dict | None = None, *, request_id: str = "") -> Any:
    """请求日志：上游回对象（含分页元信息）或数组，二者都接受，其它形状报错。"""
    data = await _request(settings, "GET", "/api/logs", purpose=_READ, params=params,
                          request_id=request_id)
    return _log_payload(data, "请求日志")


async def read_task_logs(settings, params: dict | None = None, *,
                         request_id: str = "") -> Any:
    data = await _request(settings, "GET", "/api/task-logs", purpose=_READ,
                          params=params, request_id=request_id)
    return _log_payload(data, "任务日志")


async def read_checkin_logs(settings, params: dict | None = None, *,
                            request_id: str = "") -> Any:
    data = await _request(settings, "GET", "/api/checkin-logs", purpose=_READ,
                          params=params, request_id=request_id)
    return _log_payload(data, "签到日志")


async def read_audit_logs(settings, params: dict | None = None, *,
                          request_id: str = "") -> Any:
    """审计日志：需要 Manager 的 `require_admin_or_read_scope`（v2 起 scope=read 可读）。"""
    data = await _request(settings, "GET", "/api/audit-logs", purpose=_READ,
                          params=params, request_id=request_id)
    return _log_payload(data, "审计日志")


async def read_accounts(settings, params: dict | None = None, *,
                        request_id: str = "") -> Any:
    """账号列表：Manager 回 `{"accounts": [...]}` / `{"items": [...]}` / 数组。"""
    data = await _request(settings, "GET", "/api/accounts", purpose=_READ,
                          params=params, request_id=request_id)
    if isinstance(data, list):
        return _as_list(data, "账号列表")
    if not isinstance(data, dict):
        raise UpstreamPayloadError(
            f"Manager 的账号列表结构异常：期望对象或数组，实际是 {type(data).__name__}")
    if not data:
        raise UpstreamPayloadError("Manager 的账号列表结构异常：返回了空对象 {}")
    for key in ("accounts", "items"):
        if key in data:
            _as_list(data[key], f"账号列表的 {key} 字段")
            return data
    raise UpstreamPayloadError("Manager 的账号列表结构异常：对象里没有 accounts/items 数组")


async def read_models(settings, params: dict | None = None, *,
                      request_id: str = "") -> Any:
    data = await _request(settings, "GET", "/api/models", purpose=_READ,
                          params=params, request_id=request_id)
    if isinstance(data, list):
        return _as_list(data, "模型列表")
    if not isinstance(data, dict):
        raise UpstreamPayloadError(
            f"Manager 的模型列表结构异常：期望对象或数组，实际是 {type(data).__name__}")
    if not data:
        raise UpstreamPayloadError("Manager 的模型列表结构异常：返回了空对象 {}")
    return data


async def read_api_keys(settings, *, request_id: str = "") -> list[dict]:
    """读取密钥列表（只含 prefix，无明文/哈希）。**只读**，走 read 令牌。"""
    data = await _request(settings, "GET", "/api/keys", purpose=_READ,
                          request_id=request_id)
    return _as_list(data, "密钥列表")


async def read_settings_panel(settings, *, request_id: str = "") -> dict:
    """读取管理端设置面板（settings 表）。Manager 侧走 /api/settings/panel。

    settings 表为空时 Manager 回 `{}`（"没配置"是合法状态，Manager 侧有测试钉住
    `test_panel_empty_table_returns_empty_dict`），因此这里允许空对象；其它读端点
    回 `{}` 仍是结构异常。
    """
    data = await _request(settings, "GET", "/api/settings/panel", purpose=_READ,
                          request_id=request_id, allow_empty_object=True)
    if not isinstance(data, dict):
        raise UpstreamPayloadError(
            f"Manager 的设置面板结构异常：期望 JSON 对象，实际是 {type(data).__name__}")
    return data


# ---------------------------------------------------------------- K26 v2：usage 族
# QTG 的 `usage()`（按天×模型）与 `usage_analytics()`（全表拆分）无法由既有
# stats 端点无损表达（/api/stats/daily 不带模型维度；summary 只有 total_tokens）。
# Manager v2 因此新增两个只读端点，见 routers/read_api.py 的口径说明。
# **刻意返回"原始行"**：QTG 两种读模式共用同一段映射代码，切换时逐位对账一致。


async def read_usage_matrix(settings, since: str, *, request_id: str = "") -> dict:
    """按天×模型的用量原始行 + 日级失败数（Manager v2 `usage` 族）。

    返回 `{"version", "since", "rows": [...], "failures": {day: n}}`。
    """
    data = await _request(settings, "GET", "/api/read-api/usage-matrix", purpose=_READ,
                          params={"since": since}, request_id=request_id)
    payload = _as_dict(data, "用量矩阵")
    version = payload.get("version")
    if version not in KNOWN_READ_API_VERSIONS:
        raise UpstreamError(
            f"Manager 的用量矩阵响应版本未知：{version!r}"
            f"（QTG 支持 {sorted(KNOWN_READ_API_VERSIONS)}）",
            code="read_api_unsupported")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise UpstreamPayloadError(
            f"Manager 的用量矩阵结构异常：rows 不是数组（{type(rows).__name__}）")
    failures = payload.get("failures")
    if not isinstance(failures, dict):
        raise UpstreamPayloadError(
            f"Manager 的用量矩阵结构异常：failures 不是对象（{type(failures).__name__}）")
    return payload


async def read_usage_totals(settings, *, request_id: str = "") -> dict:
    """全表用量拆分（Manager v2 `usage` 族）。"""
    data = await _request(settings, "GET", "/api/read-api/usage-totals", purpose=_READ,
                          request_id=request_id)
    payload = _as_dict(data, "用量全表汇总")
    version = payload.get("version")
    if version not in KNOWN_READ_API_VERSIONS:
        raise UpstreamError(
            f"Manager 的用量汇总响应版本未知：{version!r}"
            f"（QTG 支持 {sorted(KNOWN_READ_API_VERSIONS)}）",
            code="read_api_unsupported")
    return payload

