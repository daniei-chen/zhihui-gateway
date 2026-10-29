"""Request identity, bounded SSE audit, and per-request outcome bookkeeping.

本模块不保留消息正文或凭据，只保留身份、时序、终态与用量摘要。

## 两套 ID：服务端执行 ID 与调用方关联 ID

* **执行 ID**（`RequestAudit.request_id`）：本进程用 `uuid.uuid4().hex` 生成，
  调用方**无法指定**。它是这一笔执行的唯一所有权键：`budget_reservations` /
  `budget_adjustments` 主键、`request_logs.request_id`、`request_outcomes`
  主键、ledger / attempt_events 的 `request_id` 全部用它，一笔执行一个键。
* **调用方关联 ID**（`RequestAudit.client_request_id`）：只来自请求头
  `X-Request-ID`，沿用 `_CALLER_REQUEST_ID` 的字符集/长度校验，不合法就是
  `None`（**不**回退成执行 ID 写进这个字段 —— 那等于伪造调用方的值）。
  它只做跨系统相关性：落 `request_logs.client_request_id` /
  `request_outcomes.client_request_id`，转发上游时作为 `X-Request-ID` 透传，
  同时用 `X-Manager-Request-ID` 带上执行 ID。它**不参与任何主键**。

为什么必须这样（RV01，已在本地复现）：外部 `X-Request-ID` 曾被原样采纳为
执行 ID，于是同一调用方重发同一个 ID 会撞预算主键（第二个请求 500，上游只
收到第一笔）；不同租户写同一个 ID 会共用同一个全局命名空间；匿名请求先以
401 占下某个 ID，后续合法请求的成功结果被 `INSERT OR IGNORE` 丢弃，终态停在
先前的 401/rejected。**字符合法不等于身份可信，也不等于全局唯一。**

## 外部 ID 不是幂等键（本轮不实现幂等）

`X-Request-ID` 一类的 correlation ID **不是**幂等键，不得拿它冒充幂等机制：

* 幂等键必须绑定「主体（用户/密钥）+ 端点 + 请求摘要 + 有效期 + 处理中/
  已完成状态」，并明确定义并发下第二次请求返回什么（重放首个结果还是 409）；
* 关联 ID 由调用方任意填写，可重复、可猜测、跨主体共用命名空间。拿它做幂等
  会让 A 用户的重复 ID 命中 B 用户的结果，或者让一次真实重试被当成新请求。

当前没有幂等 API；真正的幂等需要独立的键表与状态机。本轮只做「执行 ID 单一
所有权 + 关联 ID 独立保存」，DDL 见 `migrations._request_identity`。
"""
from __future__ import annotations
import codecs
import contextvars
import json
import logging
import re
import os
import sys
import time
import uuid
import anyio
import threading
import asyncio
from starlette.responses import JSONResponse, StreamingResponse
from dataclasses import dataclass

# 调用方可以自带 X-Request-ID（K47：edge → Manager → 引擎全链路关联），但它
# 只是**关联 ID**，不再是执行 ID（RV01）。字符集与长度仍然必须收紧：这个值会
# 进响应头、转发头和日志列，放开就等于允许头注入 / 日志伪造 / 超长取值。
_CALLER_REQUEST_ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')

#: 调用方关联 ID 的请求头名（ASGI `scope["headers"]` 的形态）。
CLIENT_REQUEST_ID_HEADER = b'x-request-id'

#: 响应头：调用方关联 ID 的回显；只在通过校验时出现。
CLIENT_REQUEST_ID_RESPONSE_HEADER = b'x-client-request-id'


def new_request_id() -> str:
    """服务端执行 ID：32 位 uuid hex，调用方不可控制（RV01）。

    所有账本/预算/终态主键都从这里取值；它不是「随机兜底」，而是唯一来源。
    """
    return uuid.uuid4().hex


def client_request_id_from_headers(headers: dict) -> str | None:
    """只提取**调用方关联 ID**；缺失或不合法一律返回 None。

    这个函数**不生成任何 ID**：执行 ID 必须来自 `new_request_id()`。把两者
    混在一处（不合法就用生成的 ID 顶上）正是 RV01 的成因，也会让「调用方到
    底给了什么」变得不可区分。latin-1 是 ASGI 头的字节语义，正常必然成功解码；
    try 只是防御非字节输入。
    """
    raw = headers.get(CLIENT_REQUEST_ID_HEADER)
    if not raw:
        return None
    try:
        text = raw.decode('latin-1')
    except (UnicodeDecodeError, AttributeError):
        return None
    return text if _CALLER_REQUEST_ID.fullmatch(text) else None


# ── U05：输出上限被预算收缩时的响应头 ────────────────────────────────────
#
# 为什么用响应头而不是 `usage` 扩展字段：
#   1. 三条协议（chat / messages / responses）的 usage 结构各不相同，加字段要改
#      三套 schema，还要各自处理流式末帧；响应头一套就够。
#   2. 收缩发生在 `budget.prepare`（转发**之前**），所以流式响应在开流那一刻
#      就已经能带上头；写进 usage 则只有流末事件才拿得到。
#   3. 未登记的响应头对客户端是**透明**的：DSH / OpenAI SDK / Anthropic SDK
#      都只读自己认识的字段，多出来的头不会报错（HTTP 层没有严格模式）。
#
# 只在**真的收缩**（effective < requested）时出现；未收缩时零标记，
# 因此「有没有这个头」本身就是信号，不需要再带一个布尔位。
MAX_TOKENS_REQUESTED_HEADER = b'x-manager-max-tokens-requested'
MAX_TOKENS_EFFECTIVE_HEADER = b'x-manager-max-tokens-effective'
MAX_TOKENS_REASON_HEADER = b'x-manager-max-tokens-shrink-reason'
#: 收缩原因：目前只有一种（token 配额不足，见 budget.prepare）。
MAX_TOKENS_REASON_BUDGET = b'budget_quota'
#: 本中间件独占的响应头（下游路由自带的同名头先剥后加）。
MAX_TOKENS_HEADERS = (MAX_TOKENS_REQUESTED_HEADER, MAX_TOKENS_EFFECTIVE_HEADER,
                      MAX_TOKENS_REASON_HEADER)


