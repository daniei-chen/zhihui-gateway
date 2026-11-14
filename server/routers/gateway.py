"""对外反代网关：密钥鉴权 → IP 管控 → 模型映射 → 转发 → 计量落库。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from urllib.parse import urlsplit
import logging
import math
from pathlib import Path
import re
import threading
import time
import uuid
from contextvars import ContextVar

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response

from .. import config, db, iputil, keysvc, upstreamsvc, platformpolicy
from .. import observability, requestperf, metering
from ..outputlimits import OutputLimitExceeded
from ..requestbody import (BodyReadTimeout, BodyReadWindow, BodyEncodingError,
                          BodySizeExceeded, body_frames, content_encoding)
from ..services import modelcatalog, modelmap, proxyroutes
from ..config import _env_int
from ..routers.security import get_config as get_security_config

logger = logging.getLogger('workbuddy.gateway')

router = APIRouter(tags=['gateway'])

# 三平台：中间件把 /qoder/v1/* /trae/v1/* 重写成 /v1/* 时，在这里留下平台名。
# 空 = 按密钥绑定 / 默认上游；非空 = 锁定该平台上游（一次请求一个值）。
forced_platform = platformpolicy.current_platform

# 三平台数据面上游：**刻意不写进 upstreams 表**。
# 那张表同时是「账号池分组」的数据源，落进去会让账号页凭空多出
# qoder / trae 两个分组（2026-10-23 踩过，被当成串台）。
_THREEWAY_SPEC = {
    'qoder': ('http://127.0.0.1:8799', '/opt/workbuddy-manager/engine/secrets/qoder_upstream_key'),
    'trae': ('http://127.0.0.1:8080', '/opt/workbuddy-manager/engine/secrets/aiwork_api_key'),
}
_THREEWAY_ROUTE_FILES = {
    'qoder': '/opt/workbuddy-manager/engine/var/qoder_gateway_base',
    'trae': '/opt/workbuddy-manager/engine/var/trae_gateway_base',
}
_DYNAMIC_UPSTREAM_LOCK = threading.Lock()
_DYNAMIC_UPSTREAM_CACHE: dict[str, tuple[tuple[object, ...], str | None]] = {}


def _safe_loopback_upstream(value: str) -> str | None:
    """Accept only an HTTP loopback endpoint with no path or credentials.

    The route file is an operational handoff point used during a drained
    upstream cutover.  Keeping its grammar narrower than a general URL makes
    a bad or tampered file fail back to the signed default instead of turning
    the gateway into an arbitrary proxy.
    """
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (parsed.scheme != 'http' or hostname not in ('127.0.0.1', '::1')
            or port is None or not 1 <= port <= 65535
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        return None
    host = '[::1]' if hostname == '::1' else hostname
    return f'http://{host}:{port}'


def _threeway_base(name: str, default: str) -> str:
    """Read an optional atomic local route handoff without per-request parsing."""
    if name not in _THREEWAY_ROUTE_FILES:
        return default
    env_name = 'WB_%s_UPSTREAM_FILE' % name.upper()
    route_path = os.environ.get(env_name, _THREEWAY_ROUTE_FILES[name])
    route = Path(route_path)
    if route.is_symlink():
        return default
    try:
        info = route.stat()
        marker = (route_path, info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
    except OSError:
        return default
    # The endpoint is not secret, but an untrusted writer must not be able to
    # race the operator's handoff with a local service choice.  The published
    # file is root-owned in production and intentionally read-only to group/
    # others; the mode check keeps the same contract in portable deployments.
    if os.name == 'posix' and info.st_mode & 0o022:
        return default
    with _DYNAMIC_UPSTREAM_LOCK:
        cached = _DYNAMIC_UPSTREAM_CACHE.get(name)
        if cached is not None and cached[0] == marker:
            return cached[1] or default
        try:
            raw = route.read_text(encoding='ascii')
        except (OSError, UnicodeDecodeError):
            base = None
        else:
            base = _safe_loopback_upstream(raw)
        _DYNAMIC_UPSTREAM_CACHE[name] = (marker, base)
        if base is None:
            logger.warning('invalid %s upstream handoff; retaining trusted default', name)
        return base or default


def _threeway_upstream(name: str) -> dict | None:
    """按平台名构造一个上游描述；密钥从 qtg 的 secrets 文件读，不进数据库。"""
    spec = _THREEWAY_SPEC.get(name)
    if not spec:
        return None
    base, keyfile = _threeway_base(name, spec[0]), spec[1]
    try:
        with open(keyfile, encoding='utf-8') as fh:
            api_key = fh.read().strip()
    except OSError:
        api_key = ''
    if not api_key:
        return None
    return {
        'id': None, 'name': name, 'base_url': base, 'api_key': api_key,
        'note': '', 'enabled': 1, 'is_default': False,
        'auth_dir': '', 'container': '',
    }


def _oai_error(message: str, status: int = 400, err_type: str = 'invalid_request_error',
               code: str | None = None, hint: str | None = None, *,
               request_id: str | None = None,
               upstream_request_id: str | None = None) -> JSONResponse:
    """OpenAI 形状的错误体。

    `hint` 对应上游 2026-09-16 新增的 `error.gateway_hint`：与 message **并列**的
    网关视角补充说明（措辞为英文，面向客户端工具链）。上游只给**可执行的**建议
    （如「no healthy account available in pool; check /status or retry later」），
    未覆盖的错误形态不带该字段。

    为空时**不写这个字段**：与上游「不编造 hint」的口径一致，也避免客户端拿到
    一个空值还要自己判断。

    U13（2026-10-28）：错误契约要求保留**上下游两个 ID**。它们放在 `error`
    之外（与 Anthropic 侧 `_err` 的形状一致），未提供时不写字段、不编造。
    `error` 对象本身的形状不变 —— 既有客户端与断言依赖它。
    """
    err: dict = {'message': message, 'type': err_type, 'code': code}
    if hint:
        err['gateway_hint'] = hint
    body: dict = {'error': err}
    if request_id:
        body['request_id'] = request_id
    if upstream_request_id:
        body['upstream_request_id'] = upstream_request_id
    return JSONResponse(body, status_code=status)


def _oai_error_with_blip(message: str, status: int = 502, exc=None,
                         err_type: str = 'api_error',
                         code: str | None = 'upstream_unavailable') -> JSONResponse:
    """抖动感知的 502 报错：附加 Retry-After（秒）+ X-Upstream-Blip: 1。

    网关在调腾讯 copilot 上游遇网络层错误（EOF / 关闭的连接 / TLS 握手超时 /
    连接重置）时返回。客户端拿到头后按 HTTP 标准退避，避免「007ms 固定间隔撞
    恢复窗口」式的连续失败。

    退避策略：单次返回头，不在网关内累计（保留每请求独立判断语义）。
    """
    resp = _oai_error(message, status, err_type, code)
    resp.headers['Retry-After'] = '5'
    resp.headers['X-Upstream-Blip'] = '1'
    return resp


def _oai_error_with_blip_body(message: str, status: int = 502, exc=None,
                              err_type: str = 'api_error',
                              code: str | None = 'upstream_unavailable') -> JSONResponse:
    """502 上游不可用：在响应头 + 错误体中同时提示客户端等 5 秒再试。

    响应头路径（Retry-After + X-Upstream-Blip）在 nginx 反代链路上透传不稳，
    实测 deepseek-harness 等客户端读不到；错误体字段（retry_after_seconds
    + message 末尾提示）任何 OpenAI 兼容客户端都能从 message/原样 JSON 拿到。
    """
    suffix = ' 请等 5 秒后重试'
    body = {'error': {
        'message': message + suffix,
        'type': err_type,
        'code': code,
        'retry_after_seconds': 5,
    }}
    resp = JSONResponse(body, status_code=status)
    resp.headers['Retry-After'] = '5'
    resp.headers['X-Upstream-Blip'] = '1'
    return resp


# ── 上游「明确拒绝且未处理」的有界重试（2026-10-28）────────────────────────
#
# 现场（2026-10-28 取证）：上游把**内容拒绝**伪装成 `11102 模型不存在`，而且是
# 1–2 分钟的短窗口 —— 单日 421 次、涉及 31/32 个账号，同一分钟同一模型既有 200
# 也有被拒，同一账号同时出现在成功侧与被拒侧。所以它既不是账号缺模型，
# 也不是映射没生效（失败请求的 `mapped_model` 都是 `cn:deepseek-v4.1-flash`）。
#
# 这类响应意味着请求**没有被上游处理**：没有生成、没有 usage、没有计费 ——
# 与 `budget._REJECTED_MARKERS` 是同一判据。因此**重放是安全的**，
# 不会重复生成、也不会重复计费。
#
# 客户端（deepseek-harness 等）默认重试 8 次且间隔更长；在网关内补一次短重试
# 就能把窗口挡在客户端之外，同时省掉客户端那 8 次重试的等待。
#
# 可调：设置项 `upstream_rejection_retry`（settings 表，不用发版）
#   0 或 1 → 关闭重试；N → 最多 N 次尝试（含首次）。默认 2（1 次重试）。
_REJECTION_RETRY_CODES = (400, 502, 503)
_REJECTION_RETRY_DEFAULT = 2
_REJECTION_RETRY_MAX = 4          # 再高就是把上游抖动放大成重试风暴
_REJECTION_RETRY_DELAY_S = 0.8
_REJECTION_LIMIT_TTL_S = 60.0     # 设置项缓存：转发热路径上不能每条请求查一次库（U27）
_rejection_limit_cache: tuple = (0.0, -1)


def _rejection_retry_limit() -> int:
    """本次请求允许的上游尝试次数（含首次）。读不到设置就用默认值，绝不影响转发。"""
    global _rejection_limit_cache
    now = time.monotonic()
    cached_at, cached = _rejection_limit_cache
    if cached >= 0 and (now - cached_at) < _REJECTION_LIMIT_TTL_S:
        return cached
    limit = _REJECTION_RETRY_DEFAULT
    try:
        value = db.get_setting('upstream_rejection_retry')
    except Exception:  # noqa: BLE001  设置读不到不是转发失败的理由
        value = None
    if value is not None and not isinstance(value, bool):
        try:
            limit = max(0, min(int(value), _REJECTION_RETRY_MAX))
        except (TypeError, ValueError):
            limit = _REJECTION_RETRY_DEFAULT
    _rejection_limit_cache = (now, limit)
    return limit


def _is_not_processed_rejection(status: int, body_text: str) -> bool:
    """上游**明确拒绝且没有处理**这个请求 —— 可以安全重放。"""
    if status not in _REJECTION_RETRY_CODES:
        return False
    text = body_text or ''
    if not text:
        return False
    try:
        data = json.loads(text)
    except RecursionError:
        return False
    except (ValueError, TypeError):
        # Legacy plain-text model lookup failures have one bounded grammar.
        import re
        return bool(re.fullmatch(r'model \[[^\]\r\n]{1,128}\] service info not found', text.strip()))
    if not isinstance(data, dict):
        return False

    def has_processing_evidence(value, depth=0):
        if depth > 32:
            return True
        if isinstance(value, dict):
            if any(name in value for name in (
                    'usage', 'choices', 'output', 'completion', 'partial_output',
                    'credit', 'cost', 'prompt_tokens', 'completion_tokens', 'total_tokens',
                    'attempts', 'retry_count')):
                return True
            if value.get('processed') or value.get('generated'):
                return True
            return any(has_processing_evidence(v, depth + 1) for v in value.values())
        if isinstance(value, list):
            return any(has_processing_evidence(v, depth + 1) for v in value)
        if isinstance(value, str) and value.lstrip().startswith(('{', '[')):
            try:
                return has_processing_evidence(json.loads(value), depth + 1)
            except (ValueError, RecursionError):
                return True
        return False

    if has_processing_evidence(data):
        return False
    error = data.get('error') if isinstance(data.get('error'), dict) else data
    code = error.get('code')
    return str(code) in ('11102', 'no_healthy_account', 'model_not_found')


def _response_usage(data):
    return data['usage'] if isinstance(data, dict) and isinstance(data.get('usage'), dict) else {}


def _valid_chat_response(data):
    return (isinstance(data, dict) and not data.get('error')
            and isinstance(data.get('choices'), list) and bool(data['choices'])
            and all(isinstance(c, dict) and isinstance(c.get('message'), dict)
                    and bool(c['message'])
                    for c in data['choices']))


def _mark_invalid_response():
    state = observability.current.get()
    if state:
        state.terminal_state, state.error_stage = 'protocol_incomplete', 'protocol'
    observability.mark_error('upstream_invalid_body', observability.ERROR_SOURCE_UPSTREAM)


def _mark_output_limit_failure():
    state = observability.current.get()
    if state:
        state.terminal_state, state.error_stage = 'protocol_incomplete', 'protocol'
    observability.mark_error('upstream_output_limit')


def _mark_pre_stream_failure(exc):
    from .. import deadline
    state = observability.current.get()
    if state is None:
        return
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        state.terminal_state, state.error_stage = 'client_cancelled', 'client'
    elif isinstance(exc, deadline.BudgetExceeded):
        failure = deadline.classify_budget_exceeded(exc)
        state.terminal_state, state.error_stage = failure.terminal_state, failure.error_stage
        observability.mark_error('upstream_timeout')
    elif isinstance(exc, deadline.ErrorBodyTooLarge):
        _mark_invalid_response()
    else:
        state.terminal_state, state.error_stage = 'transport_error', 'stream_read'


async def _close_pending_response(response, client, budgets):
    from .. import deadline
    await observability.close_stream(response, client, timeout=deadline.close_timeout(budgets))


def _response_text(resp) -> str:
    """安全取响应体文本：非流式响应才有 `.text`，取不到就当空串（不猜）。"""
    try:
        return resp.text or ''
    except Exception:  # noqa: BLE001
        return ''


def _bearer(request: Request) -> str:
    auth = request.headers.get('authorization', '')
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return request.headers.get('x-api-key', '').strip()


# Manager owns ingress limits. A legacy upstream setting may only tighten them.
# Serialized, transformed bodies have a separate finite egress limit.
_BODY_LIMIT_TTL = 10
_body_limit_cache: dict[str, float | int] = {'at': 0.0, 'bytes': 0, 'source': ''}
DEFAULT_MAX_BODY_MB = max(1, _env_int('WB_GATEWAY_MAX_BODY_MB', 64))
# 本端硬顶：上游配置再大也不越过它（保护本进程内存，见上面的说明）
BODY_CEILING_MB = max(1, _env_int('WB_GATEWAY_MAX_BODY_CEILING_MB', 1024))
DEFAULT_MAX_FORWARD_BODY_MB = max(1, _env_int('WB_GATEWAY_MAX_FORWARD_BODY_MB', 64))
# 上限是**谁**定的：'upstream' / 'env' / ''（默认），'+ceiling' 表示被硬顶截断。
# 保留这个字段是为了 413 文案能指向真正该改的地方。
_BODY_LIMIT_SOURCE: dict[str, str] = {'value': ''}


def _env_set(name: str) -> bool:
    value = (os.environ.get(name) or '').strip()
    return value != ''


def max_body_bytes() -> int:
    """当前生效的请求体上限（字节）。

    本端 WB_GATEWAY_MAX_BODY_MB 与硬顶取较小值；旧 server.max_body_mb
    只允许进一步收紧，不能放大本端上限。
    10 秒缓存：改配置很快生效，又不必每请求读文件。
    """
    now = time.time()
    cached = int(_body_limit_cache['bytes'])
    if cached and 0 <= now - float(_body_limit_cache['at']) < _BODY_LIMIT_TTL:
        return cached
    limit_mb = DEFAULT_MAX_BODY_MB
    source = 'env' if _env_set('WB_GATEWAY_MAX_BODY_MB') else ''
    try:
        cfg = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8'))
        mb = (cfg.get('server') or {}).get('max_body_mb')
        if type(mb) is int and 0 < mb < limit_mb:
            limit_mb = mb
            source = 'upstream'
    except Exception:  # noqa: BLE001
        # 配置读不到就沿用本端默认值：网关不能因为读不到配置而拒绝服务
        pass
    if limit_mb > BODY_CEILING_MB:
        limit_mb = BODY_CEILING_MB
        source = f'{source}+ceiling' if source else 'ceiling'
    limit = limit_mb * 1024 * 1024
    _body_limit_cache['at'] = now
    _body_limit_cache['bytes'] = limit
    _body_limit_cache['source'] = source
    _BODY_LIMIT_SOURCE['value'] = source
    return limit


def max_forward_body_bytes() -> int:
    return min(DEFAULT_MAX_FORWARD_BODY_MB, BODY_CEILING_MB) * 1024 * 1024

# ── max_tokens 上限钳制 ────────────────────────────────────────────
#
# 上游的 `Request.max_tokens` 是 **int32**（上限 2147483647）。超出后上游拒绝时
# 不是回 400，而是把它包成 `no_healthy_account` + 503——客户端据此以为「服务暂时
# 不可用」而重试，但参数越界重试多少次都一样失败。
#
# 实测触发来源：有客户端按「剩余额度 → max_tokens」做减法（形如 `1e10 - 已消耗`，
# 约 99.99 亿，是 int32 上限的 4.66 倍）。这类值是**算出来的**、不是用户填的，
# 客户端那侧的换算逻辑我们改不到，只能由网关兜住。
#
# 阈值取 1e6：上游 /v1/models 里最大的 max_output_tokens 实测为 131072，
# 1e6 有约 7 倍余量，不会误伤正常请求，又能挡住这类减法式产物。
MAX_TOKENS_CEILING = 1_000_000

# 一并钳制的字段：三种协议各自的输出上限字段名。
# `max_completion_tokens` 是 OpenAI 较新的名字（o-series / gpt-5 时代取代
# `max_tokens`），部分客户端已默认用它；上游同样按 int32 读，实测越界时一样
# 回 503。网关是透传的，客户端发什么就转发什么，故必须一并兜住。
_MAX_TOKENS_FIELDS = ('max_tokens', 'max_completion_tokens', 'max_output_tokens')


def clamp_max_tokens(body: dict) -> None:
    """原地钳制请求体里的输出上限字段（见 _MAX_TOKENS_FIELDS）。

    为什么是**原地改**而不是返回新 dict：`_chat` 与两个协议层都是拿到 body 后
    直接转发，原地把越界值压回上限，调用方无需改动其余逻辑。

    非数字一律**原样保留**（不猜测、不置默认）：把它交回上游去判，网关不替
    上游做协议决定——历史上替上游兜底过的字段都留下了难以回溯的偏差。
    """
    if not isinstance(body, dict):
        return
    for field in _MAX_TOKENS_FIELDS:
        value = body.get(field)
        # bool 是 int 的子类，必须排除；None 交给上游按「未提供」处理
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value <= MAX_TOKENS_CEILING:
            continue
        body[field] = MAX_TOKENS_CEILING


def _payload_too_large(limit: int) -> JSONResponse:
    """Describe the actual Manager/legacy restriction, never promise unlimited upstreams."""
    mb = limit // 1024 // 1024
    source = _BODY_LIMIT_SOURCE.get('value') or ''
    if source == 'upstream':
        where = ('旧 config.json 的 server.max_body_mb 进一步收紧了上限；'
                 '它不能放大本端 WB_GATEWAY_MAX_BODY_MB 或硬顶')
    elif source == 'upstream+ceiling':
        where = (f'上游允许更大，但被本端硬顶 WB_GATEWAY_MAX_BODY_CEILING_MB 截到 {mb} MB'
                 '（保护本进程内存：单请求峰值约为体积的 2~3 倍）')
    elif source == 'ceiling':
        where = (f'默认允许更大，但被本端硬顶 WB_GATEWAY_MAX_BODY_CEILING_MB 截到 {mb} MB'
                 '（保护本进程内存）')
    elif source == 'env':
        where = ('该上限来自环境变量 WB_GATEWAY_MAX_BODY_MB（改完要重启面板）')
    else:
        where = ('当前用的是默认上限；可在 .env 里设 WB_GATEWAY_MAX_BODY_MB 提高'
                 '（改完要重启面板）')
    return _oai_error(
        f'请求体超过 {mb} MB 上限：请压缩内容（精简上下文或附件），或提高上限。'
        f'{where}。边缘和所选上游仍各自执行独立上限，调整需同步验收内存与并发预算',
        413, 'invalid_request_error', 'payload_too_large',
    )


async def _read_json_body(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """读取并解析网关请求体，带大小与格式校验。

    大小校验必须落在**实际读取**上，不能只看 Content-Length：
    分块传输（chunked）时该头缺失，伪造该头也可以偏小，
    只看头会让超大请求被整段读进内存（少量并发即可耗尽内存）。
    """
    limit = max_body_bytes()
    try:
        declared = int(request.headers.get('content-length') or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        return None, _payload_too_large(limit)

    chunks: list[bytes] = []
    size = 0
    # 复用中间件那遍读取的时间窗口：body 被读两遍，若各自从零开始计时，一次慢速
    # 上传的实际预算是两份（180 秒 × 2），而它全程占着一个准入名额。
    window = request.scope.get('wb_body_window') or BodyReadWindow()
    try:
        encoding = content_encoding(request.scope.get('headers', []))
        async for frame in body_frames(request.receive, encoding, limit, window=window):
            chunk = frame.get('body', b'')
            size += len(chunk)
            if size > limit:
                return None, _payload_too_large(limit)
            chunks.append(chunk)
    except BodyReadTimeout as exc:
        return None, _oai_error(exc.message, 408, 'invalid_request_error', 'body_read_timeout')
    except BodySizeExceeded:
        return None, _payload_too_large(limit)
    except BodyEncodingError as exc:
        unsupported = str(exc) == 'unsupported_content_encoding'
        return None, _oai_error('请求体编码仅支持 identity 或 gzip' if unsupported else 'gzip 请求体损坏或未完整传输',
                                415 if unsupported else 400, 'invalid_request_error', str(exc))
    except observability.CapacityError:
        return None, _oai_error('大上下文处理容量已满，请稍后重试',503,'server_error','gateway_capacity_exceeded')
    except Exception:  # noqa: BLE001
        return None, _oai_error('读取请求体失败', 400)

    def parse():
        value = json.loads(b''.join(chunks))
        invalid = _invalid_unicode_path(value) if isinstance(value, dict) else None
        return value, invalid
    try:
        from ..services.asyncwork import joined_thread
        from .. import requestperf
        with requestperf.measure('json_parse'):
            body, invalid = await joined_thread(parse)
    except Exception:  # noqa: BLE001
        return None, _oai_error('请求体不是合法 JSON', 400)
    if not isinstance(body, dict):
        return None, _oai_error('请求体必须是 JSON 对象', 400)

    if invalid is not None:
        return None, _oai_error(f'JSON 字符串包含孤立 Unicode surrogate：{invalid}', 400,
                                'invalid_request_error', 'invalid_unicode')
    state = observability.current.get()
    if state and isinstance(body.get('model'),str):
        state.requested_model = db._clean(body['model'],128)
    return body, None


# ── 简单的每密钥速率限制（滑动窗口）─────────────────────
# 目的：单个密钥被打爆时保护上游账号池，避免拖垮其他调用方。
# 计数放在进程内存，单实例足够；多实例部署时可换成 Redis。
_rate: dict[int, list[float]] = {}
RATE_WINDOW = 60
RATE_MAX_PER_MIN = _env_int('WB_GATEWAY_RATE_PER_MIN', 120)


def _rate_limited(key: dict) -> tuple[bool, int]:
    """返回 (是否限流, 窗口内计数)。

    **只有放行的请求才占用窗口额度**（issue #52）。此前不分放行与拒绝一律
    `append()`，被拒的请求同样计入，于是与客户端重试构成正反馈：客户端以高于
    阈值的速率持续重试时，窗口计数永远降不回阈值以下，表现为「凑满一窗之后
    持续全拒、永不恢复」——限流器本该限速，却变成了熔断。

    现在的语义是「滑动窗口内**放行**了多少次」：超出的请求被拒且不占额度，
    因此客户端把速率降到阈值以下能立刻恢复，保持高于阈值则稳定放行到上限
    （多余的被拒，不会雪崩）。放行数上限仍是 `RATE_MAX_PER_MIN`。
    """
    kid = int(key['id'])
    if RATE_MAX_PER_MIN <= 0:
        return False, 0
    now = time.time()
    hits = [t for t in _rate.get(kid, []) if now - t < RATE_WINDOW]
    limited = len(hits) >= RATE_MAX_PER_MIN
    if not limited:
        hits.append(now)
    # 拒绝的分支也要回写：窗口内已过期的记录要顺手清掉，否则长期被限的密钥
    # 会一直拖着一串陈旧时间戳（金额上限看不出问题，但内存是白占的）。
    _rate[kid] = hits
    # 顺带清理过期键，避免长期运行后字典无限增长
    if len(_rate) > 2000:
        for k in [k for k, v in _rate.items() if not v or now - v[-1] > RATE_WINDOW]:
            _rate.pop(k, None)
    return limited, len(hits)


# ── 未鉴权入口的入站日志采样 ─────────────────────────────
# 「没有密钥」「密钥不认识」这两条路径**在校验密钥之前**就写库，任何匿名者都能
# 触发（`/v1/chat/completions` 与 `/v1/usage` 都是这样）。攻击者只要写个循环
# 用随机 token 打过来，就能以每请求一行的速度把 `ip_access_logs` 的两万行窗口
# 顶掉——真正该看的扫描记录反而被自己人（同一个人）挤了出去，外加持续的写盘与
# 行数清理开销。这是**匿名写放大**，与 `_log_ip` 里清洗/截断防的是同一类问题。
#
# 做法：按 (来源 IP, 原因) 做**窗口内计数**，前 N 次照常逐条记录（保留细节：
# 路径、UA 都不一样，排查时有用），超出的折叠掉，并在本窗口第一次折叠时写一条
# `key_auth_flood` 汇总行——运维能看到「这个 IP 在这一分钟里刷了更多」，但库里
# 每分钟最多 N+1 行/IP。折叠不是丢弃信号，而是把「很多条一样的东西」压成一条
# 仍能看出发生了什么的记录。
#
# 只对这两个**未鉴权可达**的原因采样：`ip_blocked` / `rate_limited` / 密钥层
# 拒绝都要求先拿出一把已知密钥，写放大的成本已经由密钥本身承担，采样反而会
# 藏掉「某把密钥被反复拒绝」的线索。
#
# 计数在进程内存里（与上面的每密钥限流同一口径）：单实例足够；`workers > 1`
# 时每个进程各自计数，上限按进程数放大——仍然有界，可接受。
_AUTH_FAIL_REASONS = frozenset({'missing_key', 'invalid_key'})
_AUTH_LOG_WINDOW = 60
_AUTH_LOG_PER_WINDOW = max(1, _env_int('WB_AUTH_LOG_PER_MIN', 5))
# 计数表的硬上限。大量不同来源 IP 在同一个窗口内各打一次时，过期清理
# 永远不会被触发（每个 state 的计数都停在 1）；必须有一个不依赖计数的上限。
_AUTH_LOG_MAX_ENTRIES = 8192
# (ip, reason) -> [窗口起点, 本窗次数, 是否已写过汇总行]
_auth_log_state: dict[tuple[str, str], list] = {}
_auth_log_lock = threading.Lock()


def _auth_log_verdict(ip: str, reason: str) -> str:
    """返回 'log'（逐条记录）/ 'flood'（写一条汇总行）/ 'drop'（折叠掉）。"""
    now = time.time()
    key = (ip, reason)
    with _auth_log_lock:
        state = _auth_log_state.get(key)
        if not state or now - state[0] >= _AUTH_LOG_WINDOW:
            state = [now, 0, False]
            _auth_log_state[key] = state
        state[1] += 1
        # 硬上限：大量不同来源 IP 各打一次时，按 state 计数的过期清理永远
        # 轮不到（每个计数都是 1），字典会无界增长。超过上限就清空重来——
        # 宁可少采样一小段，也不让外部输入把进程内存撑爆。
        if len(_auth_log_state) > _AUTH_LOG_MAX_ENTRIES:
            _auth_log_state.clear()
            state = [now, 1, False]
            _auth_log_state[key] = state
        if state[1] <= _AUTH_LOG_PER_WINDOW:
            return 'log'
        if not state[2]:
            state[2] = True
            return 'flood'
        return 'drop'


def _log_ip(ip: str, path: str, blocked: bool, ua: str | None,
            reason: str | None = None) -> None:
    """记录一次入站访问。**默认只记拦截**，放行不记（见下）。

    reason 是**被拦的原因**（issue #33）。此前只有一个 blocked 布尔，界面上
    显示「已拦截」却看不出是哪一关拦的：没带密钥 / 密钥不认识 / IP 规则拦的，
    三者的处置方式完全不同（改客户端配置 / 重新发密钥 / 改 IP 规则）。用户
    只能靠猜——实测有用户因此提了 issue 也说不清属于哪一种。

    为什么放行不记（服务器审计的结论）：这张表在安全页叫「IP 访问日志」，
    用途是**安全审计**——回答「谁在扫我、谁被挡了」。此前每个成功请求也写一行，
    实测线上 **7877 行里只有 17 行是拦截记录（0.2%）**，真正该看的信号被
    7860 行正常流量淹没；而表有 2 万行滚动上限（其注释写明「按每次拒绝一行
    估算足够回溯近期攻击」），被成功请求占满后保留窗口从数月压到约 17 天
    —— 真出事时记录可能已经被挤掉了。正常流量在「请求日志」页有完整记录，
    这里重复记一遍只制造噪声。

    例外：`WB_AUDIT_ALL_ACCESS=1` 时恢复全量记录（需要核对"某 IP 到底来过
    什么"时临时打开）。放行的明细始终能在请求日志里查到，所以默认不记不丢信息。

    **这条路径在校验密钥之前执行**，所以它是**未鉴权可达**的写库入口：
    「没有 token」「token 无效」都会先写一行。因此这里必须自我约束，
    否则任何匿名者都能用它膨胀数据库（实测：带 2KB UA 的请求每条约 1.3KB，
    而 ip_access_logs 此前既无清洗也无上限、更没有清理机制）：

      * `ua` / `path` / `reason` 都过 `db._clean`（去换行等控制字符 + 截断）
        ——文档一直声称「日志写入前统一 `_clean()`」，但实际上只有 audit_logs
        这么做了，网关这两张表被漏掉：带换行的 UA 能伪造出额外日志行，
        污染事后排查（实测确认）。
      * 写入走 `db.add_ip_access_log`，由它负责行数上限（超出就丢最旧的），
        避免表无限增长直到磁盘写满。
      * 「没有密钥」「密钥不认识」这两种**匿名可达**的原因再做一层窗口采样
        （见 `_auth_log_verdict`）：每 IP 每分钟前几条照常记，其余折叠成一条
        `key_auth_flood`。所以「一次都没记」不会发生，被折叠的那部分仍留了
        一条能看出「这个 IP 在刷」的记录。
    """
    if not blocked and not config.AUDIT_ALL_ACCESS:
        return
    if reason and reason in _AUTH_FAIL_REASONS:
        verdict = _auth_log_verdict(ip, reason)
        if verdict == 'drop':
            return
        if verdict == 'flood':
            reason = 'key_auth_flood'
    db.add_ip_access_log(ip, path, blocked, ua, reason)


def _upstream_request_id(resp: object) -> str | None:
    """上游请求 ID：把「客户端看到的错误」和「上游工单」对上号（U13）。

    这个值会进错误体与 SSE 错误帧，所以只接受 **ASCII 可打印且 <=128** 的串；
    放开就等于允许上游往我们的响应体里注入内容。取不到就是 None，不编造。
    """
    headers = getattr(resp, 'headers', None) or {}
    try:
        value = headers.get('request-id') or headers.get('x-request-id')
    except (AttributeError, TypeError):
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128 or not value.isascii() or not value.isprintable():
        return None
    return value


def _error_hint(data: object) -> str | None:
    """从上游错误体里取 `error.gateway_hint`（取不到返回 None）。

    上游 2026-09-16 起在错误响应里附这个字段：与 message **并列**的网关视角补充
    说明，只给**可执行的**建议（例如「no healthy account available in pool;
    check /status or retry later」），未覆盖的错误形态不带。

    两个**协议翻译层**（Anthropic / Responses）此前只取 `message` 重建错误体，
    这个字段会被丢掉——于是同一份上游错误，走 `/v1/chat/completions` 的客户端
    能看到建议、走 `/v1/messages` 的看不到。与 issue #18 同一条原则：**真实原因
    与可执行建议都要能到达客户端**。故抽成共用实现，三处口径一致。
    """
    if not isinstance(data, dict):
        return None
    err = data.get('error')
    if not isinstance(err, dict):
        return None
    hint = err.get('gateway_hint')
    if isinstance(hint, str) and hint.strip():
        return hint.strip()[:300]
    return None


def _usage_credit(usage: dict | None) -> float | None:
    """从 usage 里取上游的真实扣费（credit）。

    上游从 2026-09-13 起在末帧 usage 里带 credit（本次真实扣费）。
    取不到就返回 None（存 NULL），不要退化成 0——「没数据」和「免费」
    在成本判断上是两回事，混为一谈会误判成免费号。
    """
    if not isinstance(usage, dict):
        return None
    raw = usage.get('credit')
    if raw is None:
        raw = usage.get('credits')
    return metering.parse_credit(raw)[0]


def _usage_cache(usage: dict | None) -> tuple[int | None, int | None, int | None]:
    """从 usage 里取提示词缓存的三段 token（issue #69）。

    腾讯在流式末帧 usage 里给 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
    / `prompt_cache_write_tokens`；**上游自己的缓存统计就是从这三个字段来的**
    （其 `/v1/stats` 的 cache_hit_rate 同源），所以我们不必自己估算。

    三个都要能区分「没给」与「给了 0」：老上游不给 = None（界面上显示「—」），
    给了 0 = 这次真的没命中缓存。混为一谈会让用户以为缓存生效了。
    """

    def _one(name: str) -> int | None:
        if not isinstance(usage, dict):
            return None
        raw = usage.get(name)
        # bool 是 int 的子类，`True` 不能当成 1 个 token
        if raw is None or isinstance(raw, bool):
            return None
        return metering._int_or_none(raw)[0]

    return (_one('prompt_cache_hit_tokens'),
            _one('prompt_cache_miss_tokens'),
            _one('prompt_cache_write_tokens'))


def client_channel(ua: str | None) -> str:
    text = (ua or '').strip().lower()
    if not text:
        return 'unknown'
    if 'codex' in text:
        return 'codex'
    if 'claude-code' in text or 'claude code' in text:
        return 'claude-code'
    if 'deepseek' in text or 'dsh' in text:
        return 'dsh'
    if 'vscode' in text or 'visual studio code' in text or 'cursor' in text:
        return 'vscode'
    if 'jetbrains' in text or 'intellij' in text:
        return 'jetbrains'
    if 'workbuddy' in text:
        return 'wb'
    if 'mozilla' in text or 'chrome/' in text or 'safari/' in text or 'edg/' in text:
        return 'web'
    if any(k in text for k in ('python', 'httpx', 'requests/', 'curl/', 'node-fetch',
                               'axios', 'okhttp', 'go-http-client')):
        return 'api'
    return 'other'


def _image_part_to_openai(part: dict) -> dict | None:
    """把常见的「非标准图片 part」归一成 OpenAI 的 image_url 形态（issue #134）。

    各家客户端写法不一，上游只认 `{"type":"image_url","image_url":{"url":...}}`：
      · `{"type":"input_image","image_url":"data:..."}`（Responses 风格混进 chat）
      · `{"type":"image_url","image_url":"https://..."}`（url 直接给字符串）
      · Anthropic 风格 `{"type":"image","source":{"type":"base64",...}}`
    认不出的原样返回 None（调用方保持原 part 不动），不做有损改写。
    """
    kind = part.get('type')

    def _url_of(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            inner = value.get('url')
            return inner if isinstance(inner, str) else ''
        return ''

    if kind in ('input_image', 'image_url'):
        url = _url_of(part.get('image_url')) or _url_of(part.get('url'))
        if url:
            return {'type': 'image_url', 'image_url': {'url': url}}
        return None
    if kind == 'image':
        source = part.get('source')
        if isinstance(source, dict):
            stype = str(source.get('type') or '')
            if stype == 'base64':
                media = str(source.get('media_type') or 'image/png')
                data = source.get('data')
                if isinstance(data, str) and data:
                    return {'type': 'image_url',
                            'image_url': {'url': f'data:{media};base64,{data}'}}
            elif stype == 'url':
                url = _url_of(source.get('url'))
                if url:
                    return {'type': 'image_url', 'image_url': {'url': url}}
    return None


# ── 上游提示词指纹风控对冲（2026-10-27 判别实验实锤）─────────────────
# 上游对特定系统提示词特征串按内容拒绝，且伪装成 11102「模型不存在」：
# 同模型同密钥下，裸请求 / 普通 2KB 提示词 / 400KB 大体积全部 200，
# 唯独携带该特征句的请求 503。对冲方式：转发前对特征句做**同义改写**
# （语义不变、字符序列改变），规则可经 settings.prompt_defuse_rules 扩展
# （元素为 [pattern, replacement] 二元组列表；命中即整串替换）。
_DEFAULT_DEFUSE_RULES = (
    ('Tokens prefixed with @ are paths the user explicitly referenced. Relative paths resolve from the workspace root',
     'Tokens beginning with @ refer to file paths cited by the user. Relative paths are resolved against the workspace root'),
)


def defuse_prompt_fingerprint(body: object, *, platform: str = 'workbuddy') -> int:
    """Use the signed, versioned bundle; the hot disable switch is never cached."""
    from .. import promptcompat
    applied = promptcompat.apply(body, platform=platform, get_setting=db.get_setting)
    state = observability.current.get()
    if state is not None and applied.version:
        state.compatibility_version = applied.version
        state.compatibility_rules = applied.rules
    return applied.changes


def normalize_chat_images(body: object) -> int:
    """就地归一 chat 请求里的图片 part，返回改写的数量。

    只动 `messages[*].content` 里的**列表**；字符串内容与其他字段一字不改。
    客户端发图失败时最常见的形态是 url 直接给字符串或 input_image 混用，
    上游解析不了就整条 400，这里换成标准形态即可（issue #134）。
    """
    if not isinstance(body, dict):
        return 0
    messages = body.get('messages')
    if not isinstance(messages, list):
        return 0
    changed = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        for idx, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            fixed = _image_part_to_openai(part)
            if fixed is not None and fixed != part:
                content[idx] = fixed
                changed += 1
    return changed


@requestperf.timed('settlement')
def _record(key: dict | None, ip: str, model: str, mapped: str, status: int, pt: int, ct: int, latency: int, ua: str | None, error: str | None, stream: bool, *, credit: float | None = None, first_token: int | None = None, usage: dict | None = None, effort: str | None = None, session_key: str | None = None) -> None:
    # Raw usage is the authority. Caller-side legacy integer coercion must not
    # turn invalid/absent supplier values into complete zero-valued evidence.
    if credit is not None:
        usage = {**(usage if isinstance(usage, dict) else {}), 'credit': credit}
    measured = metering.parse_usage(usage)
    pt = ((measured.prompt_tokens or 0) if isinstance(usage, dict) and 'prompt_tokens' in usage
          else metering._int_or_none(pt)[0] or 0)
    ct = ((measured.completion_tokens or 0) if isinstance(usage, dict) and 'completion_tokens' in usage
          else metering._int_or_none(ct)[0] or 0)
    credit = measured.credit
    cache_hit, cache_miss, cache_write = _usage_cache(usage)
    state = observability.current.get()
    http_status = state.http_status if state and state.http_status else status
    terminal = state.terminal_state if state else None
    if stream and terminal == 'client_cancelled':
        status = 499
    elif (stream and terminal in ('upstream_error','transport_error','protocol_incomplete')
          and not 400 <= http_status < 500):
        status = 502
    if terminal is None:
        terminal = 'rejected' if 400 <= status < 500 else 'upstream_error' if status >= 500 else 'completed'
    if state:
        state.terminal_state = terminal
        state.first_token_ms = first_token
        state.metrics_model = mapped or model
    # U01（2026-10-28）：先把「要记的事实」算成纯数据。它不依赖事务，所以事务
    # 失败时可以直接落 spool；重放写的是同一组列（见 db.add_request_log 的列元组）。
    model_clean = db._clean(model, 128)
    realm = db.realm_of_model(db._clean(mapped, 128) or model_clean)
    facts = {
        'ts': int(time.time()), 'key_id': key['id'] if key else None,
        'ip': db._clean(ip, 64), 'model': model_clean,
        'mapped_model': db._clean(mapped, 128), 'status': status,
        'prompt_tokens': max(0, pt), 'completion_tokens': max(0, ct),
        'latency_ms': latency, 'first_token_ms': first_token,
        'ua': db._clean(ua, 512),
        # 调用方显式传的原因优先；没传时回退到请求状态里记的那份。
        # 流中失败（`stream_read`）走的是「发一个错误帧给客户端 + finally 收尾」的
        # 路径，那里没有人再调用 `_record` 传 error —— 不回落就会丢成空字符串，
        # 记录里只剩一个 `transport_error` 的壳。
        'error': db._clean(error or getattr(state, 'error', None), 500),
        'stream': 1 if stream else 0, 'credit': credit, 'realm': realm,
        'cache_hit_tokens': cache_hit, 'cache_miss_tokens': cache_miss,
        'cache_write_tokens': cache_write,
        'effort': db._clean(effort, 16) if effort else None,
        'channel': client_channel(ua),
        'session_key': db._clean(session_key, 64) if session_key else None,
        'request_id': state.request_id if state else None,
        'endpoint': state.endpoint if state else None,
        'method': state.method if state else None,
        'protocol': state.protocol if state else None,
        'http_status': http_status, 'terminal_state': terminal,
        'compatibility_version': getattr(state, 'compatibility_version', None),
        'compatibility_rules': json.dumps(getattr(state, 'compatibility_rules', ())) if state else None,
        'error_stage': state.error_stage if state else None,
        'release_id': observability.RELEASE_ID,
        'account': state.account if state else None,
        'usage_source': 'not_applicable' if state and state.endpoint in metering.NON_BILLABLE_PATHS else 'upstream' if measured.observed else 'unknown' if stream else 'not_reported',
        'usage_complete': int(measured.complete),
        'first_byte_ms': state.first_byte_ms if state else None,
        # K27：实际路由到的平台/上游。NULL = 本请求没有记录到路由结果
        # （例如鉴权阶段就被拒），**不是** workbuddy —— 对账时单独归类。
        'actual_platform': getattr(state, 'actual_platform', None) if state else None,
        'upstream_ref': getattr(state, 'upstream_ref', None) if state else None,
        # D-03：入口平台必须在这里定下来。`db.add_request_log` 的兜底读的是
        # `forced_platform` 这个 ContextVar，而 spool 重放跑在后台线程里，
        # ContextVar 是空的 —— 结果每条重放都记成 workbuddy，把别的平台的
        # 调用算到 workbuddy 头上。写进 facts 之后，重放用的是入口那一刻的值。
        'platform': forced_platform.get() or 'workbuddy',
        # RV01：调用方的关联 ID（X-Request-ID，已校验）。与 request_id
        # （服务端执行 ID）是两个字段：这里记的只是「调用方说自己是
        # 谁」，不参与主键，也不能用来反推执行归属。
        'client_request_id': state.client_request_id if state else None,
    }
    try:
        with db.transaction():
            # 幂等由 `request_logs` 的唯一索引直接提供：插入命中冲突时返回 0 行，
            # 说明这条请求已经记过账，直接收工。原先这里先 `SELECT ... WHERE
            # request_id=?` 再插入，等于每个请求多一次同表定位。
            inserted = db.add_request_log(**facts)
            if state and not inserted:
                return
            from .. import budget
            budget.settle(usage,pt,ct,credit)
            from .. import ledger
            ledger.record(state.request_id if state else None, key['id'] if key else None,
                          model_clean, realm, pt, ct, credit,
                          bool(key and (pt+ct or credit)), terminal, observability.RELEASE_ID)
            if state and getattr(state,'config_hash',None):
                ledger.forwarding(state,'terminal',terminal)
            if key and (status < 400 or pt+ct>0 or credit):
                total=max(0,pt)+max(0,ct)
                keysvc.touch(key,ip,total,credit,ts=facts['ts'])
                if total or credit:
                    db.bump_usage(key['id'],model_clean,max(0,pt),max(0,ct),credit,realm=realm,cache=(cache_hit or 0,cache_miss or 0),ts=facts['ts'])
        if state:
            state.recorded=True
            # 事务到这里已经提交：结算真的把预算行迁走了，收尾阶段不必再用一次
            # 空事务去"兜底"（见 budget.finish 的注释）。
            state.budget_settled=getattr(state,'budget_settled_pending',False)
    except Exception:
        from .. import operational, ledgerreplay
        operational.ledger_write_healthy=False
        # U01：事实先落 spool，由后台按 request_id 幂等重放。spool 也写不进
        # （同盘写满/无权限）时置降级标志：就绪判定失败 + 新请求背压，停止放大。
        entry = ledgerreplay.build_entry(
            facts,
            config_hash=getattr(state,'config_hash',None) if state else None,
            budget={'reserved': bool(getattr(state,'budget_reserved',False)),
                    'portal_owner': getattr(state,'portal_owner',None),
                    'fence': getattr(state,'budget_fence',None)},
            release_id=observability.RELEASE_ID)
        if ledgerreplay.spill(entry) is None:
            operational.ledger_spool_failed=True
        logger.exception('请求账本事务失败 request_id=%s（已落回执等待重放）',
                         state.request_id if state else None)
    # 侧表清理放在事务之外：它原先挂在结算事务里，每两千次结算让账本多跑四条
    # DELETE（含一条 OFFSET 子查询）。旁路工作不该占用账本的写窗口。
    db.prune_if_pending()


async def _record_async(*args, **kwargs) -> None:
    """在线程里跑 `_record`：它是 1 个事务 + 5～7 条语句 + 至少一次 fsync。

    直接在事件循环上跑会把所有在途 SSE 流一起卡住（`db` 是同步 SQLite）。
    `asyncio.to_thread` 会复制 contextvars，所以 `_record` 里读的
    `observability.current` 与 `forced_platform` 仍然是**这个请求**的。

    流式响应的 `finally` 不走这里：那里 await 会在取消/关闭时丢掉结算，
    丢一行账比阻塞一会儿更糟。
    """
    await asyncio.to_thread(_record, *args, **kwargs)


def _global_ip_blocked(ip: str) -> bool:
    """全局入站 IP 规则是否拦截该来源（未启用安全策略时恒为放行）。

    从 `_authorize` 抽出：`/v1/usage` 与调用路径必须同口径——它虽是只读端点，
    但同样暴露密钥元数据与余量，被全局黑名单拦住的来源不该换条路就能查
    （2026-10-21 安全检查 L4）。
    """
    sec = get_security_config()
    if not sec.get('enabled'):
        return False
    rules = [
        {'kind': r['kind'], 'cidr': r['cidr']}
        for r in db.query('SELECT kind, cidr FROM ip_rules')
    ]
    return not iputil.evaluate(ip, rules, sec.get('mode', 'blacklist'))


def _authorize(request: Request, model: str | None,
               *, is_model_list: bool = False,
               mapped: str | None = None) -> tuple[dict | None, str, JSONResponse | None]:
    """返回 (key, ip, error_response)。

    mapped：`model` 经「模型映射」后的名字。**默认在这里算**，因为版本归属判的
    是映射后的名字（issue #47，见 `keysvc.validate`）——调用方算好了可以传进来
    省一次设置读取，但漏传不会退回旧行为。调用方仍要自己再算一次用于改写 body
    （或把这里的算法抽出去共用）。
    """
    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')
    path = request.url.path

    # 拦截原因写成**稳定的短码**而不是散文：界面按码翻译（5 种语言都能准确
    # 对应），改文案不必迁移历史数据，也不会因为中英混排让日志列宽失控。
    token = _bearer(request)
    if not token:
        _log_ip(ip, path, True, ua, 'missing_key')
        return None, ip, _oai_error('缺少 API Key，请在 Authorization 头中提供 Bearer 令牌', 401, 'authentication_error', 'missing_api_key')

    key = keysvc.resolve(token)
    if not key:
        _log_ip(ip, path, True, ua, 'invalid_key')
        return None, ip, _oai_error('API Key 无效', 401, 'authentication_error', 'invalid_api_key')

    return _authorize_key(request, key, ip, model, is_model_list=is_model_list, mapped=mapped)


def _authorize_key(request: Request, key: dict, ip: str, model: str | None,
                   *, is_model_list: bool = False, mapped: str | None = None):
    """One policy/order for OpenAI, Responses and Anthropic after token selection."""
    ua = request.headers.get('user-agent')
    path = request.url.path
    state=observability.current.get()
    if state: state.key_id=int(key["id"])

    # 映射只算一次，后面三处拒绝路径都要用它记账（见下）。
    # 为什么拒绝路径也要传：日志的 `realm` 按**实际要用的那个名字**归档（issue #47
    # 的统一口径）。别名不带 `global:` 前缀，若拒绝路径按请求名归档，一次「别名指向
    # 国际版、却被国内版密钥拒绝」的记录会落在国内版栏目里——按版本筛日志的人会看到
    # 一条本不该在那儿的记录，且看不出它原本指向国际版。
    try:
        selected_platform = platformpolicy.bind_request(request, model)
    except platformpolicy.PlatformConflict as exc:
        return key, ip, _oai_error(str(exc), 400, 'invalid_request_error', exc.code)
    mapped = _map_model(model) if mapped is None else mapped

    # 全局入站 IP 规则
    if _global_ip_blocked(ip):
        _log_ip(ip, path, True, ua, 'ip_blocked')
        # U21：稳定错误码。这里刻意复用下面 `_oai_error` 里那个 code 字符串 ——
        # 客户端看到的码与库里记的码必须是同一个，否则统计与排障各说一套。
        observability.mark_error('ip_blocked')
        _record(key, ip, model or '', mapped or '', 403, 0, 0, 0, ua, 'IP 被拦截', False)
        return None, ip, _oai_error(f'来源 IP {ip} 被安全策略拦截', 403, 'permission_error', 'ip_blocked')

    # 注意这一行**在密钥校验之前**：密钥层面的拒绝（停用 / 过期 / 配额用尽 /
    # 模型与版本不符 / IP 白名单）发生在这之后，若就这样返回，日志里会显示
    # 「已放行」而请求其实失败了 —— 用户对着「已放行」找问题，方向直接跑偏。
    # 因此校验失败时改写这一行为拦截（见下），放行时才落「已放行」。
    reason = keysvc.validate(key, ip, model, is_model_list=is_model_list,
                             mapped_model=mapped,
                             # K01-S1：迁移旧 key 的平台范围要按"本次请求的生效
                             # 平台"判（入口决定；空 = 默认 workbuddy，与
                             # db.add_request_log 的平台归属同一口径）。
                             platform=selected_platform)
    if reason:
        # 状态码由 keysvc 决定，不再一律 403：一批客户端（DeepSeek Harness 等）
        # 把 401/403 统一显示成「API 密钥无效」，一律 403 会把「密钥版本不匹配」
        # 这种配置问题说成密钥坏了，用户便反复重建密钥（issue #18）。
        # U21：keysvc 的拒绝原因本身就是一个稳定码（invalid_api_key /
        # key_disabled / quota_exhausted / model_not_allowed …），直接沿用。
        observability.mark_error(getattr(reason, 'code', None))
        _record(key, ip, model or '', mapped or '',
                getattr(reason, 'status', 403), 0, 0, 0, ua, reason, False)
        _log_ip(ip, path, True, ua, _key_reject_code(reason, is_model_list))
        return None, ip, _oai_error(
            reason,
            getattr(reason, 'status', 403),
            getattr(reason, 'err_type', 'permission_error'),
            getattr(reason, 'code', 'forbidden'),
        )

    # Counting keeps full key/model/IP/rate checks and body capacity, but never
    # consumes the generation slot held by an in-progress answer on that key.
    metadata_count = (request.scope.get('method') == 'POST'
                      and request.scope.get('path', '') == '/v1/messages/count_tokens')
    if not is_model_list and not metadata_count and not observability.admit_key(key):
        observability.mark_error('key_concurrency_exceeded')
        _record(key,ip,model or '',mapped or '',429,0,0,0,ua,'密钥并发容量已满',False)
        return None,ip,_oai_error('密钥并发容量已满，请等待当前请求结束',429,'rate_limit_error','key_concurrency_exceeded')
    limited, count = _rate_limited(key)
    if limited:
        msg = f'请求过于频繁（{RATE_WINDOW}s 内超过 {RATE_MAX_PER_MIN} 次）'
        observability.mark_error('rate_limit_exceeded')
        _record(key, ip, model or '', mapped or '', 429, 0, 0, 0, ua, msg, False)
        _log_ip(ip, path, True, ua, 'rate_limited')
        return None, ip, _oai_error(msg, 429, 'rate_limit_error', 'rate_limit_exceeded')

    _log_ip(ip, path, False, ua)
    return key, ip, None


def preauthorize_body(request: Request, *, anthropic: bool = False):
    """Reject header-proven failures before buffering; never reserve or grant use.

    Key resolution and all policies run again after the body has arrived. This
    read-only check consumes no rate, concurrency, budget or bound-IP allowance.
    """
    ip = iputil.client_ip(request)
    if anthropic:
        from .anthropic import _resolve_key
        key, token = _resolve_key(request)
    else:
        token = _bearer(request)
        key = keysvc.resolve(token) if token else None
    if key is None:
        _log_ip(ip, request.url.path, True, request.headers.get('user-agent'),
                'invalid_key' if token else 'missing_key')
        return _oai_error('API Key 无效' if token else '缺少 API Key', 401,
                          'authentication_error', 'invalid_api_key' if token else 'missing_api_key')
    state = observability.current.get()
    if state:
        state.key_id = int(key['id'])
    if _global_ip_blocked(ip):
        reason = keysvc.Rejection('来源 IP 被安全策略拦截', 403, 'permission_error', 'ip_blocked')
    else:
        original = request.scope.get('zhihui.original_path', request.url.path)
        hint = request.headers.get('x-qtg-platform', '')
        explicit = bool(hint.strip()) or any(
            original == f'/{p}/v1' or original.startswith(f'/{p}/v1/')
            for p in platformpolicy.PLATFORMS)
        try:
            selected = platformpolicy.select(original, hint)
        except platformpolicy.PlatformConflict as error:
            return _oai_error(str(error), 400, 'invalid_request_error', error.code)
        reason = keysvc.validate(key, ip, None, is_model_list=True,
                                 platform=selected, defer_platform=not explicit)
    if reason:
        observability.mark_error(reason.code)
        _log_ip(ip, request.url.path, True, request.headers.get('user-agent'), reason.code)
        _record(key, ip, '', '', reason.status, 0, 0, 0, request.headers.get('user-agent'), reason, False)
        return _oai_error(reason, reason.status, reason.err_type, reason.code)
    return None


def _key_reject_code(reason: object, is_model_list: bool) -> str:
    """把密钥层的拒绝原因归成一个稳定的短码，供入站日志显示。

    为什么不用 keysvc 的原话：那是**给调用方客户端看的完整句子**（含具体数字，
    如「已用 1200 / 上限 1000」），直接塞进日志列表会撑爆列宽；而且它是中文的，
    界面切成英文时那一列会中英混排。这里只归**类别**，展示侧按语言翻译。

    取不到 code 时回落到 'key_rejected'（宁可说得笼统，也不要漏记成「已放行」）。
    """
    code = getattr(reason, 'code', '') or ''
    return code if isinstance(code, str) and code else 'key_rejected'


def _map_model(model: str | None) -> str | None:
    """「模型映射」查表：别名 → 真名；没命中就落到**兜底模型**（issue #173），
    两者都没有时原样返回。

    设置坏掉（存成了非对象）时按「没有映射」处理，**不能让整个网关 5xx**：
    映射是可选的便利功能，坏数据只该影响它自己那一项，不连坐（与
    `db._json_list` 对非法 JSON 列的处理同一原则）。

    语义集中在 `services/modelmap`，这里只取结果：白名单补条目与别名校验走同一份
    实现 —— 三处各写一遍必然漂移，而漂移出来的错位（能调用、但客户端的模型清单里
    没有它）只有用户能发现。
    """
    if model and "/" in model:
        _pfx, _rest = model.split("/", 1)
        if _pfx.lower() in platformpolicy.PLATFORMS and _rest:
            return _rest
    return modelmap.target_for(db.get_setting("model_map", {}) or {}, model)


def _upstream_headers(upstream: dict | None = None) -> dict:
    """转发时用的请求头；`upstream` 为空 = 默认上游。

    多上游（密钥绑定上游）之后，请求头**必须按密钥解析出来的上游来构造** ——
    拿错上游的 api_key 会打到别的账号池上，而这正是本功能要隔离的东西。
    默认参数只为兼容「没有密钥」的调用方（探活 / 管理端调试台）。
    """
    up = upstream if upstream is not None else upstreamsvc.default_upstream()
    headers = {'Content-Type': 'application/json', 'Accept-Encoding':'gzip, deflate, identity'}
    # 分组把 api_key 留空、且地址与默认上游相同时沿用默认那把（见
    # upstreamsvc.forward_api_key）——「添加分组」只填名称就是这个形态，
    # 不沿用的话绑定这类分组的密钥一调用就吃上游 401。
    api_key = upstreamsvc.forward_api_key(up)
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    state = observability.current.get()
    if state:
        # RV01：上游看到两个不同的 ID，语义明确、互不冒充。
        #   X-Request-ID           = 调用方关联 ID（有就用，没有才用执行 ID）——
        #                            保住 K47 的 edge→Manager→引擎链路关联；
        #   X-Manager-Request-ID   = 本进程的执行 ID（预算/账本/outcome 的键）。
        # 只在这里构造一次：anthropic.py / responses.py / playground.py 都调用
        # 本函数，因此不需要各处重复改（也不要各写一套而漂移）。
        headers['X-Request-ID'] = state.client_request_id or state.request_id
        headers['X-Manager-Request-ID'] = state.request_id
        dispatch_id = uuid.uuid4().hex
        try:
            db.execute('INSERT INTO provider_dispatches_v2 VALUES(?,?,?)',
                       (dispatch_id, state.request_id, int(time.time())))
        except Exception:        # noqa: BLE001
            # 归因证据不是转发的先决条件：dispatch_id 马上就要写进请求头，这一行
            # 掉了只影响"事后把上游回执对回请求"，不该让整次转发失败。正常情况下
            # 这条路径不会触发；它挡的是磁盘/锁的瞬时故障把 200 变成 5xx。
            logger.warning('provider dispatch 记录失败 request_id=%s', state.request_id)
        headers['X-Manager-Dispatch-ID'] = dispatch_id
        headers['X-WB-Compatibility'] = 'manager-scoped-v1'
        platform = getattr(state, 'actual_platform', None) or forced_platform.get() or up.get('name')
        if platform not in ('qoder', 'trae'):
            state.provider_budget_enabled = True
            headers['X-WB-Attempt-Budget'] = str(state.provider_attempts_remaining)
            from .. import deadline as provider_deadline
            total = provider_deadline.request_budgets(config.UPSTREAM_TIMEOUT).total_s
            remaining = max(1, min(600000, int((total - (time.monotonic() - state.started)) * 1000)))
            headers['X-WB-Remaining-Ms'] = str(remaining)
        if getattr(state,'config_hash',None):
            headers['X-WB-Config-Hash'] = state.config_hash
        if state.key_id is not None:
            namespace = f'key:{state.key_id}/upstream:{up.get("id") or 0}'
            headers['X-WB-Tenant'] = namespace
    return headers


async def _upstream_headers_async(upstream: dict | None = None) -> dict:
    # The dispatch marker is a durable SQLite write. Keep lock/fsync contention
    # away from every active SSE stream, and join the write on cancellation.
    from ..services.asyncwork import joined_thread
    return await joined_thread(_upstream_headers, upstream)


def _consume_attempt_budget(response) -> bool:
    state = observability.current.get()
    if state is None or not state.provider_budget_enabled:
        return True
    raw = (getattr(response, 'headers', {}) or {}).get('x-wb-provider-attempts', '')
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or len(raw) > 2:
        state.provider_attempts_remaining = 0
        return False
    spent = int(raw)
    if spent > state.provider_attempts_remaining:
        state.provider_attempts_remaining = 0
        return False
    state.provider_attempts_remaining -= spent
    return state.provider_attempts_remaining > 0


def _upstream_hint(upstream: dict | None) -> str:
    """502/503 时补一句「本次打的是哪个上游、这一跳会不会走出口代理」。

    为什么要有（issue #167：两位用户都报「直接 curl 上游是通的、经面板全 502」）：
    原来的报错只有异常文本，看不出面板往哪打的、中间有没有代理。而最常见的两种成因
    恰恰要靠这两条信息才能自查：

      · 上游地址 / 端口填错（面板打的是配置里那个地址，不是你以为的那个）；
      · **上游地址写成公网域名、同时又配了出口代理**：面板→上游这一跳会走代理，
        代理绕不回你自己那台机器，于是全 502 —— 而内网与本机地址是自动直连的。

    `_is_internal_host` 与转发用的判断是同一个函数，所以这里说的「会走代理」就是
    真实行为，不是提示语里另算一套。
    """
    up = upstream or upstreamsvc.default_upstream()
    base = str(up.get('base_url') or '').strip()
    if not base:
        return ''
    display_base = proxyroutes.mask_url(base)
    try:
        host = urlsplit(base).hostname
    except ValueError:
        host = None
    proxy = config.effective_http_proxy()
    if proxy and host:
        if config._is_internal_host(host):       # noqa: SLF001 —— 与转发判断同一份实现
            return f'（本次请求的上游：{display_base}；该地址属内网/本机，这一跳直连）'
        masked = proxyroutes.mask_url(proxy)
        return (f'（本次请求的上游：{display_base}；这一跳会经出口代理 {masked} —— '
                f'代理到不了这个地址就会 502）')
    return f'（本次请求的上游：{display_base}）'


def _public_upstream_error(upstream: dict | None) -> str:
    """Return a client-safe upstream failure message.

    The full exception remains in the request record for operators. Raw
    transport errors must not cross the API boundary because http clients may
    include credentials, proxy URLs, or internal socket addresses in them.
    """
    return '上游暂时不可用，请稍后重试' + _upstream_hint(upstream)


#: 看起来像平台名、但**不是**的东西。
#:
#: 用户写错前缀时（最典型的是把网关自己的名字当平台名），我们目前会把这个字符串
#: 原样转发给上游，上游回一句 `no such model`，用户看到 503 会以为是模型不存在，
#: 而真正的错因是前缀写错了。
#:
#: **不能泛化判断"含斜杠就是写错"**：真实模型名也有带斜杠的（`meta-llama/Llama-3`
#: 这类）。所以只对这张明确的清单提示 —— 保守，但覆盖实际踩过的坑。
_PLATFORM_LIKE_PREFIXES = frozenset({
    'zhihui', 'wb', 'workbuddy2', 'qtg', 'buddy', 'manager', 'gateway',
})


def _misused_platform_prefix(model: object) -> JSONResponse | None:
    """模型名带斜杠、且前缀像平台名却不是合法平台时，给一条**能照做**的错误。

    返回 None 表示没问题或无从判断，继续正常路由。

    为什么不往后放：上游那一趟是真实开销，而且它给的错误会把人引向「模型不存在」
    这个错误方向。能在本地判出来的，就别让它跑一趟。
    """
    if not isinstance(model, str) or '/' not in model:
        return None
    prefix = model.split('/', 1)[0].lower()
    if prefix in platformpolicy.PLATFORMS or prefix not in _PLATFORM_LIKE_PREFIXES:
        return None
    return _oai_error(
        f"模型名里的前缀 `{prefix}` 不是可用的平台名。"
        f"可用的是：{', '.join(platformpolicy.PLATFORMS)}（例如 trae/Doubao-Seed-Code）。"
        f"如果你想调用的模型名本身带斜杠，请确认它确实是上游提供的完整名称。",
        400, 'invalid_request_error', 'unknown_platform_prefix')


def _upstream_for(key: dict | None, *, ip: str, model: str, mapped: str,
                  ua: str | None) -> tuple[dict | None, JSONResponse | None]:
    """按密钥解析本次请求该走哪个上游；不可用时返回**明确报错**。

    不静默回落默认上游的原因见 upstreamsvc 模块注释第 2 条：那把密钥本应只碰
    某个分组的账号，回落到默认上游会让它打到全池——隔离失效而且无人察觉。
    失败同时记一条请求日志（503 + 原因），排障的人第一时间就能看到。
    """
    _fp = forced_platform.get()
    # 前缀写错就地拒绝：省一趟上游，也避免上游那句 "no such model" 把人带偏。
    misuse = _misused_platform_prefix(model)
    if misuse is not None:
        observability.mark_error('unknown_platform_prefix')
        _record(key, ip, model, mapped, 400, 0, 0, 0, ua,
                '模型名前缀不是可用的平台名', False)
        return None, misuse
    if not _fp and model and "/" in model:
        _cand = model.split("/", 1)[0].lower()
        if _cand in _THREEWAY_SPEC:
            _fp = _cand
    if _fp in _THREEWAY_SPEC:
        _tw = _threeway_upstream(_fp)
        if _tw is not None:
            return _tw, None
        observability.mark_error('upstream_unavailable')
        message = f'{_fp} upstream is unavailable; refusing cross-platform fallback'
        _record(key, ip, model, mapped, 503, 0, 0, 0, ua, message, False)
        return None, _oai_error(message, 503, 'api_error', 'upstream_unavailable')
    try:
        return upstreamsvc.resolve_for_key(key), None
    except upstreamsvc.UpstreamUnavailable as exc:
        observability.mark_error('upstream_unavailable')
        _record(key, ip, model, mapped, 503, 0, 0, 0, ua, str(exc), False)
        # 这里不加 _upstream_hint：本函数讲的是「密钥绑定的上游不可用」，作用域里
        # 也没有那个上游对象 —— 报出默认上游的地址只会把人带偏（异常文本已经说清
        # 是哪个分组、该启用还是改绑）。
        return None, _oai_error(str(exc), 503, 'api_error', 'upstream_unavailable')


def _stamp_route(upstream: dict | None, requested_model: str | None,
                 resolved_model: str | None = None) -> None:
    """K27：把「实际被路由到的平台/上游」记进请求上下文（供 `_record` 落库）。

    为什么不能只看 `request_logs.platform`：那一列记的是**入口/强制平台**
    （`db.add_request_log` 从 `forced_platform` 取，默认 `workbuddy`），而
    `qoder/…` / `trae/…` 模型前缀与密钥绑定上游都会改变**实际**路由目标。
    两列分开存，事后才能核对「日志说的」与「实际走的」是否一致。

    只写可确证的字段：拿不到上游对象时 `upstream_ref` 允许为空（NULL = 未记录），
    绝不填一个看起来合理的默认值。
    """
    from .. import metering
    state = observability.current.get()
    if state is None:
        return
    info = metering.route_attribution(
        entry_platform=forced_platform.get(),
        requested_model=requested_model,
        resolved_model=resolved_model or requested_model,
        upstream=upstream,
    )
    state.actual_platform = info['actual_platform']
    state.upstream_ref = info['upstream_ref']


def _scan_sse(pending: str, usage: dict) -> tuple[str, bool]:
    """扫描 SSE 文本片段：提取 usage，并判断是否已出现首个正文。

    返回 (未处理完的残留缓冲, 本次是否见到正文 delta)。
    为什么要单独判断「正文」：OpenAI 流的第一块通常只有 role、content 为空，
    若用「收到首块」当首字，会把连接建立时间也算进去，数字偏小且失真。
    """
    saw_content = False
    while '\n' in pending:
        line, pending = pending.split('\n', 1)
        line = line.strip()
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if not payload or payload == '[DONE]':
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get('usage'), dict):
            observability.merge_usage(usage, obj['usage'])
        choices = obj.get('choices')
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get('delta') or choice.get('message') or {}
                if isinstance(delta, dict) and (
                    delta.get('content') or delta.get('reasoning_content')
                ):
                    saw_content = True
    return pending, saw_content


# ── 密钥自查用量（issue #137）─────────────────────────────
# 拿密钥自身鉴权、只回这把密钥自己的数字，**只读面板已记录的值、不触发任何上游请求**
# （否则「查一下还剩多少」会变成一次真实调用，反过来加剧额度消耗与限流）。
#
# 有意**不套用**调用时的那套校验（额度用尽 / 已过期 / 被停用 / IP 白名单）：恰恰是
# 在调不动的时候，用户最需要看到「还剩多少、为什么调不动」。密钥本身已证明归属，
# 这里只暴露它自己的元数据，把状态如实回给调用方，而不是再甩一个 401。
#
# 本地定制：`/v1/usage` 已经是本仓库的「按密钥用量统计」（在用、且被客户端依赖），
# 上游 issue #137 的「配额自查」改挂 /v1/key，两个契约都保留、互不覆盖。
@router.get('/v1/key')
def key_usage(request: Request):
    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')
    path = request.url.path
    token = _bearer(request)
    # 认不出调用方的访问仍要进**入站日志**：这条路径与其它网关入口一样是
    # 未鉴权可达的，漏记会让「谁在扫我」少一块（安全审计口径，见 `_log_ip`）。
    if not token:
        _log_ip(ip, path, True, ua, 'missing_key')
        return _oai_error('缺少 API Key，请在 Authorization 头中提供 Bearer 令牌',
                          401, 'authentication_error', 'missing_api_key')
    key = keysvc.resolve(token)
    if not key:
        _log_ip(ip, path, True, ua, 'invalid_key')
        return _oai_error('API Key 无效', 401, 'authentication_error', 'invalid_api_key')

    # 全局入站 IP 规则：与其他 /v1 入口同口径（见 _global_ip_blocked 的说明）。
    if _global_ip_blocked(ip):
        _log_ip(ip, path, True, ua, 'ip_blocked')
        observability.mark_error('ip_blocked')
        _record(key, ip, '', '', 403, 0, 0, 0, ua, 'IP 被拦截', False)
        return _oai_error(f'来源 IP {ip} 被安全策略拦截', 403, 'permission_error', 'ip_blocked')

    def allowance(limit: int | None, used: int | None) -> dict:
        used_n = int(used or 0)
        limit_n = int(limit or 0)
        return {
            'used': used_n,
            'limit': limit_n or None,          # None = 不限制
            'remaining': max(0, limit_n - used_n) if limit_n else None,
        }

    def credit_allowance(limit, used):
        from .. import ledger
        # Subtract whole micro-units: int(0.3) would incorrectly claim unlimited
        # credit, and floating subtraction would expose 0.09999999999999998.
        limit_units = ledger.units(limit or 0)
        used_units = ledger.units(used or 0)
        return {
            'used': used_units / ledger.SCALE,
            'limit': limit_units / ledger.SCALE if limit_units else None,
            'remaining': max(0, limit_units - used_units) / ledger.SCALE if limit_units else None,
        }

    now = int(time.time())
    expires_at = int(key.get('expires_at') or 0)
    # 记一行**请求日志**（与 `/v1/models` 同口径，0 token）：否则一把密钥被
    # 监控脚本每分钟轮询查用量时，运维在「请求日志」里完全看不出流量从哪来。
    # 这一行经 `_record` 顺带把「最近使用」与「来源 IP」留痕——与模型列表页、
    # 面板 API 令牌一致；但 token / 积分消耗**一分不记**（记账口径只认真实调用）。
    _record(key, ip, '', '', 200, 0, 0, 0, ua, None, False)
    result = {
        'object': 'key.usage',
        'key': {
            'name': key.get('name') or '',
            'prefix': key.get('prefix') or '',
            'realm': key.get('realm') or '',
            'enabled': bool(key.get('enabled')),
            'expires_at': expires_at,
            'expired': bool(expires_at and now >= expires_at),
            'models': list(key.get('models') or []),
        },
        'tokens': allowance(key.get('quota'), key.get('used_tokens')),
        'credits': credit_allowance(key.get('quota_credit'), key.get('used_credit')),
        'last_used_at': int(key.get('last_used_at') or 0),
        # 这几个数字是面板**已记录**的值，读取时刻即 snapshot_at；不是向上游现问的。
        'snapshot_at': now,
        'data_source': 'panel-recorded',
    }
    from .. import portalpolicy
    if portalpolicy.is_portal(key):
        from .. import portalstore
        try:
            with portalstore.transaction() as conn:
                owner = portalpolicy.identity(key, conn)
        except Exception:
            return _oai_error('用户密钥授权已失效，请重新登录核对', 401, 'authentication_error', 'invalid_api_key')
        wallet = portalstore.overview(owner)
        with portalstore.transaction() as conn:
            charged = conn.execute("SELECT COALESCE(SUM(charged_units),0) FROM portal_spends WHERE owner_id=? AND key_id=? AND state='settled'",
                                   (owner, key['id'])).fetchone()[0]
        result['credits'] = {'used': charged / portalstore.SCALE, 'limit': None,
                             'remaining': wallet['callable_units'] / wallet['scale'],
                             'shared_wallet': True, 'unlimited': False}
        result['portal'] = {name: wallet[name] for name in ('balance_units', 'callable_units', 'pending_pool_units',
            'held_units', 'debt_units', 'scale', 'pool_enabled', 'dispatch_enabled', 'dispatch_note')}
        result['portal']['model_discovery_enabled'] = True
    return result


# ── 模型列表 ─────────────────────────────────────────────
# 列表按密钥的版本归属过滤：国际版密钥只看到 `global:` 条目、国内版密钥只看到
# 其余条目（限定了版本的密钥看不到另一版本，免得挑出一个注定 403 的模型）。
# 未限定版本的密钥（存量）照旧看到全部——它们本来就两版都能调。
# ── 按密钥用量查询 /v1/usage ──────────────────────────────────────────────
#
# 给密钥持有者一个自助查用量的入口：带自己的密钥即可查当天 / 近 7 天 / 近 30 天
# 的请求数、Token 用量与缓存命中率。**不返回实付（credit）**——那是内部口径，
# 对外只回答"用了多少"。
#
# 鉴权刻意比 chat 轻：配额用尽的密钥也能查。配额打满时 chat 会被 429 拒绝，
# 而"我用了多少、还剩多少"的答案只能从本接口拿——此时反而是最需要它的时刻。
# 所以不走 _authorize 的完整链路（其中 keysvc.validate 会把配额用尽直接拦掉），
# 只保留"密钥本身有效"这一层：resolve + 停用 / 过期。
# 数据口径与 /api/stats 完全一致（同一张 usage_daily 表、同一套 day 过滤），
# 保证"用户自查"与"控制台看到"的数字一致。


def _usage_since(days: int) -> str:
    """近 days 天（含今天）的起始日期串——口径与 /api/stats 的 _since 一致。"""
    return time.strftime('%Y-%m-%d', time.localtime(time.time() - (days - 1) * 86400))


def _usage_window(key_id: int, since_day: str) -> dict:
    """聚合单个密钥自 since_day 起的用量（含当天）。

    hit_rate 分母为 0（无调用 / 上游从未返回缓存字段）时给 null：
    0 会被读成"缓存全没命中"，与"没有数据"是两回事。
    """
    row = db.query_one(
        'SELECT COALESCE(SUM(requests),0) AS r, '
        'COALESCE(SUM(prompt_tokens + completion_tokens),0) AS t, '
        'COALESCE(SUM(cache_hit_tokens),0) AS hit, '
        'COALESCE(SUM(cache_miss_tokens),0) AS miss '
        'FROM usage_daily WHERE key_id = ? AND day >= ?',
        (key_id, since_day),
    )
    hit = int((row['hit'] if row is not None else 0) or 0)
    miss = int((row['miss'] if row is not None else 0) or 0)
    cached = hit + miss
    return {
        'requests': int((row['r'] if row is not None else 0) or 0),
        'tokens': int((row['t'] if row is not None else 0) or 0),
        'cache_hit_tokens': hit,
        'cache_miss_tokens': miss,
        'hit_rate': round(hit / cached, 4) if cached else None,
    }


def _usage_live(key_id: int, window: int = 3) -> dict:
    """实时速度：最近 window 秒完成的生成 token 速率（客户端轮询即可看到跳动）。

    取 request_logs 而不是 usage_daily：后者按天聚合，颗粒度不够。
    只计 completion_tokens（输出速度才是"token 速度"的直觉口径）；
    窗口内没有请求时速率自然归零——0 本身就是有意义的信息。
    """
    since = int(time.time()) - window
    row = db.query_one(
        'SELECT COALESCE(SUM(completion_tokens),0) AS t FROM request_logs '
        'WHERE key_id = ? AND ts >= ?',
        (key_id, since),
    )
    tokens = int((row['t'] if row is not None else 0) or 0)
    return {
        'window_seconds': window,
        'tokens': tokens,
        'tokens_per_second': round(tokens / window, 1),
    }


def _usage_by_model(key_id: int, since_day: str) -> list[dict]:
    """按模型拆分用量（近 N 天，含当天）。口径与 _usage_window 一致，按 tokens 降序。"""
    rows = db.query(
        "SELECT COALESCE(model, '未知') AS name, "
        'COALESCE(SUM(requests),0) AS r, '
        'COALESCE(SUM(prompt_tokens + completion_tokens),0) AS t, '
        'COALESCE(SUM(cache_hit_tokens),0) AS h, '
        'COALESCE(SUM(cache_miss_tokens),0) AS m '
        'FROM usage_daily WHERE key_id = ? AND day >= ? GROUP BY model '
        'ORDER BY t DESC',
        (key_id, since_day),
    )
    out = []
    for row in rows:
        hit, miss = int(row['h'] or 0), int(row['m'] or 0)
        cached = hit + miss
        out.append({
            'model': row['name'],
            'requests': int(row['r'] or 0),
            'tokens': int(row['t'] or 0),
            'cache_hit_tokens': hit,
            'cache_miss_tokens': miss,
            'hit_rate': round(hit / cached, 4) if cached else None,
        })
    return out


def _usage_norm_model(raw) -> str:
    """模型 ID 规范化：剥 realm 前缀（cn:/global:）、转小写；空名归「未知」。"""
    name = str(raw or '').strip()
    if not name:
        return '未知'
    if ':' in name:
        name = name.rsplit(':', 1)[-1].strip()
    return name.lower()


def _usage_by_model_windows(key_id: int, today: str, d7: str, d30: str) -> dict:
    """规范模型 → {today, last_7d, last_30d}，字段口径与 _usage_window 完全一致。

    一次取全部分日×分模型行，Python 侧归并 realm 变体与大小写；分模型卡之和
    == 聚合段（空模型名的失败请求计入「未知」，对账不打架）。usage_daily 的
    主键本就含 model——上线之前的历史同样可分（需求信 B2：可回溯）。
    """
    rows = db.query(
        "SELECT day, COALESCE(NULLIF(model, ''), '未知') AS name, "
        'COALESCE(SUM(requests),0) AS r, '
        'COALESCE(SUM(prompt_tokens + completion_tokens),0) AS t, '
        'COALESCE(SUM(cache_hit_tokens),0) AS h, '
        'COALESCE(SUM(cache_miss_tokens),0) AS m '
        'FROM usage_daily WHERE key_id = ? AND day >= ? GROUP BY day, name',
        (key_id, d30),
    )
    acc = {}
    for row in rows:
        name = _usage_norm_model(row['name'])
        bucket = acc.setdefault(
            name, {'today': [0, 0, 0, 0], 'last_7d': [0, 0, 0, 0], 'last_30d': [0, 0, 0, 0]})
        for win, since in (('today', today), ('last_7d', d7), ('last_30d', d30)):
            if row['day'] >= since:
                cell = bucket[win]
                cell[0] += int(row['r'] or 0)
                cell[1] += int(row['t'] or 0)
                cell[2] += int(row['h'] or 0)
                cell[3] += int(row['m'] or 0)
    out = {}
    for name, bucket in acc.items():
        cards = {}
        for win in ('today', 'last_7d', 'last_30d'):
            r, t, h, m = bucket[win]
            cards[win] = {
                'requests': r,
                'tokens': t,
                'cache_hit_tokens': h,
                'cache_miss_tokens': m,
                'hit_rate': round(h / (h + m), 4) if (h + m) else None,
            }
        out[name] = cards
    return out


def _usage_live_model(key_id: int, window: int) -> dict:
    """规范模型 → 实时速度（口径同 _usage_live：只计 completion_tokens）。"""
    since = int(time.time()) - window
    rows = db.query(
        'SELECT model, COALESCE(SUM(completion_tokens),0) AS t FROM request_logs '
        'WHERE key_id = ? AND ts >= ? GROUP BY model',
        (key_id, since),
    )
    acc = {}
    for row in rows:
        name = _usage_norm_model(row['model'])
        acc[name] = acc.get(name, 0) + int(row['t'] or 0)
    return {
        name: {'window_seconds': window, 'tokens': tk, 'tokens_per_second': round(tk / window, 1)}
        for name, tk in sorted(acc.items())
    }


@router.get('/v1/usage')
async def usage_v1(request: Request):
    """按密钥查询用量：当天 / 近 7 天 / 近 30 天。

    调用方式：Authorization: Bearer <wbk_密钥>（也认 x-api-key 头）。
    返回 tokens = prompt + completion 合计；hit_rate = 缓存命中 token 占比，
    0~1 小数（无数据时为 null）；含义与 /api/stats 页面同源。
    live_window 查询参数可调实时速度窗口（秒，默认 3，钳制 2~300）；
    响应恒含 by_model（规范模型 → today/last_7d/last_30d 全窗口拆分）与
    live_by_model（规范模型 → 实时速度），新模型自动出现。
    """
    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')
    token = _bearer(request)
    if not token:
        _log_ip(ip, request.url.path, True, ua, 'missing_key')
        return _oai_error('缺少 API Key，请在 Authorization 头中提供 Bearer 令牌', 401,
                          'authentication_error', 'missing_api_key')
    key = keysvc.resolve(token)
    if not key:
        _log_ip(ip, request.url.path, True, ua, 'invalid_key')
        return _oai_error('API Key 无效', 401, 'authentication_error', 'invalid_api_key')
    if _global_ip_blocked(ip):
        _log_ip(ip, request.url.path, True, ua, 'ip_blocked')
        observability.mark_error('ip_blocked')
        await _record_async(key, ip, '', '', 403, 0, 0, 0, ua, 'IP 被拦截', False)
        return _oai_error('来源 IP 被安全策略拦截', 403, 'permission_error', 'ip_blocked')
    if not key['enabled']:
        return _oai_error('密钥已停用', 403, 'permission_error', 'key_disabled')
    if key['expires_at'] and key['expires_at'] < time.time():
        return _oai_error('密钥已过期', 403, 'permission_error', 'key_expired')
    today = time.strftime('%Y-%m-%d')
    # 实时速度窗口可调（秒）：默认 3 秒——请求完成瞬间数字尖跳，轮询越快越动感。
    # 客户端轮询间隔建议不超过窗口的一半，否则会踩空看不到跳（见 _usage_live 文档）。
    try:
        live_window = int(request.query_params.get('live_window', '3'))
    except (TypeError, ValueError):
        live_window = 3
    live_window = min(300, max(2, live_window))
    by_model = _usage_by_model_windows(key['id'], today, _usage_since(7), _usage_since(30))
    live_by_model = _usage_live_model(key['id'], live_window)
    # Totals and breakdowns describe the same read, and each history table is
    # scanned once instead of independently for each window and total.
    resp = {'key': {'name': key['name'], 'prefix': key['prefix']}}
    for window in ('today', 'last_7d', 'last_30d'):
        totals = {name: sum(windows[window][name] for windows in by_model.values())
                  for name in ('requests', 'tokens', 'cache_hit_tokens', 'cache_miss_tokens')}
        cached = totals['cache_hit_tokens'] + totals['cache_miss_tokens']
        totals['hit_rate'] = round(totals['cache_hit_tokens'] / cached, 4) if cached else None
        resp[window] = totals
    live_tokens = sum(row['tokens'] for row in live_by_model.values())
    resp['live'] = {'window_seconds': live_window, 'tokens': live_tokens,
                    'tokens_per_second': round(live_tokens / live_window, 1)}
    # by_model / live_by_model：恒返回的增量字段（需求信 A2，向后兼容）。
    resp['by_model'] = by_model
    resp['live_by_model'] = live_by_model
    for _name in resp['by_model']:
        resp['live_by_model'].setdefault(
            _name, {'window_seconds': live_window, 'tokens': 0, 'tokens_per_second': 0.0})
    return resp


@router.get('/v1/models')
async def list_models(request: Request):
    key, ip, err = _authorize(request, None, is_model_list=True)
    if err:
        return err
    # 模型列表也按密钥的上游取：不同上游的模型清单未必一致（版本 / 部署差异），
    # 拿默认上游的清单给一把绑定了别的上游的密钥，会给出它其实调不了的模型。
    upstream, upstream_err = _upstream_for(
        key, ip=ip, model='', mapped='', ua=request.headers.get('user-agent'))
    if upstream_err:
        return upstream_err
    started = time.time()
    try:
        async with config.http_client(30, connect=3) as client:
            resp = await client.get(f'{upstream["base_url"]}/v1/models',
                                    headers=await _upstream_headers_async(upstream))
        latency = int((time.time() - started) * 1000)
        from .. import portalpolicy
        if portalpolicy.is_portal(key):
            try:
                catalog = resp.json()
            except ValueError:
                catalog = None
            if (resp.status_code != 200 or not isinstance(catalog, dict) or catalog.get('error')
                    or not isinstance(catalog.get('data'), list)):
                await _record_async(key, ip, '', '', 502, 0, 0, latency, request.headers.get('user-agent'), '用户模型目录上游不可用', False)
                return _oai_error('上游模型目录暂不可用，请联系管理员检查公共池连接；不是用户密钥无效',
                                  502, 'api_error', 'portal_model_catalog_unavailable')
        await _record_async(key, ip, '', '', resp.status_code, 0, 0, latency, request.headers.get('user-agent'), None, False)
        request_platform = forced_platform.get() or 'workbuddy'
        payload = _scope_models(resp.json(), key, platform=request_platform)
        # 目录收缩（issue #239）：上游模型表可能仍列着腾讯已经下架的 id，
        # 客户端选中后才 503。模型中心直连腾讯的目录是当前可用的权威清单，
        # 取得到且来源是腾讯时按它裁一次；取不到就原样透传。
        # qoder/trae 的目录不是 workbuddy/腾讯目录；拿 workbuddy 目录裁它们
        # 会把整张列表清空（ZG-02）。只有 workbuddy 入口才走这次裁剪。
        if request_platform == 'workbuddy':
            payload = await _drop_models_outside_catalog(payload, key)
        # 能力字段兜底（issue #211）：随包上游的版本可能比面板旧 —— 补丁只在上游
        # 侧时，下游经面板拿到的清单仍缺字段，客户端会整条跳过模型。
        payload = _listing_compat_models(payload)
        payload = _bare_cn_model_ids(payload)
        if portalpolicy.is_portal(key):
            fresh = keysvc.resolve(_bearer(request))
            if fresh is None or fresh['id'] != key['id']:
                return _oai_error('用户密钥授权已失效，请重新登录核对', 401, 'authentication_error', 'invalid_api_key')
        # Anthropic 客户端（Claude Code 等）也会调这个路径，但期望的结构不同
        if request.headers.get('anthropic-version'):
            payload = _as_anthropic_models(payload)
        else:
            # 只在 OpenAI 形状上标注：Anthropic 的转换会重排结构，多出的字段留不住，
            # 白拉一趟没有意义。
            payload = await _annotate_other_platforms(payload, key, request_platform)
        return JSONResponse(payload, status_code=resp.status_code)
    except Exception as exc:  # noqa: BLE001
        latency = int((time.time() - started) * 1000)
        await _record_async(key, ip, '', '', 502, 0, 0, latency, request.headers.get('user-agent'), str(exc), False)
        return _oai_error_with_blip_body(_public_upstream_error(upstream), 502, exc)


async def _annotate_other_platforms(payload: object, key: dict | None,
                                    current_platform: str) -> object:
    """在模型清单旁标注「其它平台也能用」的模型。

    ## 为什么要它

    `/v1/models` 一直按**本次请求的上游**返回清单。这对「这个密钥此刻能看到什么」
    是准确的，但它回答不了另一个问题：**这个密钥到底还能用什么**。

    具体表现：豆包系列在 trae 平台上一直可用（实测文本、流式、推理内容都正常），
    但持有 workbuddy 入口密钥的人在模型列表里看不到它们 —— 于是这些能力事实上
    不存在，除非有人专门去读文档。

    ## 怎么标

    只在 `payload` 的**同级**加两个字段，`data` 数组一个字节都不动：

        payload['other_platforms'] = {'trae': ['Doubao-Seed-2.1-Turbo', ...]}
        payload['other_platforms_hint'] = '调用时加平台前缀…'

    这样「客户端拿 data 当模型选择器」的行为完全不变，而人或智能客户端读一眼就知道
    还有别的可选。**不把它们混进 data**：混进去会让客户端选中一个需要前缀的模型名，
    然后收到「不可用」，那比看不到更糟。

    ## 边界

    · 只列**该密钥有权限**的平台（`key.platforms`），不列它调不了的；
    · 拉不到就跳过（超时 2 秒），**绝不让某个平台不可用拖垮整个模型列表**；
    · 每个平台最多 80 个，避免异常上游把响应撑大；
    · `data` 不是列表时原样返回 —— 认不出的结构不动手。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        return payload
    allowed = set((key or {}).get('platforms') or ())
    others = [name for name in _THREEWAY_SPEC
              if name != current_platform and name in allowed]
    if not others:
        return payload
    extra: dict[str, list[str]] = {}
    for platform in others:
        upstream = _threeway_upstream(platform)
        if not upstream:
            continue
        try:
            async with config.http_client(2, connect=1) as client:
                resp = await client.get(f'{upstream["base_url"]}/v1/models',
                                        headers=await _upstream_headers_async(upstream))
            if resp.status_code != 200:
                continue
            items = (resp.json() or {}).get('data')
            if not isinstance(items, list):
                continue
            ids = [str(item.get('id')) for item in items
                   if isinstance(item, dict) and item.get('id')]
            if ids:
                extra[platform] = ids[:80]
        except Exception as exc:  # noqa: BLE001 - 标注是附加信息，不能连坐
            logger.debug('%s 模型清单不可用，跳过标注：%s', platform, exc)
    if not extra:
        return payload
    payload['other_platforms'] = extra
    payload['other_platforms_hint'] = (
        '这些模型在其它平台可用；调用时加平台前缀，例如 trae/Doubao-Seed-2.1-Turbo。')
    return payload


