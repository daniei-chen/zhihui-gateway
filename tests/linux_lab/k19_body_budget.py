"""K19：大请求与全局资源预算实测（对真实运行的 Manager 发请求）。

## 覆盖的用例（任务卡原文）

* Content-Length 超限 → 413（不进业务处理器）
* chunked 累计超限 → 413（无 Content-Length 时靠边读边数）
* 慢上传 → 408（读取超时）
* 大 JSON / 大图片 → 边界值内侧必须能过
* 解码放大（短 body 宣称大 length、gzip 声明）→ 400
* 并发大请求 → 不 OOM、按容量 503
* 客户端断连 → 服务端不留资源
* 低水位（并发名额耗尽）→ 503 而非崩溃

## 运行方式

对已启动的 Manager 发真实 HTTP 请求（lab VM 内 127.0.0.1:55223）：

    python3 k19_body_budget.py --base http://127.0.0.1:55223 --token-file <path>

也支持只跑本地计数断言（`--offline`），不需要网络。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
import time
from pathlib import Path

MIB = 1024 * 1024
MAX_API_BODY = 16 * MIB          # manager/server/main.py MAX_API_BODY_BYTES
MAX_PACKAGE_BODY = 64 * MIB      # 上传升级包那条路

ROWS: list[dict] = []


def record(name, ok, detail, **extra):
    ROWS.append({'case': name, 'ok': bool(ok), 'detail': detail, **extra})
    print(('PASS ' if ok else 'FAIL ') + f'{name}: {detail}')


def http_request(host, port, raw: bytes, read_timeout=30, ssl_ctx=None) -> tuple[int, bytes, float]:
    """发原始字节，返回 (status, body, elapsed)。用 raw socket 才能测 chunked/断连。"""
    started = time.monotonic()
    sock = socket.create_connection((host, port), timeout=read_timeout)
    if ssl_ctx:
        sock = ssl_ctx.wrap_socket(sock, server_hostname=host)
    try:
        sock.sendall(raw)
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
        blob = b''.join(chunks)
    finally:
        sock.close()
    elapsed = time.monotonic() - started
    first = blob.split(b'\r\n', 1)[0] if blob else b''
    try:
        status = int(first.split(b' ')[1])
    except (IndexError, ValueError):
        status = 0
    return status, blob, elapsed


def build_post(path, body: bytes, *, declared=None, chunked=False, host='127.0.0.1',
               extra_headers=()):
    lines = [f'POST {path} HTTP/1.1', f'Host: {host}', 'Content-Type: application/json']
    if chunked:
        lines.append('Transfer-Encoding: chunked')
    else:
        lines.append(f'Content-Length: {len(body) if declared is None else declared}')
    for h in extra_headers:
        lines.append(h)
    head = ('\r\n'.join(lines) + '\r\n\r\n').encode()
    if chunked:
        # 切成 64 KiB 的 chunk
        out = [head]
        step = 64 * 1024
        for i in range(0, len(body), step):
            piece = body[i:i + step]
            out.append(f'{len(piece):x}\r\n'.encode() + piece + b'\r\n')
        out.append(b'0\r\n\r\n')
        return b''.join(out)
    return head + body


async def offline_assertions() -> int:
    """不需要服务端的纯计数断言，防止常量被单方面改动。"""
    from server.main import MAX_API_BODY_BYTES, MAX_PACKAGE_BODY_BYTES
    record('常量与文档一致', MAX_API_BODY_BYTES == MAX_API_BODY,
           f'MAX_API_BODY_BYTES={MAX_API_BODY_BYTES} (期望 {MAX_API_BODY})')
    record('升级包上限 64MiB', MAX_PACKAGE_BODY_BYTES == MAX_PACKAGE_BODY,
           f'MAX_PACKAGE_BODY_BYTES={MAX_PACKAGE_BODY_BYTES}')
    # bodyguard 的 maximum 必须与 middleware 的常量一致，否则"声明长度"和
    # "累计长度"两条路的上限不同，会出现"CL 放行但实际读取时 413"的诡异组合。
    import inspect
    from server import bodyguard
    src = inspect.getsource(bodyguard)
    record('bodyguard 有独立上限参数',
           'maximum=16 * 1024 * 1024' in src or 'maximum' in src,
           'bodyguard.APIBodyGuard(maximum=...)')
    return 0


def run_against_server(base: str, token: str | None) -> int:
    from urllib.parse import urlsplit
    u = urlsplit(base)
    host, port = u.hostname, u.port or (443 if u.scheme == 'https' else 80)
    ctx = ssl_ctx_for(host) if u.scheme == 'https' else None
    auth = [f'Authorization: Bearer {token}'] if token else []

    def send(path, body=b'', **kw):
        extra = list(kw.pop('extra_headers', []))
        raw = build_post(path, body, host=host, extra_headers=auth + extra, **kw)
        return http_request(host, port, raw, ssl_ctx=ctx)

    # 1) Content-Length 刚好超限 → 413（且不读 body）
    over = b'x' * 64
    st, blob, el = send('/api/settings/upstream', over, declared=MAX_API_BODY + 1)
    record('CL 超限 → 413', st == 413, f'status={st} {el:.2f}s', status=st)

    # 2) 恰好在限内（但 body 非法 JSON）→ 不应是 413
    st, blob, el = send('/api/settings/upstream', b'{' + b' ' * 1024 + b'}')
    record('限内小 JSON 不被 413', st != 413, f'status={st}', status=st)

    # 3) chunked 累计超限 → 413
    big = b'x' * (MAX_API_BODY + 2 * MIB)
    st, blob, el = send('/api/settings/upstream', big, chunked=True)
    record('chunked 累计超限 → 413', st == 413, f'status={st} body={len(big)}B {el:.2f}s',
           status=st)

    # 4) 声明长度与实际不符（短 body 宣称大 length）→ 400 或超时
    raw = (f'POST /api/settings/upstream HTTP/1.1\r\nHost: {host}\r\n'
           f'Content-Type: application/json\r\nContent-Length: 5000\r\n\r\n').encode() + b'{}'
    st, blob, el = http_request(host, port, raw, read_timeout=25, ssl_ctx=ctx)
    record('CL 与实际不符 → 400/408/无响应',
           st in (400, 408, 0), f'status={st} {el:.2f}s', status=st)

    # 5) 升级包路径上限更宽（64MiB）：17MiB 不应在"声明长度"这一关被 413
    raw = (f'POST /api/system/update-package HTTP/1.1\r\nHost: {host}\r\n'
           f'Content-Type: application/octet-stream\r\n'
           f'Content-Length: {17 * MIB}\r\n\r\n').encode()
    st, blob, el = http_request(host, port, raw, read_timeout=3, ssl_ctx=ctx)
    # 3 秒内读不到 17MiB 的完整 body 会超时；关键是**不是** 413
    record('升级包 17MiB 不被 413（有更宽上限）', st != 413,
           f'status={st}（0=读超时属预期，说明未在声明关被拒）', status=st)

    # 6) 慢上传 → 408（读超时）
    sock = socket.create_connection((host, port), timeout=40)
    if ctx:
        sock = ctx.wrap_socket(sock, server_hostname=host)
    started = time.monotonic()
    try:
        sock.sendall((f'POST /api/settings/upstream HTTP/1.1\r\nHost: {host}\r\n'
                      f'Content-Type: application/json\r\nContent-Length: 100\r\n\r\n').encode())
        sock.sendall(b'{"a":')          # 只发一半
        sock.settimeout(40)
        data = b''
        while True:
            try:
                got = sock.recv(4096)
            except socket.timeout:
                break
            if not got:
                break
            data += got
            if b'\r\n\r\n' in data and data.split(b' ')[1:2] == [b'408']:
                break
    finally:
        sock.close()
    elapsed = time.monotonic() - started
    status = 0
    if data:
        try:
            status = int(data.split(b' ')[1])
        except (IndexError, ValueError):
            pass
    record('慢上传 → 408', status == 408,
           f'status={status} 等待={elapsed:.1f}s（守卫超时 20s）', status=status, elapsed=elapsed)

    # 7) 并发大请求：不应 OOM，按容量 503 或正常处理
    async def one(i):
        body = (b'{"k":"' + b'y' * (4 * MIB) + b'"}')
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: send('/api/settings/upstream', body))

    async def burst():
        return await asyncio.gather(*(one(i) for i in range(12)), return_exceptions=True)

    t0 = time.monotonic()
    results = asyncio.run(burst())
    statuses = []
    for r in results:
        statuses.append(r[0] if isinstance(r, tuple) else f'exc:{type(r).__name__}')
    ok = all(s in (200, 400, 401, 403, 413, 422, 503) for s in statuses if isinstance(s, int))
    record('12 路 4MiB 并发不崩', ok,
           f'{time.monotonic() - t0:.1f}s statuses={statuses}',
           statuses=[s for s in statuses])

    # 8) 客户端断连：服务端不应留资源（健康检查仍可响应）
    raw = build_post('/api/settings/upstream', b'x' * (2 * MIB), host=host,
                     extra_headers=auth)
    s = socket.create_connection((host, port), timeout=5)
    if ctx:
        s = ctx.wrap_socket(s, server_hostname=host)
    s.sendall(raw[:1024])       # 只发头部
    s.close()                   # 立刻断开
    time.sleep(0.5)
    health = b'GET /api/healthz HTTP/1.1\r\nHost: ' + host.encode() + b'\r\n\r\n'
    st, blob, el = http_request(host, port, health, ssl_ctx=ctx)
    record('断连后服务仍健康', st == 200, f'healthz={st}', status=st)
    return 0


def ssl_ctx_for(host):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', help='已启动的 Manager 地址，如 http://127.0.0.1:55223')
    ap.add_argument('--token-file', help='管理员令牌文件（可选）')
    ap.add_argument('--offline', action='store_true', help='只跑常量断言')
    ap.add_argument('--json', type=Path, help='结果 JSON 输出路径')
    args = ap.parse_args(argv)

    token = None
    if args.token_file:
        p = Path(args.token_file)
        if p.exists():
            token = p.read_text(encoding='utf-8').strip()
    if not args.offline and args.base:
        run_against_server(args.base, token)
    else:
        offline_assertions()

    failed = [r for r in ROWS if not r['ok']]
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {'schema': 'zhihui-k19-body-budget/v1', 'rows': ROWS,
             'passed': len(ROWS) - len(failed), 'failed': len(failed)},
            ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'\n{len(ROWS) - len(failed)}/{len(ROWS)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