def _limit_int(value: object) -> int | None:
    """只接受**正整数**（bool 是 int 的子类，必须排除）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def record_max_tokens_contract(state, field: object, requested: object,
                               effective: object) -> None:
    """记录「本次输出上限是否被 budget.prepare 收缩」（U05）。

    只**读**收缩前后的值，不改 budget.py，也不影响硬预算：配额不足时
    `budget.prepare` 仍然照旧抛 `BudgetRejected`（429），这里只在**成功预留**
    之后补一个客户端可见的说明。
    """
    if state is None:
        return
    want, got = _limit_int(requested), _limit_int(effective)
    if want is None or got is None:
        return
    state.max_tokens_field = str(field)[:32]
    state.max_tokens_requested = want
    state.max_tokens_effective = got


def _max_tokens_headers(state) -> list[tuple[bytes, bytes]]:
    want = _limit_int(getattr(state, 'max_tokens_requested', None))
    got = _limit_int(getattr(state, 'max_tokens_effective', None))
    if want is None or got is None or got >= want:
        return []
    return [(MAX_TOKENS_REQUESTED_HEADER, str(want).encode()),
            (MAX_TOKENS_EFFECTIVE_HEADER, str(got).encode()),
            (MAX_TOKENS_REASON_HEADER, MAX_TOKENS_REASON_BUDGET)]


def _response_headers(state: "RequestAudit") -> list[tuple[bytes, bytes]]:
    """本中间件负责的响应头（先剥后加，见 `RequestAuditMiddleware.send_audited`）。"""
    out = [(b'x-request-id', state.request_id.encode()),
           (b'x-workbuddy-release', RELEASE_ID.encode())]
    # 只在调用方真的给了合法值时才回显；None 表示「没给或不合法」，
    # 不能拿执行 ID 冒充，也不能回显未校验的原值。
    if state.client_request_id:
        out.append((CLIENT_REQUEST_ID_RESPONSE_HEADER, state.client_request_id.encode()))
    # U05：仅在输出上限被预算收缩时追加（未收缩时零标记）。
    out.extend(_max_tokens_headers(state))
    return out


def _release_id() -> str:
    """发布标识：读 /opt/workbuddy-manager/.version。

    2026-10-23 审计：此处曾写死 v1.0.79+closure.20261005.1，而实际部署已到
    v2.8.3+local.20261022.3，导致 14.5 万行 request_outcomes/usage_events 的
    release_id 全部错标、审计追溯失效。改为读版本文件。
    """
    from . import config
    for path in (str(config.ROOT / '.version'), '/opt/workbuddy-manager/.version'):
        try:
            with open(path, encoding='utf-8') as fh:
                text = fh.read().strip()
            if text:
                return text
        except OSError:
            continue
    return "unknown"


RELEASE_ID = _release_id()
log = logging.getLogger("workbuddy.audit")

@dataclass
class RequestAudit:
    request_id: str
    endpoint: str
    method: str
    protocol: str
    started: float
    http_status: int = 0
    terminal_state: str | None = None
    error_stage: str | None = None
    account: str | None = None
    first_byte_ms: int | None = None
    recorded: bool = False
    key_slot: int | None = None
    key_id: int | None = None
    budget_reserved: bool = False
    #: 调用方关联 ID（`X-Request-ID`，校验过）；None = 未提供或不合法。
    #: **不参与任何主键**，只是相关性提示；见模块 docstring。
    client_request_id: str | None = None
    # ── U05（2026-10-28）：输出上限契约 ────────────────────────────────
    # `budget.prepare()` 在 token 配额不足时会把 `body[output_field]` 收缩成
    # `remaining - estimated_input`，但**不产生任何客户端可见信号**。这三个字段
    # 只用来把「客户端要了多少 / 实际发了多少」带到响应头，budget.py 一行不改。
    #: 客户端在转发体里给出的输出上限（`budget.prepare` **之前**的值）。
    max_tokens_requested: int | None = None
    #: `budget.prepare` **之后**真正发给上游的值。
    max_tokens_effective: int | None = None
    #: 承载该上限的字段名（`max_tokens` / `max_completion_tokens`）。
    max_tokens_field: str | None = None
    # ── U21（2026-10-28）：稳定错误码 ────────────────────────────────────
    #: 稳定错误码（上游业务码或网关内部分类码）；None = 尚未标注。
    #: 只允许 `[A-Za-z0-9._:-]{1,64}` —— 这个字符集从结构上排除了 URL（含 `/`）、
    #: 空白、中文文案与 `Bearer xxx` 之类凭据片段进入这一列。
    error_code: str | None = None
    #: 码的来源：`upstream`（上游业务码）/ `gateway`（网关分类）/ `client`（取消）。
    error_source: str | None = None

current: contextvars.ContextVar[RequestAudit | None] = contextvars.ContextVar("wb_request_audit", default=None)
_key_slots={}
_key_lock=threading.RLock()
_outcome_writes=0

#: F3（2026-10-27）：中间件实例引用，供 /metrics 读取实时准入水位。
#: 单进程单实例是本部署的既定契约（限流/并发闸均在进程内存），
#: 这里只登记首个实例，行为与契约一致。
_middleware_ref = None


# ── U21（2026-10-28）：稳定 error_code ──────────────────────────────────────
#
# 问题：`request_outcomes` 只有 `terminal_state` + `error_stage` 两个宽泛枚举，
# 「429 到底是限流、密钥并发还是预算拒绝」「502 是上游拒绝还是我们连不上」都
# 分不出来，发布后想回答「哪类错误变多了」只能去翻文案。
#
# 稳定码的来源顺序（与审计口径一致）：
#   1. **上游业务码优先**：上游错误体里的 `error.code`（如 `11102`、
#      `no_healthy_account`）—— 那是唯一能区分「上游说不行」与「我们说不行」的
#      事实，直接沿用，不重新发明一套；
#   2. **网关内部分类**：鉴权 / 预算 / 容量 / 协议 / 传输 / 取消，由
#      `terminal_state` + `error_stage` + `http_status` 确定性推导；
#   3. 都拿不到 → `gateway.unknown`（不猜成成功，也不留空冒充"没出错"）。
#
# 落 `request_outcomes.error_code` / `error_source`；`request_logs.error` 仍然
# 保存**已脱敏**的原消息（本项不改它的语义）。两列都不允许承载提示词、凭据或
# URL —— 靠下面的字符集白名单在写入前就挡住，而不是靠调用方自觉。
ERROR_CODE_NONE = 'none'
ERROR_CODE_UNKNOWN = 'gateway.unknown'
ERROR_SOURCE_UPSTREAM = 'upstream'
ERROR_SOURCE_GATEWAY = 'gateway'
ERROR_SOURCE_CLIENT = 'client'
ERROR_SOURCE_NONE = 'none'

#: 稳定码的字符集（也是长度上限）。写库前必须通过它。
_ERROR_CODE_RE = re.compile(r'[A-Za-z0-9._:-]{1,64}\Z')

#: U21：`request_outcomes` 需要补的列（幂等补，见 `ensure_outcome_schema`）。
_OUTCOME_EXTRA_COLUMNS = (('error_code', 'TEXT'), ('error_source', 'TEXT'))
#: 每个连接只探测一次 schema；`conn` 用连接对象身份做键（测试会换连接）。
_OUTCOME_SCHEMA: dict = {'conn': None, 'ready': False}


def sanitize_error_code(value) -> str | None:
    """把候选码收成 `[A-Za-z0-9._:-]{1,64}`；不合法一律 None（不截断成半截码）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _ERROR_CODE_RE.match(text) else None