def _as_anthropic_models(payload: object) -> object:
    """把 OpenAI 形状的模型列表翻成 Anthropic 的形状。

    两边都叫 `/v1/models`，结构却完全不同：Anthropic 是
    `{data:[{type:"model", id, display_name, created_at}], has_more, first_id, last_id}`。
    Claude Code 按这个结构解析，形状不对会直接报错——所以只能在**同一个路径上
    按请求头分流**（用 `anthropic-version` 区分），而不能各注册一个路由
    （FastAPI 里先注册的会赢，另一个永远收不到请求）。

    认不出的结构**原样返回**：不在我们看不懂的响应上动手脚。
    """
    items = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return payload
    data = [
        {
            'type': 'model',
            'id': str(m.get('id') or ''),
            'display_name': str(m.get('name') or m.get('id') or ''),
            # 协议要求 ISO8601；上游给的是 created(epoch)，缺省时用纪元起点占位
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(m.get('created') or 0)),
        }
        for m in items
        if isinstance(m, dict) and m.get('id')
    ]
    return {
        'data': data,
        'has_more': False,
        'first_id': data[0]['id'] if data else None,
        'last_id': data[-1]['id'] if data else None,
    }


def _model_realm_of(m: object) -> str:
    """清单条目的版本：`global:` 前缀 = 国际版，其余 = 国内版。"""
    return 'global' if str((m or {}).get('id') or '').lower().startswith('global:') else 'cn'


