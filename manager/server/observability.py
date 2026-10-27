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
from starlette.responses import StreamingResponse
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


def _response_headers(state: "RequestAudit") -> list[tuple[bytes, bytes]]:
    """本中间件负责的响应头（先剥后加，见 `RequestAuditMiddleware.send_audited`）。"""
    out = [(b'x-request-id', state.request_id.encode()),
           (b'x-workbuddy-release', RELEASE_ID.encode())]
    # 只在调用方真的给了合法值时才回显；None 表示「没给或不合法」，
    # 不能拿执行 ID 冒充，也不能回显未校验的原值。
    if state.client_request_id:
        out.append((CLIENT_REQUEST_ID_RESPONSE_HEADER, state.client_request_id.encode()))
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

current: contextvars.ContextVar[RequestAudit | None] = contextvars.ContextVar("wb_request_audit", default=None)
_key_slots={}
_key_lock=threading.RLock()
_outcome_writes=0

class CapacityError(Exception): pass

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

def sse_error(message):
    return ('data: '+json.dumps({'error':{'type':'api_error','code':'stream_incomplete','message':message}},ensure_ascii=False)+'\n\ndata: [DONE]\n\n').encode()

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

async def close_stream(response, client):
    # Starlette uses level cancellation; shield cleanup and keep it bounded.
    try:
        with anyio.move_on_after(5, shield=True):
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
        self.maximum = _env_int('WB_GATEWAY_MAX_CONCURRENCY', 8)
        self.large_maximum = _env_int('WB_GATEWAY_LARGE_MAX_CONCURRENCY', 2)
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
                    "openai" if "/chat/completions" in path else "http")
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
        tracked = path.startswith(("/v1/", "/v2/", "/api/")) or path in ("/responses", "/messages")
        inference=state.protocol!='http' and state.method=='POST'
        admitted=False; large=False; received=0
        try: declared=int(headers.get(b'content-length',b'0'))
        except ValueError: declared=0
        async def receive_audited():
            nonlocal received,large
            message=await receive(); received+=len(message.get('body',b''))
            if inference and received>1024*1024 and not large:
                if self.large>=self.large_maximum: raise CapacityError('large context capacity exceeded')
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
                                                    b"x-workbuddy-release")]
                out_headers.extend(_response_headers(state))
                message["headers"] = out_headers
            await send(message)
        try:
            if inference and (self.active>=self.maximum or declared>1024*1024 and self.large>=self.large_maximum):
                state.terminal_state,state.error_stage='rejected','admission'
                body=json.dumps({'error':{'message':'网关处理容量已满，请稍后重试','type':'server_error','code':'gateway_capacity_exceeded'}},ensure_ascii=False).encode()
                await send_audited({'type':'http.response.start','status':503,'headers':[(b'content-type',b'application/json'),(b'retry-after',b'1')]})
                await send_audited({'type':'http.response.body','body':body})
            else:
                if inference:
                    self.active+=1; admitted=True
                    if declared>1024*1024: self.large+=1; large=True
                await self.app(scope, receive_audited, send_audited)
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
                    db.execute(
                        "INSERT OR IGNORE INTO request_outcomes(request_id, client_request_id, ts, endpoint, "
                        "method, protocol, http_status, terminal_state, error_stage, latency_ms, release_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (state.request_id, state.client_request_id, int(time.time()), state.endpoint,
                         state.method, state.protocol, status, terminal, state.error_stage,
                         int((time.monotonic()-state.started)*1000), RELEASE_ID))
                    if path.startswith('/api/') and (state.method not in ('GET','HEAD','OPTIONS') or status >= 400):
                        from . import ledger
                        route = scope.get('route')
                        action = state.method + ' ' + (getattr(route, 'path', '/api/unmatched'))
                        ledger.admin(scope.get('state', {}).get('audit_actor'), action,
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
                if admitted: self.active-=1
                if large: self.large-=1
                current.reset(token)