def mark_error(code, source: str = ERROR_SOURCE_GATEWAY) -> str | None:
    """在当前请求上标注稳定错误码；返回最终生效的码。

    上游业务码优先：已经标了 `upstream` 来源的码不会被后来的网关分类覆盖
    （网关只知道"哪一步失败"，不知道上游为什么拒绝）。
    """
    state = current.get()
    if state is None:
        return None
    clean = sanitize_error_code(code)
    if clean is None:
        return None
    if (getattr(state, 'error_code', None)
            and getattr(state, 'error_source', None) == ERROR_SOURCE_UPSTREAM):
        return state.error_code
    state.error_code = clean
    state.error_source = source
    return clean


def _gateway_code(terminal, stage, status) -> tuple[str, str]:
    """由 (终态, 阶段, 状态码) 确定性地推出网关侧分类码。"""
    if terminal == 'client_cancelled' or status == 499:
        return 'client.cancelled', ERROR_SOURCE_CLIENT
    if terminal == 'completed' or (terminal is None and 200 <= status < 400):
        return ERROR_CODE_NONE, ERROR_SOURCE_NONE
    if terminal == 'rejected':
        if status in (401, 403):
            return 'gateway.auth.denied', ERROR_SOURCE_GATEWAY
        if status == 429:
            return 'gateway.rate_limited', ERROR_SOURCE_GATEWAY
        if status == 503:
            return 'gateway.capacity.rejected', ERROR_SOURCE_GATEWAY
        if status == 413:
            return 'gateway.request.too_large', ERROR_SOURCE_GATEWAY
        if stage == 'request_body':
            return 'gateway.request.body_error', ERROR_SOURCE_GATEWAY
        return 'gateway.request.invalid', ERROR_SOURCE_GATEWAY
    if terminal == 'protocol_incomplete':
        return 'gateway.protocol.incomplete', ERROR_SOURCE_GATEWAY
    if terminal == 'transport_error':
        return 'gateway.transport.error', ERROR_SOURCE_GATEWAY
    if terminal == 'upstream_error':
        return 'gateway.upstream.error', ERROR_SOURCE_GATEWAY
    if terminal == 'application_error':
        return 'gateway.application.error', ERROR_SOURCE_GATEWAY
    if terminal == 'interrupted':
        return 'gateway.interrupted', ERROR_SOURCE_GATEWAY
    if status >= 500:
        return 'gateway.application.error', ERROR_SOURCE_GATEWAY
    if status >= 400:
        return 'gateway.request.invalid', ERROR_SOURCE_GATEWAY
    if status:
        return ERROR_CODE_NONE, ERROR_SOURCE_NONE
    return ERROR_CODE_UNKNOWN, ERROR_SOURCE_GATEWAY