def _alias_entries(items: list, allow: list[str]) -> list[dict]:
    """白名单里写的是**别名**时，补一条以别名为 id 的模型条目（issue #46）。

    别名（设置页「模型映射」）是给下游用的名字，它**不在上游模型清单里**。
    若只按清单字面裁剪，白名单填成别名的密钥会拿到**空列表**——而那个别名明明
    能调用（鉴权判的是请求里的名字，模型映射发生在鉴权**之后**）。空列表比不裁
    更糟：客户端会显示「没有可用模型」，用户看不出是自己把名字写成了别名。

    条目取别名所指模型的信息，只把 `id` 换成别名——**那才是客户端要发的名字**
    （发清单里的真名会被白名单拒掉）。

    解析走 `modelmap.target_for`（含兜底，issue #173）：配了兜底模型之后，白名单里
    任意名字**都调得通**，那就不该只在没有兜底时补条目 —— 否则客户端拿到的清单里
    缺了那个名字，用户会以为"白名单没生效"，而其实是清单没列。
    """
    mapping = db.get_setting('model_map', {}) or {}
    if not isinstance(mapping, dict) or not mapping:
        return []
    by_bare: dict[str, dict] = {}
    for m in items:
        if isinstance(m, dict) and m.get('id'):
            by_bare.setdefault(keysvc._bare_model(m.get('id')), m)
    out: list[dict] = []
    seen: set[str] = set()
    for raw in allow:
        name = keysvc._bare_model(raw)
        # 本身就是清单里的模型 → 上面的白名单裁剪已保留它，不重复添加
        if not name or name in seen or name in by_bare:
            continue
        target = modelmap.target_for(mapping, name)
        src = by_bare.get(keysvc._bare_model(target)) if target else None
        if not src:
            continue          # 别名指向的模型不在本版本清单里 → 调不通，不给
        out.append({**src, 'id': name})
        seen.add(name)
    return out


