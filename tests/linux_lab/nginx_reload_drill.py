#!/usr/bin/env python3
"""nginx reload 不中断演练（R3-L6a / G4-4 的一部分）。

目的：在**临时 nginx（非生产配置）**上验证「内部 Bearer 轮换 + reload」的请求连续性：
      include 里放 OLD → 起 nginx → 连续请求 → 改成 NEW（同时 QTG 侧双接受）→
      `nginx -t` → `nginx -s reload` → 继续请求 → 删除旧值（收口）→ 旧 token 401。

设计（为什么这样做）：
* **不用生产配置**：全部文件写在 `/tmp/r3l6-nginx-*` 临时目录，监听 127.0.0.1 高端口，
  演练结束 `nginx -s quit` 并删除目录；
* **不用真实凭据**：令牌是本次演练随机生成的合成值（`synthetic-old/new` 前缀）；
* **对端是本地 stub，不是 QTG 本体**：stub 按 QTG `internal_service_tokens()` 的语义
  读取 `<token>` 与 `<token>.next` 并双接受；QTG 侧双接受已有单测
  （`qtg/tests/test_credential_rotation.py::InternalBearerDualAcceptTest`）。本演练要证的是
  **nginx reload 前后请求不中断**，不是 QTG 逻辑；
* 请求循环在 reload 期间**持续发**，统计 `failures`（非 200/连接错误）与各阶段计数。

两种运行方式：
1. 直接在本机跑（需要非 root 可启动的 nginx）：`python3 nginx_reload_drill.py`
2. 由宿主通过 SSH 在 lab VM 上跑（推荐；不碰生产）：
   `python -X utf8 tests/linux_lab/nginx_reload_drill.py --run-on-vm`
   → 把本脚本源码通过 ssh stdin 交给 VM 上的 python3，结果写
     `evidence/r3-l6/nginx-reload.json` + `.log`。

**只在本地 lab VM（127.0.0.1:22224 / gateway-lab）上跑；禁止连接生产。**
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

LAB_KEY = Path(r'D:\AI\codex\codex1\zhihui-linux-lab\lab_ed25519')
LAB_KNOWN = Path(r'D:\AI\codex\codex1\zhihui-linux-lab\known_hosts')
REMOTE = 'gateway-lab@127.0.0.1'
EVIDENCE = Path('evidence/r3-l6')


# ─────────────────────────────── VM 内执行的演练主体 ───────────────────────────
def drill() -> dict:
    """在 VM（或任意有 nginx 的 POSIX 主机）上执行：临时 nginx + reload 连续性。"""
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix='r3l6-nginx-'))
    result: dict = {'workdir': str(work), 'phases': {}, 'steps': []}
    nginx_bin = shutil.which('nginx')
    if not nginx_bin:
        return {'skipped': 'nginx binary not found', 'workdir': str(work)}
    token_current = work / 'internal_token'
    token_next = work / 'internal_token.next'
    old_token = 'synthetic-old-' + secrets.token_hex(16)
    new_token = 'synthetic-new-' + secrets.token_hex(16)
    token_current.write_text(old_token + '\n', encoding='utf-8')
    os.chmod(token_current, 0o600)

    # ---- stub 上游：按 QTG 语义双接受 <token> 与 <token>.next ----
    import http.server

    class Stub(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):  # 静音
            return

        def do_GET(self):  # noqa: N802
            accepted = []
            for path in (token_next, token_current):
                try:
                    value = path.read_text(encoding='utf-8').strip()
                except OSError:
                    continue
                if value:
                    accepted.append(value)
            got = self.headers.get('X-QTG-Internal-Auth', '')
            ok = got in accepted
            body = json.dumps({'ok': ok, 'accepted': len(accepted)},
                              ensure_ascii=False).encode()
            self.send_response(200 if ok else 401)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    stub = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Stub)
    stub_port = stub.server_address[1]
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    # ---- nginx 配置（临时 prefix，仅监听 loopback 高端口） ----
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        listen_port = sock.getsockname()[1]
    include = work / 'qtg_bearer.conf'
    include.write_text(f'proxy_set_header X-QTG-Internal-Auth "{old_token}";\n',
                       encoding='utf-8')
    conf = work / 'nginx.conf'
    conf.write_text(f'''
pid {work}/nginx.pid;
error_log {work}/error.log warn;
events {{ worker_connections 128; }}
http {{
    access_log {work}/access.log;
    client_body_temp_path {work}/body;
    proxy_temp_path {work}/proxy;
    fastcgi_temp_path {work}/fastcgi;
    uwsgi_temp_path {work}/uwsgi;
    scgi_temp_path {work}/scgi;
    server {{
        listen 127.0.0.1:{listen_port};
        location / {{
            include {include};
            proxy_pass http://127.0.0.1:{stub_port};
            proxy_http_version 1.1;
            proxy_set_header Connection "";
        }}
    }}
}}
''', encoding='utf-8')

    def nginx(*args: str, timeout: int = 30):
        return subprocess.run([nginx_bin, '-p', str(work), '-c', str(conf), *args],
                              capture_output=True, text=True, timeout=timeout)

    result['nginx_version'] = subprocess.run([nginx_bin, '-v'], capture_output=True,
                                             text=True).stderr.strip()
    test = nginx('-t')
    result['steps'].append({'step': 'nginx -t (initial)', 'exit': test.returncode,
                            'stderr': test.stderr[-300:]})
    if test.returncode != 0:
        return {**result, 'error': 'initial nginx -t failed'}
    start = nginx()
    result['steps'].append({'step': 'nginx start', 'exit': start.returncode,
                            'stderr': start.stderr[-300:]})

    def wait_ready(port: int, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.05)
        return False
    if not wait_ready(listen_port):
        return {**result, 'error': 'nginx did not start listening'}

    # ---- 连续请求线程（贯穿 reload） ----
    stop = threading.Event()
    lock = threading.Lock()
    records: list[dict] = []

    def one_request() -> dict:
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{listen_port}/', timeout=5) as resp:
                status = resp.status
                resp.read()
            err = ''
        except urllib.error.HTTPError as exc:
            status = exc.code
            err = 'http'
        except Exception as exc:  # noqa: BLE001
            status = 0
            err = f'{type(exc).__name__}: {exc}'[:120]
        return {'t': round(t0 - started, 4), 'status': status, 'error': err}

    def loop() -> None:
        while not stop.is_set():
            rec = one_request()
            with lock:
                records.append(rec)
            time.sleep(0.001)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()

    def phase_until(count: int, name: str) -> dict:
        deadline = time.time() + 30
        while time.time() < deadline:
            with lock:
                if len(records) >= count:
                    break
            time.sleep(0.01)
        with lock:
            window = [r for r in records if r.get('phase') == name]
        return {'records': len(window)}

    # P0：旧值稳定期（目标 ≥60 个请求）
    time.sleep(4.0)
    with lock:
        p0_count = len(records)
    result['phases']['P0_old_token'] = {'requests_so_far': p0_count}
    with lock:
        for r in records:
            r['phase'] = 'P0_old_token'

    # P1：QTG 侧加入新值（.next），nginx include 改成 NEW，然后 reload
    token_next.write_text(new_token + '\n', encoding='utf-8')
    os.chmod(token_next, 0o600)
    tmp_include = include.with_name(include.name + '.tmp')
    tmp_include.write_text(f'proxy_set_header X-QTG-Internal-Auth "{new_token}";\n',
                           encoding='utf-8')
    os.replace(tmp_include, include)
    reload_test = nginx('-t')
    reload_started = time.monotonic()
    reload = nginx('-s', 'reload')
    reload_elapsed = round((time.monotonic() - reload_started) * 1000, 1)
    time.sleep(1.6)
    with lock:
        boundary = len(records)
        for r in records:
            if r.get('phase') is None:
                r['phase'] = 'P1_reload_window'
    result['steps'].append({'step': 'nginx -t (after include change)',
                            'exit': reload_test.returncode,
                            'stderr': reload_test.stderr[-300:]})
    result['steps'].append({'step': 'nginx -s reload', 'exit': reload.returncode,
                            'stderr': reload.stderr[-300:],
                            'elapsed_ms': reload_elapsed})
    result['phases']['P1_reload_window'] = {
        'requests_during_0.4s_after_reload': boundary - p0_count}

    # P2：把新值落为 current（原子写：临时文件 + os.replace），再进入稳定期。
    # 这一步是演练发现的**流程修正**：只改 nginx include、不更新接受方 current，
    # 删掉 .next 之后会出现"nginx 发新值、QTG 只接受旧值"的 401 窗口。
    tmp_current = token_current.with_name(token_current.name + '.tmp')
    tmp_current.write_text(new_token + '\n', encoding='utf-8')
    os.chmod(tmp_current, 0o600)
    os.replace(tmp_current, token_current)
    time.sleep(4.0)
    with lock:
        p2_count = len(records)
        for r in records:
            if r.get('phase') is None:
                r['phase'] = 'P2_new_token'
    result['phases']['P2_new_token'] = {'requests_total': p2_count}

    # P3：收口（删 .next；此时 current=NEW，接受集合={NEW}）
    token_next.unlink(missing_ok=True)
    time.sleep(0.2)
    with lock:
        for r in records:
            if r.get('phase') is None:
                r['phase'] = 'P3_closeout'

    def direct(token_value: str) -> int:
        req = urllib.request.Request(f'http://127.0.0.1:{stub_port}/',
                                     headers={'X-QTG-Internal-Auth': token_value})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception:  # noqa: BLE001
            return 0
    old_direct = direct(old_token)
    new_direct = direct(new_token)
    time.sleep(0.2)

    # 收尾：停止请求前把最后一段单独标记
    time.sleep(0.2)
    with lock:
        for r in records:
            if r.get('phase') is None:
                r['phase'] = 'P4_tail'
    stop.set()
    thread.join(timeout=5)
    quit_proc = nginx('-s', 'quit')
    stub.shutdown()
    with lock:
        all_records = list(records)
    failures = [r for r in all_records if r.get('status') != 200]
    by_phase: dict[str, dict] = {}
    for r in all_records:
        slot = by_phase.setdefault(r.get('phase') or '?', {'n': 0, 'fail': 0})
        slot['n'] += 1
        if r.get('status') != 200:
            slot['fail'] += 1
    result.update({
        'listen_port': listen_port,
        'stub_port': stub_port,
        'requests_total': len(all_records),
        'failures': len(failures),
        'failure_samples': failures[:5],
        'by_phase': by_phase,
        'old_token_direct_after_closeout': old_direct,
        'new_token_direct_after_closeout': new_direct,
        'nginx_quit_exit': quit_proc.returncode,
        'elapsed_s': round(time.monotonic() - started, 2),
    })
    result['verdict'] = {
        'reload_exit_zero': reload.returncode == 0,
        'nginx_t_ok_after_change': reload_test.returncode == 0,
        'no_failures_during_reload': len(failures) == 0,
        'old_token_401_after_closeout': old_direct == 401,
        'new_token_200_after_closeout': new_direct == 200,
        'requests_observed': len(all_records) >= 30,
        'no_failures_in_closeout': by_phase.get('P3_closeout', {}).get('fail', 0) == 0,
        'at_least_60_requests_before_reload': by_phase.get('P0_old_token', {}).get('n', 0) >= 60,
        'at_least_60_requests_after_reload': by_phase.get('P2_new_token', {}).get('n', 0) >= 60,
    }
    result['rotation_order_used'] = [
        'current=OLD（nginx include=OLD）',
        'stage: 写 .next=NEW（stub 双接受 OLD+NEW）',
        '改 include=NEW → nginx -t → nginx -s reload（期间持续请求）',
        'promote: 原子写 current=NEW（此时 OLD 已不再被接受）',
        'closeout: 删除 .next（接受集合={NEW}），旧值 401',
    ]
    result['process_finding'] = ('只改 include 不更新 current，会让删 .next 后出现'
                                 '"nginx 发新值 / QTG 只认旧值"的 401 窗口；'
                                 '正确顺序必须包含 promote(current←NEW) 再删 .next。')
    result['verdict']['all_pass'] = all(result['verdict'].values())
    shutil.rmtree(work, ignore_errors=True)
    return result


# ─────────────────────────────── 宿主侧：SSH 到 lab VM 跑 ─────────────────────
def _ssh(cmd: str, *, stdin: str | None = None, timeout: int = 300):
    args = ['ssh.exe' if os.name == 'nt' else 'ssh', '-F',
            'NUL' if os.name == 'nt' else '/dev/null', '-p', '22224',
            '-i', str(LAB_KEY), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', f'UserKnownHostsFile={LAB_KNOWN}', '-o', 'ConnectTimeout=10',
            REMOTE, cmd]
    return subprocess.run(args, input=stdin, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def run_on_vm() -> int:
    if not LAB_KEY.exists():
        print(json.dumps({'error': f'lab key missing: {LAB_KEY}'}, ensure_ascii=False))
        return 2
    probe = _ssh('command -v python3 && command -v nginx || true', timeout=30)
    if probe.returncode != 0:
        print(json.dumps({'error': 'ssh probe failed', 'detail': probe.stderr[-300:]},
                         ensure_ascii=False))
        return 2
    if 'nginx' not in (probe.stdout or ''):
        print(json.dumps({'skipped': 'nginx not installed on VM',
                          'probe': probe.stdout.strip()}, ensure_ascii=False))
        return 3
    source = Path(__file__).read_text(encoding='utf-8')
    proc = _ssh('python3 -', stdin=source, timeout=600)
    log = manifest_log = proc.stdout + '\n--- stderr ---\n' + proc.stderr
    payload: dict
    try:
        payload = json.loads(proc.stdout.strip())
    except Exception:  # noqa: BLE001
        # VM 可能打印了额外噪声：尝试取最后一个完整的 JSON 对象
        payload = {'error': 'no JSON from VM', 'exit': proc.returncode,
                   'stdout_tail': proc.stdout[-1500:], 'stderr_tail': proc.stderr[-1500:]}
        text = proc.stdout.strip()
        starts = [i for i, ch in enumerate(text) if ch == '{']
        for start in reversed(starts):
            try:
                parsed = json.loads(text[start:])
            except Exception:  # noqa: BLE001
                continue
            if isinstance(parsed, dict):
                payload = parsed
                break
    payload['mode'] = 'vm-temp-nginx'
    payload['vm'] = {'host': '127.0.0.1:22224', 'user': 'gateway-lab',
                     'production_connected': False,
                     'note': '临时目录 + 127.0.0.1 高端口；未使用生产配置/真实凭据'}
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / 'nginx-reload.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    (EVIDENCE / 'nginx-reload.log').write_text(log, encoding='utf-8')
    print(json.dumps({'evidence': str(EVIDENCE / 'nginx-reload.json'),
                      'verdict': payload.get('verdict'),
                      'skipped': payload.get('skipped'),
                      'error': payload.get('error')}, ensure_ascii=False, indent=1))
    return 0 if payload.get('verdict', {}).get('all_pass') else 1


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if '--run-on-vm' in argv:
        return run_on_vm()
    print(json.dumps(drill(), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