def classify_error(state=None, *, terminal_state=None, error_stage=None,
                   http_status: int = 0) -> tuple[str, str]:
    """返回 `(error_code, error_source)`：上游业务码优先，其次网关内部分类。

    成功一律是 `none` —— 即使请求中途标注过错误码（例如一次上游抖动后被重试
    成功），成功的结果不该带上错误码。
    """
    state = state if state is not None else current.get()
    terminal = terminal_state if terminal_state is not None else getattr(
        state, 'terminal_state', None)
    stage = error_stage if error_stage is not None else getattr(state, 'error_stage', None)
    status = int(http_status or (getattr(state, 'http_status', 0) or 0))
    if terminal == 'completed' or (terminal is None and 200 <= status < 400):
        return ERROR_CODE_NONE, ERROR_SOURCE_NONE
    code = getattr(state, 'error_code', None) if state is not None else None
    if code:
        return code, (getattr(state, 'error_source', None) or ERROR_SOURCE_GATEWAY)
    return _gateway_code(terminal, stage, status)


#: U21：探针端点（每分钟都在打，不能算进真实流量的失败率）。
PROBE_ENDPOINTS = ('/healthz', '/api/healthz', '/readyz', '/api/readyz', '/metrics')

#: U21：与 `traffic_class()` **同一口径**的 SQL 表达式（列名必须是 `endpoint`）。
#: 两份实现必然漂移，所以统计与检索都用这一条；Python 侧只在单行展示时用
#: `traffic_class()`，两者由测试锁定一致。
def traffic_class_sql(column: str = 'endpoint') -> str:
    """返回分类表达式；`column` 要带表别名（联表查询里 `endpoint` 会歧义）。"""
    return (
        f"CASE WHEN {column} IN ('/healthz','/api/healthz','/readyz','/api/readyz','/metrics') "
        f"OR {column} LIKE '/metrics%' THEN 'probe' "
        f"WHEN {column} LIKE '/api/%' THEN 'admin' "
        f"WHEN {column} LIKE '/v1/%' OR {column} LIKE '/v2/%' THEN 'inference' "
        f"ELSE 'other' END")


def traffic_class(endpoint, method=None) -> str:
    """把一条轨迹归到 `inference` / `admin` / `probe` / `other`（U21）。

    为什么需要它：`/healthz`、`/api/readyz` 这类探针每分钟都在打，`/api/*` 是
    管理动作，只有 `/v1|/v2` 才是真实推理流量。三者混在一起算"失败率"时，一次
    面板刷新或一轮探针就能把失败率抬起来，而真实流量其实全好；反过来，
    「真实/探针/测试混在一起」也正是台账 U21 要关的那条。
    """
    path = str(endpoint or '')
    if path in PROBE_ENDPOINTS or path.startswith('/metrics'):
        return 'probe'
    if path.startswith('/api/'):
        return 'admin'
    if path.startswith('/v1/') or path.startswith('/v2/'):
        return 'inference'
    return 'other'


def ensure_outcome_schema() -> bool:
    """幂等补列 `request_outcomes.error_code` / `error_source`（U21）。

    迁移安全性（为什么可以在运行期做、为什么不需要版本步骤）：

    * SQLite 的 `ALTER TABLE ... ADD COLUMN` **只改 schema 元数据、不重写表**，
      在 20 万行的 `request_outcomes` 上是 O(1)（毫秒级），不阻塞读写；
    * 两列都可空 → 历史行保持 NULL，语义是「未分类/历史」，**不猜、不回填**
      （回填会给出看似精确的错因，实际是从终态反推的猜测）；
    * 幂等：先 `PRAGMA table_info` 探测；并发启动（蓝绿部署两个进程同时起）
      撞出 `duplicate column name` 时吞掉并重新探测；
    * `schemaver` 的守卫只禁止**删除**表/列，新增列属 expand-safe，
      不需要 `CODE_SCHEMA_VERSION` 递增；`migrations.BASELINE_COLUMNS` 不列它，
      所以旧库/新库都能通过 baseline 校验。
    """
    from . import db
    try:
        conn = db.connect()
    except Exception:  # noqa: BLE001 - 拿不到连接就不写这两列
        log.exception('request_outcomes schema ensure skipped (no connection)')
        return False
    if _OUTCOME_SCHEMA['conn'] is conn:
        return bool(_OUTCOME_SCHEMA['ready'])
    try:
        columns = {str(row[1]) for row in conn.execute('PRAGMA table_info(request_outcomes)')}
    except Exception:  # noqa: BLE001
        log.exception('request_outcomes schema probe failed')
        return False
    if not columns:
        return False                      # 表还没建出来：本轮不写这两列
    for name, declaration in _OUTCOME_EXTRA_COLUMNS:
        if name in columns:
            continue
        try:
            with db.transaction() as tx:
                tx.execute(f'ALTER TABLE request_outcomes ADD COLUMN {name} {declaration}')
        except Exception:  # noqa: BLE001 - 并发撞车/只读库：下面重新探测定论
            log.exception('request_outcomes add column failed name=%s', name)
    try:
        columns = {str(row[1]) for row in conn.execute('PRAGMA table_info(request_outcomes)')}
    except Exception:  # noqa: BLE001
        return False
    ready = all(name in columns for name, _ in _OUTCOME_EXTRA_COLUMNS)
    _OUTCOME_SCHEMA.update(conn=conn, ready=ready)
    return ready


def admission_snapshot() -> dict | None:
    """当前全局/大请求准入占用（in-flight）；未启动时返回 None。"""
    inst = _middleware_ref
    if inst is None:
        return None
    return {'active': int(getattr(inst, 'active', 0)),
            'large': int(getattr(inst, 'large', 0)),
            'maximum': int(getattr(inst, 'maximum', 0)),
            'large_maximum': int(getattr(inst, 'large_maximum', 0)),
            'inflight_body_bytes': int(getattr(inst, 'inflight_body_bytes', 0)),
            'maximum_body_bytes': int(getattr(inst, 'maximum_body_bytes', 0)),
            'capacity_rejections': int(getattr(inst, 'capacity_rejections', 0)),
            'key_slots': len(_key_slots)}