def _catalog_model_key(model: object) -> str:
    """模型名的跨源比对键：去掉 `cn:` / `global:` 前缀。

    两个来源的前缀口径不同：上游清单国际版带 `global:`，模型中心直连腾讯
    给的是裸名。同一把密钥只看一个版本，所以去掉前缀比对不会把两版同名
    模型混在一起（版本维度由 `_model_realm_of` 先筛过）。
    """
    raw = keysvc._bare_model(model)
    return raw[7:] if raw.lower().startswith('global:') else raw


def _filter_catalog_models(payload: object, key: dict | None,
                           realm: str, catalog_ids: set[str]) -> object:
    """按模型中心的实时目录裁掉已下架的 id（issue #239）。

    上游 `/v1/models` 的表里仍留着腾讯已经收缩掉的旧模型；下游把它当选择器，
    选中后才 503。模型中心直连腾讯的目录是当前可用的权威清单，取得到时以它
    为准。白名单里的**别名**要继续保留（它指向的模型仍在目录里）。
    """
    if not isinstance(payload, dict) or not catalog_ids:
        return payload
    items = payload.get('data')
    if not isinstance(items, list):
        return payload
    key = key or {}
    mapping = db.get_setting('model_map', {}) or {}
    aliases: set[str] = set()
    for raw in key.get('models') or []:
        name = keysvc._bare_model(raw)
        target = modelmap.target_for(mapping, name)
        if target and _catalog_model_key(target) in catalog_ids:
            aliases.add(_catalog_model_key(name))
    kept: list = []
    for m in items:
        if not isinstance(m, dict) or not m.get('id'):
            kept.append(m)
            continue
        if _model_realm_of(m) != realm:
            kept.append(m)
            continue
        mid = _catalog_model_key(m.get('id'))
        if mid in catalog_ids or mid in aliases:
            kept.append(m)
    return {**payload, 'data': kept}


