"""R3-L2：本地 lab 的“双 Manager + nginx 双 upstream”环境与冒烟（仅 disposable local VM）。

这个脚本只服务 R3-L2 实验的**入口层**，不改任何业务代码：

  * 生成 nginx 配置：`upstream manager_backend { ... max_fails=1 fail_timeout=2s; }`
    + `proxy_next_upstream error timeout http_502 http_503` + `proxy_next_upstream_tries 2`；
    **保留 R2 的 K37 缓存片段（map + server 级 add_header）与安全头**（不得回退）；
  * 起 fake provider + 两个 Manager（同一快照、共享 WB_DB/WB_DATA_DIR，不同端口）；
  * `switch` / `restore` 由 `tests/linux_lab/switch_upstream.py` 执行（本脚本写 state 并暴露 URL）；
  * `smoke`：两个 Manager + nginx + 一条长流，验证“摘除/恢复”的时间线，并测 VM 内存。

用法（VM 内，R2/R3 快照均可）::

    python tests/linux_lab/r3l2_env.py up --host-port 443           # 前台常驻，写 state/env.json
    python tests/linux_lab/r3l2_env.py down --root <root>           # touch stop_file
    python tests/linux_lab/r3l2_env.py smoke --host-port 443        # 一次性冒烟，输出报告路径

产物（root = /home/gateway-lab/lab/r3l2-runs/<id>）：env.json、nginx.conf、smoke.ndjson、
smoke-report.json、result.json。宿主再用 ssh 收集到 evidence/r3-l2-env/。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

LAB = Path('/home/gateway-lab/lab')
MARK_BEGIN = '# >>> manager_upstream'
MARK_END = '# <<< manager_upstream'
TLS_SUBJECT = '/CN=zhihui.lab'
SECURITY_HEADERS = (
    'add_header X-Content-Type-Options nosniff always;\n'
    '  add_header X-Frame-Options DENY always;\n'
    '  add_header Referrer-Policy no-referrer always;\n'
    '  add_header Content-Security-Policy-Report-Only "default-src \'self\' \'unsafe-inline\' '
    '\'unsafe-eval\' data: blob:; connect-src \'self\'; img-src \'self\' data: blob:; '
    'font-src \'self\' data:" always;\n'
)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def http_status(url: str, timeout: float = 5.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


def wait_http(url: str, want: int, timeout: float, poll: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if http_status(url) == want:
            return True
        time.sleep(poll)
    return False


def front_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def front_probe(host_port: int, path: str, timeout: float = 10.0) -> dict:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(f'https://127.0.0.1:{host_port}{path}', timeout=timeout,
                                    context=front_ssl_context()) as resp:
            return {'status': resp.status, 'elapsed_s': round(time.monotonic() - started, 4),
                    'headers': {k.lower(): v for k, v in resp.headers.items()}}
    except urllib.error.HTTPError as exc:
        return {'status': exc.code, 'elapsed_s': round(time.monotonic() - started, 4),
                'headers': {k.lower(): v for k, v in exc.headers.items()}}
    except Exception as exc:  # noqa: BLE001
        return {'status': None, 'elapsed_s': round(time.monotonic() - started, 4), 'error': repr(exc)[:200]}


def build_nginx_conf(source: Path, root: Path, cert: Path, key: Path, ports: list[int],
                     host_port: int) -> str:
    """双 upstream + K37 缓存片段 + 安全头；upstream 段带可改写标记。"""
    cache_map = (source / 'ops/nginx/static-cache-map.http.conf').read_text()
    cache_server = (source / 'ops/nginx/static-cache.server.conf').read_text()
    (root / 'static-cache-map.http.conf').write_text(cache_map, encoding='utf-8')
    (root / 'static-cache.server.conf').write_text(cache_server, encoding='utf-8')
    servers = ''.join(f'\n    server 127.0.0.1:{port} max_fails=1 fail_timeout=2s;' for port in ports)
    proxy_headers = (
        '   proxy_http_version 1.1;\n'
        '   proxy_set_header Host $http_host;\n'
        '   proxy_set_header X-Real-IP $remote_addr;\n'
        '   proxy_set_header X-Forwarded-For $remote_addr;\n'
        '   proxy_set_header X-Forwarded-Proto $scheme;\n'
        '   proxy_set_header Authorization $http_authorization;\n'
        '   proxy_set_header X-API-Key $http_x_api_key;\n'
        '   proxy_set_header Cookie $http_cookie;\n'
        '   proxy_set_header X-QTG-Platform $http_x_qtg_platform;\n'
        '   proxy_set_header X-QTG-Identity "";\n'
        '   proxy_set_header X-QTG-Internal-Auth "";\n'
        '   proxy_next_upstream error timeout http_502 http_503;\n'
        '   proxy_next_upstream_tries 2;\n'
        '   proxy_read_timeout 600s;\n'
    )
    return f'''user gateway-lab;
worker_processes 1;
pid {root}/nginx.pid;
error_log {root}/nginx-error.log;
events {{ worker_connections 256; }}
http {{
 access_log {root}/access.log;
 include /etc/nginx/mime.types;
 default_type application/octet-stream;
 include {root}/static-cache-map.http.conf;
 {MARK_BEGIN}
upstream manager_backend {{{servers}
}}
{MARK_END}
 server {{ listen 0.0.0.0:{host_port} ssl; server_name zhihui.lab;
  ssl_certificate {cert};
  ssl_certificate_key {key};
  include {root}/static-cache.server.conf;
  {SECURITY_HEADERS}
  location /_next/ {{ root {source}/src/three-platform/web/out; }}
  location ~ ^/(?!api/|v1/|_next/)(.*)\\.txt$ {{
   root {source}/src/three-platform/web/out;
   try_files /$1/index.txt $uri =404;
   default_type text/x-component;
  }}
  location / {{
   root {source}/src/three-platform/web/out;
   try_files $uri $uri/index.html $uri.html =404;
  }}
  location /api/ {{
   proxy_pass http://manager_backend;
{proxy_headers}  }}
  location /v1/ {{
   proxy_pass http://manager_backend;
{proxy_headers}   proxy_buffering off;
  }}
 }}
}}
'''


def nginx_cmd(root: Path, *extra: str) -> list[str]:
    return ['sudo', '-n', '/usr/sbin/nginx', '-p', str(root),
            '-c', str(root / 'nginx.conf'), *extra]


def seed_data(source: Path, data_dir: Path) -> dict:
    """在共享数据目录里建库 + 一把 workbuddy 平台 API key（冒烟只走 fake provider）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ['WB_DB'] = str(data_dir / 'manager.db')
    os.environ['WB_DATA_DIR'] = str(data_dir)
    os.environ.setdefault('WB_USERS_FILE', str(data_dir / 'users.json'))
    if str(source / 'manager') not in sys.path:
        sys.path.insert(0, str(source / 'manager'))
    from server import config, db, keysvc  # noqa: E402
    db.connect()
    existing = db.query_one("SELECT id, name FROM api_keys WHERE name='r3l2-env-smoke'")
    token = None
    if existing is None:
        created = keysvc.create_key('r3l2-env-smoke', quota=1_000_000, platforms=['workbuddy'])
        token = created['key']
    else:
        # 老库：进程内只拿得到哈希，冒烟需要明文 → 重新建一把
        created = keysvc.create_key('r3l2-env-smoke-' + uuid.uuid4().hex[:6], quota=1_000_000,
                                    platforms=['workbuddy'])
        token = created['key']
    with db._lock:
        db._close_connection_locked()
    return {'api_key': token, 'db': str(config.DB_PATH)}


