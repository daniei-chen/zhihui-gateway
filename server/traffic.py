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


# ── 写入 ──────────────────────────────────────────────────────────────────

def _write_payload(payload: dict) -> None:
    """真正落盘。只在后台线程里执行。"""
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    truncated = False
    if len(raw) > LIMIT_BYTES:
        # 截断而不是拒写：能看到前 256 KB 也比什么都没有强，且明确标记过。
        raw = raw[:LIMIT_BYTES]
        truncated = True
        _STATS['truncated'] += 1
    payload['truncated'] = truncated

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
        'truncated': truncated,
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
    summary = {
        'kind': 'stream',
        'finish_reason': getattr(audit, 'finish_reason', None) if audit is not None else None,
        'completed': getattr(audit, 'completed', None) if audit is not None else None,
        'usage': getattr(audit, 'usage', None) if audit is not None else None,
        'error': error,
        'error_code': getattr(audit, 'error_code', None) if audit is not None else None,
        'saw_visible_text': getattr(audit, 'saw_visible_text', None) if audit is not None else None,
        'saw_tool_delta': getattr(audit, 'saw_tool_delta', None) if audit is not None else None,
        'text': getattr(audit, 'text', None) if audit is not None else None,
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
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{font:14px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:24px;background:#0f1115;color:#e6e6e6}}
h1{{font-size:18px;margin:0 0 4px}} .meta{{color:#8b93a1;font-size:13px;margin-bottom:20px}}
section{{margin:0 0 22px;border:1px solid #262b34;border-radius:8px;overflow:hidden}}
h2{{font-size:13px;margin:0;padding:9px 12px;background:#171b22;color:#9aa4b2;font-weight:600;letter-spacing:.04em}}
pre{{margin:0;padding:12px;overflow:auto;max-height:520px;font:12px/1.55 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
td{{border-top:1px solid #262b34;padding:8px 10px;vertical-align:top}}
td:first-child{{width:26%;color:#8bb7ff;font-family:ui-monospace,Menlo,Consolas,monospace}}
</style></head><body>
<h1>智汇网关 · 调用轨迹</h1>
<div class="meta">{text(record.get('request_id'))} ｜ {text(record.get('path'))} ｜ 状态 {text(record.get('status'))} ｜ {text(record.get('latency_ms'))} ms ｜ 流式 {text(record.get('stream'))}</div>
<section><h2>客户端请求</h2><pre>{dump(record.get('client'))}</pre></section>
<section><h2>网关发往上游</h2><pre>{dump(record.get('gateway'))}</pre></section>
<section><h2>差异</h2><table>{diff_rows}</table></section>
<section><h2>上游响应</h2><pre>{dump(record.get('response'))}</pre></section>
</body></html>"""