async def _drop_models_outside_catalog(payload: object, key: dict | None) -> object:
    """取该版本的模型中心目录并裁掉已下架模型；取不到就原样返回。

    目录走 `modelcatalog.catalog()`（5 分钟缓存，来源可能是直连腾讯，也可能
    是上游回退）。**只有来源是腾讯时才裁**：上游回退本身就是同一份旧表，
    拿它裁等于没裁。超时/异常一律放弃裁剪，不能因为目录取不到就把列表清空。
    """
    if not isinstance(payload, dict):
        return payload
    realm = keysvc._norm_realm((key or {}).get('realm'))
    if realm not in ('cn', 'global'):
        return payload
    try:
        cat = await asyncio.wait_for(modelcatalog.catalog(realm), timeout=5)
    except Exception as exc:  # noqa: BLE001
        logger.debug('模型目录裁剪跳过：%s', exc)
        return payload
    if cat.get('source') != 'tencent':
        return payload
    ids = {_catalog_model_key(m.get('id')) for m in (cat.get('models') or [])
           if isinstance(m, dict) and m.get('id')}
    return _filter_catalog_models(payload, key, realm, ids)


def _scope_models(payload: object, key: dict | None, *, platform: str = 'workbuddy') -> object:
    """按密钥的**版本**与**模型白名单**裁剪模型列表。

    这个接口要回答的是「**你能用**什么模型」——客户端普遍拿它当模型选择器
    （issue #46）。所以两维都得裁：只裁版本会出现「列表里有、一选就失败」
    （白名单外的模型照旧列出来，选中就 400）。

    白名单判据与调用侧**共用 `keysvc.model_allowed`**，不另写一份：两处各写一遍
    迟早漂移，而漂移的表现正是这个函数要修的问题。白名单为空 = 不限制，这一维
    不裁（与调用侧语义一致）。

    结构不是预期的 `{data: [...]}` 时**原样透传**——配额耗尽之类的判断
    不该因为我们认不出结构就去改动上游的响应。
    """
    key = key or {}
    from .. import portalpolicy
    if portalpolicy.is_portal(key):
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            return {'object': 'list', 'data': []}
        return {**payload, 'data': [item for item in payload['data'] if isinstance(item, dict)
            and portalpolicy.model_allowed(item.get('id'))
            and portalpolicy.model_allowed(_map_model(item.get('id')))]}
    # qoder/trae 的模型 id 不属于 workbuddy 的 cn/global realm；对它们做
    # realm 裁剪会把整张清单清空。白名单维度仍然保留（那是密钥授权）。
    want = '' if platform in ('qoder', 'trae') else keysvc._norm_realm(key.get('realm'))
    allow = [str(x) for x in (key.get('models') or []) if str(x).strip()]
    if not want and not allow:
        return payload
    if not isinstance(payload, dict):
        return payload
    items = payload.get('data')
    if not isinstance(items, list):
        return payload

    in_realm = [m for m in items
                if isinstance(m, dict) and (not want or _model_realm_of(m) == want)]
    if not allow:
        return {**payload, 'data': in_realm}

    scoped = [m for m in in_realm if keysvc.model_allowed(key, m.get('id'))]
    # 别名：白名单可能写的是下游熟悉的别名，它不在清单里，要补进来
    scoped += _alias_entries(in_realm, allow)
    return {**payload, 'data': scoped}


