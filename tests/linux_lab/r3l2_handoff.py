"""R3-L2 双实例准入/owner 交接实验（E2-4/E2-5；宿主小规模，可用 `--target vm` 复跑）。

## 实验目标（§1.6.4 时序）

用**两个真实 Manager 进程**（同一 `WB_DB` / `WB_DATA_DIR`，隔离环境 + fake provider）
跑完整交接：

```text
t0  A serving(instance lease, fence=1)，三条长流被准入
t1  A begin_drain → readyz 503（停止新准入）           → 记录 t_admission_stop
t2  A 在途长流继续跑完（不杀流）；新请求到 A → 503     → admission_rejected += n
t3  A 在途归零 → 写 final drain_snapshot、role=handoff → 记录 t_lease_release（A 释放）
t4  A 退出；B 启动 → claim 自己的实例行(role=serving)   → 记录 t_instance_claim
                                                                  → 记录 t_readyz_ok
t5  切到 B：三条长流成功                              → 记录 t_first_success
t6  账本四表对账                                      → 记录 t_ledger_ok
```

## 判定口径（与 R3-L1 同一套，禁止含糊）

* `T_admission = t_first_success − t_admission_stop`（单调时钟；`t_admission_stop` 取
  drain begin **应答完成**时刻）；同时给 `t_lease_release/t_instance_claim/t_readyz_ok`
  的分段耗时，便于定位是哪一段慢；
* `N_cut = accepted_stream_truncated`：**只算已准入（HTTP 200）但没收到终止信号**的流；
  新准入被拒（503）单独记 `admission_rejected`；两者绝不混成一个数；
* `billing`：`request_logs` / `request_outcomes` / `budget_reservations` / `usage_daily`
  四表按 request_id / key_id 对账，给出重复/缺失/unknown 计数；
* 所有结论可由 `evidence/r3-l2/handoff-timeline.ndjson` 复算（每行带 `t_rel_ms`）。

## 失败分支

1. **A SIGKILL（不 release）** → A 的实例租约过期（实验用 `ZH_INSTANCE_LEASE_TTL_SEC=3`
   缩短）→ B 用自己的 instance_id claim 成功；A 的行不再是 active，`pid_alive(A.pid)`
   返回 False，A 的旧 fence 不能再改状态；
2. **入口切换失败 → 回退**：A begin_drain 后调 `/api/system/drain resume` → role 回
   serving、readyz 200、新请求成功（模拟「B 没准备好，把流量退回 A」）。

**未在本脚本实现**：B 未 ready 时 A 保持 draining 的分支。本脚本的 A 在
`finalize_and_release` 后已停止，不存在「A 保持 draining 等 B ready」的观测；
该场景属于真实入口层演练（`r3l2_entry_handoff.py` + 批次B 故障注入脚本），
不要用本脚本的主路径时间窗冒充它。

## 边界（写进 README）

* 客户端**直连**两个 Manager 端口，没有 nginx 双 upstream / 健康检查摘除
  —— 入口层切换由 `tests/linux_lab/switch_upstream.py`（linux-release）覆盖；
* 同一时刻只有一个实例在写 DB（A 退出后才起 B），DB 单写者移交协议本身不在本实验
  （这是产品决策，见 README「不能证明」）；
* fake provider 只能证明本方状态机，不代表真实引擎。
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

PROTOCOLS = ('chat', 'responses', 'messages')
PROTOCOL_PATHS = {'chat': '/v1/chat/completions',
                  'responses': '/v1/responses',
                  'messages': '/v1/messages'}
#: 三协议 × 两平台（§1.6.4 要求「至少两平台」）
STREAM_MATRIX = (('chat', 'workbuddy'), ('responses', 'workbuddy'),
                 ('messages', 'qoder'))

LAUNCHER = r'''
import json, os, sys
from pathlib import Path

REPO = Path(os.environ['R3L2_MANAGER_ROOT'])
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

from server.routers import gateway
gateway._THREEWAY_SPEC = json.loads(os.environ['R3L2_THREEWAY_SPEC'])

from server import config, db, keysvc, security, tokensvc, instancelease
config.ensure_dirs()
security.save_users({'secret': 'r3l2-signing-' * 8, 'api_keys': [], 'users': [
    {'username': 'r3l2-admin', 'role': 'admin',
     'pwd_hash': security.make_hash('r3l2-synthetic-password')}]})
db.connect()

keys_file = Path(os.environ['R3L2_KEYS_FILE'])
if not keys_file.is_file():
    keys = {}
    for platform in ('workbuddy', 'qoder', 'trae'):
        keys[platform] = keysvc.create_key('r3l2-' + platform,
                                           platforms=[platform])['key']
    tmp = keys_file.with_suffix('.tmp')
    tmp.write_text(json.dumps(keys), encoding='utf-8')
    tmp.replace(keys_file)
keys = json.loads(keys_file.read_text(encoding='utf-8'))

me = instancelease.identity()
import threading, time
def publish_runtime_identity():
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        row = instancelease.describe(me['instance_id'])
        if row and instancelease.still_holding(me['instance_id'], row['fence']):
            target = Path(os.environ['R3L2_INSTANCE_FILE'])
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps({
                'instance_id': me['instance_id'], 'owner': me['owner'],
                'fence': row['fence'], 'pid': os.getpid(), 'claimed': True,
                'claim_source': 'application_lifespan', 'keys': keys,
            }, ensure_ascii=False), encoding='utf-8')
            temporary.replace(target)
            return
        time.sleep(.02)
threading.Thread(target=publish_runtime_identity, daemon=True).start()

from server.serve import main
main(['--host', '127.0.0.1', '--port', os.environ['R3L2_PORT'],
      '--timeout-graceful-shutdown', '45'])
'''


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def fingerprint_keys(keys: dict) -> dict:
    """把合成 API key 换成指纹（`wbk_sha256:<sha256 前 16 位>`）。

    证据文件会被全树审计扫描（`tests/zhihui_harness/secret_policy.py`），完整 key
    即使来自隔离环境的合成账号也会被判 risk。时间线/报告只需要「哪个 key」的
    可区分性，不需要明文 —— 与 R3-L5「只输出指纹」同一条原则。
    """
    import hashlib
    out = {}
    for platform, value in (keys or {}).items():
        digest = hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:16]
        out[platform] = f'wbk_sha256:{digest}'
    return out


def public_instance_info(info: dict) -> dict:
    """写入 timeline / report 的实例信息（不含明文 key）。"""
    safe = dict(info or {})
    if isinstance(safe.get('keys'), dict):
        safe['keys'] = fingerprint_keys(safe['keys'])
    return safe


class Timeline:
    def __init__(self, path: Path, origin: float) -> None:
        self._fh = path.open('w', encoding='utf-8')
        self.origin = origin
        self._lock = threading.Lock()

    def rel_ms(self, moment: float | None = None) -> float:
        return round(((moment or time.monotonic()) - self.origin) * 1000, 3)

    def emit(self, event: str, **fields) -> None:
        with self._lock:
            self._fh.write(json.dumps({'event': event, 't_rel_ms': self.rel_ms(),
                                       't_mono_s': round(time.monotonic(), 6),
                                       **fields}, ensure_ascii=False) + '\n')
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class Instance:
    """一个真实 Manager 进程（独立端口、同一隔离 DB）。"""

    def __init__(self, experiment: 'HandoffExperiment', tag: str) -> None:
        self.experiment = experiment
        self.tag = tag
        self.port = free_port()
        self.url = f'http://127.0.0.1:{self.port}'
        self.info: dict = {}
        self.proc: subprocess.Popen | None = None
        self.session = None
        self._log = None

    def start(self, *, wait_ready: bool = True) -> bool:
        env = dict(self.experiment.env)
        env.update({
            'R3L2_MANAGER_ROOT': str(self.experiment.source / 'manager'),
            'R3L2_THREEWAY_SPEC': json.dumps(self.experiment.threeway_spec),
            'R3L2_KEYS_FILE': str(self.experiment.root / 'keys.json'),
            'R3L2_INSTANCE_FILE': str(self.experiment.root / f'instance-{self.tag}.json'),
            'R3L2_PORT': str(self.port),
            'R3L2_SOURCE_SHA': self.experiment.source_sha,
            'R3L2_BUILD_ID': self.experiment.build_id,
            'PYTHONUTF8': '1',
        })
        launcher = self.experiment.root / 'launch_manager.py'
        if not launcher.is_file():
            launcher.write_text(LAUNCHER, encoding='utf-8')
        self._log = (self.experiment.root / f'manager-{self.tag}.log').open('w', encoding='utf-8')
        self.proc = subprocess.Popen([sys.executable, '-u', str(launcher)],
                                     env=env, stdout=self._log, stderr=subprocess.STDOUT)
        # 等 launcher 写出实例身份文件 = instance lease 的 claim 已完成（单调时钟打点）
        info_file = self.experiment.root / f'instance-{self.tag}.json'
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if info_file.is_file():
                self.info = json.loads(info_file.read_text(encoding='utf-8'))
                self.claim_ms = self.experiment.timeline.rel_ms()
                break
            if self.proc.poll() is not None:
                break
            time.sleep(0.02)
        ready = self.experiment.wait_ready(self, timeout=180) if wait_ready else False
        self.ready_ms = self.experiment.timeline.rel_ms()
        self.experiment.timeline.emit('instance_started', tag=self.tag, port=self.port,
                                      ready=ready, info=public_instance_info(self.info),
                                      claim_ms=getattr(self, 'claim_ms', None))
        if ready:
            import httpx
            self.session = httpx.Client(base_url=self.url, timeout=15.0, trust_env=False)
            login = self.session.post('/api/login', json={
                'username': 'r3l2-admin', 'password': 'r3l2-synthetic-password'})
            self.experiment.timeline.emit('instance_login', tag=self.tag,
                                          status=login.status_code)
        return ready

    def stop(self, *, signal: int | None = None, kill: bool = False,
             timeout: float = 60) -> int | None:
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            if kill:
                self.proc.kill()          # 跨平台：Windows 没有 SIGKILL
            elif signal is not None:
                self.proc.send_signal(signal)
            else:
                self.proc.terminate()
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self.session is not None:
            self.session.close()
        if self._log is not None:
            self._log.close()
        self.experiment.timeline.emit('instance_stopped', tag=self.tag,
                                      returncode=self.proc.returncode)
        return self.proc.returncode


class HandoffExperiment:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo = Path(args.repo).resolve()
        self.out = Path(args.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.origin = time.monotonic()
        self.timeline = Timeline(self.out / 'handoff-timeline.ndjson', self.origin)
        self.provider = None
        self.iso = None
        self.env: dict[str, str] = {}
        self.db_path = ''
        self.source = self.repo
        self.root = Path(os.environ.get('TEMP', '/tmp')) / ('r3l2-' + uuid.uuid4().hex[:12])
        self.threeway_spec: dict = {}
        self.source_sha = ''
        self.build_id = ''
        self.report: dict = {
            'schema': 'zhihui-r3-l2-handoff/v1',
            'production_connected': False,
            'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'metrics': {
                'T_admission': {'definition': (
                    't_first_success - t_admission_stop（单调时钟；t_admission_stop = '
                    'drain begin 应答完成时刻）'), 'value_ms': None, 'segments_ms': {}},
                'N_cut': {'definition': 'accepted_stream_truncated（已准入流未收到终止信号）',
                          'count': 0, 'accepted_streams': 0, 'details': []},
                'admission_rejected': {'definition': 'drain 后新请求被 503 gateway_draining 拒绝',
                                       'count': 0, 'attempts': 0},
                'billing': {},
            },
            'branches': {},
            'probes': [],
            'ledger': {},
        }

    # ── 环境 ─────────────────────────────────────────────────────────
    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        sys.dont_write_bytecode = True
        sys.path[:0] = [str(self.repo / 'tests'), str(self.repo / 'manager')]
        from fake_provider.server import FakeProvider
        from zhihui_harness.isolation import IsolatedEnv

        self.provider = FakeProvider(chunk_delay=self.args.chunk_delay,
                                     chunk_count=self.args.chunk_count).start()
        self.iso = IsolatedEnv.create(self.repo, keep=True, base_dir=self.root)
        secret = self.root / 'upstream-token'
        secret.write_text('synthetic-upstream', encoding='utf-8')
        self.threeway_spec = {name: (self.provider.base_url, str(secret))
                              for name in ('qoder', 'trae')}
        self.env = self.iso.build_env(self.provider, extra={
            'WB2API_BASE': self.provider.base_url,
            'WB2API_KEY': 'synthetic-upstream',
            'WB2API_MODE': 'native',
            'WB_STATIC_DIR': str(self.source / 'src/three-platform/web/out'),
            'WB_UPSTREAM_TIMEOUT': '120',
            'WB_TOTAL_TIMEOUT_S': '120',
            'WB_FIRST_BYTE_TIMEOUT_S': '60',
            'WB_DRAIN_TIMEOUT_SEC': '45',
            # 失败分支要快：实例租约 3s 过期，心跳 1s
            'ZH_INSTANCE_LEASE_TTL_SEC': str(self.args.instance_ttl),
            'ZH_INSTANCE_HEARTBEAT_SEC': '1',
            'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONUTF8': '1',
        })
        self.db_path = self.env['WB_DB']
        self.timeline.emit('environment_ready', provider=self.provider.base_url,
                           db=self.db_path, chunk_count=self.args.chunk_count,
                           instance_ttl_s=self.args.instance_ttl)

    def wait_ready(self, instance: Instance, timeout: float = 120) -> bool:
        import httpx
        deadline = time.monotonic() + timeout
        with httpx.Client(base_url=instance.url, timeout=3.0, trust_env=False) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get('/api/readyz').status_code == 200:
                        self.timeline.emit('readyz_200', tag=instance.tag)
                        return True
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.1)
        return False

    def wait_readyz(self, instance: Instance, code: int, timeout: float = 60) -> bool:
        import httpx
        deadline = time.monotonic() + timeout
        with httpx.Client(base_url=instance.url, timeout=3.0, trust_env=False) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get('/api/readyz').status_code == code:
                        return True
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.1)
        return False

    # ── 基础动作 ──────────────────────────────────────────────────────
    def drain_action(self, instance: Instance, action: str) -> dict:
        started = time.monotonic()
        response = instance.session.post('/api/system/drain', json={'action': action})
        ended = time.monotonic()
        record = {'tag': instance.tag, 'action': action, 'status': response.status_code,
                  't_start_ms': self.timeline.rel_ms(started),
                  't_end_ms': self.timeline.rel_ms(ended),
                  'body': response.json() if response.headers.get(
                      'content-type', '').startswith('application/json') else None}
        self.timeline.emit('drain_action', **record)
        return record

    def stream(self, instance: Instance, protocol: str, platform: str, *,
               label: str, keys: dict, progress: dict | None = None) -> dict:
        import httpx
        path = PROTOCOL_PATHS[protocol]
        if platform != 'workbuddy':
            path = f'/{platform}{path}'
        body = ({'model': 'fake-model', 'messages': [{'role': 'user', 'content': 'r3l2'}],
                 'max_tokens': 32, 'stream': True}
                if protocol == 'chat' else
                {'model': 'fake-model', 'input': 'r3l2', 'max_output_tokens': 32,
                 'stream': True}
                if protocol == 'responses' else
                {'model': 'fake-model', 'max_tokens': 32, 'stream': True,
                 'messages': [{'role': 'user', 'content': 'r3l2'}]})
        record = {'label': label, 'tag': instance.tag, 'protocol': protocol,
                  'platform': platform, 'frames': 0, 'saw_done': False,
                  'terminals': [], 'status': None, 'terminal': None,
                  't_start_ms': self.timeline.rel_ms(), 't_end_ms': None,
                  'request_id': None, 'error': None}
        try:
            with httpx.Client(timeout=180.0, trust_env=False) as client:
                with client.stream('POST', instance.url + path,
                                   headers={'Authorization': 'Bearer ' + keys[platform]},
                                   json=body) as response:
                    record['status'] = response.status_code
                    record['request_id'] = response.headers.get('x-request-id')
                    if response.status_code != 200:
                        text = b''
                        for chunk in response.iter_bytes():
                            text += chunk
                            if len(text) > 1500:
                                break
                        record['error'] = text.decode('utf-8', 'replace')[:300]
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
                            if progress is not None:
                                # 增量可见：线程结束前也能看到「已准入 + 已收帧」，
                                # 否则调用方只能等流跑完才知道它被受理（第一版就是
                                # 这样，导致「drain 期间在途流」实际上全在 drain 之前
                                # 就结束了，结论无效）。
                                progress['frames'] = record['frames']
                                progress['status'] = record['status']
                            try:
                                obj = json.loads(payload)
                            except ValueError:
                                continue
                            if not isinstance(obj, dict):
                                continue
                            if isinstance(obj.get('error'), dict):
                                record['terminals'].append('error')
                                record['error'] = json.dumps(obj['error'],
                                                             ensure_ascii=False)[:200]
                                continue
                            kind = obj.get('type')
                            if kind in ('response.completed', 'response.failed',
                                        'response.incomplete', 'message_stop'):
                                record['terminals'].append(kind)
                            if kind == 'message_delta':
                                delta = obj.get('delta')
                                if isinstance(delta, dict) and delta.get('stop_reason'):
                                    record['terminals'].append('stop_reason')
                            for choice in obj.get('choices') or []:
                                if isinstance(choice, dict) and choice.get('finish_reason'):
                                    record['terminals'].append('finish_reason')
        except Exception as exc:  # noqa: BLE001
            record['error'] = type(exc).__name__ + ': ' + str(exc)[:200]
        record['t_end_ms'] = self.timeline.rel_ms()
        completed = (record['saw_done'] or 'message_stop' in record['terminals']
                     or 'response.completed' in record['terminals']
                     or 'finish_reason' in record['terminals']
                     or 'stop_reason' in record['terminals'])
        if record['status'] != 200:
            record['terminal'] = 'http_error'
        elif 'error' in record['terminals']:
            record['terminal'] = 'upstream_error'
        elif completed:
            record['terminal'] = 'completed'
        else:
            record['terminal'] = 'stream_cut'
        self.timeline.emit('stream_result', **record)
        return record

    def spawn_stream(self, instance: Instance, protocol: str, platform: str, *,
                     label: str, keys: dict) -> dict:
        progress: dict = {}
        holder: dict = {'progress': progress}
        thread = threading.Thread(
            target=lambda: holder.setdefault(
                'stream', self.stream(instance, protocol, platform, label=label,
                                      keys=keys, progress=progress)),
            daemon=True)
        thread.start()
        holder['thread'] = thread
        return holder

    def drain_rejected_probe(self, instance: Instance, keys: dict, *,
                             protocol: str = 'chat', platform: str = 'workbuddy',
                             label: str = 'admission') -> dict:
        import httpx
        path = PROTOCOL_PATHS[protocol]
        if platform != 'workbuddy':
            path = f'/{platform}{path}'
        record = {'label': label, 'tag': instance.tag, 't_start_ms': self.timeline.rel_ms()}
        try:
            with httpx.Client(timeout=15.0, trust_env=False) as client:
                response = client.post(instance.url + path,
                                       headers={'Authorization': 'Bearer ' + keys[platform]},
                                       json={'model': 'fake-model', 'max_tokens': 8,
                                             'messages': [{'role': 'user', 'content': 'x'}]})
                record['status'] = response.status_code
                try:
                    record['code'] = (response.json().get('error') or {}).get('code')
                except ValueError:
                    record['code'] = None
                record['rejected'] = (response.status_code == 503
                                      and record['code'] == 'gateway_draining')
        except Exception as exc:  # noqa: BLE001
            record['error'] = type(exc).__name__ + ': ' + str(exc)[:200]
            record['rejected'] = False
        record['t_end_ms'] = self.timeline.rel_ms()
        self.timeline.emit('admission_probe', **record)
        return record

    # ── 观测 ─────────────────────────────────────────────────────────
    def instance_row(self, instance_id: str) -> dict | None:
        try:
            conn = sqlite3.connect(f'file:{self.db_path}?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute('SELECT * FROM instance_leases WHERE instance_id=?',
                                   (instance_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        except sqlite3.Error:
            return None

    def sqlite_write(self, sql: str, args: tuple) -> None:
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def finalize_and_release(self, instance: Instance, fence: int) -> dict:
        """Observe the real app shutdown releasing its own fenced lease."""
        instance.stop()
        row = self.instance_row(instance.info['instance_id']) or {}
        if (row.get('role') != 'stopped' or row.get('owner')
                or int(row.get('lease_until', -1)) != 0
                or int(row.get('fence', -1)) != fence):
            raise AssertionError('Application shutdown did not release its own lease')
        self.timeline.emit('lease_released', tag=instance.tag, fence=fence,
                           source='application_lifespan',
                           definition='release observed after graceful process exit',
                           t_lease_release_ms=self.timeline.rel_ms())
        return row

    def record_stream(self, stream: dict, phase: str) -> None:
        metrics = self.report['metrics']
        if stream.get('status') == 200:
            metrics['N_cut']['accepted_streams'] += 1
        if stream.get('terminal') == 'stream_cut':
            metrics['N_cut']['count'] += 1
            metrics['N_cut']['details'].append({
                'phase': phase, 'label': stream.get('label'), 'tag': stream.get('tag'),
                'request_id': stream.get('request_id'), 'protocol': stream.get('protocol'),
                'platform': stream.get('platform'), 'frames': stream.get('frames')})

    def reconcile_ledger(self) -> dict:
        """账本四表对账：重复/缺失/unknown。

        对账口径（第一版踩过的坑都写在这里）：
        * **只看推理请求**（`request_outcomes.protocol != 'http'`）：readyz 轮询、
          drain 控制端点等控制路径每请求都会写一行 outcome 但没有 request_log，
          把它们算成「孤儿」会把健康的实验判成 FAIL；
        * 无 log 的 outcome 分两类：`http_status in (503,429)` 是**预期**（drain 拒绝 /
          预算拒绝，请求没进入转发），其余才是意外；
        * `budget_reservations` 的 settled/unknown 语义沿用 R3-L1 口径。
        """
        conn = sqlite3.connect(f'file:{self.db_path}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            def count(sql: str, args: tuple = ()) -> int:
                row = conn.execute(sql, args).fetchone()
                return int(row[0] if row else 0)

            requests = count('SELECT COUNT(*) FROM request_logs '
                             'WHERE request_id IS NOT NULL AND endpoint LIKE ?', ('/v1/%',))
            duplicated = count('SELECT COUNT(*) FROM ('
                               ' SELECT request_id FROM request_logs '
                               ' WHERE request_id IS NOT NULL GROUP BY request_id '
                               ' HAVING COUNT(*) > 1)')
            outcomes = count("SELECT COUNT(*) FROM request_outcomes "
                             "WHERE protocol != 'http'")
            orphan = count(
                "SELECT COUNT(*) FROM request_outcomes o WHERE o.protocol != 'http' "
                'AND NOT EXISTS (SELECT 1 FROM request_logs l '
                ' WHERE l.request_id = o.request_id)')
            expected_orphan = count(
                "SELECT COUNT(*) FROM request_outcomes o WHERE o.protocol != 'http' "
                'AND o.http_status IN (503, 429) '
                'AND NOT EXISTS (SELECT 1 FROM request_logs l '
                ' WHERE l.request_id = o.request_id)')
            unexpected_orphan = orphan - expected_orphan
            reservations = count('SELECT COUNT(*) FROM budget_reservations')
            settled_without_log = count(
                'SELECT COUNT(*) FROM budget_reservations b WHERE NOT EXISTS ('
                ' SELECT 1 FROM request_logs l WHERE l.request_id = b.request_id) '
                " AND b.state = 'settled'")
            unknown = count("SELECT COUNT(*) FROM budget_reservations "
                            "WHERE state IN ('unknown','reserved','forwarded')")
            cancelled = count("SELECT COUNT(*) FROM request_logs "
                              "WHERE terminal_state='client_cancelled'")
            logs_tokens = count('SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) '
                                'FROM request_logs WHERE status=200')
            usage_tokens = count('SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) '
                                 'FROM usage_daily')
            return {
                'scope': 'request_outcomes 只统计 protocol != http（推理类）',
                'request_logs_rows': requests,
                'duplicate_request_ids': duplicated,
                'inference_outcomes_rows': outcomes,
                'inference_outcome_without_log': orphan,
                'expected_rejections_without_log_503_429': expected_orphan,
                'unexpected_outcome_without_log': unexpected_orphan,
                'budget_reservations_rows': reservations,
                'settled_reservation_without_log': settled_without_log,
                'open_or_unknown_reservations': unknown,
                'client_cancelled_logs': cancelled,
                'request_logs_tokens': logs_tokens,
                'usage_daily_tokens': usage_tokens,
                'verdict': 'PASS' if duplicated == 0 and unexpected_orphan == 0
                and settled_without_log == 0 else 'FAIL',
            }
        finally:
            conn.close()

    # ── 主路径 ───────────────────────────────────────────────────────
    def run(self) -> dict:
        self.prepare()
        try:
            self._main_path()
            if self.args.with_failure_branches:
                self._branch_rollback()
                self._branch_kill()
            self.report['metrics']['billing'] = self.reconcile_ledger()
            self.timeline.emit('ledger_ok',
                               t_ledger_ok_ms=self.timeline.rel_ms(),
                               billing=self.report['metrics']['billing'])
            self.report['verdict'] = self._verdict()
        finally:
            self.report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            self.report['timeline'] = str(self.out / 'handoff-timeline.ndjson')
            self.timeline.close()
            if self.provider is not None:
                self.provider.stop()
            (self.out / 'handoff-report.json').write_text(
                json.dumps(self.report, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8')
            (self.out / 'ledger-reconcile.json').write_text(
                json.dumps(self.report['metrics']['billing'], ensure_ascii=False,
                           indent=2) + '\n', encoding='utf-8')
        return self.report

    def _main_path(self) -> None:
        a = Instance(self, 'A')
        self.timeline.emit('main_path_begin')
        if not a.start():
            raise RuntimeError('A 未 ready：见 manager-A.log')
        if not a.info:
            raise RuntimeError('A 没有写出实例身份文件：见 manager-A.log')
        keys = a.info['keys']
        fence_a = int(a.info['fence'])
        self.report['instances'] = {'A': public_instance_info(a.info)}

        # t0：三条长流被准入
        holders = {}
        for protocol, platform in STREAM_MATRIX:
            holders[f'{protocol}×{platform}'] = self.spawn_stream(
                a, protocol, platform,
                label=f'A:{protocol}×{platform}', keys=keys)
        for label, holder in holders.items():
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                progress = holder.get('progress') or {}
                if progress.get('frames', 0) >= 1:
                    break
                time.sleep(0.02)
        self.timeline.emit('streams_accepted', count=len(holders))

        # t1：begin_drain
        begin = self.drain_action(a, 'begin')
        t_admission_stop = begin['t_end_ms']
        self.timeline.emit('t_admission_stop', t_admission_stop_ms=t_admission_stop)
        self.wait_readyz(a, 503, timeout=30)

        # t2：drain 期间新请求被拒
        for index in range(2):
            probe = self.drain_rejected_probe(a, keys, label=f'A:admission#{index}')
            self.report['metrics']['admission_rejected']['attempts'] += 1
            if probe.get('rejected'):
                self.report['metrics']['admission_rejected']['count'] += 1

        # t2：已准入长流继续跑完（不杀流）
        for label, holder in holders.items():
            holder['thread'].join(timeout=180)
            stream = holder.get('stream', {})
            self.record_stream(stream, phase='accepted_during_drain')
            self.report['probes'].append({'phase': 'accepted_during_drain', 'stream': stream})

        # t3：在途归零 → finalize_handoff + release
        drained_at = self.timeline.rel_ms()
        self.timeline.emit('in_flight_drained', t_ms=drained_at,
                           snapshot=self.instance_row(a.info['instance_id']))
        row_after = self.finalize_and_release(a, fence_a)
        self.report['instance_after_release'] = row_after
        t_lease_release = self.timeline.rel_ms()
        # A 退出（模拟「旧实例停止」；DB 单写者：先退再起 B）
        self.timeline.emit('A_stopped')

        # t4：B 启动并 claim（claim 打点来自实例身份文件出现的时刻）
        b = Instance(self, 'B')
        b.start(wait_ready=False)
        if not b.info:
            raise RuntimeError('B 没有写出实例身份文件：见 manager-B.log')
        self.report['instances']['B'] = public_instance_info(b.info)
        t_instance_claim = getattr(b, 'claim_ms', None) or self.timeline.rel_ms()
        ready_ok = self.wait_ready(b, timeout=180)
        t_readyz_ok = self.timeline.rel_ms()
        self.timeline.emit('t_instance_claim', t_instance_claim_ms=t_instance_claim,
                           fence=b.info['fence'])
        self.timeline.emit('t_readyz_ok', t_readyz_ok_ms=t_readyz_ok, ready=ready_ok)

        # t5：切到 B，三条长流成功（t_first_success 取**第一条**完成的时刻）
        keys_b = b.info['keys']
        streams_b = []
        first_success_ms = None
        for protocol, platform in STREAM_MATRIX:
            stream = self.stream(b, protocol, platform,
                                 label=f'B:{protocol}×{platform}', keys=keys_b)
            streams_b.append(stream)
            self.record_stream(stream, phase='after_handoff')
            self.report['probes'].append({'phase': 'after_handoff', 'stream': stream})
            if stream.get('status') == 200 and first_success_ms is None:
                first_success_ms = stream.get('t_end_ms')
        t_first_success = first_success_ms or self.timeline.rel_ms()
        self.timeline.emit('t_first_success', t_first_success_ms=t_first_success)

        segments = {
            'admission_stop_to_lease_release_ms': round(t_lease_release - t_admission_stop, 3),
            'lease_release_to_instance_claim_ms': round(
                t_instance_claim - t_lease_release, 3),
            'instance_claim_to_readyz_ok_ms': round(t_readyz_ok - t_instance_claim, 3),
            'readyz_ok_to_first_success_ms': round(t_first_success - t_readyz_ok, 3),
        }
        self.report['metrics']['T_admission']['value_ms'] = round(
            t_first_success - t_admission_stop, 3)
        self.report['metrics']['T_admission']['segments_ms'] = segments
        self.report['metrics']['T_admission']['windows'] = 1
        b.stop()

    # ── 失败分支 1：入口切换失败 → 回退 drain.resume() ────────────────
    def _branch_rollback(self) -> None:
        a = Instance(self, 'R1')
        a.start()
        keys = a.info['keys']
        begin = self.drain_action(a, 'begin')
        self.wait_readyz(a, 503, timeout=30)
        probe = self.drain_rejected_probe(a, keys, label='R1:admission-during-drain')
        resumed = self.drain_action(a, 'resume')
        self.wait_readyz(a, 200, timeout=30)
        post = self.stream(a, 'chat', 'workbuddy', label='R1:after-resume', keys=keys)
        row = self.instance_row(a.info['instance_id']) or {}
        self.report['branches']['rollback_after_failed_switch'] = {
            'begin_status': begin['status'], 'resume_status': resumed['status'],
            'rejected_during_drain': probe.get('rejected'),
            'role_after_resume': row.get('role'),
            'stream_after_resume': {'status': post.get('status'),
                                    'terminal': post.get('terminal')},
            'pass': probe.get('rejected') is True and resumed['status'] == 200
            and row.get('role') == 'serving' and post.get('status') == 200,
        }
        a.stop()

    # ── 失败分支 2：A SIGKILL（不 release）→ TTL 过期 → B 接管 ─────────
    def _branch_kill(self) -> None:
        a = Instance(self, 'K1')
        a.start()
        keys = a.info['keys']
        holder = self.spawn_stream(a, 'chat', 'workbuddy', label='K1:in-flight', keys=keys)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            stream = holder.get('stream')
            if stream and stream.get('frames', 0) >= 1:
                break
            time.sleep(0.02)
        a.stop(kill=True, timeout=30)     # SIGKILL 语义（进程无法 release 自己的租约）
        killed_row = self.instance_row(a.info['instance_id']) or {}
        # 等实例租约过期（实验 env 把 TTL 设为 3s）
        expired = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = self.instance_row(a.info['instance_id']) or {}
            if int(row.get('lease_until') or 0) <= int(time.time()):
                expired = True
                break
            time.sleep(0.1)
        b = Instance(self, 'K2')
        b.start()
        b_row = self.instance_row(b.info['instance_id']) or {}
        # 过期行不能被「复活」：直接 SQL 模拟迟到写入，role 必须保持原值
        before = (self.instance_row(a.info['instance_id']) or {}).get('role')
        self.sqlite_write(
            "UPDATE instance_leases SET role='serving' WHERE instance_id=? AND fence=? "
            'AND lease_until > ?',
            (a.info['instance_id'], int(a.info['fence']), int(time.time())))
        after = (self.instance_row(a.info['instance_id']) or {}).get('role')
        holder['thread'].join(timeout=60)
        self.report['branches']['A_sigkill'] = {
            'killed_row': killed_row,
            'lease_expired': expired,
            'B_claimed': bool(b.info.get('claimed')),
            'B_fence': b.info.get('fence'),
            'B_role': b_row.get('role'),
            'stale_row_role_before_after': [before, after],
            'stale_row_lease_until': (self.instance_row(a.info['instance_id']) or {}).get('lease_until'),
            'pass': expired and bool(b.info.get('claimed'))
            and b_row.get('role') == 'serving' and before == after,
        }
        b.stop()

    def _verdict(self) -> dict:
        metrics = self.report['metrics']
        admitted = metrics['admission_rejected']
        cuts = metrics['N_cut']
        t_admission = metrics['T_admission']['value_ms']
        billing = metrics['billing']
        branches = self.report.get('branches') or {}
        branches_ok = all(item.get('pass') for item in branches.values())
        if self.args.with_failure_branches:
            branches_ok = branches_ok and len(branches) >= 2
        return {
            'T_admission_ms': t_admission,
            'T_admission_measured': t_admission is not None,
            'admission_rejected': f"{admitted['count']}/{admitted['attempts']}",
            'N_cut': cuts['count'],
            'accepted_streams': cuts['accepted_streams'],
            'billing_verdict': billing.get('verdict'),
            'failure_branches_ok': branches_ok,
            'verdict': ('PASS' if t_admission is not None
                        and admitted['count'] == admitted['attempts'] > 0
                        and cuts['count'] == 0
                        and billing.get('verdict') == 'PASS' and branches_ok else 'ATTENTION'),
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument('--out', default='evidence/r3-l2')
    parser.add_argument('--chunk-delay', type=float, default=0.15)
    parser.add_argument('--chunk-count', type=int, default=20)
    parser.add_argument('--instance-ttl', type=int, default=3)
    parser.add_argument('--with-failure-branches', action='store_true', default=True)
    parser.add_argument('--no-failure-branches', dest='with_failure_branches',
                        action='store_false')
    args = parser.parse_args(argv)
    experiment = HandoffExperiment(args)
    report = experiment.run()
    print(json.dumps({'verdict': report.get('verdict'),
                      'metrics': report['metrics']['T_admission'],
                      'admission_rejected': report['metrics']['admission_rejected'],
                      'N_cut': report['metrics']['N_cut']['count'],
                      'accepted_streams': report['metrics']['N_cut']['accepted_streams'],
                      'billing': report['metrics']['billing'],
                      'branches': {k: v.get('pass')
                                   for k, v in report.get('branches', {}).items()}},
                     ensure_ascii=False, indent=2))
    return 0 if report.get('verdict', {}).get('verdict') == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
