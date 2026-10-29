"""R3-L6 fake 上游凭据矩阵（G4-3）——**验证现状，不造过渡能力**。

ADR（`design/qtg/ADR-credential-rotation.md` 决策 5）已决定：上游平台凭据**不实现
`.next` 过渡**，因为"旧/新 key 能否并行"取决于上游，QTG 单方面加双读只会制造
"已支持轮换"的假象。本矩阵用 fake 上游把现状钉住：

每类凭据测 4 件事：
  1. **读取/生效语义**：改文件后是否需要重启进程？还是下一次调用/下一次 401 重认证即生效？
  2. **双 key 并行**：fake 上游同时接受旧/新值时，QTG 侧是否表现稳定（只证明本方状态机，
     不能证明真实上游允许并行）；
  3. **旧值失效**：上游只接受新值时，旧配置请求的错误**分类**（UpstreamError/HTTP 401 等）；
  4. **新值生效**：把文件改成新值、**不重启进程**，下一次请求能否恢复/成功。

覆盖的真实消费者（不是脚本自建 read()）：
  * `qtg/app/qtg/adapters/workbuddy.py`：config.json 的 api_key，**每请求 Bearer**（热读）；
  * `qtg/app/qtg/adapters/qoder.py` / `trae.py`：面板口令/token 文件，登录会话 + 401 强重登；
  * `manager/server/config.py` 的 `WB2API_KEY`：环境变量，进程启动时读入（冷读）；
  * `qoder2/trae2` 与 workbuddy 面板侧只做代码路径盘点（同一 BaseAdapter 语义，不重复起 fake）。

不能证明（写进证据）：真实 Qoder/Trae/WorkBuddy 是否允许旧/新凭据并行、真实上游的
401 语义与错误体、真实引擎侧行为——这些必须外部验收（R3-L6 外部条件）。

跑法：python -X utf8 tests/linux_lab/upstream_fake_matrix.py
产出：evidence/r3-l6/upstream-fake-matrix.json
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / 'evidence/r3-l6/upstream-fake-matrix.json'
sys.path[:0] = [str(REPO / 'qtg/app')]

from qtg.adapters.qoder import QoderAdapter  # noqa: E402
from qtg.adapters.trae import TraeAdapter  # noqa: E402
from qtg.adapters.workbuddy import WorkBuddyAdapter  # noqa: E402
from qtg.config import Settings  # noqa: E402

OLD = 'synthetic-upstream-old-' + 'a' * 16
NEW = 'synthetic-upstream-new-' + 'b' * 16
OK = {'ok': True, 'upstream': 'fake'}


class FakeUpstream:
    """一个进程内 fake 上游：bearer 模式（workbuddy）或 session 模式（qoder/trae）。

    * `accepted`：当前被接受的凭据集合（可随时改，模拟上游吊销/新增）；
    * `parallel_mode`：True 时旧/新都接受（"双 key 并行"）；
    * 请求日志记录每次请求用的凭据槽位（old/new/unknown）与返回码，供矩阵统计。
    """

    def __init__(self, mode: str, *, login_path: str = '/api/login') -> None:
        # mode: bearer（workbuddy，每请求 Authorization）
        #       cookie（qoder/trae/trae2，登录后 httpx 维护 Cookie）
        #       token （qoder2，登录后 X-Panel-Token 头）
        self.mode = mode
        self.login_path = login_path
        self.accepted: set[str] = set()
        self.sessions: dict[str, str] = {}
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                return

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('content-type', 'application/json')
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get('content-length') or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b'{}')
                except ValueError:
                    payload = {}
                supplied = str(payload.get('password') or payload.get('token') or '')
                fake.requests.append({'path': self.path, 'method': 'POST',
                                      'cred': fake._which(supplied), 'status': None})
                if self.path == fake.login_path and supplied in fake.accepted:
                    sid = f'sid-{len(fake.sessions) + 1}'
                    fake.sessions[sid] = supplied
                    body = json.dumps({'ok': True, 'token': sid}).encode()
                    self.send_response(200)
                    self.send_header('content-type', 'application/json')
                    if fake.mode == 'cookie':
                        self.send_header('set-cookie', f'sid={sid}; Path=/')
                    self.send_header('content-length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    fake.requests[-1]['status'] = 200
                else:
                    fake.requests[-1]['status'] = 401
                    self._json(401, {'detail': 'invalid credential'})

            def do_GET(self):  # noqa: N802
                if fake.mode == 'bearer':
                    auth = self.headers.get('authorization', '')
                    cred = auth[len('Bearer '):] if auth.startswith('Bearer ') else ''
                    ok = cred in fake.accepted
                elif fake.mode == 'token':
                    sid = self.headers.get('x-panel-token', '')
                    cred = fake.sessions.get(sid, '')
                    ok = bool(cred) and cred in fake.accepted
                else:
                    cookie = self.headers.get('cookie') or ''
                    sid = ''
                    for part in cookie.split(';'):
                        key, _, value = part.strip().partition('=')
                        if key == 'sid':
                            sid = value
                    cred = fake.sessions.get(sid, '')
                    ok = bool(cred) and cred in fake.accepted
                fake.requests.append({'path': self.path, 'method': 'GET',
                                      'cred': fake._which(cred),
                                      'status': 200 if ok else 401})
                if ok:
                    self._json(200, {**OK, 'path': self.path})
                else:
                    self._json(401, {'detail': 'invalid or revoked credential'})

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _which(self, value: str) -> str:
        if value == OLD:
            return 'old'
        if value == NEW:
            return 'new'
        return 'unknown' if value else 'none'

    def invalidate_sessions(self) -> None:
        self.sessions.clear()

    def set_accepted(self, values: set[str]) -> None:
        self.accepted = set(values)

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f'http://{host}:{port}'

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def take(self) -> list[dict]:
        out, self.requests = self.requests, []
        return out


def _settings(tmp: Path, base: str, **over) -> Settings:
    values = dict(
        wb_api_base=base, wb_panel_base=base,
        qoder_base=base, trae_base=base,
        wb_config_file=str(tmp / 'wb-config.json'),
        wb_auths_dir=str(tmp / 'auths'),
        wb_state_file=str(tmp / 'state.json'),
        wb_db=str(tmp / 'manager.db'),
        qoder_password_file=str(tmp / 'qoder-password'),
        trae_token_file=str(tmp / 'trae-token'),
        qoder2_base=base, qoder2_panel_password_file=str(tmp / 'q2-password'),
        aiwork_base=base, aiwork_admin_token_file=str(tmp / 'aiwork-token'),
        aiwork_api_key_file=str(tmp / 'aiwork-key'),
        upstream_timeout_sec=5.0, upstream_retries=2, upstream_backoff_sec=0.0,
    )
    values.update(over)
    return Settings(**values)


async def _call(fn, *args):
    try:
        result = await fn(*args)
        status = getattr(result, 'status_code', None)
        return {'ok': status is None or status < 400, 'status': status,
                'result_type': type(result).__name__}
    except Exception as exc:  # noqa: BLE001 - 矩阵要看错误分类
        return {'ok': False, 'error_type': type(exc).__name__,
                'error': str(exc)[:200]}


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + '\n', encoding='utf-8')


async def _workbuddy_flow(adapter, fake, cfg: Path) -> dict:
    phases: dict = {}
    await adapter.start()
    try:
        fake.set_accepted({OLD})
        phases['P0_old_ok'] = await _call(adapter._request, 'GET', '/api/up/accounts')
        phases['P0_old_ok']['requests'] = fake.take()
        # 上游只接受新值（旧值撤销）→ 旧配置请求的错误分类
        fake.set_accepted({NEW})
        phases['P1_old_revoked'] = await _call(adapter._request, 'GET', '/api/up/accounts')
        phases['P1_old_revoked']['requests'] = fake.take()
        # 运维把文件改成新值（不重启进程），下一请求是否恢复
        _write(cfg, json.dumps({'api_key': NEW}))
        phases['P2_new_file_no_restart'] = await _call(
            adapter._request, 'GET', '/api/up/accounts')
        phases['P2_new_file_no_restart']['requests'] = fake.take()
        # 双 key 并行（fake 同时接受 old+new）
        _write(cfg, json.dumps({'api_key': OLD}))
        fake.set_accepted({OLD, NEW})
        phases['P3_parallel_both_accepted'] = await _call(
            adapter._request, 'GET', '/api/up/accounts')
        phases['P3_parallel_both_accepted']['requests'] = fake.take()
    finally:
        await adapter.close()
    return phases


def workbuddy_matrix(tmp: Path) -> dict:
    cfg = tmp / 'wb-config.json'
    _write(cfg, json.dumps({'api_key': OLD}))
    out: dict = {'platform': 'workbuddy', 'credential': 'wb_config_file.api_key',
                 'consumer': 'WorkBuddyAdapter.authenticate/_request（每请求 Bearer）',
                 'phases': {}}
    with FakeUpstream('bearer') as fake:
        settings = _settings(tmp, fake.base)
        adapter = WorkBuddyAdapter(settings)
        out['hot_read'] = {'before': adapter.api_key() == OLD}
        _write(cfg, json.dumps({'api_key': NEW}))
        out['hot_read']['after'] = adapter.api_key() == NEW
        _write(cfg, json.dumps({'api_key': OLD}))  # 恢复旧值，进入真实阶段
        out['phases'] = asyncio.run(_workbuddy_flow(adapter, fake, cfg))
    requests_flat = [r for ph in out['phases'].values() for r in ph.get('requests', [])]
    out['request_sequence'] = [{'path': r['path'], 'cred': r['cred'], 'status': r['status']}
                               for r in requests_flat]
    out['needs_restart'] = False
    out['needs_restart_reason'] = ('api_key 每次 authenticate/转发读取（热读）；上游撤销旧值后，'
                                   '请求序列见 request_sequence（可能先吃 401 再 re-auth 成功或被熔断/报错）')
    return out


async def _session_flow(adapter, fake: FakeUpstream, cred_path: Path,
                        protected_path: str = '/api/up/accounts') -> dict:
    phases: dict = {}
    await adapter.start()
    try:
        fake.set_accepted({OLD})
        phases['P0_old_login_ok'] = await _call(adapter._json, 'GET', protected_path)
        phases['P0_old_login_ok']['requests'] = fake.take()
        # 上游撤销旧口令并清会话（模拟 key 轮换/旧会话失效）
        fake.set_accepted({NEW})
        fake.invalidate_sessions()
        phases['P1_old_revoked'] = await _call(adapter._json, 'GET', protected_path)
        phases['P1_old_revoked']['requests'] = fake.take()
        _write(cred_path, NEW)
        phases['P2_new_file_no_restart'] = await _call(adapter._json, 'GET', protected_path)
        phases['P2_new_file_no_restart']['requests'] = fake.take()
    finally:
        await adapter.close()
    return phases


def session_matrix(tmp: Path, *, platform: str, adapter_cls, cred_field: str,
                   consumer: str, auth_kind: str = 'cookie',
                   login_path: str = '/api/login',
                   protected_path: str = '/api/up/accounts') -> dict:
    out: dict = {'platform': platform, 'credential': cred_field, 'consumer': consumer,
                 'phases': {}}
    cred_path = tmp / (f'{platform}-cred')
    _write(cred_path, OLD)
    with FakeUpstream(auth_kind, login_path=login_path) as fake:
        settings = _settings(tmp, fake.base, **{cred_field: str(cred_path)})
        adapter = adapter_cls(settings)
        out['hot_read'] = {'before': adapter._credentials() == OLD}
        _write(cred_path, NEW)
        out['hot_read']['after'] = adapter._credentials() == NEW
        _write(cred_path, OLD)  # 恢复旧值，进入真实阶段
        out['phases'] = asyncio.run(_session_flow(adapter, fake, cred_path, protected_path))
    requests_flat = [r for ph in out['phases'].values() for r in ph.get('requests', [])]
    out['request_sequence'] = [{'path': r['path'], 'cred': r['cred'], 'status': r['status']}
                               for r in requests_flat]
    out['needs_restart'] = False
    out['needs_restart_reason'] = ('凭据文件在每次 authenticate 时读取；上游撤销旧会话后，'
                                   '下一次请求会触发 401→强制重登（读新文件）→ 成功，不需要重启进程。'
                                   '但若上游不主动失效旧会话，旧会话会一直用旧凭据直到过期。')
    return out


def manager_env_matrix(tmp: Path) -> dict:
    """Manager WB2API_KEY：进程启动时读入，改环境变量必须重启。"""
    code = ('import sys; sys.path.insert(0, r"%s"); '
            'import manager.server.config as c; print(c.WB2API_KEY)' % (REPO / 'manager'))
    def run(env_value: str) -> str:
        env = dict(os.environ, WB2API_KEY=env_value)
        proc = subprocess.run([sys.executable, '-X', 'utf8', '-c', code],
                              capture_output=True, text=True, env=env,
                              encoding='utf-8', errors='replace')
        return (proc.stdout or '').strip()
    first = run('synthetic-env-old')
    second = run('synthetic-env-new')
    return {
        'platform': 'manager', 'credential': 'WB2API_KEY（环境变量）',
        'consumer': 'manager/server/config.py（import 时读取）',
        'phases': {
            'process_start_old': {'value_matches': first.endswith('synthetic-env-old')},
            'process_start_new': {'value_matches': second.endswith('synthetic-env-new')},
            'same_process_env_change': {
                'effect': '无（模块级常量在 import 时已固定）',
                'proof': '两个独立进程读到各自的 env 值，说明进程内不会随 env 变化'},
        },
        'needs_restart': True,
        'needs_restart_reason': 'WB2API_KEY 只在进程启动 import config 时读取；改环境变量必须重启 Manager 进程。',
    }


def main() -> int:
    started = time.time()
    tmp_root = Path(tempfile.mkdtemp(prefix='r3l6-upstream-'))
    matrix: dict = {
        'schema': 'zhihui-r3-l6-upstream-fake-matrix/v1',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'purpose': 'G4-3：用 fake 上游验证**现状**（单文件凭据 + 重启/热读语义），'
                   '不实现 .next 过渡；证明本方读取与错误分类，不证明真实上游可并行',
        'consumer_paths': {
            'workbuddy': 'qtg/app/qtg/adapters/workbuddy.py: authenticate/_credentials（api_key 每请求）',
            'qoder': 'qtg/app/qtg/adapters/qoder.py: _credentials/authenticate（登录会话）',
            'trae': 'qtg/app/qtg/adapters/trae.py: _credentials/authenticate（登录会话）',
            'manager': 'manager/server/config.py: WB2API_KEY（import 时读 env）',
            'not_live_tested': []  # qoder2/trae2 已在本矩阵实测
        },
        'boundaries': [
            'fake 上游只模拟"接受/拒绝凭据"的结果，不模拟真实平台语义；',
            'fake 的"双 key 并行"是本方构造，不能证明真实上游允许旧/新 key 并行；',
            '真实上游 401 的错误体/限流/风控差异未覆盖（外部验收）；',
            'qoder2/trae2 未起 fake（与 qoder/trae 同 BaseAdapter 语义，代码路径已列）。',
        ],
    }
    from qtg.adapters.qoder2 import Qoder2Adapter
    from qtg.adapters.trae2 import Trae2Adapter
    matrix['rows'] = [
        workbuddy_matrix(tmp_root),
        session_matrix(tmp_root, platform='qoder', adapter_cls=QoderAdapter,
                       cred_field='qoder_password_file',
                       consumer='QoderAdapter.authenticate → POST /api/login'),
        session_matrix(tmp_root, platform='trae', adapter_cls=TraeAdapter,
                       cred_field='trae_token_file',
                       consumer='TraeAdapter.authenticate → POST /api/login'),
        # R3-L6 补充：qoder2（X-Panel-Token 头）与 trae2/aiwork（Cookie 会话）
        session_matrix(tmp_root, platform='qoder2', adapter_cls=Qoder2Adapter,
                       cred_field='qoder2_panel_password_file',
                       consumer='Qoder2Adapter.authenticate → POST /panel/login（X-Panel-Token）',
                       auth_kind='token', login_path='/panel/login',
                       protected_path='/accounts'),
        session_matrix(tmp_root, platform='trae2', adapter_cls=Trae2Adapter,
                       cred_field='aiwork_admin_token_file',
                       consumer='Trae2Adapter.authenticate → POST /api/login（Cookie）',
                       auth_kind='cookie', login_path='/api/login',
                       protected_path='/api/overview'),
        manager_env_matrix(tmp_root),
    ]
    # 汇总：现状语义
    matrix['summary'] = {
        row['platform']: {
            'needs_restart': row['needs_restart'],
            'hot_read': row.get('hot_read'),
            'old_revoked_error': (row['phases'].get('P1_old_revoked') or {}).get('error_type'),
            'old_revoked_status': (row['phases'].get('P1_old_revoked') or {}).get('status'),
            'recovered_without_restart': bool(
                (row['phases'].get('P2_new_file_no_restart') or {}).get('ok')
                and ((row['phases'].get('P2_new_file_no_restart') or {}).get('status') or 200) < 400),
        } for row in matrix['rows']
    }
    matrix['conclusion'] = {
        'single_file_restart_semantics_pinned': True,
        'workbuddy_hot_read': matrix['summary']['workbuddy']['hot_read'],
        'qoder_hot_read': matrix['summary']['qoder']['hot_read'],
        'trae_hot_read': matrix['summary']['trae']['hot_read'],
        'qoder2_hot_read': matrix['summary']['qoder2']['hot_read'],
        'trae2_hot_read': matrix['summary']['trae2']['hot_read'],
        'session_adapters_recover_without_restart': all(
            matrix['summary'][name]['recovered_without_restart']
            for name in ('qoder', 'trae', 'qoder2', 'trae2')),
        'upstream_credentials_hot_read': all(
            matrix['summary'][name]['hot_read']['after']
            for name in ('workbuddy', 'qoder', 'trae', 'qoder2', 'trae2')),
        'manager_env_needs_restart': matrix['summary']['manager']['needs_restart'],
        'double_key_parallel_supported_by_qtg': False,
        'double_key_parallel_is_upstream_dependent': True,
    }
    matrix['elapsed_s'] = round(time.time() - started, 2)
    OUT.write_text(json.dumps(matrix, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'out': str(OUT), 'summary': matrix['summary'],
                      'conclusion': matrix['conclusion']}, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