def _bare_cn_model_ids(payload: object) -> object:
    """Normalize display IDs only after realm, whitelist and catalog filtering."""
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        return payload
    result, positions = [], {}
    for item in payload['data']:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            result.append(item)
            continue
        model = item['id']
        normalized = model[3:] if (model.lower().startswith('cn:') and model[3:]
                                   and ':' not in model[3:]) else model
        entry = {**item, 'id': normalized}
        if normalized not in positions:
            positions[normalized] = len(result)
            result.append(entry)
        elif len(entry) > len(result[positions[normalized]]):
            result[positions[normalized]] = entry
    return {**payload, 'data': result}


def _listing_compat_models(payload: object) -> object:
    """补齐第三方模型目录客户端要读的能力字段（issue #211）。

    上游从 #156 起在它自己的 `/v1/models` 里补了 `modalities` /
    `supported_reasoning_levels`。但面板是**独立进程**，随包分发的上游可能比面板
    旧（补丁只打在上游），于是下游经面板拿到的清单仍缺字段 —— 报告人的 Magpie
    就是这样整条跳过模型、看不到多模态与推理档位的。

    这里在面板侧再补一遍，口径与上游的 `addListingCompat` 一致，且**只增不改**：

      · `modalities`：缺字段时补 `{"input": ["text"] + (["image"] if
        supports_images)}`。文本是所有模型都有的能力；缺整个字段会让客户端
        直接跳过（这正是 #211 的现场）；
      · `supported_reasoning_levels`：缺字段且有 `reasoning_supported_efforts`
        时派生成 `[{"effort": e}]`；没有档位就不编造；
      · `supports_reasoning`：有档位而字段缺失时补 `true`。

    认不出的结构（不是 `{data:[...]}`）原样透传 —— 不在看不懂的响应上动手脚。
    """
    if not isinstance(payload, dict):
        return payload
    items = payload.get('data')
    if not isinstance(items, list):
        return payload
    out: list = []
    for m in items:
        if not isinstance(m, dict) or not m.get('id'):
            out.append(m)
            continue
        entry = dict(m)
        if 'modalities' not in entry:
            inputs = ['text']
            if entry.get('supports_images') is True:
                inputs.append('image')
            entry['modalities'] = {'input': inputs}
        efforts = entry.get('reasoning_supported_efforts')
        if 'supported_reasoning_levels' not in entry and isinstance(efforts, list):
            levels = [{'effort': e} for e in efforts if isinstance(e, str) and e]
            if levels:
                entry['supported_reasoning_levels'] = levels
                entry.setdefault('supports_reasoning', True)
        out.append(entry)
    return {**payload, 'data': out}


# ── 对话补全（v1 / v2）──────────────────────────────────
# ── 本地补丁：会话键注入（提升上游缓存命中）────────────────
# 背景：ZCode 等 OpenAI 兼容客户端 body 不带 conversation_id，manager 转发又不
# 透传 x-session-id 头 → 上游会话粘性路由不启动、每请求随机换号，
# prompt_cache_key（wb2a-<uid8>-<会话>）随账号变化 → 上游前缀缓存全 miss。
# 方案：把客户端 x-session-id（会话内稳定）注入 body 的 conversation_id，令
# 「同会话 → 同账号 → 稳定缓存键」生效。客户端已带会话键时不覆盖。
# 维护：这段逻辑**已经收编在源码里**，不再有任何启动期补丁去改写 server/。
# `/opt/workbuddy-manager/local-patches/apply_patches.sh` 现在只是
# `deploy/source_preflight.py` 的入口（每次启动按 Ed25519 清单复核发布树），
# 日志 `managed source preflight passed: N files; ... startup patches disabled`。
# 2026-10-28 之前这里写的是「启动时自动重施」，已过时。
_SESSION_HEADERS = ('x-session-id', 'x-conversation-id', 'conversation-id')


