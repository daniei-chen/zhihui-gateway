"""R3-L1：准入测量器（T_admission / admission_rejected / stream_cut 分离）+ 协议×平台矩阵。

## 为什么重写而不是补旧脚本

`measure_t_admission.py` 有三个已确认的口径缺陷（`evidence/r3-inventory/gaps.json` G2-1）：

1. **sleep-after-request**：旧采样器 `fetch()` 完成后才 `sleep(interval)`，实测
   样本起点间隔 0.25–0.40s，而报告里写的是 20ms —— 「20ms 采样」根本没发生；
2. **单点失败算 0 秒**：「最长连续非 200 跨度」对只命中一个失败样本的窗口给出 0s
   —— 失败被算成了「没有中断」；
3. **只测静态页**：`GET /` 只能看 web 发布切换，看不到推理准入/已准入流被截断。

本脚本把口径先写死（见 `evidence/r3-l1/README.md`），再跑实验：

* **采样器**：单调时钟记录**每个请求的 t_start 与 t_end**；用「目标节拍 + 下一拍
  绝对时刻」做补偿（不 sleep-after-request），并记录每一拍的漂移。落后时不追
  补赶点（那会产生连发样本），而是重新对齐并记录 gap；
* **T_admission**：`t_stop`（drain begin 应答完成时刻）→ `t_first_success_after_resume`
  （resume 之后第一个 200 样本的完成时刻）。同时给保守下界（首个 503 的 t_start）
  与 `resolution_ms`（采样间隔）。**样本不足或存在意外失败时输出「不足以判定」**，
  不给 0 秒的漂亮数字；
* **指标拆分**：`admission_rejected`（drain 期间新请求被 503 `gateway_draining` 拒绝）
  与 `stream_cut`（**已准入**的长流是否收到终止事件）分别计数，逐条列出 stream_cut；
* **矩阵**：3 协议（chat/completions、responses、messages）× 3 平台
  （workbuddy/qoder/trae，走 Manager 的平台前缀路径重写）× 3 场景
  （长流 / 客户端取消 / 上游错误）+ drain 窗口。

## 运行

宿主（隔离环境自动起 Manager + fake provider）::

    python -X utf8 tests/linux_lab/measure_admission_matrix.py --out evidence/r3-l1

VM（读 `current-qa.json` 的冻结快照，仍用本机 fake provider）::

    python3 tests/linux_lab/measure_admission_matrix.py --target vm --out evidence/r3-l1

只读产品代码，不改任何业务逻辑；所有 HTTP 只连 127.0.0.1。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# ── 口径常量（写进 README，改动要同步两处）──────────────────────────────
MIN_SAMPLES = 20                 # 样本数下限：低于它不下结论
MAX_UNEXPECTED_FAILURE_RATIO = 0.05   # 意外失败（非 200/非预期 503）上限 5%
DEFAULT_PROBE_INTERVAL_MS = 20   # 采样目标节拍
PROTOCOLS = ('chat', 'responses', 'messages')
PLATFORMS = ('workbuddy', 'qoder', 'trae')
SCENARIOS = ('long_stream', 'client_cancel', 'upstream_error')

PROTOCOL_PATHS = {'chat': '/v1/chat/completions',
                  'responses': '/v1/responses',
                  'messages': '/v1/messages'}


def protocol_path(protocol: str, platform: str) -> str:
    """平台前缀由 Manager 中间件重写成 /v1/*（并记下平台名），三协议都支持。"""
    path = PROTOCOL_PATHS[protocol]
    return path if platform == 'workbuddy' else f'/{platform}{path}'


def protocol_body(protocol: str, model: str, stream: bool = True) -> dict:
    if protocol == 'chat':
        return {'model': model, 'messages': [{'role': 'user', 'content': 'r3-l1'}],
                'max_tokens': 32, 'stream': stream}
    if protocol == 'responses':
        return {'model': model, 'input': 'r3-l1', 'max_output_tokens': 32, 'stream': stream}
    return {'model': model, 'max_tokens': 32, 'stream': stream,
            'messages': [{'role': 'user', 'content': 'r3-l1'}]}


class Timeline:
    """单调时钟时间线（ndjson，一行一事件）。所有结论都能由它复算。"""

    def __init__(self, path: Path, origin: float) -> None:
        self._fh = path.open('w', encoding='utf-8')
        self.origin = origin
        self._lock = threading.Lock()

    def rel_ms(self, moment: float) -> float:
        return round((moment - self.origin) * 1000, 3)

    def emit(self, event: str, **fields) -> None:
        row = {'event': event, 't_rel_ms': self.rel_ms(time.monotonic()),
               't_mono_s': round(time.monotonic(), 6), **fields}
        with self._lock:
            self._fh.write(json.dumps(row, ensure_ascii=False) + '\n')
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class AdmissionSampler(threading.Thread):
    """固定节拍的准入采样器（单调时钟，t_start/t_end 都记）。

    节拍用「下一拍绝对时刻」推进：第 N 拍的目标起点是 `anchor + N*interval`，
    请求完成后再算还差多少，睡到目标时刻。这样请求耗时不进入节拍误差
    （旧脚本的 `sleep-after-request` 正是把耗时叠加进了间隔）。
    """

    def __init__(self, base_url: str, interval_ms: int, timeline: Timeline) -> None:
        super().__init__(daemon=True)
        self.base_url = base_url
        self.interval = interval_ms / 1000.0
        self.timeline = timeline
        self.stop_event = threading.Event()
        self.samples: list[dict] = []
        self._lock = threading.Lock()

    def run(self) -> None:
        import httpx
        client = httpx.Client(base_url=self.base_url, timeout=3.0, trust_env=False)
        anchor = time.monotonic()
        index = 0
        previous_start = None
        try:
            while not self.stop_event.is_set():
                target = anchor + index * self.interval
                now = time.monotonic()
                if target > now:
                    time.sleep(target - now)
                elif previous_start is not None and (now - previous_start) < self.interval * 0.5:
                    # 落后到不足半拍：重新对齐，避免连发造成虚假样本密度
                    anchor = now - index * self.interval
                t_start = time.monotonic()
                status = -1
                error = None
                body = None
                try:
                    response = client.get('/api/readyz')
                    status = response.status_code
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                except Exception as exc:  # noqa: BLE001 —— 连接失败也是一种样本
                    error = type(exc).__name__ + ': ' + str(exc)[:200]
                t_end = time.monotonic()
                gap = None if previous_start is None else round((t_start - previous_start) * 1000, 3)
                sample = {'seq': index, 't_start': t_start, 't_end': t_end,
                          'status': status, 'error': error,
                          'draining': bool(body.get('ok') is False) if isinstance(body, dict) else None,
                          'gap_ms': gap,
                          'duration_ms': round((t_end - t_start) * 1000, 3)}
                with self._lock:
                    self.samples.append(sample)
                self.timeline.emit('admission_probe', seq=index,
                                   t_start_ms=self.timeline.rel_ms(t_start),
                                   t_end_ms=self.timeline.rel_ms(t_end),
                                   status=status, error=error,
                                   gap_ms=gap,
                                   duration_ms=sample['duration_ms'])
                previous_start = t_start
                index += 1
            # 等最后一拍完成
        finally:
            client.close()

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.samples)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def wait_for(predicate, timeout: float = 120.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class Experiment:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo = Path(args.repo).resolve()
        self.out = Path(args.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.origin = time.monotonic()
        self.timeline = Timeline(self.out / 'timeline.ndjson', self.origin)
        self.provider = None
        self.iso = None
        self.child = None
        self.manager_url = ''
        self.env: dict[str, str] = {}
        self.db_path = ''
        self.keys: dict[str, str] = {}
        self.probe_keys: dict[str, str] = {}
        self.session = None
        self.admin_headers: dict[str, str] = {}
        self.ledger: dict[str, dict] = {}

    # ── 环境（隔离 Manager + fake provider）───────────────────────────
    def start_environment(self) -> None:
        if self.args.target == 'vm':
            lab = Path('/home/gateway-lab/lab')
            if socket.gethostname() != 'zhihui-local-lab':
                raise RuntimeError('--target vm 只能在本地 lab VM 里跑')
            qa = json.loads((lab / 'current-qa.json').read_text(encoding='utf-8'))
            source = Path(qa['workspace']).resolve()
            assert source.is_relative_to(lab / 'snapshots') and not qa['production_connected']
            self.source = source
            self.base_root = lab / 'r3-l1-runs' / uuid.uuid4().hex[:12]
        else:
            self.source = self.repo
            self.base_root = Path(self.args.workdir).resolve() if self.args.workdir else \
                Path(os.environ.get('TEMP', '/tmp')) / ('r3-l1-' + uuid.uuid4().hex[:12])
        self.base_root.mkdir(parents=True, exist_ok=True)
        self.base_root.chmod(0o755)

        sys.dont_write_bytecode = True
        sys.path[:0] = [str(self.source / 'tests'), str(self.source / 'manager')]
        from fake_provider.server import FakeProvider
        from zhihui_harness.isolation import IsolatedEnv

        self.provider = FakeProvider(chunk_delay=self.args.chunk_delay,
                                     chunk_count=self.args.chunk_count).start()
        self.iso = IsolatedEnv.create(self.source, keep=True, base_dir=self.base_root)
        port = free_port()
        self.manager_url = f'http://127.0.0.1:{port}'
        self.env = self.iso.build_env(self.provider, extra={
            'WB2API_BASE': self.provider.base_url,
            'WB2API_KEY': 'synthetic-upstream',
            'WB2API_MODE': 'native',
            'WB_MANAGER_HOST': '127.0.0.1',
            'WB_MANAGER_PORT': str(port),
            'WB_STATIC_DIR': str(self.source / 'src/three-platform/web/out'),
            'WB_UPSTREAM_TIMEOUT': '120',
            'WB_TOTAL_TIMEOUT_S': '120',
            'WB_FIRST_BYTE_TIMEOUT_S': '60',
            'WB_DRAIN_TIMEOUT_SEC': '45',
            'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONUTF8': '1',
        })
        self.db_path = self.env['WB_DB']
        secret = self.base_root / 'upstream-token'
        secret.write_text('synthetic-upstream', encoding='utf-8')
        launcher = self.base_root / 'launch_manager.py'
        keys_file = self.base_root / 'keys.json'
        launcher.write_text(
            'import json, sys\n'
            'sys.dont_write_bytecode = True\n'
            f'sys.path.insert(0, {str(self.source / "manager")!r})\n'
            'from server.routers import gateway\n'
            f'gateway._THREEWAY_SPEC = { {name: (self.provider.base_url, str(secret)) for name in ("qoder", "trae")}!r}\n'
            'from server import config, db, keysvc, security, tokensvc\n'
            'config.ensure_dirs()\n'
            # POST /api/system/drain 用的是 require_session_admin（**不接受 API Token**），
            # 所以必须建一个真实会话用户并走 /api/login；token 只用于读 drain 状态。
            "security.save_users({'secret': 'r3l1-signing-' * 8, 'api_keys': [], 'users': [\n"
            "    {'username': 'r3l1-admin', 'role': 'admin',\n"
            "     'pwd_hash': security.make_hash('r3l1-synthetic-password')}]})\n"
            'db.connect()\n'
            'keys, probe_keys = {}, {}\n'
            f'for platform in {list(PLATFORMS)!r}:\n'
            # 不限额度（quota=0）：limited key 只允许 1 个 pending 预留，而「客户端取消」
            # 会把预留留成 unknown —— 后续请求会被 429 budget_pending 挡住，测不到
            # 真实准入。unlimited key 的 pending 只算 reserved/forwarded，unknown 不阻塞。
            "    keys[platform] = keysvc.create_key('r3l1-stream-' + platform, platforms=[platform])['key']\n"
            "    probe_keys[platform] = keysvc.create_key('r3l1-probe-' + platform, platforms=[platform])['key']\n"
            "admin = tokensvc.create_token('r3l1-observer', 'admin')['token']\n"
            f"open({str(keys_file)!r}, 'w', encoding='utf-8').write(json.dumps({{'keys': keys, 'probe_keys': probe_keys, 'admin': admin}}))\n"
            'from server.serve import main\n'
            f'main(["--host", "127.0.0.1", "--port", "{port}", "--timeout-graceful-shutdown", "45"])\n',
            encoding='utf-8')
        log = (self.base_root / 'manager.log').open('w', encoding='utf-8')
        self.child = subprocess.Popen([sys.executable, '-u', str(launcher)],
                                      env=self.env, stdout=log, stderr=subprocess.STDOUT)
        self._log_handle = log
        import httpx
        probe = httpx.Client(base_url=self.manager_url, timeout=3.0, trust_env=False)
        try:
            ok = wait_for(lambda: self._ready(probe), timeout=180)
        finally:
            probe.close()
        if not ok:
            tail = (self.base_root / 'manager.log').read_text(encoding='utf-8', errors='replace')[-3000:]
            raise RuntimeError('Manager 未就绪：\n' + tail)
        wait_for(lambda: keys_file.is_file(), timeout=30)
        created = json.loads(keys_file.read_text(encoding='utf-8'))
        self.keys = created['keys']
        self.probe_keys = created['probe_keys']
        self.admin_headers = {'Authorization': 'Bearer ' + created['admin']}
        # 会话登录（drain 控制端点只接受会话 cookie）
        self.session = httpx.Client(base_url=self.manager_url, timeout=15.0, trust_env=False)
        login = self.session.post('/api/login',
                                  json={'username': 'r3l1-admin',
                                        'password': 'r3l1-synthetic-password'})
        if login.status_code != 200 or not self.session.cookies:
            raise RuntimeError(f'会话登录失败：{login.status_code} {login.text[:500]}')
        self.timeline.emit('environment_ready', manager=self.manager_url,
                           provider=self.provider.base_url, db=self.db_path,
                           chunk_count=self.args.chunk_count,
                           chunk_delay_s=self.args.chunk_delay,
                           probe_interval_ms=self.args.probe_interval_ms,
                           session_cookie_names=sorted(self.session.cookies.keys()))

    @staticmethod
    def _ready(client) -> bool:
        try:
            return client.get('/api/readyz').status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def stop_environment(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception:  # noqa: BLE001
                pass
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.child.kill()
        try:
            self._log_handle.close()
        except Exception:  # noqa: BLE001
            pass
        if self.provider is not None:
            self.provider.stop()

    # ── HTTP 工具 ─────────────────────────────────────────────────────
    def drain_action(self, action: str) -> dict:
        """Drain 控制：必须走**会话 cookie**（require_session_admin 拒绝 API Token）。"""
        started = time.monotonic()
        response = self.session.post('/api/system/drain', json={'action': action})
        ended = time.monotonic()
        record = {'action': action, 'status': response.status_code,
                  't_start_ms': self.timeline.rel_ms(started),
                  't_end_ms': self.timeline.rel_ms(ended),
                  'body': response.json() if response.headers.get('content-type', '').startswith('application/json') else None}
        self.timeline.emit('drain_action', **record)
        return record

    def drain_state(self) -> dict:
        # 读状态用 API Token（require_admin 接受 token 与会话）
        import httpx
        with httpx.Client(base_url=self.manager_url, timeout=10.0, trust_env=False) as client:
            return client.get('/api/system/drain', headers=self.admin_headers).json()

    def run_stream(self, *, label: str, protocol: str, platform: str, model: str,
                   cancel_after_frames: int | None = None) -> dict:
        """发一条流式请求，逐帧记录；返回是否收到终止事件（stream_cut 的判定依据）。"""
        import httpx
        path = protocol_path(protocol, platform)
        body = protocol_body(protocol, model)
        started = time.monotonic()
        record = {'label': label, 'protocol': protocol, 'platform': platform,
                  'path': path, 'status': None, 'frames': 0, 'terminals': [],
                  'saw_done': False, 'finish_reason': None, 'error': None,
                  'client_cancelled': cancel_after_frames is not None,
                  'request_id': None, 't_start_ms': self.timeline.rel_ms(started),
                  't_end_ms': None, 'first_byte_ms': None, 'terminal': None}
        try:
            with httpx.Client(timeout=120.0, trust_env=False) as client:
                with client.stream('POST', self.manager_url + path,
                                   headers={'Authorization': 'Bearer ' + self.keys[platform]},
                                   json=body) as response:
                    record['status'] = response.status_code
                    record['request_id'] = response.headers.get('x-request-id')
                    if response.status_code != 200:
                        text = b''
                        for chunk in response.iter_bytes():
                            text += chunk
                            if len(text) > 2000:
                                break
                        record['error'] = text.decode('utf-8', 'replace')[:500]
                    else:
                        for line in response.iter_lines():
                            stripped = line.strip()
                            if not stripped.startswith('data:'):
                                continue
                            payload = stripped[5:].strip()
                            if not payload:
                                continue
                            if payload == '[DONE]':
                                record['saw_done'] = True
                                record['terminals'].append('[DONE]')
                                break
                            record['frames'] += 1
                            if record['first_byte_ms'] is None:
                                record['first_byte_ms'] = self.timeline.rel_ms(time.monotonic())
                            try:
                                obj = json.loads(payload)
                            except ValueError:
                                continue
                            if not isinstance(obj, dict):
                                continue
                            # 三类协议的错误帧都长这样：{"type":"error","error":{...}} 或
                            # chat 的 {"error":{...}} —— 这属于「上游错误」，不是「流被截断」。
                            if isinstance(obj.get('error'), dict):
                                record['error'] = json.dumps(obj['error'],
                                                             ensure_ascii=False)[:300]
                                record['terminals'].append('error')
                                continue
                            kind = obj.get('type')
                            if kind in ('response.completed', 'response.failed',
                                        'response.incomplete', 'message_stop'):
                                record['terminals'].append(kind)
                            if kind == 'message_delta':
                                delta = obj.get('delta')
                                if isinstance(delta, dict) and delta.get('stop_reason'):
                                    record['finish_reason'] = delta['stop_reason']
                            for choice in obj.get('choices') or []:
                                if isinstance(choice, dict) and choice.get('finish_reason'):
                                    record['finish_reason'] = choice['finish_reason']
                            if record['frames'] >= record_limits(label, cancel_after_frames):
                                if cancel_after_frames is not None:
                                    break        # 故意中断：关闭连接（客户端取消）
        except Exception as exc:  # noqa: BLE001
            record['error'] = type(exc).__name__ + ': ' + str(exc)[:300]
        ended = time.monotonic()
        record['t_end_ms'] = self.timeline.rel_ms(ended)
        record['total_ms'] = round((ended - started) * 1000, 3)
        has_error = 'error' in record['terminals']
        completed = (record['saw_done']
                     or any(t in ('message_stop', 'response.completed') for t in record['terminals'])
                     or bool(record['finish_reason']))
        if record['client_cancelled']:
            record['terminal'] = 'client_cancelled'
        elif record['status'] != 200:
            record['terminal'] = 'http_error'
        elif has_error:
            record['terminal'] = 'upstream_error'
        elif completed:
            record['terminal'] = 'completed'
        else:
            record['terminal'] = 'stream_cut'
        self.timeline.emit('stream_result', **record)
        return record

    # ── 账本 ─────────────────────────────────────────────────────────
    def ledger_evidence(self, request_id: str | None) -> dict:
        if not request_id:
            return {'missing': 'no request id'}
        if request_id in self.ledger:
            return self.ledger[request_id]
        result: dict = {}
        try:
            conn = sqlite3.connect(f'file:{self.db_path}?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            try:
                log = conn.execute(
                    'SELECT status, http_status, terminal_state, stream, actual_platform, endpoint '
                    'FROM request_logs WHERE request_id=?', (request_id,)).fetchone()
                outcome = conn.execute(
                    'SELECT http_status, terminal_state FROM request_outcomes WHERE request_id=?',
                    (request_id,)).fetchone()
                budget = conn.execute(
                    'SELECT state, charged_tokens, charged_credit FROM budget_reservations '
                    'WHERE request_id=?', (request_id,)).fetchone()
                result = {
                    'request_log': dict(log) if log else None,
                    'request_outcome': dict(outcome) if outcome else None,
                    'budget_reservation': dict(budget) if budget else None,
                }
            finally:
                conn.close()
        except sqlite3.Error as exc:
            result = {'db_error': str(exc)}
        self.ledger[request_id] = result
        return result

    # ── 实验主体 ─────────────────────────────────────────────────────
    def run(self) -> dict:
        started_at = dt.datetime.now(dt.timezone.utc).isoformat()
        result: dict = {
            'schema': 'zhihui-r3-l1-admission/v1',
            'target': self.args.target,
            'production_connected': False,
            'started_at': started_at,
            'metrics': {
                'T_admission': {'definition': (
                    't_first_success_after_resume - t_stop（drain begin 应答完成 → resume 后'
                    '第一个 200 样本完成，单调时钟）'),
                    'min_samples': MIN_SAMPLES,
                    'max_unexpected_failure_ratio': MAX_UNEXPECTED_FAILURE_RATIO,
                    'windows': []},
                'admission_rejected': {'definition': (
                    'drain 期间新请求被 DrainMiddleware 以 503 + code=gateway_draining 拒绝的次数'),
                    'count': 0, 'attempts': 0, 'details': []},
                'stream_cut': {'definition': (
                    '**已准入**（HTTP 200）的流式请求未能收到终止事件（[DONE]/response.completed/'
                    'finish_reason/message_stop）即结束的次数'),
                    'count': 0, 'accepted_streams': 0, 'details': []},
            },
            'matrix': {},
            'ledger': {},
            'verdicts': {},
        }
        try:
            self.start_environment()
            self._phase_a(result)
            self._phase_b(result)
            result['verdicts'] = self._verdicts(result)
        finally:
            try:
                result['drain_state_final'] = self.drain_state()
            except Exception:  # noqa: BLE001
                pass
            self.stop_environment()
            self.timeline.close()
            result['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            result['timeline'] = str(self.out / 'timeline.ndjson')
            (self.out / 'matrix.json').write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return result

    def _phase_a(self, result: dict) -> None:
        """矩阵场景（不 drain）：长流 / 客户端取消 / 上游错误，各协议×平台。"""
        for protocol in self.args.protocols:
            for platform in self.args.platforms:
                cell = result['matrix'].setdefault(f'{protocol}×{platform}', {})
                # 1) 正常长流（终端事件必须到）
                stream = self.run_stream(label=f'{protocol}×{platform}:long_stream',
                                         protocol=protocol, platform=platform,
                                         model='fake-model')
                cell['long_stream'] = stream
                self._record_stream(result, stream, phase='long_stream')
                # 2) 客户端取消（读到 3 帧后主动关闭）
                cancelled = self.run_stream(label=f'{protocol}×{platform}:client_cancel',
                                            protocol=protocol, platform=platform,
                                            model='fake-model', cancel_after_frames=3)
                cell['client_cancel'] = cancelled
                # 3) 上游错误（fake provider 的 :error_500）
                failed = self.run_stream(label=f'{protocol}×{platform}:upstream_error',
                                         protocol=protocol, platform=platform,
                                         model='fake-model:error_500')
                cell['upstream_error'] = failed
                result['ledger'][f'{protocol}×{platform}'] = {
                    'long_stream': self.ledger_evidence(stream.get('request_id')),
                    'client_cancel': self.ledger_evidence(cancelled.get('request_id')),
                    'upstream_error': self.ledger_evidence(failed.get('request_id')),
                }

    def _phase_b(self, result: dict) -> None:
        """drain 窗口：已有长流不被截断 + 新请求被拒 + resume 后恢复（T_admission）。

        `--drain-repeats` 控制每个协议×平台组合重复做几次 drain 窗口：窗口是
        T_admission 的统计单位，重复数 < 20 时只能给中位数/极值、不能给分布
        （README「不足以判定」条目）。
        """
        import httpx
        for protocol in self.args.protocols:
            for platform in self.args.platforms:
                cell = result['matrix'].setdefault(f'{protocol}×{platform}', {})
                for repeat in range(self.args.drain_repeats):
                    sampler = AdmissionSampler(self.manager_url, self.args.probe_interval_ms,
                                               self.timeline)
                    sampler.start()
                    hold = {}
                    holder = threading.Thread(
                        target=lambda: hold.setdefault('stream', self.run_stream(
                            label=f'{protocol}×{platform}:accepted_during_drain#r{repeat}',
                            protocol=protocol, platform=platform, model='fake-model')))
                    holder.start()
                    # 等流真正被受理（拿到 200 与至少 1 帧）
                    wait_for(lambda: hold.get('stream', {}).get('frames', 0) >= 1, timeout=30)
                    begin = self.drain_action('begin')
                    self.timeline.emit('window_begin', protocol=protocol,
                                       platform=platform, repeat=repeat,
                                       t_stop_ms=begin['t_end_ms'])
                    # drain 期间的新请求：必须被 503 + gateway_draining 拒绝
                    for attempt in range(2):
                        rejected = self._admission_probe(
                            protocol, platform,
                            f'{protocol}×{platform}:admission#r{repeat}#{attempt}')
                        result['metrics']['admission_rejected']['attempts'] += 1
                        result['metrics']['admission_rejected']['details'].append(rejected)
                        if rejected.get('rejected'):
                            result['metrics']['admission_rejected']['count'] += 1
                    holder.join(timeout=120)
                    stream = hold.get('stream', {})
                    self._record_stream(result, stream, phase='accepted_during_drain')
                    resume = self.drain_action('resume')
                    t_stop_ms = begin['t_end_ms']
                    t_resume_ms = resume['t_end_ms']
                    with httpx.Client(base_url=self.manager_url, timeout=2.0,
                                      trust_env=False) as ready_client:
                        wait_for(lambda: self._ready(ready_client), timeout=30)
                    # 让采样器再跑几拍，确保 resume 后的成功样本被捕捉
                    time.sleep(max(0.2, self.args.probe_interval_ms * 3 / 1000.0))
                    sampler.stop_event.set()
                    sampler.join(timeout=10)
                    window = self._compute_window(protocol, platform, sampler.snapshot(),
                                                  t_stop_ms, t_resume_ms)
                    window['repeat'] = repeat
                    result['metrics']['T_admission']['windows'].append(window)
                    cell.setdefault('drain_windows', []).append(
                        {'repeat': repeat, 'begin': begin, 'resume': resume,
                         't_admission': window,
                         'stream': {'request_id': stream.get('request_id'),
                                    'terminal': stream.get('terminal'),
                                    'frames': stream.get('frames')}})
                    result['ledger'].setdefault(f'{protocol}×{platform}', {})[
                        f'accepted_during_drain#r{repeat}'] = \
                        self.ledger_evidence(stream.get('request_id'))

    def _admission_probe(self, protocol: str, platform: str, label: str) -> dict:
        import httpx
        started = time.monotonic()
        record: dict = {'label': label, 'protocol': protocol, 'platform': platform,
                        't_start_ms': self.timeline.rel_ms(started)}
        try:
            with httpx.Client(timeout=15.0, trust_env=False) as client:
                with client.stream('POST', self.manager_url + protocol_path(protocol, platform),
                                   headers={'Authorization': 'Bearer ' + self.probe_keys[platform]},
                                   json=protocol_body(protocol, 'fake-model')) as response:
                    record['status'] = response.status_code
                    body = b''
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > 2000:
                            break
                    text = body.decode('utf-8', 'replace')
                    record['code'] = None
                    try:
                        payload = json.loads(text)
                        record['code'] = (payload.get('error') or {}).get('code')
                    except ValueError:
                        pass
                    record['rejected'] = (response.status_code == 503
                                          and record.get('code') == 'gateway_draining')
                    record['body_head'] = text[:200]
        except Exception as exc:  # noqa: BLE001
            record['error'] = type(exc).__name__ + ': ' + str(exc)[:200]
            record['rejected'] = False
        record['t_end_ms'] = self.timeline.rel_ms(time.monotonic())
        self.timeline.emit('admission_probe_request', **record)
        return record

    def _record_stream(self, result: dict, stream: dict, *, phase: str) -> None:
        metrics = result['metrics']
        if stream.get('status') == 200:
            metrics['stream_cut']['accepted_streams'] += 1
        if stream.get('terminal') == 'stream_cut':
            metrics['stream_cut']['count'] += 1
            metrics['stream_cut']['details'].append({
                'label': stream.get('label'), 'phase': phase,
                'request_id': stream.get('request_id'),
                'protocol': stream.get('protocol'), 'platform': stream.get('platform'),
                'frames': stream.get('frames'), 'status': stream.get('status'),
                'error': stream.get('error'),
                'terminal': stream.get('terminal'),
            })

    def _compute_window(self, protocol: str, platform: str,
                        samples: list[dict], t_stop_ms: float,
                        t_resume_ms: float) -> dict:
        """按写死的口径算一个 drain 窗口的 T_admission（样本不足就明确说不足）。

        关键口径：`t_first_success_after_resume` **只取 resume 应答完成之后发起**的
        样本（`t_start >= t_resume`）。用 `t_stop` 之后就会把「drain begin 应答与
        某次采样同毫秒、该采样已被受理」的竞态样本当成恢复成功 → 算出 0.0ms
        （实测踩到过：unavailable_window 变成 -15ms）。
        """
        gaps = [s['gap_ms'] for s in samples if s.get('gap_ms') is not None]
        unexpected = [s for s in samples if s['status'] not in (200, 503)]
        denied = [s for s in samples if s['status'] == 503]
        recovered = [s for s in samples
                     if s['status'] == 200 and self.timeline.rel_ms(s['t_start']) >= t_resume_ms]
        window = {
            'protocol': protocol, 'platform': platform,
            't_stop_ms': t_stop_ms,
            't_resume_ms': t_resume_ms,
            'drain_window_ms': round(t_resume_ms - t_stop_ms, 3),
            'samples': len(samples),
            'sample_gap_median_ms': sorted(gaps)[len(gaps) // 2] if gaps else None,
            'sample_gap_max_ms': max(gaps) if gaps else None,
            'denied_samples': len(denied),
            'unexpected_failure_samples': len(unexpected),
            't_first_denied_ms': (self.timeline.rel_ms(denied[0]['t_start'])
                                  if denied else None),
            't_last_denied_ms': (self.timeline.rel_ms(denied[-1]['t_end'])
                                 if denied else None),
            't_first_success_after_resume_ms': (
                self.timeline.rel_ms(recovered[0]['t_end']) if recovered else None),
            'resolution_ms': self.args.probe_interval_ms,
        }
        reasons = []
        if len(samples) < MIN_SAMPLES:
            reasons.append(f'样本数 {len(samples)} < {MIN_SAMPLES}')
        if samples and (len(unexpected) / len(samples)) > MAX_UNEXPECTED_FAILURE_RATIO:
            reasons.append(f'意外失败样本 {len(unexpected)}/{len(samples)} 超过 '
                           f'{int(MAX_UNEXPECTED_FAILURE_RATIO * 100)}%')
        if not denied:
            reasons.append('drain 期间没有观测到 503 拒绝（drain 可能未生效）')
        if window['t_first_success_after_resume_ms'] is None:
            reasons.append('resume 之后没有观测到 200 样本')
        if reasons:
            window['T_admission_ms'] = None
            window['verdict'] = '不足以判定'
            window['insufficient_reasons'] = reasons
        else:
            window['T_admission_ms'] = round(
                window['t_first_success_after_resume_ms'] - t_stop_ms, 3)
            span = round(window['t_first_success_after_resume_ms']
                         - window['t_first_denied_ms'], 3)
            window['unavailable_window_ms'] = max(0.0, span)
            if span < 0:
                window['anomaly'] = '首个成功样本早于首个拒绝样本（采样与 drain 应答竞态）'
            window['verdict'] = 'measured'
        return window

    def _verdicts(self, result: dict) -> dict:
        windows = result['metrics']['T_admission']['windows']
        measured = [w for w in windows if w.get('T_admission_ms') is not None]
        values = sorted(w['T_admission_ms'] for w in measured)
        rejected = result['metrics']['admission_rejected']
        cuts = result['metrics']['stream_cut']
        verdicts = {
            'T_admission': {
                'windows_total': len(windows),
                'windows_measured': len(measured),
                'window_values_ms': values,
                'median_ms': values[len(values) // 2] if values else None,
                'max_ms': values[-1] if values else None,
                'verdict': ('measured' if values else '不足以判定'),
                'note': ('每个窗口的 n 是采样点数（见 timeline 的 admission_probe）；'
                         '窗口重复数见 windows_total'),
            },
            'admission_rejected': {
                'count': rejected['count'], 'attempts': rejected['attempts'],
                'verdict': ('PASS' if rejected['count'] == rejected['attempts'] > 0
                            else '不足以判定' if rejected['attempts'] == 0 else 'FAIL'),
            },
            'stream_cut': {
                'accepted_streams': cuts['accepted_streams'], 'cuts': cuts['count'],
                'verdict': ('PASS' if cuts['count'] == 0 and cuts['accepted_streams'] > 0
                            else '不足以判定' if cuts['accepted_streams'] == 0 else 'CUT'),
            },
        }
        ledger_missing = []
        for cell, entries in result['ledger'].items():
            for scenario, evidence in entries.items():
                if not evidence.get('request_log'):
                    ledger_missing.append(f'{cell}/{scenario}')
        verdicts['ledger'] = {'missing_request_log': ledger_missing,
                              'verdict': 'PASS' if not ledger_missing else 'partial'}
        return verdicts


def record_limits(label: str, cancel_after_frames: int | None) -> int:
    """读到多少帧后主动取消（客户端取消场景）；其它场景不设上限。"""
    return cancel_after_frames if cancel_after_frames is not None else 10 ** 9


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument('--out', default='evidence/r3-l1')
    parser.add_argument('--workdir', default='')
    parser.add_argument('--target', choices=('local', 'vm'), default='local')
    parser.add_argument('--protocols', default=','.join(PROTOCOLS))
    parser.add_argument('--platforms', default=','.join(PLATFORMS))
    parser.add_argument('--probe-interval-ms', type=int, default=DEFAULT_PROBE_INTERVAL_MS)
    parser.add_argument('--chunk-delay', type=float, default=0.15)
    parser.add_argument('--chunk-count', type=int, default=25)
    parser.add_argument('--drain-repeats', type=int, default=1,
                        help='每个协议×平台组合重复几次 drain 窗口（T_admission 的统计单位）')
    args = parser.parse_args(argv)
    args.protocols = [p.strip() for p in args.protocols.split(',') if p.strip()]
    args.platforms = [p.strip() for p in args.platforms.split(',') if p.strip()]
    experiment = Experiment(args)
    result = experiment.run()
    summary = {name: payload.get('verdict') for name, payload in result['verdicts'].items()}
    print(json.dumps({'metrics': {
        'T_admission_median_ms': result['verdicts']['T_admission']['median_ms'],
        'T_admission_values_ms': result['verdicts']['T_admission']['window_values_ms'],
        'admission_rejected': result['metrics']['admission_rejected']['count'],
        'admission_attempts': result['metrics']['admission_rejected']['attempts'],
        'stream_cut': result['metrics']['stream_cut']['count'],
        'accepted_streams': result['metrics']['stream_cut']['accepted_streams'],
    }, 'verdicts': summary, 'out': str(Path(args.out) / 'matrix.json')},
        ensure_ascii=False, indent=2))
    return 0 if all(v in ('PASS', 'measured') for v in summary.values()) else 0


if __name__ == '__main__':
    raise SystemExit(main())