class CapacityError(Exception): pass
class BodyLimitError(Exception): pass

def admit_key(key):
    state=current.get()
    if state is None or state.key_slot is not None or state.protocol=='http': return True
    key_id=int(key['id'])
    # C-004：每密钥并发上限改为源码直读 env（默认值与历史硬编码一致）。
    # 有配额密钥默认 1（配额闸只能有 1 个在途），无配额默认 4。
    if key.get('quota') or key.get('quota_credit'):
        limit=_env_int('WB_KEY_CONCURRENCY_QUOTA', 1)
    else:
        limit=_env_int('WB_KEY_CONCURRENCY', 4)
    with _key_lock:
        if _key_slots.get(key_id,0)>=limit: return False
        _key_slots[key_id]=_key_slots.get(key_id,0)+1; state.key_slot=key_id
    return True

def release_key(state):
    if state.key_slot is None: return
    with _key_lock:
        key=state.key_slot; _key_slots[key]=max(0,_key_slots.get(key,0)-1)
        if not _key_slots[key]: _key_slots.pop(key,None)
        state.key_slot=None

def usage_int(value):
    try:
        return min(2**53-1,max(0,int(value or 0)))
    except (ValueError,TypeError,OverflowError):
        return 0

#: U13：流中错误要携带的上游细节（业务 code / hint / 上游请求 ID）。
#:
#: 用 contextvar 而不是给 `sse_error` 加必填参数，是因为 `gateway.py` 的转发
#: 热路径被 `test_gateway_passthrough` 按**行文本**钉死：
#: `yield observability.sse_error(error_text)` 必须逐字出现且只有那几处。
#: 在调用点追加实参会改变该行文本，等于绕过那条断言；把细节放进上下文既
#: 保住了断言，又让三条协议的错误帧字段对齐。
_stream_error_detail: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    'wb_stream_error_detail', default=None)


def set_stream_error_detail(*, code: object = None, hint: object = None,
                            upstream_request_id: object = None) -> None:
    _stream_error_detail.set({'code': code, 'hint': hint,
                              'upstream_request_id': upstream_request_id})