def _session_key_source(body: dict) -> str:
    meta = body.get('metadata')
    for container, fields in ((meta, ('conversation_id', 'conversationId')),
                              (body, ('conversation_id', 'conversationId', 'prompt_cache_key'))):
        if isinstance(container, dict):
            for field in fields:
                value = container.get(field)
                if isinstance(value, str) and value.strip():
                    return value
    return ''


def _session_key_digest(body: dict) -> str | None:
    """Log only a digest, using the engine's explicit session-key precedence."""
    import hashlib
    value = _session_key_source(body)
    return hashlib.sha256(value.strip().encode('utf-8', 'ignore')).hexdigest()[:16] if value else None


def _stable_session_fallback(body: 'dict') -> str:
    """客户端不带会话头时，用 body 的稳定前缀生成会话键。

    优先取**首条非 system 消息**：system 常带日期/环境等动态内容，逐请求变化会让
    会话键不稳定（等于没粘住）；用户消息在会话内稳定。没有用户消息时才退回 system。
    """
    import hashlib
    msgs = body.get('messages')
    if not isinstance(msgs, list) or not msgs:
        return ''
    picked = []
    for msg in msgs[:6]:
        if isinstance(msg, dict) and str(msg.get('role') or '') != 'system':
            picked = [msg]
            break
    if not picked:
        picked = [m for m in msgs[:1] if isinstance(m, dict)]
    pieces = []
    for msg in picked:
        content = msg.get('content')
        if isinstance(content, str):
            pieces.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get('text'), str):
                    pieces.append(part['text'])
    if not pieces:
        return ''
    digest = hashlib.sha256(chr(0).join(pieces).encode('utf-8', 'ignore')).hexdigest()[:16]
    return 'wb-' + digest


def _inject_session_key(body: 'dict', request: Request) -> None:
    if not isinstance(body, dict):
        return
    if _session_key_source(body):
        return
    for name in _SESSION_HEADERS:
        v = (request.headers.get(name) or '').strip()
        if v:
            body['conversation_id'] = v
            return
    # 兜底：客户端不带会话头时用 body 稳定前缀生成会话键（缓存命中关键）。
    fallback = _stable_session_fallback(body)
    if fallback:
        body['conversation_id'] = fallback



def _forwarding_identity(key,upstream):
    import hashlib
    token=upstreamsvc.forward_api_key(upstream)
    return str(key.get('id'))+'|'+str(upstream.get('base_url'))+'|'+hashlib.sha256(token.encode()).hexdigest()