class Environment:
    def __init__(self, instances: int = 2, host_port: int = 443, qa_file=None):
        if socket.gethostname() != 'zhihui-local-lab' or Path.home() != LAB.parent:
            raise SystemExit('Restricted to the disposable local Linux VM')
        self.instances = instances
        self.host_port = host_port
        self.root = LAB / 'r3l2-runs' / uuid.uuid4().hex[:12]
        self.root.mkdir(parents=True)
        self.root.chmod(0o755)
        qa_path = Path(qa_file).resolve() if qa_file else LAB / 'current-qa.json'
        if not qa_path.is_relative_to(LAB):
            raise SystemExit('QA profile must remain inside the disposable local lab')
        self.qa = json.loads(qa_path.read_text())
        if self.qa['production_connected']:
            raise SystemExit('production_connected must be false')
        self.source = Path(self.qa['workspace']).resolve()
        if not self.source.is_relative_to(LAB / 'snapshots'):
            raise SystemExit('snapshot boundary violated')
        self.stop_file = self.root / 'r3l2-stop'
        self.state_file = self.root / 'env.json'
        self.provider = None
        self.managers: list[dict] = []
        self.logs: list = []

    # ---- start / stop ----
    def start(self) -> dict:
        sys.path.insert(0, str(self.source / 'tests'))
        from fake_provider.server import FakeProvider  # noqa: E402
        # Keep a body genuinely alive while the switch subprocess starts/reloads.
        self.provider = FakeProvider(chunk_delay=0.15, chunk_count=80).start()
        seeded = seed_data(self.source, self.root / 'data')
        env = {**os.environ, 'PYTHONUTF8': '1', 'TZ': 'Asia/Shanghai',
               'PYTHONDONTWRITEBYTECODE': '1',
               'WB_DB': str(self.root / 'data' / 'manager.db'),
               'WB_DATA_DIR': str(self.root / 'data'),
               'WB_STATIC_DIR': str(self.root / 'empty-static'),
               'WB_DRAIN_TIMEOUT_SEC': '30',
               'WB2API_BASE': self.provider.base_url, 'WB2API_KEY': 'synthetic-upstream',
               'WB2API_MODE': 'native'}
        (self.root / 'empty-static' / '_next').mkdir(parents=True, exist_ok=True)
        for index in range(self.instances):
            port = free_port()
            launcher = self.root / f'manager-{index}.py'
            launcher.write_text(
                'import sys\nsys.dont_write_bytecode=True\n'
                f'sys.path.insert(0, {str(self.source / "manager")!r})\n'
                'from server.serve import main\n'
                f'main(["--host","127.0.0.1","--port",{str(port)!r},'
                '"--timeout-graceful-shutdown","30"])\n', encoding='utf-8')
            log = (self.root / f'manager-{index}.log').open('w')
            self.logs.append(log)
            child = subprocess.Popen([self.qa['python'], '-B', str(launcher)], cwd=self.source,
                                     env=env, stdout=log, stderr=subprocess.STDOUT)
            self.managers.append({'id': index, 'port': port, 'pid': child.pid,
                                  'launcher': str(launcher), 'child': child})
            # 串行等待：两个 Manager 同时做首次初始化（弱口令扫描 PBKDF2 + v7/v8 迁移）
            # 会在 2 vCPU 上互相拖慢，还会抢同一个共享 DB 的迁移锁；逐个 ready 更稳。
            if not wait_http(f'http://127.0.0.1:{port}/api/readyz', 200, 360):
                log_path = self.root / f'manager-{index}.log'
                tail = log_path.read_text(errors='replace')[-1500:] if log_path.exists() else ''
                raise SystemExit(f'manager on {port} never became ready; log tail: {tail}')
        cert, key = self.root / 'tls.crt', self.root / 'tls.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                        'ec_paramgen_curve:P-256', '-nodes', '-days', '1',
                        '-subj', TLS_SUBJECT, '-addext',
                        'subjectAltName=DNS:zhihui.lab,IP:127.0.0.1',
                        '-keyout', str(key), '-out', str(cert)],
                       check=True, capture_output=True)
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', self.host_port)) == 0:
                raise RuntimeError(f'port {self.host_port} is already in use; refusing')
        (self.root / 'nginx.conf').write_text(
            build_nginx_conf(self.source, self.root, cert, key,
                             [item['port'] for item in self.managers], self.host_port),
            encoding='utf-8')
        check = subprocess.run(nginx_cmd(self.root, '-t'), capture_output=True, text=True)
        if check.returncode != 0:
            raise SystemExit('nginx -t failed: ' + (check.stderr or check.stdout)[-2000:])
        start = subprocess.run(['sudo', '-n', '/usr/sbin/nginx', '-p', str(self.root),
                                '-c', str(self.root / 'nginx.conf')],
                               capture_output=True, text=True)
        if start.returncode != 0:
            raise SystemExit('nginx start failed: ' + (start.stderr or start.stdout)[-2000:])
        deadline = time.monotonic() + 90
        front_ready = False
        while time.monotonic() < deadline:
            if front_probe(self.host_port, '/api/readyz').get('status') == 200:
                front_ready = True
                break
            time.sleep(0.25)
        if not front_ready:
            error_log = (self.root / 'nginx-error.log')
            tail = error_log.read_text(errors='replace')[-1500:] if error_log.exists() else ''
            raise SystemExit('front /api/readyz never became 200; nginx error tail: ' + tail)
        state = {
            'root': str(self.root), 'source_sha256': self.qa['source_sha256'],
            'workspace': str(self.source), 'production_connected': False,
            'host_port': self.host_port, 'host_url': f'https://127.0.0.1:{self.host_port}',
            'instances': [{'id': item['id'], 'port': item['port'], 'pid': item['pid'],
                           'launcher': item['launcher']} for item in self.managers],
            'nginx_conf': str(self.root / 'nginx.conf'),
            'nginx_pid_file': str(self.root / 'nginx.pid'),
            'fake_provider': self.provider.base_url, 'api_key_file': str(self.root / 'api-key'),
            'stop_file': str(self.stop_file), 'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        (self.root / 'api-key').write_text(seeded['api_key'], encoding='utf-8')
        (self.root / 'api-key').chmod(0o600)
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        self.state = state
        return state

    def wait_for_stop(self) -> dict:
        while not self.stop_file.exists():
            time.sleep(0.5)
        return self.cleanup()

    def cleanup(self) -> dict:
        result = {'root': str(self.root), 'stopped_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  'instances': [{'id': item['id'], 'port': item['port'], 'pid': item['pid']}
                                for item in self.managers]}
        if (self.root / 'nginx.pid').exists():
            subprocess.run(nginx_cmd(self.root, '-s', 'quit'), capture_output=True, timeout=30)
        for item in reversed(self.managers):
            child = item['child']
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
        for log in self.logs:
            log.close()
        if self.provider is not None:
            self.provider.stop()
        result['status'] = 'environment_stopped'
        (self.root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                               encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result


# --------------------------------------------------------------------------- smoke
def open_long_stream(state: dict, api_key: str, tag: str, events: list, timeout: float = 90.0) -> threading.Thread:
    def run() -> None:
        body = json.dumps({'model': 'gpt-4o-mini', 'stream': True, 'max_tokens': 200,
                           'messages': [{'role': 'user', 'content': f'r3l2 env smoke {tag}'}]}).encode()
        request = urllib.request.Request(
            f"https://127.0.0.1:{state['host_port']}/v1/chat/completions", data=body,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key},
            method='POST')
        record = {'tag': tag, 'started_mono': round(time.monotonic(), 6), 'chunks': 0,
                  'first_chunk_mono': None, 'finished_mono': None, 'done': False, 'error': None}
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=front_ssl_context()) as resp:
                record['status'] = resp.status
                for raw in resp:
                    if record['first_chunk_mono'] is None:
                        record['first_chunk_mono'] = round(time.monotonic(), 6)
                    line = raw.decode('utf-8', 'replace')
                    if 'data:' in line:
                        record['chunks'] += 1
                        if '[DONE]' in line:
                            record['done'] = True
        except Exception as exc:  # noqa: BLE001
            record['error'] = repr(exc)[:200]
        finally:
            record['finished_mono'] = round(time.monotonic(), 6)
            events.append(record)
    thread = threading.Thread(target=run, name=f'stream-{tag}', daemon=True)
    thread.start()
    return thread


