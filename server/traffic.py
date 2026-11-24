"""调用抓包：把「客户端发来的」「网关实际发出的」「上游回来的」三份内容留档。

## 为什么要有它

`request_logs` 只存元数据（状态码、token 数、延迟）。用户拿着「模型为什么这么答」
来问时，我们**答不上来** —— 没有当时的请求体，也没有网关改写后的样子。同类项目
把「轨迹」做成了差异点，而我们的协议层有十几条改写规则（身份替换、system 合并、
参数归一），这些恰恰是最需要能被看见的部分。

## 三条硬约束（设计就是围绕它们展开的）

1. **默认关闭，且关闭时零成本。** 关闭时不读取、不缓存、不落盘任何正文；`begin()`
   返回 None，热路径上只剩一次布尔判断。正文属于用户内容，没有显式开启就不该落盘。
2. **不进 SQLite。** 正文进主库会让备份体积、异地同步、误导出三件事同时变糟。
   这里写独立目录（`$WB_DATA_DIR/traffic/`），与数据库的生命周期解耦。
3. **不阻塞转发。** 写入走后台线程 + 有界队列；队列满就丢弃并计数，绝不因为
   抓包把一次正常的模型调用拖慢或拖挂。

## 文件布局

    traffic/<YYYY-MM-DD>/<时间戳>-<请求ID>.json.gz     单条记录
    traffic/index.jsonl                                 元数据索引（每行一条）

索引存在的理由：列表页要显示几十条摘要，而 gzip 必须整文件解开才能读到元数据；
每次列表都解压 50 个文件（最多 12 MB）是没必要的开销。索引每次只追加一行。
"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import time

from . import config, db

#: 单条记录上限。超过就截断并标记 —— 一个巨型 prompt 不该把磁盘吃满。
LIMIT_BYTES = 256 * 1024
#: 保留时长（小时）与总条数上限。两者谁先到就按谁清。
RETENTION_HOURS = 24
MAX_RECORDS = 5000
#: 后台队列容量。满即丢弃（并计数），不阻塞请求路径。
QUEUE_SIZE = 256

#: 值一律替换成占位符的键名（小写匹配）。这些是凭据，不是排障重点。
REDACT_KEYS = (
    'authorization', 'x-api-key', 'api_key', 'apikey', 'cookie', 'set-cookie',
    'token', 'access_token', 'refresh_token', 'password', 'secret',
    'x-device-token', 'x-wb-token',
)
#: 正文里的个人标识。工具参数不动 —— 那是排障的核心内容。
_PII = (
    (re.compile(r'\b1[3-9]\d{9}\b'), '<手机号>'),
    (re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b'), '<邮箱>'),
    (re.compile(r'\b\d{17}[\dXx]\b'), '<身份证>'),
)

_QUEUE: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
_WORKER: threading.Thread | None = None
_LOCK = threading.Lock()
_STATS = {'written': 0, 'dropped': 0, 'truncated': 0, 'failed': 0}
#: 开关缓存：热路径不能每请求查一次数据库。
_FLAG = {'value': None, 'expires': 0.0}
_FLAG_TTL = 5.0


class Trace:
    """一次请求的抓包上下文。关闭抓包时不存在这个对象（`begin` 返回 None）。"""

    __slots__ = ('rid', 'started', 'request', 'client', 'gateway', 'status', 'response')

    def __init__(self, rid: str, request, client):
        self.rid = rid
        self.started = time.time()
        self.request = request
        self.client = client
        self.gateway = None
        self.status = None
        self.response = None


def _flag() -> bool:
    """是否开启抓包：环境变量是**总开关**，数据库开关只能在其内临时收紧。

    环境变量为 0 时数据库开关**无效** —— 否则一次误操作就能让正文静默落盘，
    而运维以为自己关着。
    """
    if not config.TRAFFIC_TAP:
        return False
    now = time.time()
    if _FLAG['expires'] > now:
        return bool(_FLAG['value'])
    try:
        setting = db.get_setting('traffic.tap', True)
    except Exception:  # noqa: BLE001 - 数据库不可用时按「开启」处理会在故障期落盘
        setting = False
    _FLAG['value'] = setting is not False
    _FLAG['expires'] = now + _FLAG_TTL
    return bool(_FLAG['value'])


def enabled() -> bool:
    return _flag()


def base_dir() -> Path:
    return Path(config.DATA_DIR) / 'traffic'


# ── 脱敏与差异 ────────────────────────────────────────────────────────────

def _scrub_text(value: str) -> str:
    for pattern, replacement in _PII:
        value = pattern.sub(replacement, value)
    return value


def _scrub(node, key: str = ''):
    """递归脱敏：键名命中黑名单就整体替换，字符串值里的个人标识打码。"""
    if isinstance(node, dict):
        out = {}
        for name, value in node.items():
            if str(name).lower() in REDACT_KEYS:
                out[name] = '<已脱敏>'
            else:
                out[name] = _scrub(value, str(name))
        return out
    if isinstance(node, list):
        return [_scrub(item, key) for item in node]
    if isinstance(node, str):
        return _scrub_text(node)
    return node


def _diff(before, after, path: str = '$', out: list | None = None, limit: int = 200) -> list:
    """列出两棵树的差异（路径 + 前后值）。

    只报**事实**，不解释原因：判断「这条改写是哪条规则干的」需要调用方结合规则表
    去看，写死在这里会随着规则增删而过时。
    """
    if out is None:
        out = []
    if len(out) >= limit:
        return out
    if type(before) is not type(after) and not (
            isinstance(before, (int, float)) and isinstance(after, (int, float))):
        out.append({'path': path, 'before': before, 'after': after})
        return out
    if isinstance(before, dict):
        for name in sorted(set(before) | set(after), key=str):
            if name not in before:
                out.append({'path': f'{path}.{name}', 'before': None, 'after': after[name],
                            'change': 'added'})
            elif name not in after:
                out.append({'path': f'{path}.{name}', 'before': before[name], 'after': None,
                            'change': 'removed'})
            else:
                _diff(before[name], after[name], f'{path}.{name}', out, limit)
            if len(out) >= limit:
                break
        return out
    if isinstance(before, list):
        if len(before) != len(after):
            out.append({'path': f'{path}.length', 'before': len(before), 'after': len(after)})
        for index, (left, right) in enumerate(zip(before, after)):
            _diff(left, right, f'{path}[{index}]', out, limit)
            if len(out) >= limit:
                break
        return out
    if before != after:
        out.append({'path': path, 'before': before, 'after': after})
    return out


# ── 裁剪 ──────────────────────────────────────────────────────────────────
#
# 2026-11-24 改进：原先超过上限就 `raw[:LIMIT_BYTES]`，把 JSON **砍断**。
# 真实数据立刻打脸 —— 上线第一天 156 条记录里 124 条（79%）被砍，而这个功能
# 的意义恰恰是「看清当时到底发了什么」；砍断后 JSON 非法，前端展开不了，
# 等于在最需要它的长上下文场景下什么都看不到。
#
# 现在改成**逐字段裁剪，保持 JSON 始终合法**：超长的字符串保留头尾、中间标注
# 省略了多少，消息条数过多时保留首尾、中间插一条说明。代价是单个字段看不全，
# 换来的是结构、顺序、条数与其余所有字段都完整可读。

#: 单个字符串字段的上限。32 KB 足够看清一段 prompt 或一段代码的轮廓。
FIELD_LIMIT = 32 * 1024
#: 整体兜底上限（字段裁剪后仍超，就裁剪消息列表）。
LIST_LIMIT = 256 * 1024
#: 消息列表裁剪时保留的头部与尾部条数。
LIST_HEAD = 8
LIST_TAIL = 4
#: 最后兜底时对「最后一条消息」用的收紧上限。排障只需要看清这一轮问了什么。
FINAL_LIMIT = 4 * 1024
#: 一段超过这个体积且条数/字段都裁不动时，才整体省略。
FINAL_DROP = 64 * 1024


def _shrink_text(text: str, limit: int = FIELD_LIMIT) -> tuple[str, bool]:
    """保留头尾、中间标注省略。按字节判断，但按字符边界切，不切坏多字节。"""
    encoded = text.encode('utf-8')
    if len(encoded) <= limit:
        return text, False
    half = max(limit // 2, 64)
    head = encoded[:half].decode('utf-8', 'ignore')
    tail = encoded[-half:].decode('utf-8', 'ignore')
    omitted = len(encoded) - len(head.encode('utf-8')) - len(tail.encode('utf-8'))
    marker = f'\n……（此处省略 {omitted} 字节，共 {len(encoded)} 字节）……\n'
    return head + marker + tail, True


def _shrink(node, path: str, found: list, limit: int = FIELD_LIMIT) -> None:
    """就地裁剪一个子树里的超长字符串，把被裁剪的路径记进 `found`。"""
    if isinstance(node, dict):
        for key in list(node):
            value = node[key]
            if isinstance(value, str):
                shrank, was = _shrink_text(value, limit)
                if was:
                    node[key] = shrank
                    found.append(f'{path}.{key}')
            else:
                _shrink(value, f'{path}.{key}', found, limit)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                shrank, was = _shrink_text(value, limit)
                if was:
                    node[index] = shrank
                    found.append(f'{path}[{index}]')
            else:
                _shrink(value, f'{path}[{index}]', found, limit)


def _shrink_messages(section: dict, path: str, found: list,
                     head: int = LIST_HEAD, tail: int = LIST_TAIL) -> bool:
    """消息条数太多时保留首尾、中间插一条说明。返回是否动过。

    为什么需要这层：字段裁剪只解决「单条太长」，解决不了「几千条消息」。
    后者如果继续整体砍字节，JSON 又会非法 —— 所以这里按**条**裁剪，
    结构仍然是合法的 JSON。
    """
    messages = section.get('messages')
    if not isinstance(messages, list) or len(messages) <= head + tail + 1:
        return False
    dropped = len(messages) - head - tail
    placeholder = {
        'role': 'system',
        'content': f'……（此处省略 {dropped} 条消息，共 {len(messages)} 条）……',
        '_traffic_placeholder': True,
    }
    section['messages'] = messages[:head] + [placeholder] + (messages[-tail:] if tail else [])
    found.append(f'{path}.messages（省略 {dropped} 条）')
    return True


def _dumps(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()


def _prepare(payload: dict) -> bytes:
    """裁剪到可落盘尺寸，返回要写的字节。**保证 JSON 合法**，并遵守整体上限。"""
    found: list[str] = []
    for section_name in ('client', 'gateway', 'response'):
        section = payload.get(section_name)
        if isinstance(section, (dict, list)):
            _shrink(section, section_name, found)

    raw = _dumps(payload)
    if len(raw) > LIST_LIMIT:
        # 逐步收紧保留条数。**两个 section 都要裁** —— 2026-11-24 这里曾写成
        # 裁完 client 就 break，于是 gateway 的 386 条（541 KB）原样落盘，
        # 单条记录达到 790 KB，上限形同虚设。
        # 逐步收紧保留条数：**先减头部、后减尾部**。长会话里「最后几条」才是
        # 当前这一轮要问的东西，历史可以丢、当前不能丢。所以最优一级是
        # (0, 1)：只留最后一条，前面一条占位说明。
        for head, tail in ((LIST_HEAD, LIST_TAIL), (4, 4), (2, 4), (0, 2), (0, 1)):
            if len(raw) <= LIST_LIMIT:
                break
            touched = False
            for section_name in ('client', 'gateway'):
                section = payload.get(section_name)
                if isinstance(section, dict):
                    touched = _shrink_messages(section, section_name, found, head, tail) or touched
            if not touched:
                break
            raw = _dumps(payload)

    if len(raw) > LIST_LIMIT:
        # 仍然超：说明超的不是「条数」而是「单条内部有海量小字段」
        # （例如一条 assistant 消息带几百个工具调用片段，每段都小于单字段上限，
        # 加起来却很大）。这时**不能**整段省略 —— 那等于什么都没留下，
        # 和最初「砍断 JSON」一样没法排障。
        #
        # 改为只保留「最后一轮」：最后一条消息 + 其余参数骨架，并把这条消息
        # 用更小的上限再裁一次。排障最想看的恰恰是「这次问了什么」，
        # 历史可以没有，当前这一轮必须有。
        for section_name in ('client', 'gateway'):
            section = payload.get(section_name)
            if not isinstance(section, dict):
                continue
            messages = section.get('messages')
            tail = messages[-1:] if isinstance(messages, list) and messages else []
            for item in tail:
                _shrink(item, f'{section_name}.messages[-1]', found, FINAL_LIMIT)
            section['messages'] = ([{'role': 'system',
                                     '_traffic_placeholder': True,
                                     'content': '……（此前的历史消息已省略，仅保留最后一条）……'}]
                                   + tail)
            found.append(f'{section_name}.messages（仅保留最后 1 条）')
        raw = _dumps(payload)

    if len(raw) > LIST_LIMIT:
        # 极端兜底：某一段的「内部」就塞不下（例如一条消息带几百个工具调用片段，
        # 每段都小于单字段上限，加起来却远超总量）。先省略真正超限的请求侧，
        # **再重算**，因为裁掉 client/gateway 往往就够了。
        #
        # 注意 `response` 不参与这一轮：它是排障核心（终止原因、用量、生成文本），
        # 而且通常很小。2026-11-24 真实数据里它被无条件牵连省略，导致「为什么
        # 这么答」完全无从判断 —— 记录里只剩一个 diff。
        for section_name in ('client', 'gateway'):
            section = payload.get(section_name)
            if isinstance(section, (dict, list)) and len(_dumps(section)) > FINAL_DROP:
                payload[section_name] = {
                    '_traffic_oversize': True,
                    '_note': f'该段超过 {FINAL_DROP // 1024} KB 且无法逐条裁剪，已整体省略；'
                             f'需要完整内容请在该时刻直接复现请求。',
                }
                found.append(f'{section_name}（整体省略）')
        raw = _dumps(payload)

    if len(raw) > LIST_LIMIT:
        # 连响应摘要都留不住时，才动它 —— 但也只是**收紧字段**，绝不整体丢。
        section = payload.get('response')
        if isinstance(section, (dict, list)):
            _shrink(section, 'response', found, FINAL_LIMIT)
            raw = _dumps(payload)

    if len(raw) > LIST_LIMIT:
        # 最后一手：response 里可能有个巨大且裁不动的结构，此时才整体省略。
        section = payload.get('response')
        if isinstance(section, (dict, list)):
            payload['response'] = {
                '_traffic_oversize': True,
                '_note': '响应摘要超过上限，已省略。',
            }
            found.append('response（整体省略）')
        raw = _dumps(payload)

    payload['truncated'] = bool(found)
    payload['truncated_fields'] = list(dict.fromkeys(found))[:40]
    if payload['truncated']:
        _STATS['truncated'] += 1
    return _dumps(payload)


# ── 写入 ──────────────────────────────────────────────────────────────────

def _write_payload(payload: dict) -> None:
    """真正落盘。只在后台线程里执行。"""
    raw = _prepare(payload)

    root = base_dir()
    day = time.strftime('%Y-%m-%d', time.gmtime(payload['ts']))
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{payload['ts']}-{payload['request_id']}.json.gz"
    with gzip.open(folder / name, 'wb', compresslevel=6) as handle:
        handle.write(raw)

    meta = {
        'request_id': payload['request_id'],
        'ts': payload['ts'],
        'status': payload.get('status'),
        'model': (payload.get('model') or {}).get('requested'),
        'mapped': (payload.get('model') or {}).get('mapped'),
        'stream': payload.get('stream'),
        'key': (payload.get('key') or {}).get('name'),
        'latency_ms': payload.get('latency_ms'),
        'size': len(raw),
        'truncated': payload.get('truncated', False),
        'truncated_fields': payload.get('truncated_fields', []),
        'file': f'{day}/{name}',
    }
    with (root / 'index.jsonl').open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(meta, ensure_ascii=False) + '\n')
    _STATS['written'] += 1


def _worker() -> None:
    while True:
        payload = _QUEUE.get()
        try:
            _write_payload(payload)
        except Exception:  # noqa: BLE001 - 抓包失败绝不影响转发
            _STATS['failed'] += 1
        finally:
            _QUEUE.task_done()


def _ensure_worker() -> None:
    global _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return
    with _LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(target=_worker, name='traffic-writer', daemon=True)
        _WORKER.start()


def _enqueue(payload: dict) -> None:
    _ensure_worker()
    try:
        _QUEUE.put_nowait(payload)
    except queue.Full:
        # 宁可丢一条抓包，也不能让转发等一个写盘线程。
        _STATS['dropped'] += 1


# ── 请求侧接入（三处调用，全在 `_chat` 里）───────────────────────────────

def begin(body, request, *, upstream_path: str = '') -> Trace | None:
    """在读完请求体后立刻调用 —— 必须在任何改写**之前**，否则「客户端原样」就丢了。"""
    if not _flag():
        return None
    rid = f'{int(time.time() * 1000):x}{secrets.token_hex(4)}'
    return Trace(rid, request, _scrub(body))


def forwarded(trace: Trace | None, body) -> None:
    """记录网关实际发往上游的请求体。位置在最终序列化之后、发送之前。"""
    if trace is None:
        return
    trace.gateway = _scrub(body)


def response(trace: Trace | None, *, status: int, data=None, error: str | None = None) -> None:
    """非流式：上游回什么就记什么（含错误体），不做二次解释。"""
    if trace is None:
        return
    trace.status = status
    trace.response = {'kind': 'json', 'body': _scrub(data), 'error': error}
    _finish(trace)


def response_stream(trace: Trace | None, *, status: int, audit=None,
                    error: str | None = None) -> None:
    """流式：存**聚合视图**而不是逐帧原文。

    逐帧原文动辄几 MB，而排障要看的是「最后给了什么、为什么结束」：终止原因、
    工具调用、用量、错误。这些 `StreamAudit` 已经算好了，直接取它的结论。
    """
    if trace is None:
        return
    trace.status = status
    collect = getattr(audit, 'collected', None) if audit is not None else None
    summary = {
        'kind': 'stream',
        'finish_reason': getattr(audit, 'finish_reason', None) if audit is not None else None,
        'completed': getattr(audit, 'completed', None) if audit is not None else None,
        'usage': getattr(audit, 'usage', None) if audit is not None else None,
        'error': error,
        'error_code': getattr(audit, 'error_code', None) if audit is not None else None,
        'saw_visible_text': getattr(audit, 'saw_visible_text', None) if audit is not None else None,
        'saw_tool_delta': getattr(audit, 'saw_tool_delta', None) if audit is not None else None,
        # 模型实际生成的内容。排障问「为什么这么答」时，这是除终止原因之外
        # 唯一能看的东西；只记 `finish_reason=length` 说明不了它断在哪一句。
        'text': collect('text') if collect else None,
        'reasoning': collect('reasoning') if collect else None,
        'text_truncated': getattr(audit, 'text_truncated', None) if audit is not None else None,
    }
    trace.response = summary
    _finish(trace)


def _finish(trace: Trace) -> None:
    request = trace.request
    payload = {
        'request_id': trace.rid,
        'ts': int(trace.started),
        'latency_ms': int((time.time() - trace.started) * 1000),
        'status': trace.status,
        'stream': (trace.response or {}).get('kind') == 'stream',
        'ip': getattr(getattr(request, 'client', None), 'host', None),
        'path': getattr(getattr(request, 'url', None), 'path', None),
        'client': trace.client,
        'gateway': trace.gateway,
        'diff': _diff(trace.client, trace.gateway),
        'response': trace.response,
        'model': {
            'requested': (trace.client or {}).get('model') if isinstance(trace.client, dict) else None,
            'mapped': (trace.gateway or {}).get('model') if isinstance(trace.gateway, dict) else None,
        },
        'key': _key_brief(request),
    }
    _enqueue(payload)


def _key_brief(request) -> dict:
    """密钥只留名字与前缀：抓包是为了看内容，不是留凭据。"""
    state = getattr(request, 'state', None)
    key = getattr(state, 'traffic_key', None) if state is not None else None
    if isinstance(key, dict):
        return {'name': key.get('name'), 'prefix': key.get('prefix')}
    return {}


def key_for(request, key) -> None:
    """把密钥摘要挂到请求上，供 `_finish` 取用（避免在热路径多传一个参数）。"""
    if request is None or not isinstance(key, dict):
        return
    try:
        request.state.traffic_key = {'name': key.get('name'), 'prefix': key.get('prefix')}
    except Exception:  # noqa: BLE001 - 某些测试桩的 Request 没有可写 state
        pass


# ── 读取 ──────────────────────────────────────────────────────────────────

def list_records(limit: int = 50) -> list[dict]:
    index = base_dir() / 'index.jsonl'
    if not index.is_file():
        return []
    rows: list[dict] = []
    with index.open('r', encoding='utf-8', errors='replace') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows[-limit:][::-1]


def read_record(request_id: str) -> dict | None:
    safe = ''.join(ch for ch in request_id if ch.isalnum() or ch in '-_')
    if not safe or safe != request_id:
        return None
    root = base_dir()
    for day in sorted((p for p in root.glob('*') if p.is_dir()), reverse=True):
        for path in day.glob(f'*-{safe}.json.gz'):
            with gzip.open(path, 'rb') as handle:
                return json.loads(handle.read())
    return None


def stats() -> dict:
    return {
        'enabled': _flag(),
        'retention_hours': RETENTION_HOURS,
        'max_records': MAX_RECORDS,
        'limit_bytes': LIMIT_BYTES,
        'counter': dict(_STATS),
        'queued': _QUEUE.qsize(),
        'dir': str(base_dir()),
    }


# ── 清理 ──────────────────────────────────────────────────────────────────

def sweep() -> dict:
    """删除超过保留期或超过总量上限的记录，并重写索引。

    只在数据面线程外调用（启动时 + 每小时一次），不参与请求路径。
    """
    root = base_dir()
    if not root.is_dir():
        return {'removed': 0, 'kept': 0}
    cutoff = time.time() - RETENTION_HOURS * 3600
    index = root / 'index.jsonl'
    rows = list_records(MAX_RECORDS * 2)
    keep: list[dict] = []
    removed = 0
    for row in rows:
        path = root / str(row.get('file') or '')
        if not row.get('file') or not path.is_file() or float(row.get('ts') or 0) < cutoff:
            if path.is_file() and row.get('file'):
                path.unlink(missing_ok=True)
            removed += 1
            continue
        keep.append(row)
    # 索引反着写过，按时间回正
    keep.sort(key=lambda item: float(item.get('ts') or 0))
    if len(keep) > MAX_RECORDS:
        for row in keep[:-MAX_RECORDS]:
            (root / str(row['file'])).unlink(missing_ok=True)
            removed += 1
        keep = keep[-MAX_RECORDS:]
    tmp = index.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        for row in keep:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    os.replace(tmp, index)
    for folder in root.iterdir():
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    return {'removed': removed, 'kept': len(keep)}


def render_html(record: dict) -> str:
    """导出单文件 HTML：把三份内容与差异放在一页里，可直接发给别人看。

    **所有内容都要 HTML 转义**：正文是用户写的，里面出现 `<script>` 是完全正常的
    （有人就在问前端问题）。不转义的话，这个导出文件一打开就会执行对方的脚本 ——
    而它恰恰是要发给同事看的。
    """
    import html as html_mod

    def dump(value) -> str:
        return html_mod.escape(json.dumps(value, ensure_ascii=False, indent=2))

    def text(value) -> str:
        return html_mod.escape('' if value is None else str(value))

    diff_rows = ''.join(
        '<tr><td><code>{}</code></td><td><pre>{}</pre></td><td><pre>{}</pre></td></tr>'.format(
            text(item.get('path')), dump(item.get('before')), dump(item.get('after')))
        for item in record.get('diff') or []
    ) or '<tr><td colspan="3">（与客户端请求一致，网关未改写）</td></tr>'
    title = '抓包 ' + text(record.get('request_id'))
    # 裁剪说明要写进导出文件：这份文件是要发给别人看的，不说明的话对方会以为
    # 原始请求就只有这么短，比不记录更误导。
    fields = record.get('truncated_fields') or []
    truncated_note = ''
    if fields:
        items = '、'.join(text(item) for item in fields)
        truncated_note = (f'<div class="note">以下内容因过长被省略，只保留了最近的部分：'
                          f'<code>{items}</code></div>')
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{font:14px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:24px;background:#0f1115;color:#e6e6e6}}
h1{{font-size:18px;margin:0 0 4px}} .meta{{color:#8b93a1;font-size:13px;margin-bottom:20px}}
.note{{margin:0 0 18px;padding:10px 12px;border:1px solid #6b4b16;background:#2a2113;color:#f0d9a8;font-size:12px;border-radius:8px}}
.note code{{display:block;margin-top:6px;font:11px/1.5 ui-monospace,Menlo,Consolas,monospace;word-break:break-all;color:#ffd591}}
section{{margin:0 0 22px;border:1px solid #262b34;border-radius:8px;overflow:hidden}}
h2{{font-size:13px;margin:0;padding:9px 12px;background:#171b22;color:#9aa4b2;font-weight:600;letter-spacing:.04em}}
pre{{margin:0;padding:12px;overflow:auto;max-height:520px;font:12px/1.55 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
td{{border-top:1px solid #262b34;padding:8px 10px;vertical-align:top}}
td:first-child{{width:26%;color:#8bb7ff;font-family:ui-monospace,Menlo,Consolas,monospace}}
</style></head><body>
<h1>智汇网关 · 调用轨迹</h1>
<div class="meta">{text(record.get('request_id'))} ｜ {text(record.get('path'))} ｜ 状态 {text(record.get('status'))} ｜ {text(record.get('latency_ms'))} ms ｜ 流式 {text(record.get('stream'))}</div>{truncated_note}
<section><h2>客户端请求</h2><pre>{dump(record.get('client'))}</pre></section>
<section><h2>网关发往上游</h2><pre>{dump(record.get('gateway'))}</pre></section>
<section><h2>差异</h2><table>{diff_rows}</table></section>
<section><h2>上游响应</h2><pre>{dump(record.get('response'))}</pre></section>
</body></html>"""