def _prepare_forward(key,body, *, reserve=None, check_cancelled=None, defer_forwarded=False):
    from .. import budget, ledger, outputlimits, wirebody
    from .. import requestperf
    # K27：Anthropic / Responses 两个入口自己解析上游（不在本文件），所以它们
    # 路径上没有 `_chat` 的精确归因。这里补一个**粗归因**（平台 + 密钥绑定上游
    # 的 id，没有 host），保证三条协议都有 actual_platform/upstream_ref 可查；
    # 若上游对象已在 `_chat` 里精确记录过则不覆盖。
    state = observability.current.get()
    if state is not None and getattr(state, 'actual_platform', None) is None:
        upstream_id = key.get('upstream_id') if isinstance(key, dict) else None
        _stamp_route({'id': upstream_id} if upstream_id else None,
                     db._clean(body.get('model') if isinstance(body, dict) else None, 128),
                     db._clean(body.get('model') if isinstance(body, dict) else None, 128))
    try:
        platform = getattr(state, 'actual_platform', None) if state is not None else None
        defuse_prompt_fingerprint(body, platform=platform or 'workbuddy')
        limit_field, _, limit_before = outputlimits.inspect(body)
        reserve = reserve if reserve is not None else getattr(state, 'reserve_wire_body', None)
        with requestperf.measure('json_encode'):
            wire = wirebody.encode(body, max_forward_body_bytes(), reserve=reserve)
        if check_cancelled is not None:
            check_cancelled()
        # U05（2026-10-28）：`budget.prepare` 会在 token 配额不足时**收缩**输出
        # 上限（`body[field] = min(output, remaining - estimated_input)`），但那条
        # 路径不产生任何客户端可见信号 —— 客户端以为额度是 max_tokens，实际拿到
        # 更小的值，表现为「回答莫名被截断」。记录收缩契约，并重新编码最终转发体，
        # 确保实际发送的字节与校验及配额调整一致。三条协议共用此处。
        with requestperf.measure('budget_prepare'):
            budget.prepare(key,body,encoded_body=wire)
        if body.get(limit_field) != limit_before:
            with requestperf.measure('json_encode'):
                wire = wirebody.encode(body, max_forward_body_bytes(), reserve=reserve)
        if check_cancelled is not None:
            check_cancelled()
        if isinstance(body, dict):
            observability.record_max_tokens_contract(
                observability.current.get(), limit_field, limit_before,
                body.get(limit_field))
        state=observability.current.get()
        if state:
            import hashlib
            from ..services import wb2api
            values=wb2api.load_upstream_config()
            safe={name:values.get(name) for name in ('pool','cooldown','features','session_sticky','server','admin')}
            safe['prompt_mode']=(values.get('prompt') or {}).get('mode')
            safe['global_enabled']=(values.get('global') or {}).get('enabled')
            state.config_hash=hashlib.sha256(json.dumps(safe,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            state.upstream_id=str(key.get('upstream_id') or 'default')
            state.resolved_model=db._clean(body.get('model'),128)
            state.realm=db.realm_of_model(state.resolved_model)
            changed=state.resolved_model != getattr(state,'requested_model',None)
            state.transform=json.dumps({'protocol_bridge':state.protocol if state.protocol in ('anthropic','responses') else None,'model_alias':changed},separators=(',',':'))
            ledger.forwarding(state,'prepared')
        if not defer_forwarded:
            budget.forwarded()
    except outputlimits.Invalid as exc:
        observability.mark_error(exc.code)
        return _oai_error(str(exc), 400, 'invalid_request_error', exc.code)
    except wirebody.TooLarge:
        observability.mark_error('gateway.request.forward_too_large')
        return _oai_error('协议转换后的请求体超过转发上限，请精简上下文或附件；'
                          '本端 WB_GATEWAY_MAX_FORWARD_BODY_MB 与所选上游需分别核验',
                          413,'invalid_request_error','forward_payload_too_large')
    except wirebody.Invalid:
        observability.mark_error('gateway.request.invalid')
        return _oai_error('请求转换后不是可转发的合法 JSON',400,
                          'invalid_request_error','invalid_forward_json')
    except observability.CapacityError:
        response = _oai_error('入站与转发请求体容量已满，请稍后重试',503,
                              'server_error','gateway_capacity_exceeded')
        response.headers['Retry-After'] = '1'
        return response
    except budget.BudgetRejected as exc:
        # U21：预算拒绝的原因本身就是稳定码（budget_pending / insufficient_quota /
        # credit_quota_exhausted / ledger_backpressure …），直接沿用。
        observability.mark_error(getattr(exc, 'code', None))
        # 0.1.0 / P-4：带退避提示的拒绝走错误体通道。
        # 响应头 Retry-After 在 nginx 反代链路上透传不稳（本文件 118-119 行已有
        # 同样结论），所以以 body 里的 retry_after_seconds 为准，头部只做双通道补充。
        _retry_after = getattr(exc, 'retry_after', None)
        if _retry_after:
            _err_type = 'server_error' if exc.status >= 500 else 'insufficient_quota'
            _body = {'error': {'message': str(exc), 'type': _err_type,
                               'code': exc.code,
                               'retry_after_seconds': int(_retry_after)}}
            _resp = JSONResponse(_body, status_code=exc.status)
            _resp.headers['Retry-After'] = str(int(_retry_after))
            return _resp
        return _oai_error(str(exc),exc.status,'server_error' if exc.status >= 500 else 'insufficient_quota',exc.code)
    if state and getattr(state, 'performance', None):
        state.performance.size('forward_bytes', len(wire))
    return wire


#: 准入字节预留的阶梯。编码器每 64 KiB flush 一次；若每次 flush 都去协调器走一个
#: 事务，一个 8 MiB 请求就是 128 次事务（每次 `BEGIN IMMEDIATE` + 提交），外加 128
#: 次跨线程往返。改成只在**跨过阶梯**时真正预留，中间块只更新本地计数：协调器上的
#: 预留仍然 ≥ 实际字节，最多多留一个阶梯（4 倍），对 384 MiB 的池子可以接受。
_RESERVE_STEPS = (64 << 10, 256 << 10, 1 << 20, 4 << 20, 16 << 20, 64 << 20)


def _reserve_step(size: int) -> int:
    """覆盖 `size` 的最小阶梯；超过最大阶梯时按实际值预留。"""
    for step in _RESERVE_STEPS:
        if step >= size:
            return step
    return size


async def _prepare_forward_async(key, body, *, request=None):
    """Keep blocking JSON/SQLite work off the loop; admission stays loop-owned."""
    from concurrent.futures import Future
    import threading
    from .. import requestperf
    from ..services.asyncwork import joined_task
    loop = asyncio.get_running_loop()
    state = observability.current.get()
    original_reserve = getattr(state, 'reserve_wire_body', None)
    output_reserve=getattr(state,'reserve_output_body',None)
    if output_reserve is not None:
        from ..outputlimits import maximum_bytes, FAILURE_ENVELOPE_BYTES, declared_output_bytes
        try:
            # 输出承诺按**这个请求实际能产出多少**来算，而不是一律按上限。
            #
            # 原先每个请求都预留 `2 × 最大输出 + 信封`（本机 32 MiB）。那对一次
            # 一万六千 token 的长回答是合理的，但对 `max_tokens: 100` 的短问答
            # 就是三十多兆的空占 —— 而它决定并发：384 MiB ÷ 32 MiB = 12 个。
            # 按声明值算之后，短请求只占几百 KB，并发回到由连接数决定。
            #
            # 声明值不可信时（缺省、非法、超上限）退回上限值 —— 宁可少放几个，
            # 也不能让一个自称"只要 10 个 token"的请求把预算骗过去。
            output_reserve(declared_output_bytes(body) + FAILURE_ENVELOPE_BYTES)
        except observability.CapacityError:
            # 这是**内存预算**到顶，不是并发数、也不是配额。三者被拒时用户看到的
            # 都是 503，所以消息必须自己说清是哪一种，否则排障会往错的方向走。
            #
            # 2026-11-14 实测：某密钥在 12 分钟内被拒 20 次，全是这一条。它当时的
            # 并发约 59、单个请求并不大 —— 是「很多长输出同时在途」把预留吃满了。
            # 原来的文案只说「响应容量已满」，用户无法据此做任何事。
            #
            # 加两样东西：
            #   · `gateway_hint`：告诉调用方这是容量保护、做什么有用；
            #   · 一行带占用的日志：事后能区分「并发多」还是「单个请求声明得太大」。
            snap = observability.admission_snapshot() or {}
            held = snap.get('inflight_body_bytes')
            ceiling = snap.get('maximum_body_bytes')
            logger.warning('输出预留被拒 key_id=%s 声明输出=%s 在途字节=%s/%s',
                           (key or {}).get('id'), declared_output_bytes(body),
                           held, ceiling)
            response = _oai_error(
                '响应容量已满，请稍后重试', 503, 'server_error',
                'gateway_capacity_exceeded',
                hint=('in-flight response reservations reached the memory ceiling; '
                      'this is a capacity guard, not a key, quota or model problem. '
                      'Retry shortly, or lower the concurrency of long-output requests.'))
            response.headers['Retry-After'] = '1'
            return response
    cancelled = threading.Event()
    reserved = 0

    def check_cancelled():
        if cancelled.is_set():
            raise asyncio.CancelledError()

    def reserve(size):
        nonlocal reserved
        check_cancelled()
        if original_reserve is None or size <= reserved:
            return
        # 只在跨过阶梯时才真去协调器预留；中间的 64 KiB 块只更新本地计数。
        target = _reserve_step(size)
        if target <= reserved:
            reserved = size
            return
        # The encoder flushes exact blocks, not one callback per JSON token.
        result = Future()
        def apply():
            try:
                check_cancelled()
                original_reserve(target)
            except BaseException as error:
                result.set_exception(error)
            else:
                result.set_result(None)
        loop.call_soon_threadsafe(apply)
        result.result()
        reserved = target

    def work():
        with requestperf.measure('forward_prepare'):
            result = _prepare_forward(key, body, reserve=reserve, check_cancelled=check_cancelled,
                                      defer_forwarded=True)
        requestperf.mark('forward_prepared')
        return result

    def validate_dispatch(result):
        check_cancelled()
        if request is not None and not isinstance(result, JSONResponse):
            # A key can be revoked/rotated while blocking preparation is running.
            # Recheck without consuming a second rate/concurrency allowance.
            if state is not None and state.protocol == 'anthropic':
                from .anthropic import _resolve_key
                fresh, _ = _resolve_key(request)
            else:
                fresh = keysvc.resolve(_bearer(request))
            if fresh is None or fresh['id'] != key['id']:
                return _oai_error('API Key 已撤销或已更换', 401, 'authentication_error', 'invalid_api_key')
            requested = getattr(state, 'requested_model', None) if state is not None else body.get('model')
            try:
                platform = platformpolicy.select(request.scope.get('zhihui.original_path', request.url.path),
                                                  request.headers.get('x-qtg-platform', ''), requested)
            except platformpolicy.PlatformConflict as error:
                return _oai_error(str(error), 400, 'invalid_request_error', error.code)
            ip = iputil.client_ip(request)
            reason = keysvc.validate(fresh, ip, requested, mapped_model=body.get('model'), platform=platform)
            if reason:
                observability.mark_error(reason.code)
                return _oai_error(reason, reason.status, reason.err_type, reason.code)
            if _global_ip_blocked(ip):
                return _oai_error('来源 IP 被安全策略拦截', 403, 'permission_error', 'ip_blocked')
            if fresh.get('upstream_id') != key.get('upstream_id'):
                return _oai_error('密钥路由在准备期间已变更，请重试', 409,
                                  'invalid_request_error', 'key_configuration_changed')
            check_cancelled()
            reason = keysvc.claim_ip(fresh, ip)
            if reason:
                observability.mark_error(reason.code)
                return _oai_error(reason, reason.status, reason.err_type, reason.code)
        check_cancelled()
        if not isinstance(result, JSONResponse):
            from .. import budget
            try:
                budget.forwarded()
            except budget.BudgetRejected as error:
                observability.mark_error(error.code)
                return _oai_error(str(error), error.status, 'server_error' if error.status >= 500 else 'insufficient_quota', error.code)
        return result

    task = asyncio.create_task(asyncio.to_thread(work))
    try:
        result = await asyncio.shield(task)
        if isinstance(result, JSONResponse):
            return result
        # Revalidation, IP binding and durable dispatch marking can wait for the
        # SQLite writer. Keep that wait off the event loop, while retaining the
        # same joined cancellation ownership as request encoding above.
        task = asyncio.create_task(asyncio.to_thread(validate_dispatch, result))
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        # Never release body bytes or key/budget ownership while a worker lives.
        try:
            await joined_task(task)
        except BaseException:
            pass
        raise

async def _chat(request: Request, upstream_path: str):
    body, err = await _read_json_body(request)
    if err:
        return err

    _inject_session_key(body, request)
    # 观测：把最终会话键的 sha256 前 16 位落库，用于验证「同会话是否固定账号」。
    session_key = _session_key_digest(body) if isinstance(body, dict) else None

    requested_model = body.get('model') if isinstance(body, dict) else None
    # model 必须是字符串：非字符串（对象/数组/数字）会让后面的 _map_model 与
    # 上游处理出现意外行为（历史上有过 dict 触发 dict.get 未哈希 → 500）。
    # 这里直接拒掉，也顺带让模型白名单的判定有确定的输入。
    if requested_model is not None and not isinstance(requested_model, str):
        return _oai_error('model 必须是字符串', 400, 'invalid_request_error', 'invalid_model')
    # 映射**先于鉴权**：版本归属判的是实际要用的名字（issue #47），配了
    # 「别名 → 带前缀的真名」的密钥此前会被 realm 检查当成错版本打回。
    mapped = _map_model(requested_model)
    key, ip, err = _authorize(request, requested_model, mapped=mapped)
    if err:
        return err

    if mapped:
        body['model'] = mapped

    from .. import outputlimits
    try:
        outputlimits.inspect(body)
    except outputlimits.Invalid as exc:
        observability.mark_error(exc.code)
        return _oai_error(str(exc), 400, 'invalid_request_error', exc.code)

    # 越界的 max_tokens 会被上游包装成 503（见 clamp_max_tokens 的说明），
    # 鉴权后、转发前压回上限。
    clamp_max_tokens(body)

    # 图片 part 归一（issue #134）：input_image / image_url 字符串 / Anthropic
    # source 形态改写成上游认的标准形态；认不出的原样保留。
    normalize_chat_images(body)
    # 思考强度（reasoning_effort）：仅用于日志展示，不改写请求体。
    effort = None
    if isinstance(body, dict):
        raw_effort = body.get('reasoning_effort')
        if isinstance(raw_effort, str) and raw_effort.strip():
            effort = raw_effort.strip()[:16]

    stream = bool(isinstance(body, dict) and body.get('stream'))
    if stream:
        # 让上游在最后一个 chunk 返回 usage，便于精确计量
        body.setdefault('stream_options', {})
        if isinstance(body['stream_options'], dict):
            body['stream_options'].setdefault('include_usage', True)

    ua = request.headers.get('user-agent')
    upstream, upstream_err = _upstream_for(key, ip=ip, model=requested_model or '',
                                           mapped=mapped or '', ua=ua)
    if upstream_err:
        return upstream_err
    # K27：路由此刻已确定 —— 先把实际平台/上游记进上下文，这样即使后面被预算
    # 拒绝（没有发出请求），归因也是准的。
    _stamp_route(upstream, requested_model or '', mapped or '')
    from .. import metering
    actual_platform = metering.route_attribution(
        entry_platform=forced_platform.get(), requested_model=requested_model,
        resolved_model=mapped or requested_model, upstream=upstream)['actual_platform']
    url = f'{upstream["base_url"]}{upstream_path}'
    wire = await _prepare_forward_async(key,body, request=request)
    if isinstance(wire, JSONResponse):
        return wire
    started = time.time()
    # K21：五类时间预算（连接 / 首包 / 流中空闲 / 总时长 / 客户端取消）。
    # 默认值不改变线上行为（见 deadline.Budgets.from_config 的说明）。
    from .. import deadline as deadline_mod
    # U13：三条协议共用同一套预算对象（对照表见 deadline 模块 docstring）。
    budgets = deadline_mod.request_budgets(config.UPSTREAM_TIMEOUT)
    request_deadline = deadline_mod.Deadline(budgets)
    _timeouts = deadline_mod.transport_timeouts(budgets)

    if not stream:
        try:
            # K21：只在**生成尚未开始**的阶段允许重试（连接/发送失败）。
            # 默认 WB_SAFE_RETRY_MAX=1 = 不重试，与改动前完全一致；开启后也只会在
            # 安全阶段重试 —— 一旦拿到响应就不再重放。
            policy = deadline_mod.RetryPolicy.from_config()
            attempt = 1
            while True:
                try:
                    async with observability.AuditedClientContext(config.http_client(_timeouts['timeout'], connect=_timeouts['connect'], identity=_forwarding_identity(key,upstream), bounded_response=True)) as client:
                        resp = await deadline_mod.buffered_post(client,url,content=wire,
                            headers=await _upstream_headers_async(upstream),deadline=request_deadline)
                    # 2026-10-28：上游「明确拒绝且未处理」的有界重试。
                    # 判据见 _is_not_processed_rejection()：这类响应没有生成、
                    # 没有 usage、没有计费，重放安全。
                    # 先按状态码短路：2xx 连 `.text` 都不碰（热路径）。
                    budget_available = _consume_attempt_budget(resp)
                    rejection_limit = _rejection_retry_limit()
                    if (budget_available and attempt < rejection_limit
                            and resp.status_code in _REJECTION_RETRY_CODES
                            and _is_not_processed_rejection(resp.status_code,
                                                            _response_text(resp))):
                        attempt += 1
                        logger.warning('上游明确拒绝且未处理 status=%s，重试 attempt=%s/%s',
                                       resp.status_code, attempt, rejection_limit)
                        await deadline_mod.await_total(asyncio.sleep(_REJECTION_RETRY_DELAY_S), request_deadline)
                        continue
                    break
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except Exception as exc:  # noqa: BLE001
                    phase = deadline_mod.failure_phase(exc)
                    decision = deadline_mod.decide_retry(
                        policy, phase=phase, attempt=attempt,
                        generated_started=False, error=exc)
                    if not decision.retry:
                        raise
                    attempt = decision.next_attempt
                    logger.warning('连接或连接池获取失败，重试 attempt=%s/%s error=%s',
                                   attempt, policy.max_attempts, type(exc).__name__)
                    await deadline_mod.await_total(asyncio.sleep(decision.delay_s), request_deadline)
            state=observability.current.get()
            if state:
                state.account=(getattr(resp,'headers',{}) or {}).get('x-wb-account-uid')
            latency = int((time.time() - started) * 1000)
            usage = {}
            try:
                data = resp.json()
                usage = _response_usage(data)
            except Exception:
                data = None
            pt = observability.usage_int(usage.get('prompt_tokens'))
            ct = observability.usage_int(usage.get('completion_tokens'))
            if resp.status_code < 400:
                try:
                    if not _valid_chat_response(data):
                        raise ValueError('invalid upstream chat response')
                    converted = JSONResponse(data, status_code=resp.status_code)
                except Exception as exc:
                    _mark_invalid_response()
                    await _record_async(key, ip, requested_model or '', mapped or '', 502, pt, ct,
                            latency, ua, str(exc), False, usage=usage,
                            effort=effort, session_key=session_key)
                    return _oai_error('上游返回了无法解析的响应', 502, 'api_error', 'upstream_invalid_body')
            error = None if resp.status_code < 400 else (str(data)[:500] if data is not None else resp.text[:500])
            if error:
                # U21：上游业务码优先（`error.code`，如 11102 / no_healthy_account）。
                # 取不到就留空，让 outcome 侧按终态/阶段/状态码做网关分类 —— 不猜。
                upstream_code = None
                if isinstance(data, dict) and isinstance(data.get('error'), dict):
                    upstream_code = data['error'].get('code')
                if upstream_code is not None:
                    observability.mark_error(upstream_code, observability.ERROR_SOURCE_UPSTREAM)
            await _record_async(
                key, ip, requested_model or '', mapped or '', resp.status_code, pt, ct,
                latency, ua, error, False, usage=usage, effort=effort, session_key=session_key,
            )
            if resp.status_code < 400:
                return converted
            if data is not None:
                return JSONResponse(data, status_code=resp.status_code)
            return JSONResponse({'error': {'message': resp.text[:1000], 'type': 'api_error'}}, status_code=resp.status_code)
        except OutputLimitExceeded:
            _mark_output_limit_failure()
            message = 'Upstream output exceeds the configured byte limit'
            await _record_async(key, ip, requested_model or '', mapped or '', 502, 0, 0,
                    int((time.time() - started) * 1000), ua, message, False,
                    effort=effort, session_key=session_key)
            return _oai_error(message, 502, 'api_error', 'upstream_output_limit')
        except deadline_mod.BudgetExceeded as exc:
            _mark_pre_stream_failure(exc)
            failure = deadline_mod.classify_budget_exceeded(exc)
            await _record_async(key, ip, requested_model or '', mapped or '', 502, 0, 0,
                    int((time.time() - started) * 1000), ua, failure.message, False,
                    effort=effort, session_key=session_key)
            return _oai_error(failure.message, 502, 'api_error', 'upstream_timeout')
        except Exception as exc:  # noqa: BLE001
            latency = int((time.time() - started) * 1000)
            await _record_async(key, ip, requested_model or '', mapped or '', 502, 0, 0, latency, ua, str(exc), False, effort=effort, session_key=session_key)
            return _oai_error_with_blip(_public_upstream_error(upstream), 502, exc)

    # 流式转发
    client = config.http_client(_timeouts['timeout'], connect=_timeouts['connect'], identity=_forwarding_identity(key,upstream), bounded_response=True)
    rejection_limit = _rejection_retry_limit()
    attempt = 1
    resp = None
    handed_off = False
    try:
        while True:
            req = client.build_request('POST', url, content=wire, headers=await _upstream_headers_async(upstream))
            resp = await deadline_mod.await_headers(
                client.send(req, stream=True), request_deadline)
            budget_available = _consume_attempt_budget(resp)
            if resp.status_code < 400:
                handed_off = True
                break
            early = await deadline_mod.read_error_body(resp, request_deadline, preserve_status=True)
            text = early.decode('utf-8', 'replace')
            if budget_available and attempt < rejection_limit and _is_not_processed_rejection(resp.status_code, text):
                # Each attempt owns its client facade; transport pooling is shared.
                await _close_pending_response(resp, client, budgets)
                resp = None
                attempt += 1
                await deadline_mod.await_total(asyncio.sleep(_REJECTION_RETRY_DELAY_S), request_deadline)
                client = config.http_client(_timeouts['timeout'], connect=_timeouts['connect'],
                                            identity=_forwarding_identity(key, upstream), bounded_response=True)
                continue
            try:
                data = json.loads(early)
            except (ValueError, TypeError):
                data = None
            usage = _response_usage(data)
            if isinstance(data, dict) and isinstance(data.get('error'), dict):
                observability.mark_error(data['error'].get('code'), observability.ERROR_SOURCE_UPSTREAM)
            state = observability.current.get()
            if state:
                state.account = resp.headers.get('x-wb-account-uid')
            await _record_async(key, ip, requested_model or '', mapped or '', resp.status_code,
                    observability.usage_int(usage.get('prompt_tokens')),
                    observability.usage_int(usage.get('completion_tokens')),
                    int((time.time() - started) * 1000), ua, text[:500], True,
                    usage=usage, effort=effort, session_key=session_key)
            return Response(early, status_code=resp.status_code,
                            media_type=resp.headers.get('content-type', 'application/json'))
    except (asyncio.CancelledError, GeneratorExit) as exc:
        _mark_pre_stream_failure(exc)
        await _record_async(key, ip, requested_model or '', mapped or '', 499, 0, 0,
                int((time.time() - started) * 1000), ua, 'client cancelled before stream', True,
                effort=effort, session_key=session_key)
        raise
    except deadline_mod.BudgetExceeded as exc:
        _mark_pre_stream_failure(exc)
        failure = deadline_mod.classify_budget_exceeded(exc)
        await _record_async(key, ip, requested_model or '', mapped or '', 502, 0, 0,
                int((time.time() - started) * 1000), ua, failure.message, True,
                effort=effort, session_key=session_key)
        return _oai_error(failure.message, 502, 'api_error', 'upstream_timeout')
    except Exception as exc:
        _mark_pre_stream_failure(exc)
        await _record_async(key, ip, requested_model or '', mapped or '', 502, 0, 0,
                int((time.time() - started) * 1000), ua, str(exc), True,
                effort=effort, session_key=session_key)
        return _oai_error_with_blip(_public_upstream_error(upstream), 502, exc)
    finally:
        if not handed_off:
            await _close_pending_response(resp, client, budgets)

    status_code = resp.status_code
    content_type = resp.headers.get('content-type', 'text/event-stream')
    # U13：流中错误帧要带上游请求 ID（流开始后状态码已定，ID 是客户端唯一
    # 还能拿来对工单的线索）。取不到就是 None，不编造。
    upstream_request_id = _upstream_request_id(resp)

    async def generator():
        audit = observability.StreamAudit()
        error_text = None
        first_token_ms = None
        cancelled = False
        error_bytes = bytearray()
        state = observability.current.get()
        if state:
            state.account = (getattr(resp,'headers',{}) or {}).get('x-wb-account-uid')
        try:
            # K21：首包 / 流中空闲 / 总时长三类预算在这里逐块施加。
            # 客户端取消（asyncio.CancelledError）原样上抛，由外层 close_stream
            # 在 client_cancel_s 内有界关闭上游 —— 不在这里吞掉取消。
            iterator = deadline_mod.decoded_bytes(resp)

            def _mark_first_byte() -> None:
                if state and state.first_byte_ms is None:
                    state.first_byte_ms = int((time.time()-started)*1000)

            async for chunk in deadline_mod.iterate_with_budgets(
                    iterator, request_deadline, on_first_chunk=_mark_first_byte):
                if status_code >= 400:
                    error_bytes.extend(chunk[:max(0,2000-len(error_bytes))])
                    error_text = bytes(error_bytes).decode('utf-8','replace')[:500] or f'上游 HTTP {status_code}'
                    audit.error_stage='upstream'
                else:
                    audit.feed(chunk)
                    if audit.saw_useful and first_token_ms is None:
                        first_token_ms=int((time.time()-started)*1000)
                        from .. import requestperf
                        requestperf.mark('first_useful')
                yield chunk
            if status_code < 400:
                audit.finish()
                error_text=audit.error
                if not audit.completed and not error_text:
                    error_text='上游流提前结束：未收到可信终止事件'
                if error_text and not audit.error:
                    observability.set_stream_error_detail(
                        code=audit.error_code, hint=audit.error_hint,
                        upstream_request_id=upstream_request_id)
                    yield observability.sse_error(error_text, leading_separator=True)
        except (asyncio.CancelledError,GeneratorExit):
            cancelled=True
            raise
        except Exception as exc:
            # K21：预算用尽（首包/空闲/总时长）也走这一个终止分支 —— 不重放已开始的
            # 生成、不新增 yield 形态（转发热路径的透传断言要求只有这三处 yield）。
            if isinstance(exc, deadline_mod.BudgetExceeded):
                error_text = f'上游响应超出时间预算：{exc.kind}（{exc.detail or exc}）'
                audit.error_stage='stream_read'
                audit.error_code='upstream_timeout'
                logger.warning('stream budget exceeded request_id=%s kind=%s',
                               state.request_id if state else None, exc.kind)
            elif isinstance(exc, OutputLimitExceeded):
                # 0.1.6: 输出字节上限必须先于 generic 归类，否则会被记成
                # transport_error/502，导致「上游已生成但被截断」不可归因、usage 丢失。
                error_text='上游输出超过字节上限，本次回答被截断（请降低 max_tokens 或调大 WB_MAX_OUTPUT_BYTES）'
                audit.error_stage='stream_read'
                audit.error_code='upstream_output_limit'
                logger.warning('stream output limit exceeded request_id=%s',
                               state.request_id if state else None)
            else:
                error_text=f'上游流传输或协议异常：{type(exc).__name__}'
                audit.error_stage='stream_read'
                logger.warning('stream failed request_id=%s error=%s',state.request_id if state else None,type(exc).__name__)
            # No replay after partial output; expose a terminal error event.
            observability.set_stream_error_detail(
                code=audit.error_code, hint=audit.error_hint,
                upstream_request_id=upstream_request_id)
            yield observability.sse_error(error_text, leading_separator=True)
        finally:
            cancelled=cancelled or isinstance(sys.exc_info()[1],(asyncio.CancelledError,GeneratorExit))
            observability.set_stream_state(audit,error_text,cancelled)
            try:
                # U13：取消关闭窗口 = budgets.client_cancel_s（三条协议同一口径）。
                await observability.close_stream(
                    resp,client,timeout=deadline_mod.close_timeout(budgets))
            finally:
                usage=audit.usage
                _record(key,ip,requested_model or '',mapped or '',status_code,
                        observability.usage_int(usage.get('prompt_tokens')),observability.usage_int(usage.get('completion_tokens')),
                        int((time.time()-started)*1000),ua,error_text,True,usage=usage,first_token=first_token_ms, effort=effort, session_key=session_key)

    return observability.ManagedStreamingResponse(generator(),cleanup=lambda:observability.close_stream(resp,client,timeout=deadline_mod.close_timeout(budgets)), status_code=status_code, media_type=content_type)


@router.post('/v1/chat/completions')
async def chat_v1(request: Request):
    return await _chat(request, '/v1/chat/completions')


@router.post('/v2/chat/completions')
async def chat_v2(request: Request):
    return await _chat(request, '/v2/chat/completions')


# ── 存活探测 ─────────────────────────────────────────────
@router.get('/healthz')
async def gateway_health() -> dict:
    """未认证的存活探测。

    **只回布尔**：上游 `/healthz` 会带 total / healthy 等账号池统计，
    直接透传等于把池子规模告诉任何未认证访问者（实测线上确实返回了 total）。
    探测方只需要「上游是否可用」，不需要知道池子里有几个号。
    """
    try:
        async with config.http_client(5, connect=2) as client:
            resp = await client.get(f'{config.WB2API_BASE}/healthz')
        return {'service': 'zhihui-gateway', 'upstream_ok': resp.status_code == 200}
    except Exception:  # noqa: BLE001
        # 不回异常详情：那会暴露上游地址与网络拓扑
        return {'service': 'zhihui-gateway', 'upstream_ok': False}

_SURROGATE = re.compile('[\ud800-\udfff]')


def _invalid_unicode_path(value, path='$'):
    def children(item, location):
        if isinstance(item, dict):
            for index, (key, child) in enumerate(item.items()):
                yield key, f'{location}.key[{index}]'
                yield child, f'{location}.value[{index}]'
        else:
            for index, child in enumerate(item):
                yield child, f'{location}[{index}]'

    # Iterator frames retain only the current depth. A wide JSON array must not
    # allocate another tuple and diagnostic path for every element at once.
    pending = [iter(((value, path),))]
    while pending:
        try:
            item, location = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(item, str):
            if _SURROGATE.search(item):
                return location[:256]
        elif isinstance(item, (dict, list)):
            pending.append(children(item, location))
    return None