def stream_completed_cleanly(record: dict) -> bool:
    return bool(record.get('done')) and record.get('error') is None and record.get('status') == 200


def sample_memory(state: dict) -> dict:
    def rss_kb(pid: int) -> int | None:
        try:
            for line in Path(f'/proc/{pid}/status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
        except Exception:
            return None
        return None
    mem: dict = {'managers': {}, 'nginx': {}, 'system': {}}
    for item in state['instances']:
        mem['managers'][item['port']] = rss_kb(item['pid'])
    pid_file = Path(state['nginx_pid_file'])
    if pid_file.exists():
        mem['nginx']['master'] = rss_kb(int(pid_file.read_text().strip()))
        try:
            children = subprocess.run(['pgrep', '-P', pid_file.read_text().strip()],
                                      capture_output=True, text=True).stdout.split()
            mem['nginx']['workers'] = [rss_kb(int(pid)) for pid in children]
        except Exception:
            mem['nginx']['workers'] = []
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith(('MemTotal:', 'MemAvailable:')):
            mem['system'][line.split(':')[0]] = int(line.split()[1])
    mem['total_rss_kb'] = (sum(v for v in mem['managers'].values() if v)
                           + sum(v for v in ([mem['nginx'].get('master')] + mem['nginx'].get('workers', [])) if v))
    return mem


def run_smoke(instances: int, host_port: int, qa_file=None) -> int:
    import switch_upstream  # same directory
    env = Environment(instances=instances, host_port=host_port, qa_file=qa_file)
    state = env.start()
    report: dict = {'schema': 'zhihui-r3l2-env-smoke/1', 'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    'source_sha256': state['source_sha256'], 'host_url': state['host_url'],
                    'instances': state['instances'], 'fake_provider': state['fake_provider'],
                    'checks': {}, 'modes': []}
    timeline = switch_upstream.Timeline(env.root / 'smoke.ndjson')
    api_key = (env.root / 'api-key').read_text().strip()
    try:
        # K37 缓存片段与安全头不得回退
        static_name = sorted((env.source / 'src/three-platform/web/out/_next/static/chunks').glob('*.js'))[0].name
        static = front_probe(host_port, f'/_next/static/chunks/{static_name}')
        html = front_probe(host_port, '/')
        report['checks']['static_cache_control'] = static['headers'].get('cache-control')
        report['checks']['html_cache_control'] = html['headers'].get('cache-control')
        report['checks']['static_nosniff'] = static['headers'].get('x-content-type-options')
        report['checks']['k37_ok'] = (static['headers'].get('cache-control') == 'public, max-age=31536000, immutable'
                                      and html['headers'].get('cache-control') == 'no-cache'
                                      and static['headers'].get('x-content-type-options') == 'nosniff')
        timeline.emit('t_env_ready', host_url=state['host_url'], instances=state['instances'],
                      k37_ok=report['checks']['k37_ok'])
        # 两种顺序各跑一遍：A/B 轮换
        pairs = [('drain-then-switch', state['instances'][0]['port'], state['instances'][1]['port'],
                  state['instances'][1]['port'], state['instances'][0]['port']),
                 ('switch-then-drain', state['instances'][1]['port'], state['instances'][0]['port'],
                  state['instances'][0]['port'], state['instances'][1]['port'])]
        for mode, old_port, new_port, next_old, next_new in pairs:
            streams: list = []
            thread = open_long_stream(state, api_key, mode, streams)
            time.sleep(0.8)  # 让长流先拿到 200 与首批 chunk
            before = front_probe(host_port, '/api/healthz')
            result = switch_upstream.switch(state, timeline, mode, old_port, new_port,
                                            timeout=60, poll=0.2)
            thread.join(timeout=60)
            after = front_probe(host_port, '/api/healthz')
            record = streams[0] if streams else {}
            mode_report = {'mode': mode, 'switch': result,
                           'front_before': before.get('status'), 'front_after': after.get('status'),
                           'stream': record, 'stream_completed': stream_completed_cleanly(record),
                           'during_switch_old_readyz': result.get('old_direct_readyz')}
            report['modes'].append(mode_report)
            timeline.emit('t_mode_done', mode=mode, stream_completed=mode_report['stream_completed'],
                          old_readyz=result.get('old_direct_readyz'))
            switch_upstream.restore(state, timeline, timeout=60, poll=0.2)
            time.sleep(0.5)
            # 恢复后：两个实例都回池，前端 200
            recovery = front_probe(host_port, '/api/healthz')
            report['modes'][-1]['recovery_status'] = recovery.get('status')
            timeline.emit('t_mode_restored', mode=mode, recovery_status=recovery.get('status'))
        report['checks']['both_modes_switched'] = all(m['switch'].get('front_ready_after_switch') for m in report['modes'])
        report['checks']['streams_not_cut'] = all(m['stream_completed'] for m in report['modes'])
        report['checks']['old_readyz_was_503'] = all(m['switch'].get('old_readyz_503') for m in report['modes'])
        report['checks']['recovered_200'] = all(m.get('recovery_status') == 200 for m in report['modes'])
        report['memory'] = sample_memory(state)
        report['ok'] = all(report['checks'].values())
        report['status'] = 'passed' if report['ok'] else 'failed'
    finally:
        report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        (env.root / 'smoke-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                    encoding='utf-8')
        env.cleanup()
    print('SMOKE_REPORT ' + str(env.root / 'smoke-report.json') + ' status=' + report['status'], flush=True)
    print(json.dumps({'status': report['status'], 'checks': report['checks'],
                      'memory': report['memory']}, ensure_ascii=False, indent=1), flush=True)
    return 0 if report['ok'] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    up = sub.add_parser('up')
    up.add_argument('--instances', type=int, default=2)
    up.add_argument('--host-port', type=int, default=443)
    up.add_argument('--qa-file', default=None)
    down = sub.add_parser('down')
    down.add_argument('--root', required=True)
    smoke = sub.add_parser('smoke')
    smoke.add_argument('--instances', type=int, default=2)
    smoke.add_argument('--host-port', type=int, default=443)
    smoke.add_argument('--qa-file', default=None)
    args = parser.parse_args(argv)
    if args.command == 'down':
        root = Path(args.root)
        state = json.loads((root / 'env.json').read_text())
        if state.get('production_connected'):
            raise SystemExit('refusing non-local stop')
        Path(state['stop_file']).touch()
        print(json.dumps({'stop_requested': state['stop_file']}, ensure_ascii=False), flush=True)
        return 0
    if args.command == 'smoke':
        return run_smoke(args.instances, args.host_port, args.qa_file)
    env = Environment(instances=args.instances, host_port=args.host_port, qa_file=args.qa_file)
    state = env.start()
    print('R3L2_ENV_READY ' + json.dumps(state, ensure_ascii=False), flush=True)
    try:
        env.wait_for_stop()
    finally:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