def _bounded(value: object, limit: int, *, ascii_only: bool = False) -> str | None:
    """把上游给的值收成「可安全写进响应体」的短字符串；不可用就 None。

    只放行**可打印**字符串（控制字符一律丢弃：它们会进 SSE 帧与错误体，
    放开就等于允许上游往我们的输出里注入结构）。`ascii_only=True` 用于
    不透明 ID（上游请求 ID 只可能来自 HTTP 头，没有非 ASCII 的正当理由）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)[:limit]
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if not text.isprintable():
            return None
        if ascii_only and not text.isascii():
            return None
        return text[:limit]
    return None


def sse_error(message, *, code: object = None, hint: object = None,
              upstream_request_id: object = None):
    """OpenAI 形状的流中错误帧（`error` 事件 + 终止 `[DONE]`）。

    U13 契约：流前 JSON 错误与流中 SSE 错误必须表达同一件事——
    保留上游业务 `code`、`gateway_hint`、以及上下游两个 ID。

    * `code` 缺省仍是 `stream_incomplete`（既有语义，不改）；
      上游给了业务 code（例如 `no_healthy_account`）就换成它，
      这样客户端不必只凭文案判断该重试还是该改配置。
    * `request_id` 是本管理端的执行 ID；`upstream_request_id` 是上游的
      （来自上游响应头），两者都只在能确定时出现，不编造。
    """
    detail = _stream_error_detail.get() or {}
    state = current.get()
    err = {'type': 'api_error',
           'code': _bounded(code, 64) or _bounded(detail.get('code'), 64)
                   or 'stream_incomplete',
           'message': message}
    hint_text = _bounded(hint, 300) or _bounded(detail.get('hint'), 300)
    if hint_text:
        err['gateway_hint'] = hint_text
    payload: dict = {'error': err}
    if state is not None:
        payload['request_id'] = state.request_id
    upstream_id = (_bounded(upstream_request_id, 128, ascii_only=True)
                   or _bounded(detail.get('upstream_request_id'), 128, ascii_only=True))
    if upstream_id:
        payload['upstream_request_id'] = upstream_id
    return ('data: ' + json.dumps(payload, ensure_ascii=False)
            + '\n\ndata: [DONE]\n\n').encode()

class StreamAudit:
    """Incremental UTF-8/SSE parser. Bounds an unfinished event, preserves raw transport."""
    def __init__(self, maximum=1024 * 1024):
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.pending = ""
        self.data = []
        self.event_size = 0
        self.maximum = maximum
        self.usage = {}
        self.done_seen = False
        self.finish_seen = False
        self.saw_useful = False
        self.first_useful = None
        self.error = None
        self.error_stage = None
        self.error_hint = None
        self.error_code = None

    @property
    def completed(self):
        return (self.done_seen or self.finish_seen) and self.error is None

    def _event(self):
        raw = "\n".join(self.data).strip()
        self.data.clear()
        self.event_size = 0
        if not raw:
            return []
        if raw == "[DONE]":
            self.done_seen = True
            return []
        try:
            obj = json.loads(raw)
        except ValueError:
            self.error = self.error or "上游 SSE 包含无法解析的 JSON 事件"
            self.error_stage = "protocol"
            return []
        if not isinstance(obj, dict):
            return []
        if isinstance(obj.get("error"), dict):
            # Error body can contain private text; retain only a bounded provider message.
            self.error = str(obj["error"].get("message") or obj["error"].get("code") or "上游流错误")[:500]
            self.error_stage = "upstream"
            hint=obj['error'].get('gateway_hint')
            self.error_hint=hint.strip()[:300] if isinstance(hint,str) and hint.strip() else None
            code=obj['error'].get('code')
            self.error_code=str(code)[:64] if code else None
        if isinstance(obj.get("usage"), dict):
            self.usage.update(obj["usage"])
        choices = obj.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") is not None:
                    self.finish_seen = True
                delta = choice.get("delta") or choice.get("message") or {}
                if isinstance(delta, dict) and any(delta.get(k) for k in (
                        "content", "reasoning_content", "reasoning", "tool_calls", "function_call")):
                    self.saw_useful = True
        return [obj]

    def _consume(self, text):
        self.pending += text
        out = []
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                out.extend(self._event())
            elif line.startswith("data:"):
                item = line[5:].lstrip(" ")
                self.event_size += len(item)
                self.data.append(item)
            if self.event_size > self.maximum:
                raise ValueError("上游 SSE 事件超过审计缓冲上限")
        if len(self.pending) + self.event_size > self.maximum:
            raise ValueError("上游 SSE 事件超过审计缓冲上限")
        return out

    def feed(self, chunk):
        return self._consume(self.decoder.decode(chunk))

    def finish(self):
        out = self._consume(self.decoder.decode(b"", final=True))
        if self.pending:
            out.extend(self._consume("\n"))
        if self.data:
            out.extend(self._event())
        return out

def set_stream_state(observer, error=None, cancelled=False):
    state = current.get()
    if state is None:
        return
    # U21：上游在 SSE 错误体里给了业务码（`error.code`，如 11102 /
    # no_healthy_account）就沿用 —— 那是唯一能区分「上游说不行」与「我们说不行」
    # 的事实。成功收尾时 classify_error 仍然会把它归成 none，不会给成功请求
    # 挂上错误码。
    if getattr(observer, 'error_code', None):
        mark_error(observer.error_code, ERROR_SOURCE_UPSTREAM)
    if cancelled:
        state.terminal_state, state.error_stage = "client_cancelled", "client"
    elif error and not observer.error:
        state.terminal_state = ('upstream_error' if observer.error_stage == 'upstream' else
                                'protocol_incomplete' if observer.error_stage in (None, 'protocol') else 'transport_error')
        state.error_stage = observer.error_stage or 'protocol'
    elif observer.error:
        state.terminal_state = ("upstream_error" if observer.error_stage == "upstream" else
                                "protocol_incomplete" if observer.error_stage=='protocol' else "transport_error")
        state.error_stage = observer.error_stage or "stream_read"
    elif observer.completed:
        state.terminal_state, state.error_stage = "completed", None
    else:
        state.terminal_state, state.error_stage = "protocol_incomplete", "protocol"

async def close_stream(response, client, *, timeout: float = 5.0):
    # Starlette uses level cancellation; shield cleanup and keep it bounded.
    # U13：窗口长度由 `deadline.Budgets.client_cancel_s` 传入（三条协议同一口径）；
    # 默认 5s 保持既有行为完全不变。
    try:
        with anyio.move_on_after(float(timeout), shield=True):
            try:
                await response.aclose()
            finally:
                await client.aclose()
    except Exception:
        state=current.get()
        log.exception('stream close failed request_id=%s',state.request_id if state else None)

class ManagedStreamingResponse(StreamingResponse):
    """Close the iterator even if ASGI send fails while the generator is paused at yield."""
    def __init__(self,*args,cleanup=None,**kwargs):
        self.cleanup=cleanup
        super().__init__(*args,**kwargs)

    async def __call__(self,scope,receive,send):
        try:
            await super().__call__(scope,receive,send)
        except asyncio.CancelledError:
            state=current.get()
            if state and state.terminal_state is None:
                state.terminal_state,state.error_stage='client_cancelled','client'
            raise
        finally:
            try:
                with anyio.move_on_after(6,shield=True):
                    close=getattr(self.body_iterator,'aclose',None)
                    if close: await close()
            except Exception:
                log.exception('stream iterator cleanup failed')
            finally:
                if self.cleanup: await self.cleanup()

def _env_int(name, default):
    """读环境变量里的正整数；缺失/非法一律回落默认值（不抛异常）。

    C-004 修复（2026-10-27 审计 S1）：这些阈值此前**只**在
    /opt/workbuddy-manager/local-overrides/sitecustomize.py 里靠猴子补丁抬高，
    源码里是硬编码 8/2/4/1。后果是「用源码评估容量会错 7.5 倍」，
    且补丁锚点一旦漂移就静默回落到 8/2/4/1。现在源码自己读，默认值与
    历史硬编码一致，行为向后兼容。
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


class RequestAuditMiddleware:
    """Pure ASGI wrapper; mutable context survives streaming tasks and is reset per request."""
    def __init__(self, app):
        self.app = app
        # One worker: bounded admission before body allocation, no unbounded queue.
        # 默认 8/2 与历史硬编码一致；生产由 systemd drop-in 抬高（见 zz-gateway-concurrency.conf）。
        self.active=0; self.large=0
        self.inflight_body_bytes = 0
        # U21：启动时幂等补 request_outcomes.error_code / error_source。
        # 放在这里是因为中间件在 app 启动时构造一次，而 main.py 不在本轮可改范围。
        try:
            ensure_outcome_schema()
        except Exception:  # noqa: BLE001 - 补列失败只降级，不影响接请求
            log.exception('outcome schema ensure failed at startup')
        self.capacity_rejections = 0
        self.maximum = _env_int('WB_GATEWAY_MAX_CONCURRENCY', 8)
        self.large_maximum = _env_int('WB_GATEWAY_LARGE_MAX_CONCURRENCY', 2)
        # Raw request-body reservations remain held through response completion.
        # Allow headroom for JSON/Unicode/adapter copies when sizing this budget.
        self.maximum_body_bytes = _env_int('WB_GATEWAY_MAX_INFLIGHT_BODY_BYTES', 64 * 1024**2)
        self.minimum_body_bytes = min(self.maximum_body_bytes,
            _env_int('WB_GATEWAY_MIN_BODY_RESERVATION_BYTES', 64 * 1024))
        global _middleware_ref
        if _middleware_ref is None:
            _middleware_ref = self
        print('[admission] 全局并发上限 maximum=%d large_maximum=%d（源码直读 env）'
              % (self.maximum, self.large_maximum), file=sys.stderr, flush=True)

    async def __call__(self, scope, receive, send):
        global _outcome_writes
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        from . import config
        if config.BASE_PATH and (path == config.BASE_PATH or path.startswith(config.BASE_PATH + '/')):
            path = path[len(config.BASE_PATH):] or '/'
        protocol = ("anthropic" if "/messages" in path else
                    "responses" if path.endswith("/responses") else
                    "openai" if "/chat/completions" in path or path.endswith('/playground/chat') else "http")
        headers = dict(scope.get('headers', []))
        # RV01：执行 ID 一律服务端生成；调用方只能提供关联 ID。
        state = RequestAudit(new_request_id(), path[:256], scope.get("method", ""),
                             protocol, time.monotonic(),
                             client_request_id=client_request_id_from_headers(headers))
        token = current.set(state)
        # 下游路由/审计读 scope["state"]：执行 ID 与关联 ID 分开暴露，名字不能混。
        scope_state = scope.setdefault("state", {})
        scope_state["request_id"] = state.request_id
        scope_state["client_request_id"] = state.client_request_id
        tracked = path.startswith(("/v1/", "/v2/", "/api/", "/qoder/v1/", "/trae/v1/")) or path in ("/responses", "/messages")
        inference=state.protocol!='http' and state.method=='POST'
        admitted=False; large=False; received=0; reserved=0
        reading_body=False
        per_request_limit=None
        if inference:
            from .routers import gateway
            per_request_limit=gateway.max_body_bytes()
        try: declared=max(0,int(headers.get(b'content-length',b'0')))
        except ValueError: declared=0
        initial_reservation = max(declared, self.minimum_body_bytes)
        def capacity_rejected():
            if state.error_stage != 'admission':
                self.capacity_rejections += 1
            state.terminal_state,state.error_stage='rejected','admission'
        async def receive_audited():
            nonlocal received,large,reserved
            message=await receive()
            received+=len(message.get('body',b''))
            if inference:
                if received > per_request_limit:
                    raise BodyLimitError()
                needed = max(reserved, received)
                extra = needed - reserved
                needs_large = received > 1024*1024 and not large
                # No await between checking and reserving: atomic within this
                # ASGI worker, including chunked or understated-length requests.
                if (self.inflight_body_bytes + extra > self.maximum_body_bytes
                        or needs_large and self.large >= self.large_maximum):
                    capacity_rejected()
                    raise CapacityError('aggregate request-body capacity exceeded')
                self.inflight_body_bytes += extra
                reserved = needed
                if needs_large:
                    self.large+=1; large=True
            return message
        async def send_audited(message):
            if message["type"] == "http.response.start":
                state.http_status = message["status"]
                message = dict(message)
                # 先剥掉下游路由可能自带的同名头，再由本中间件唯一负责写入。
                # 未校验的值绝不能进响应头（RV01 契约第 5 条）。
                out_headers = [(k, v) for k, v in message.get("headers", [])
                               if k.lower() not in (b"x-request-id",
                                                    CLIENT_REQUEST_ID_RESPONSE_HEADER,
                                                    b"x-workbuddy-release",
                                                    *MAX_TOKENS_HEADERS)]
                out_headers.extend(_response_headers(state))
                message["headers"] = out_headers
            await send(message)
        async def reject_capacity():
            capacity_rejected()
            await reject_response(JSONResponse({'error':{'message':'网关处理容量已满，请稍后重试','type':'server_error','code':'gateway_capacity_exceeded'}},
                                               status_code=503, headers={'Retry-After':'1'}))
        async def reject_response(response):
            if protocol == 'anthropic':
                payload=json.loads(response.body)
                error=payload.get('error') or {}
                if error.get('type')=='server_error':
                    error['type']='api_error'
                response=JSONResponse({'type':'error','error':error,'request_id':state.request_id},
                    status_code=response.status_code,
                    headers={key:value for key,value in response.headers.items()
                             if key not in ('content-length','content-type')})
            await response(scope,receive,send_audited)
        try:
            if inference and declared > per_request_limit:
                state.terminal_state,state.error_stage='rejected','validation'
                await reject_response(gateway._payload_too_large(per_request_limit))
            elif inference and (self.active>=self.maximum or declared>1024*1024 and self.large>=self.large_maximum
                              or self.inflight_body_bytes + initial_reservation > self.maximum_body_bytes):
                await reject_capacity()
            else:
                if inference:
                    self.active+=1; admitted=True
                    reserved = initial_reservation
                    self.inflight_body_bytes += reserved
                    if declared>1024*1024: self.large+=1; large=True
                    # Validate and reserve the JSON body before entering FastAPI.
                    # Otherwise its body parser can turn a receive-side capacity
                    # exception into HTTP 500 before this wrapper can reject it.
                    reading_body=True
                    chunks=[]
                    body_deadline=time.monotonic()+30
                    while True:
                        message=await asyncio.wait_for(receive_audited(),max(0.001,body_deadline-time.monotonic()))
                        if message['type']=='http.disconnect':
                            state.http_status=499
                            state.terminal_state,state.error_stage='client_cancelled','request_body'
                            return
                        chunks.append(message)
                        if not message.get('more_body',False):
                            break
                    reading_body=False
                    cursor=0
                    async def replay_body():
                        nonlocal cursor
                        if cursor < len(chunks):
                            message=chunks[cursor]
                            cursor+=1
                            return message
                        return await receive()
                    await self.app(scope,replay_body,send_audited)
                else:
                    await self.app(scope,receive_audited,send_audited)
        except BodyLimitError:
            state.terminal_state,state.error_stage='rejected','validation'
            await reject_response(gateway._payload_too_large(per_request_limit))
        except TimeoutError:
            if not reading_body:
                raise
            state.terminal_state,state.error_stage='rejected','request_body'
            await reject_response(gateway._oai_error('请求体读取超过 30 秒',408,'invalid_request_error','body_read_timeout'))
        except CapacityError:
            if state.http_status:
                raise
            await reject_capacity()
        except BaseException:
            if state.terminal_state is None:
                state.terminal_state, state.error_stage = "interrupted", "application"
            raise
        finally:
            try:
                if tracked:
                    from . import db
                    status = state.http_status or 500
                    terminal = state.terminal_state or ("rejected" if 400 <= status < 500 else
                               "application_error" if status >= 500 else "completed")
                    # U21：稳定错误码。上游业务码（stream 路径在 set_stream_state、
                    # 非流式在 gateway 里标好）优先，其次按终态/阶段/状态码推导。
                    error_code, error_source = classify_error(
                        state, terminal_state=terminal, http_status=status)
                    if ensure_outcome_schema():
                        db.execute(
                            "INSERT OR IGNORE INTO request_outcomes(request_id, client_request_id, ts, endpoint, "
                            "method, protocol, http_status, terminal_state, error_stage, latency_ms, release_id, "
                            "error_code, error_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (state.request_id, state.client_request_id, int(time.time()), state.endpoint,
                             state.method, state.protocol, status, terminal, state.error_stage,
                             int((time.monotonic()-state.started)*1000), RELEASE_ID,
                             error_code, error_source))
                    else:
                        # 补列失败（只读库/并发竞态）：仍然落轨迹，只是没有稳定码。
                        db.execute(
                            "INSERT OR IGNORE INTO request_outcomes(request_id, client_request_id, ts, endpoint, "
                            "method, protocol, http_status, terminal_state, error_stage, latency_ms, release_id) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (state.request_id, state.client_request_id, int(time.time()), state.endpoint,
                             state.method, state.protocol, status, terminal, state.error_stage,
                             int((time.monotonic()-state.started)*1000), RELEASE_ID))
                    if path.startswith('/api/'):
                        # B-003 修复（2026-10-27 审计 S1）：原条件是
                        #   method not in ('GET','HEAD','OPTIONS') or status >= 400
                        # 于是任何匿名 GET 只要不是 2xx 就落一条 admin_events，
                        # 而这张表有禁删触发器 —— 扫描器与未登录探针把审计链变成了
                        # 噪声日志（24h 内 645/892 条是匿名 4xx，历史累计 19,558 行
                        # 匿名 /api/me 401），且永久增长。
                        # 只保留四类有追溯价值的：
                        #   1) 有身份的 /api 操作
                        #   2) 匿名 + 写方法 + **已知端点** 上被 401/403（真实越权尝试）
                        #   3) 任何 5xx（服务端错误与身份无关）
                        #   4) 有身份但路由未匹配（打错的路径也要能追）
                        # 匿名 GET 4xx 与匿名未匹配探针不再入链；它们的痕迹仍完整
                        # 保留在 request_outcomes / ip_access_logs / nginx access.log。
                        route = scope.get('route')
                        matched = route is not None
                        actor_obj = scope.get('state', {}).get('audit_actor') or {}
                        actor_name = str(actor_obj.get('username') or 'anonymous') if isinstance(actor_obj, dict) else 'anonymous'
                        has_identity = bool(actor_obj) and actor_name != 'anonymous'
                        is_write = state.method not in ('GET', 'HEAD', 'OPTIONS')
                        if (has_identity
                                or (is_write and status in (401, 403) and matched)
                                or status >= 500):
                            from . import ledger
                            action = state.method + ' ' + (getattr(route, 'path', '/api/unmatched') if matched else '/api/unmatched')
                            ledger.admin(actor_obj, action,
                                         outcome='denied' if status in (401,403) else 'failed' if status >= 400 else 'completed',
                                         status=status, request_id=state.request_id)
                    _outcome_writes+=1
                    if _outcome_writes>=2000:
                        _outcome_writes=0
                        db.execute('DELETE FROM request_outcomes WHERE request_id IN '
                                   '(SELECT request_id FROM request_outcomes WHERE ts<? ORDER BY ts LIMIT 2000)',
                                   (int(time.time())-90*86400,))
            except Exception:
                log.exception("request outcome persistence failed request_id=%s", state.request_id)
            finally:
                from . import budget
                try:
                    budget.finish(state)
                except Exception:
                    log.exception("budget finalization failed request_id=%s",state.request_id)
                release_key(state)
                if admitted:
                    self.active-=1
                    self.inflight_body_bytes -= reserved
                if large: self.large-=1
                current.reset(token)

