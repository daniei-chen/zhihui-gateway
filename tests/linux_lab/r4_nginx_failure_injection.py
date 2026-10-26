"""R4 批次B：真实 Nginx/TLS/SSE 故障注入演练（仅 disposable local VM）。

sc1：B 真实 draining→预检真实 503→控制器拒绝切换（配置 sha256+worker pid 集合+error_log 三重
证据）；sc2：配置改写阶段写入实验 prefix 内语法错误→真实 nginx -t 失败→真 rollback resume A 并真
reload 恢复；sc3：reload 前真 drain B→入口探测真失败→不得排空 A；sc4：破坏实验 prefix 的 include
→rollback 自身真 nginx -t 再失败→子进程解析 timeline rollback_done(ok=false 且 errors 非空) 返回
78。唯一注入点是子进程内包装 reload_nginx，其余全真实；既有脚本只读 import。退出码 0/10/20/30/40。
报告不把失败前 200 当恢复，不写零宕机结论（quiet 探针只给观测上界）。
"""
from __future__ import annotations

import argparse, datetime as dt, hashlib, importlib.util, json, os, posixpath
import re, signal, socket, ssl, sqlite3, subprocess, sys, threading, time
import urllib.error, urllib.request
from pathlib import Path, PurePosixPath

LAB = Path('/home/gateway-lab/lab'); INCLUDE_NAME = 'static-cache.server.conf'
LAB_HOST, LAB_HOME = 'zhihui-local-lab', '/home/gateway-lab'
MARK_BEGIN, MARK_END = '# >>> manager_upstream', '# <<< manager_upstream'
EXIT_OK, EXIT_MANUAL, EXIT_MISMATCH, EXIT_PRECONDITION, EXIT_CLEANUP = 0, 10, 20, 30, 40
CHILD_OK, CHILD_REAL_FAILURE, CHILD_ROLLBACK_FAILED = 0, 1, 78
SCENARIOS = ('sc1', 'sc2', 'sc3', 'sc4')
MODES = {'sc1': 'drain-then-switch', 'sc2': 'drain-then-switch', 'sc3': 'switch-then-drain', 'sc4': 'switch-then-drain'}
_MODULES: dict = {}
# ─────────── 纯函数：判定与真实 IO 分离（Windows 可单测） ───────────
def sha256_text(text): return hashlib.sha256(text.encode('utf-8')).hexdigest()
def sha256_bytes(data): return hashlib.sha256(data).hexdigest()
def markers_intact(text): return MARK_BEGIN in text and MARK_END in text
def inject_syntax_error(text):
    line, index = '\nr4_injected_unknown_directive on;\n', text.find(MARK_END); return text + line if index < 0 else text[:index + len(MARK_END)] + line + text[index + len(MARK_END):]
def inject_include_error(text): return text.rstrip() + '\nr4_injected_include_directive on;\n'
def upstream_ports(text):
    begin, end = text.find(MARK_BEGIN), text.find(MARK_END); return [] if begin < 0 or end < begin else [int(v) for v in re.findall(r'server 127\.0\.0\.1:(\d+)', text[begin:end])]
def classify_nginx_t(rc, stdout, stderr):
    """把一次真实 nginx -t 归类；rc=0 永远不能算 injected 失败。"""
    if rc is None: return {'ran': False, 'exit_code': None, 'failed': False, 'kind': 'not_run', 'emerg_lines': []}
    failed, combined = rc != 0, ((stdout or '') + '\n' + (stderr or '')).lower()
    emerg = [ln for ln in ((stderr or '') + '\n' + (stdout or '')).splitlines() if '[emerg]' in ln or '[error]' in ln]
    syntax = failed and '[emerg]' in combined and any(t in combined for t in ('unknown directive', 'syntax error', 'unexpected', 'directive is not allowed'))
    return {'ran': True, 'exit_code': int(rc), 'failed': failed, 'kind': 'ok' if not failed else ('syntax_error' if syntax else 'other_failure'), 'emerg_lines': emerg[:6]}
def _inside(child, parent):
    return bool(child and parent) and PurePosixPath(posixpath.normpath(str(child))).is_relative_to(PurePosixPath(posixpath.normpath(str(parent))))
def boundary_violations(facts):
    """边界事实 → 违规列表；IO 由调用方采集成 facts，本函数纯逻辑。"""
    lab, root = facts.get('lab') or str(LAB), facts.get('root') or ''
    checks = [('hostname_not_lab', facts.get('hostname') != LAB_HOST), ('home_not_gateway_lab', facts.get('home') != LAB_HOME),
              ('qa_file_missing', facts.get('qa_exists') is not True), ('qa_outside_lab', not _inside(facts.get('qa_path') or '', lab)),
              ('production_connected_not_false', facts.get('production_connected') is not False), ('workspace_outside_snapshots', not _inside(facts.get('workspace') or '', lab + '/snapshots')),
              ('out_outside_lab', not _inside(facts.get('out') or '', lab)), ('env_script_missing', facts.get('env_script_exists') is not True),
              ('switch_script_missing', facts.get('switch_script_exists') is not True), ('host_port_in_use', facts.get('host_port_free') is not True)]
    if facts.get('state_ready') is not True: return [name for name, failed in checks if failed]
    instances = facts.get('instances') or []
    two = len(instances) == 2
    checks += [('experiment_root_outside_lab', not _inside(root, lab)), ('nginx_conf_outside_experiment', not _inside(facts.get('nginx_conf') or '', root) or str(facts.get('nginx_conf') or '').startswith('/etc/nginx')),
               ('need_two_instances', not two), ('duplicate_instance_ports', two and len({row.get('port') for row in instances}) != 2),
               ('instance_not_alive', two and not all(row.get('alive') is True for row in instances)), ('sudo_unavailable', facts.get('sudo_ok') is False)]
    return [name for name, failed in checks if failed]
def select_scenarios(requested, available=SCENARIOS):
    if requested is None: return {'selected': list(available), 'errors': []}
    chosen, errors = list(requested), []; unknown = [name for name in chosen if name not in available]
    if unknown: errors.append('unknown_scenarios:' + ','.join(unknown))
    if len(set(chosen)) != len(chosen): errors.append('duplicate_scenarios')
    return {'selected': [name for name in chosen if name in available], 'errors': errors}
def _view(row):
    return None if row is None else {'t_start_ms': round(row['t_start'] * 1000, 3), 't_end_ms': round(row['t_end'] * 1000, 3), 'status': row.get('status'), 'error': row.get('error')}


def probe_envelope(samples, win_start, win_end, mode='quiet'):
    """单一单调时钟上的探针包络；quiet 只给观测上界，outage 量失败窗口。"""
    rows, invalid = sorted(samples, key=lambda row: row['t_start']), []
    if len(rows) < 20: invalid.append('fewer_than_20_probes')
    abnormal = [row for row in rows if row.get('error') or (row.get('status') is not None and row['status'] != 200)]
    first_bad, last_bad = (abnormal[0], abnormal[-1]) if abnormal else (None, None)
    recovery = next((row for row in rows if row.get('status') == 200 and row['t_start'] >= last_bad['t_end']), None) if last_bad else None
    coverage = ([rows[0]['t_end'] - win_start, win_end - rows[-1]['t_start']] + [b['t_start'] - a['t_start'] for a, b in zip(rows, rows[1:])]) if rows else []
    measured = round((recovery['t_end'] - first_bad['t_start']) * 1000, 3) if first_bad and recovery else None; upper = round(max(coverage) * 1000, 3) if coverage else None
    if mode == 'outage':
        invalid += ['no_failure_observed'] if first_bad is None else []
        invalid += ['no_recovery_after_last_failure'] if last_bad is not None and recovery is None else []
    else:
        invalid += ['failure_fraction_above_5_percent'] if rows and len(abnormal) / len(rows) > .05 else []
        invalid += ['no_recovery_after_last_failure'] if abnormal and recovery is None else []
    return {'mode': mode, 'samples': len(rows), 'abnormal_count': len(abnormal), 'window_start_ms': round(win_start * 1000, 3), 'window_end_ms': round(win_end * 1000, 3), 'first_abnormal': _view(first_bad), 'last_abnormal': _view(last_bad), 'recovery': _view(recovery), 'measured_ms': measured, 'coverage_upper_bound_ms': upper, 'invalid_reasons': invalid, 'definition': 'first failure to recovery after the last failure; quiet mode reports an observation upper bound, never a zero-outage claim'}
def sse_evidence(record):
    terminal = record.get('terminal')
    return {'accepted': record.get('status') == 200, 'status': record.get('status'), 'first_frame_ms': record.get('first_frame_ms'), 'frames': record.get('frames'), 'terminal': terminal, 'cut': terminal == 'stream_cut', 'terminal_recorded': terminal is not None, 'request_id': record.get('request_id'), 'error': record.get('error'), 't_start_ms': record.get('t_start_ms'), 't_end_ms': record.get('t_end_ms')}
def ledger_verdict(ledger):
    bad = [key + '=' + str(ledger.get(key)) for key in ('duplicate_request_ids', 'unexpected_outcome_without_log', 'settled_reservation_without_log', 'open_or_unknown_reservations') if ledger.get(key) != 0]
    return {'ok': not bad, 'reasons': bad}
def _add_common(ev, reasons):
    sse = ev.get('sse') or {}
    for name, ok in (('sse_not_accepted', sse.get('accepted') is True), ('sse_first_frame_missing', sse.get('first_frame_ms') is not None), ('sse_terminal_missing', sse.get('terminal') is not None), ('sse_not_accepted_before_injection', sse.get('first_frame_before_injection') is True), ('ledger_unbalanced', ledger_verdict(ev.get('ledger') or {})['ok'])):
        if not ok: reasons.append(name)
def _judge(ev, checks, expected):
    reasons = [name for name, ok in checks if not ok]; _add_common(ev, reasons)
    return {'matched': not reasons, 'reasons': reasons, 'expected': expected}
def _recovery_ok(probe):
    last, recovery = probe.get('last_abnormal'), probe.get('recovery'); return probe.get('mode') == 'outage' and last is not None and recovery is not None and probe.get('measured_ms') is not None and recovery['t_start_ms'] >= last['t_end_ms'] and probe.get('coverage_upper_bound_ms') is not None
def judge_scenario1(ev):
    a, pre, probe, cfg, rel = (ev.get('attempt') or {}), (ev.get('preconditions') or {}), (ev.get('probe') or {}), (ev.get('config') or {}), (ev.get('reload') or {})
    names = a.get('event_names') or []
    return _judge(ev, [
        ('controller_did_not_fail', a.get('child_exit_code') not in (None, 0)), ('failure_reason_not_candidate_unready', 'candidate not ready' in str(a.get('failure_message'))),
        ('candidate_503_not_observed', pre.get('b_readyz') == 503), ('config_not_unchanged', cfg.get('unchanged') is True and cfg.get('sha256_before') == cfg.get('sha256_after_rollback')),
        ('reload_not_observed', rel.get('observed') == 0 and rel.get('worker_pids_same') is True and rel.get('error_log_reload_lines') == 0), ('old_instance_drained', 't_drain_signal' not in names),
        ('rewrite_stage_reached', 't_switch_reload' not in names), ('old_not_serving_after', ev.get('a_readyz_after') == 200),
        ('unexpected_front_failure', probe.get('abnormal_count') == 0 and not probe.get('invalid_reasons')), ('no_observation_upper_bound', probe.get('coverage_upper_bound_ms') is not None),
        ('accepted_sse_not_completed', (ev.get('sse') or {}).get('terminal') == 'completed'),
    ], 'candidate 503 rejected; old instance, config and nginx untouched')
def judge_scenario2(ev):
    a, inj, cfg, rel, rb, probe = (ev.get('attempt') or {}), (ev.get('injection') or {}), (ev.get('config') or {}), (ev.get('reload') or {}), (ev.get('rollback') or {}), (ev.get('probe') or {})
    runs = inj.get('nginx_t_runs') or []
    first, last = (runs[0] if runs else {}), (runs[-1] if runs else {})
    first_class = first.get('classification') or {}
    return _judge(ev, [
        ('injection_kind_wrong', inj.get('kind') == 'config_syntax_error'), ('injected_nginx_t_not_syntax_error', first_class.get('kind') == 'syntax_error'),
        ('injected_nginx_t_did_not_fail', first_class.get('failed') is True and '[emerg]' in (first.get('stderr') or '')), ('controller_hid_the_failure', a.get('child_exit_code') not in (None, 0)),
        ('failure_reason_not_nginx_t', 'nginx -t failed' in str(a.get('failure_message'))), ('rollback_not_reported_ok', rb.get('present') is True and rb.get('ok') is True and not rb.get('errors')),
        ('config_not_restored', cfg.get('sha256_after_rollback') == cfg.get('sha256_before') and cfg.get('markers_intact') is True), ('old_instance_never_drained', 't_drain_signal' in (a.get('event_names') or [])),
        ('recovery_nginx_t_not_ok', (last.get('classification') or {}).get('kind') == 'ok' and (ev.get('nginx_t_after') or {}).get('kind') == 'ok'), ('recovery_reload_not_observed', rel.get('observed') == 1),
        ('failure_window_or_recovery_missing', _recovery_ok(probe)), ('front_not_200_after_recovery', ev.get('front_after_recovery') == 200),
    ], 'real nginx -t syntax failure; original config restored and reloaded')
def judge_scenario3(ev):
    a, pre, post, cfg, rel, rb, probe = (ev.get('attempt') or {}), (ev.get('preconditions') or {}), (ev.get('postconditions') or {}), (ev.get('config') or {}), (ev.get('reload') or {}), (ev.get('rollback') or {}), (ev.get('probe') or {})
    return _judge(ev, [
        ('candidate_not_ready_before', pre.get('b_readyz') == 200), ('controller_hid_the_failure', a.get('child_exit_code') not in (None, 0)),
        ('refusal_reason_missing', 'refusing to drain old instance' in str(a.get('failure_message'))), ('old_instance_drained', 't_drain_signal' not in (a.get('event_names') or [])),
        ('old_not_serving_after', ev.get('a_readyz_after') == 200), ('candidate_drain_not_observed', post.get('b_readyz') == 503 and (ev.get('injection') or {}).get('b_drained') is True),
        ('config_not_restored', cfg.get('sha256_after_rollback') == cfg.get('sha256_before')), ('rollback_not_reported_ok', rb.get('present') is True and rb.get('ok') is True),
        ('nginx_t_after_not_ok', (ev.get('nginx_t_after') or {}).get('kind') == 'ok'), ('recovery_reload_not_observed', rel.get('observed') == 1),
        ('entry_failure_window_or_recovery_missing', _recovery_ok(probe)), ('front_not_200_after_recovery', ev.get('front_after_recovery') == 200),
    ], 'readiness lost before reload; old instance never drained')
def judge_scenario4(ev):
    a, inj, cfg, rb, manual = (ev.get('attempt') or {}), (ev.get('injection') or {}), (ev.get('config') or {}), (ev.get('rollback') or {}), (ev.get('manual_intervention') or {})
    runs, rec = inj.get('nginx_t_runs') or [], manual.get('recovery') or {}
    return _judge(ev, [
        ('injection_kind_wrong', inj.get('kind') == 'include_syntax_error'), ('need_two_real_nginx_t_runs', len(runs) >= 2),
        ('real_nginx_t_failure_missing', bool(runs) and all((r.get('classification') or {}).get('failed') is True and (r.get('classification') or {}).get('kind') == 'syntax_error' for r in runs)),
        ('rollback_done_missing', rb.get('present') is True), ('rollback_not_observed_as_failed', rb.get('ok') is False and bool(rb.get('errors'))),
        ('manual_exit_code_not_78', a.get('child_exit_code') == CHILD_ROLLBACK_FAILED), ('manual_intervention_not_declared', manual.get('required') is True and manual.get('exit_code') == CHILD_ROLLBACK_FAILED),
        ('manual_recovery_not_verified', manual.get('recovered') is True), ('manual_recovery_nginx_t_not_ok', (rec.get('nginx_t') or {}).get('kind') == 'ok' and rec.get('reload_rc') == 0),
        ('front_not_recovered_after_manual_step', rec.get('front_status') == 200), ('include_not_restored', rec.get('include_sha256') == inj.get('include_sha256_before')),
        ('injection_did_not_change_anything', any(t.get('sha256_after') != t.get('sha256_before') for t in (inj.get('targets') or []))),
        ('nginx_conf_not_restored_by_rollback', cfg.get('sha256_after_rollback') == cfg.get('sha256_before')),
    ], 'rollback really failed; exit 78 + documented manual recovery')
JUDGES = {'sc1': judge_scenario1, 'sc2': judge_scenario2, 'sc3': judge_scenario3, 'sc4': judge_scenario4}
# ─────────── IO 辅助（VM） ───────────
def now_iso(): return dt.datetime.now(dt.timezone.utc).isoformat()
def _ssl_context():
    context = ssl.create_default_context()
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    return context
def _opener(context=None):
    handlers = [urllib.request.ProxyHandler({})] + ([urllib.request.HTTPSHandler(context=context)] if context is not None else [])  # 离线：绝不经代理
    return urllib.request.build_opener(*handlers)
def _status(url, timeout, context=None):
    try:
        with _opener(context).open(url, timeout=timeout) as response: return response.status
    except urllib.error.HTTPError as exc: return exc.code
    except Exception: return None
def _write_bytes(path, data):
    temporary = path.with_name(path.name + '.r4tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)
def _atomic_write(path, text): _write_bytes(path, text.encode('utf-8'))
def _load_sibling(name):
    if name not in _MODULES:
        spec = importlib.util.spec_from_file_location('r4_' + name, Path(__file__).with_name(name + '.py'))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); _MODULES[name] = module
    return _MODULES[name]
def _children_of(parent_pid):
    kids = []
    for entry in Path('/proc').iterdir():
        try:
            stat = (entry / 'stat').read_text()
            if entry.name.isdigit() and int(stat[stat.rindex(')') + 2:].split()[1]) == parent_pid: kids.append(int(entry.name))
        except (OSError, ValueError, IndexError): continue
    return sorted(kids)
class Timeline:
    def __init__(self, path, origin):
        self.origin, self._lock, self._fh = origin, threading.Lock(), path.open('w', encoding='utf-8')
    def rel(self, moment=None): return round(((moment if moment is not None else time.monotonic()) - self.origin) * 1000, 3)
    def emit(self, event, **fields):
        row = {'event': event, 't_rel_ms': self.rel(), 't_wall': round(time.time(), 3), **fields}
        with self._lock:
            self._fh.write(json.dumps(row, ensure_ascii=False) + '\n')
            self._fh.flush()
        return row
    def close(self): self._fh.close()
# ─────────── 宿主演练本体（仅 VM） ───────────
class Drill:
    def __init__(self, args):
        self.args, self.origin, self.out = args, time.monotonic(), Path(args.out).expanduser()
        self.timeline = self.env_proc = None
        self.state, self.env_state_file, self.include_path, self.cleanup_failures = {}, None, None, 0
        self.report = {'schema': 'zhihui-r4-nginx-failure-injection/v1', 'production_connected': False, 'started_at': now_iso(), 'scenarios': {}, 'manual_intervention': False}
    def preflight(self, after=False):
        qa, payload = Path(self.args.qa_file).expanduser(), {}
        try: payload = json.loads(qa.read_text(encoding='utf-8')) if qa.is_file() else {}
        except ValueError: payload = {}
        production, workspace, root, conf, instances = payload.get('production_connected'), payload.get('workspace') or '', None, None, []
        if after:
            instances = [{'port': int(r['port']), 'alive': self._alive(int(r['pid']))} for r in self.state['instances']]
            root, conf = self.state.get('root'), self.state.get('nginx_conf')
            production, workspace = self.state.get('production_connected'), self.state.get('workspace') or ''
        facts = {'hostname': socket.gethostname(), 'home': str(Path.home()), 'lab': str(LAB), 'qa_path': str(qa), 'qa_exists': qa.is_file(), 'production_connected': production, 'workspace': workspace, 'out': str(self.out), 'root': root, 'nginx_conf': conf, 'instances': instances, 'state_ready': after, 'env_script_exists': Path(self.args.env_script).is_file(), 'switch_script_exists': Path(self.args.switch_script).is_file(), 'host_port_free': self._port_free(self.args.host_port) if not after else True, 'sudo_ok': (self.report_t()['classification']['kind'] == 'ok') if after else None}
        if (bad := boundary_violations(facts)):
            print(json.dumps({'error': 'boundary_or_precondition_violation', 'violations': bad, 'injected': False}, ensure_ascii=False), flush=True)
            raise SystemExit(EXIT_PRECONDITION)
    @staticmethod
    def _alive(pid):
        try: os.kill(pid, 0); return True
        except OSError: return False
    @staticmethod
    def _port_free(port):
        with socket.socket() as probe: return probe.connect_ex(('127.0.0.1', int(port))) != 0
    def env_up(self):
        command = [sys.executable, '-u', str(Path(self.args.env_script).resolve()), 'up', '--host-port', str(self.args.host_port)] + (['--qa-file', str(self.args.qa_file)] if self.args.qa_file else [])
        self.env_proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        deadline = time.monotonic() + self.args.env_timeout
        while not self.state and time.monotonic() < deadline:
            line = self.env_proc.stdout.readline()
            if not line and self.env_proc.poll() is not None: raise SystemExit('r3l2_env.py exited early')
            if not line: continue
            if 'R3L2_ENV_READY' in line: self.state = json.loads(line.split('R3L2_ENV_READY', 1)[1].strip()); break
            self.timeline.emit('env_stdout', line=line.strip()[:300])
        if not self.state: raise SystemExit('no R3L2_ENV_READY state')
        self.env_state_file, self.include_path = Path(self.state['root']) / 'env.json', Path(self.state['root']) / INCLUDE_NAME
        self.timeline.emit('environment_ready', host_url=self.state['host_url'], instances=self.state['instances'])
        self.preflight(after=True)
    def env_down(self):
        root = self.state.get('root')
        if root:
            subprocess.run([sys.executable, str(Path(self.args.env_script).resolve()), 'down', '--root', str(root)], capture_output=True, text=True, timeout=180)
        # 异常路径下不能只 terminate 父进程：先等 stop_file 机制清理子 Manager，再兜底。
        instances = self.state.get('instances') or []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and any(self._alive(int(row['pid'])) for row in instances):
            time.sleep(.2)
        for row in instances:
            pid = int(row['pid'])
            if not self._alive(pid): continue
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: continue
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and self._alive(pid): time.sleep(.2)
            if self._alive(pid):
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
                self.cleanup_failures += 1; self.timeline.emit('cleanup_manager_killed', pid=pid)
        if self.env_proc is not None and self.env_proc.poll() is None:
            try: self.env_proc.terminate(); self.env_proc.wait(timeout=60)
            except subprocess.TimeoutExpired: self.env_proc.kill()
        if root:
            pid_file = Path(str(root)) / 'nginx.pid'
            if pid_file.is_file():
                subprocess.run(['sudo', '-n', '/usr/sbin/nginx', '-p', str(root),
                                '-c', str(Path(str(root)) / 'nginx.conf'), '-s', 'quit'],
                               capture_output=True, text=True, timeout=60)
        if not self._port_free(self.args.host_port):
            self.cleanup_failures += 1; self.timeline.emit('cleanup_port_still_bound', port=self.args.host_port)
    def su(self): return _load_sibling('switch_upstream')
    def ports(self): return (int(self.state['instances'][0]['port']), int(self.state['instances'][1]['port']))
    def read_conf(self): return Path(self.state['nginx_conf']).read_text(encoding='utf-8')
    def report_t(self):
        result = subprocess.run(self.su().nginx_command(self.state, '-t'), capture_output=True, text=True, timeout=120)
        return {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr, 'classification': classify_nginx_t(result.returncode, result.stdout, result.stderr)}
    def snapshot(self):
        pid_file, log = Path(self.state['nginx_pid_file']), Path(self.state['root']) / 'nginx-error.log'
        try: master = int(pid_file.read_text().strip()) if pid_file.is_file() else None
        except ValueError: master = None
        return {'sha256': sha256_bytes(Path(self.state['nginx_conf']).read_bytes()), 'master_pid': master, 'worker_pids': _children_of(master) if master else [], 'error_log_size': log.stat().st_size if log.is_file() else 0}
    def reload_evidence(self, before):
        after, log = self.snapshot(), Path(self.state['root']) / 'nginx-error.log'
        new_lines = []
        if log.is_file():
            with log.open('rb') as handle:
                handle.seek(before['error_log_size'])
                chunk = handle.read().decode('utf-8', 'replace')
            new_lines = [line for line in chunk.splitlines() if 'signal process started' in line or 'reconfiguring' in line]
        same = bool(before['worker_pids']) and before['worker_pids'] == after['worker_pids']
        return {'master_pid_before': before['master_pid'], 'master_pid_after': after['master_pid'], 'worker_pids_before': before['worker_pids'], 'worker_pids_after': after['worker_pids'], 'worker_pids_same': same, 'error_log_reload_lines': len(new_lines), 'reload_log_sample': new_lines[:4], 'observed': 1 if (not same or new_lines) else 0}
    def front(self): return _status(self.state['host_url'] + '/api/readyz', 5.0, _ssl_context())
    def direct(self, port): return _status(f'http://127.0.0.1:{port}/api/readyz', 3.0)
    def _wait(self, probe, want, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if probe() == want: return True
            time.sleep(0.1)
        return False
    def start_sampler(self, interval_s):
        stop, samples, context = threading.Event(), [], _ssl_context()
        def run():
            opener, sequence = _opener(context), 0
            while not stop.is_set():
                moment, status, error = time.monotonic(), None, None
                try:
                    with opener.open(self.state['host_url'] + '/api/readyz', timeout=5.0) as response: status = response.status
                except urllib.error.HTTPError as exc: status = exc.code
                except Exception as exc: error = type(exc).__name__ + ': ' + str(exc)[:120]
                samples.append({'seq': sequence, 't_start': self.timeline.rel(moment) / 1000.0, 't_end': self.timeline.rel() / 1000.0, 'status': status, 'error': error})
                sequence += 1; time.sleep(interval_s)
        thread = threading.Thread(target=run, daemon=True); thread.start()
        return {'stop': stop, 'thread': thread, 'samples': samples}
    @staticmethod
    def stop_sampler(sampler): sampler['stop'].set(); sampler['thread'].join(timeout=10); return list(sampler['samples'])
    def spawn_stream(self, tag):
        record = {'status': None, 'first_frame_ms': None, 'frames': 0, 'terminal': None, 'request_id': None, 'error': None, 't_start_ms': self.timeline.rel()}
        key = Path(self.state['api_key_file']).read_text(encoding='utf-8').strip()
        def run():
            body = json.dumps({'model': 'fake-model', 'stream': True, 'max_tokens': 32, 'messages': [{'role': 'user', 'content': 'r4 ' + tag}]}).encode()
            request = urllib.request.Request(self.state['host_url'] + '/v1/chat/completions', data=body, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}, method='POST')
            try:
                with _opener(_ssl_context()).open(request, timeout=self.args.stream_timeout) as response:
                    record['status'], record['request_id'] = response.status, response.headers.get('x-request-id')
                    for raw in response:
                        line = raw.decode('utf-8', 'replace').strip()
                        if not line.startswith('data:'): continue
                        payload = line[5:].strip()
                        if not payload: continue
                        if record['first_frame_ms'] is None: record['first_frame_ms'] = self.timeline.rel()
                        if payload == '[DONE]': record['terminal'] = 'completed'; break
                        record['frames'] += 1
                        if 'response.completed' in payload or 'message_stop' in payload or re.search(r'"finish_reason"\s*:\s*"', payload): record['terminal'] = 'completed'
            except Exception as exc: record['error'] = type(exc).__name__ + ': ' + str(exc)[:200]
            record['t_end_ms'] = self.timeline.rel()
            record['terminal'] = 'http_error' if record['status'] != 200 else (record['terminal'] or 'stream_cut')
        thread = threading.Thread(target=run, daemon=True); thread.start()
        return {'record': record, 'thread': thread}
    def stream_record(self, holder): holder['thread'].join(timeout=self.args.stream_timeout + 10); return holder['record']
    @staticmethod
    def wait_first_frame(holder, timeout):
        for _ in range(int(timeout / 0.02)):
            if holder['record'].get('first_frame_ms') is not None: return True
            time.sleep(0.02)
        return False
    def reconcile(self):
        connection = sqlite3.connect(f"file:{Path(self.state['root']) / 'data' / 'manager.db'}?mode=ro", uri=True)
        count = lambda sql: int(connection.execute(sql).fetchone()[0])
        try:
            return {'request_logs_rows': count('SELECT COUNT(*) FROM request_logs'), 'duplicate_request_ids': count('SELECT COUNT(*) FROM (SELECT request_id FROM request_logs WHERE request_id IS NOT NULL GROUP BY request_id HAVING COUNT(*) > 1)'), 'unexpected_outcome_without_log': count("SELECT COUNT(*) FROM request_outcomes o WHERE o.protocol != 'http' AND o.http_status NOT IN (503, 429) AND NOT EXISTS (SELECT 1 FROM request_logs l WHERE l.request_id = o.request_id)"), 'settled_reservation_without_log': count("SELECT COUNT(*) FROM budget_reservations b WHERE NOT EXISTS (SELECT 1 FROM request_logs l WHERE l.request_id = b.request_id) AND b.state = 'settled'"), 'open_or_unknown_reservations': count("SELECT COUNT(*) FROM budget_reservations WHERE state IN ('unknown','reserved','forwarded')")}
        finally:
            connection.close()
    def run_child(self, name):
        events, child_report = self.out / f'{name}-attempt.ndjson', self.out / f'{name}-child.json'
        command = [sys.executable, '-u', str(Path(__file__).resolve()), '--internal-attempt', name, '--state', str(self.env_state_file), '--events', str(events), '--child-report', str(child_report), '--preflight-timeout', str(self.args.preflight_timeout), '--front-timeout', str(self.args.front_timeout)]
        spawned = self.timeline.rel(); proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=600); finished = self.timeline.rel()
        payload = {}
        try: payload = json.loads(child_report.read_text(encoding='utf-8')) if child_report.is_file() else {}
        except ValueError: payload = {}
        rows = []
        if events.is_file():
            for line in events.read_text(encoding='utf-8').splitlines():
                try: rows.append(json.loads(line))
                except ValueError: pass
        rollback = next((row for row in reversed(rows) if row.get('event') == 'rollback_done'), None)
        return {'scenario': name, 'spawned_rel_ms': spawned, 'finished_rel_ms': finished, 'child_exit_code': proc.returncode, 'failure_message': payload.get('failure_message'), 'injection': payload.get('injection') or {}, 'event_names': [row.get('event') for row in rows], 'rollback': {'present': rollback is not None, 'ok': rollback.get('ok') if rollback else None, 'errors': (rollback.get('errors') if rollback else []) or []}, 'stdout_tail': (proc.stdout or '')[-800:], 'stderr_tail': (proc.stderr or '')[-800:]}
    def cleanup_scenario(self):
        restored = False
        try:
            a, b = self.ports(); self.su().restore(self.state, self.timeline, self.args.front_timeout, 0.2)
            restored = self._wait(lambda: self.direct(a), 200, 30) and self._wait(lambda: self.direct(b), 200, 30) and self._wait(self.front, 200, 30)
        except BaseException as exc: self.timeline.emit('cleanup_error', error=repr(exc)[:300])
        if not restored: self.cleanup_failures += 1; self.timeline.emit('cleanup_not_restored')
    def scenario(self, name):
        mode, su, (a, b) = MODES[name], self.su(), self.ports()
        su.set_upstream_ports(self.state, [a]); su.reload_nginx(self.state)
        for item in self.state['instances']:
            try: su.signal_resume(int(item['pid']))
            except Exception: pass
        if not (self._wait(lambda: self.direct(a), 200, 60) and self._wait(lambda: self.direct(b), 200, 60) and self._wait(self.front, 200, 60)):
            raise RuntimeError('precondition failed: instances/front not ready')
        time.sleep(0.4)  # 让 prepare 的 reload 日志落地，避免算进场景窗口
        self.timeline.emit('scenario_begin', scenario=name, mode=mode)
        before, include_before = self.snapshot(), self.include_path.read_bytes()
        stream = self.spawn_stream(name)
        frame_ok = self.wait_first_frame(stream, 30)
        pre = {'a_port': a, 'b_port': b, 'mode': mode, 'a_readyz': self.direct(a), 'b_readyz': self.direct(b), 'entry_ports': upstream_ports(self.read_conf()), 'config_sha256': before['sha256'], 'sse_first_frame_received': frame_ok, 'nginx_t_before': self.report_t()['classification']}
        if name == 'sc1':
            su.signal_drain(int(self.state['instances'][1]['pid'])); self._wait(lambda: self.direct(b), 503, 10)
            pre['b_readyz'] = self.direct(b); pre['b_draining'] = pre['b_readyz'] == 503
        sampler, window_start = self.start_sampler(self.args.probe_interval_ms / 1000.0), self.timeline.rel()
        try: attempt = self.run_child(name)
        except BaseException as exc: attempt = {'child_exit_code': None, 'failure_message': repr(exc)[:300], 'injection': {}, 'event_names': [], 'spawned_rel_ms': window_start, 'rollback': {'present': False, 'ok': None, 'errors': []}}
        front_after = 200 if self._wait(self.front, 200, self.args.front_timeout) else None; after_child, include_after = self.snapshot(), self.include_path.read_bytes()
        manual, recovery = {'required': False}, {}
        if name == 'sc4':
            _write_bytes(self.include_path, include_before); test = self.report_t()
            reload_rc = subprocess.run(su.nginx_command(self.state, '-s', 'reload'), capture_output=True, text=True, timeout=120).returncode
            recovery = {'include_sha256': sha256_bytes(self.include_path.read_bytes()), 'nginx_t': test['classification'], 'reload_rc': reload_rc, 'front_status': 200 if self._wait(self.front, 200, self.args.front_timeout) else None}
            manual = {'required': bool(attempt.get('child_exit_code') == CHILD_ROLLBACK_FAILED and (attempt.get('rollback') or {}).get('ok') is False), 'exit_code': CHILD_ROLLBACK_FAILED, 'recovery': recovery, 'recovered': recovery['front_status'] == 200}
            injection = attempt.get('injection') or {}
            injection.update({'include_sha256_before': sha256_bytes(include_before), 'include_sha256_after_observed': sha256_bytes(include_after)})
            manual['nginx_t_failures'] = [{'label': r.get('label'), 'exit_code': r.get('exit_code'), 'stderr': r.get('stderr')} for r in injection.get('nginx_t_runs') or []]
            attempt['injection'] = injection
        time.sleep(0.3); samples, window_end = self.stop_sampler(sampler), self.timeline.rel()
        sse = sse_evidence(self.stream_record(stream))
        sse['first_frame_before_injection'] = sse['first_frame_ms'] is not None and sse['first_frame_ms'] < attempt['spawned_rel_ms']
        post = {'a_readyz': self.direct(a), 'b_readyz': self.direct(b), 'front_status': front_after}
        evidence = {'scenario': name, 'mode': mode, 'preconditions': pre, 'attempt': attempt, 'rollback': attempt.get('rollback') or {}, 'injection': attempt.get('injection') or {}, 'manual_intervention': manual, 'recovery': recovery, 'nginx_t_after': self.report_t()['classification'], 'config': {'sha256_before': before['sha256'], 'sha256_after_rollback': after_child['sha256'], 'sha256_after_recovery': self.snapshot()['sha256'], 'unchanged': before['sha256'] == after_child['sha256'], 'markers_intact': markers_intact(self.read_conf())}, 'reload': self.reload_evidence(before), 'probe': probe_envelope(samples, window_start / 1000.0, window_end / 1000.0, 'quiet' if name in ('sc1', 'sc4') else 'outage'), 'sse': sse, 'a_readyz_after': post['a_readyz'], 'postconditions': post, 'front_after_recovery': front_after, 'ledger': self.reconcile()}
        evidence['verdict'] = JUDGES[name](evidence)
        self.timeline.emit('scenario_done', scenario=name, matched=evidence['verdict']['matched'], child_exit_code=attempt.get('child_exit_code'), reasons=evidence['verdict']['reasons'])
        return evidence
    def write_report(self):
        if self.timeline is not None: self.timeline.close()
        self.report['finished_at'], self.report['cleanup_failures'] = now_iso(), self.cleanup_failures; (self.out / 'drill-report.json').write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    def exit_code(self):
        if self.cleanup_failures: return EXIT_CLEANUP
        rows = self.report.get('scenarios') or {}
        if any((row.get('attempt') or {}).get('child_exit_code') == EXIT_PRECONDITION for row in rows.values()): return EXIT_PRECONDITION
        if any(not (row.get('verdict') or {}).get('matched') for row in rows.values()): return EXIT_MISMATCH
        return EXIT_MANUAL if 'sc4' in rows else EXIT_OK
    def run(self):
        selection = select_scenarios(self.args.scenarios)
        if selection['errors']: print(json.dumps({'error': 'bad --scenarios', 'detail': selection['errors']}, ensure_ascii=False), flush=True); return EXIT_PRECONDITION
        self.out = self.out.resolve()
        if not _inside(str(self.out), str(LAB)): print('refusing out outside /home/gateway-lab/lab', flush=True); return EXIT_PRECONDITION
        self.out.mkdir(parents=True, exist_ok=True)
        self.timeline = Timeline(self.out / 'drill-timeline.ndjson', self.origin)
        try: self.preflight()
        except SystemExit: self.write_report(); return EXIT_PRECONDITION
        try:
            self.env_up()
            for name in selection['selected']:
                try: self.report['scenarios'][name] = self.scenario(name)
                except BaseException as exc:
                    self.timeline.emit('scenario_crashed', scenario=name, error=repr(exc)[:500])
                    self.report['scenarios'][name] = {'scenario': name, 'error': repr(exc)[:800], 'attempt': {}, 'verdict': {'matched': False, 'reasons': ['scenario_crashed']}}
                finally: self.cleanup_scenario()
            self.report['manual_intervention'] = bool((self.report['scenarios'].get('sc4') or {}).get('manual_intervention', {}).get('required'))
            self.report['verdict'] = {'scenarios': {name: row['verdict'] for name, row in self.report['scenarios'].items()}, 'manual_intervention': self.report['manual_intervention'], 'definition': 'no zero-outage claim; quiet probes report an upper bound only'}
        finally:
            try: self.env_down()
            except BaseException as exc: self.timeline.emit('env_down_error', error=repr(exc)[:300])
            self.write_report()
        return self.exit_code()
# ─────────── 子进程：唯一注入点 ───────────
class _Boundary(Exception):
    pass
def _assert_state_boundary(state):
    if socket.gethostname() != LAB_HOST or str(Path.home()) != LAB_HOME: raise _Boundary('not the disposable local lab VM')
    if state.get('production_connected') is not False: raise _Boundary('production_connected must be false')
    root, conf = Path(state['root']).resolve(), Path(state['nginx_conf']).resolve()
    if not root.is_relative_to(LAB) or not conf.is_relative_to(root) or str(conf).startswith('/etc/nginx'):
        raise _Boundary('experiment prefix boundary violated')
def _run_nginx_t(su, state, label):
    result = subprocess.run(su.nginx_command(state, '-t'), capture_output=True, text=True, timeout=120)
    return {'label': label, 'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr, 'classification': classify_nginx_t(result.returncode, result.stdout, result.stderr)}
class _Recording:
    def __init__(self, su, path):
        self.rows, self._timeline = [], su.Timeline(path)
    def emit(self, event, **detail):
        row = self._timeline.emit(event, **detail); self.rows.append(row); return row
def _child_attempt(scenario, state, args):
    su, timeline = _load_sibling('switch_upstream'), _Recording(_load_sibling('switch_upstream'), Path(args.events))
    old, new = (int(state['instances'][0]['port']), int(state['instances'][1]['port'])); injection = {'kind': 'none', 'targets': [], 'nginx_t_runs': []}
    original_reload, failure, done = su.reload_nginx, '', {'v': False}
    def inject_file(target, kind, make_bad, label):
        good = target.read_text(encoding='utf-8'); bad = make_bad(good)
        injection['kind'] = kind
        injection['targets'].append({'path': str(target), 'sha256_before': sha256_text(good), 'sha256_after': sha256_text(bad)})
        _atomic_write(target, bad); injection['nginx_t_runs'].append(_run_nginx_t(su, state, label))
    def faulty_reload(current):
        if scenario == 'sc2' and not done['v']:
            done['v'] = True; inject_file(Path(current['nginx_conf']), 'config_syntax_error', inject_syntax_error, 'injected_config')
        elif scenario == 'sc2':
            injection['nginx_t_runs'].append(_run_nginx_t(su, current, 'rollback_restored_config'))
        elif scenario == 'sc4' and not done['v']:
            done['v'] = True; inject_file(Path(current['root']) / INCLUDE_NAME, 'include_syntax_error', inject_include_error, 'injected_include')
        elif scenario == 'sc4':
            injection['nginx_t_runs'].append(_run_nginx_t(su, current, 'rollback_restored_nginx_conf'))
        elif not done['v']:
            done['v'] = True; candidate = current['instances'][1]
            su.signal_drain(int(candidate['pid']))
            injection.update({'kind': 'candidate_drain', 'b_pid': int(candidate['pid']), 'b_drained': True})
        return original_reload(current)
    target = {'sc2': Path(state['nginx_conf']), 'sc4': Path(state['root']) / INCLUDE_NAME}.get(scenario)
    if target is not None:
        resolved, root = target.resolve(), Path(state['root']).resolve()
        if not resolved.is_relative_to(root) or str(resolved).startswith('/etc/nginx'):
            raise _Boundary('injection target outside experiment prefix')
    if scenario in ('sc2', 'sc3', 'sc4'): su.reload_nginx = faulty_reload
    try:
        su.switch(state, timeline, MODES[scenario], old, new, args.preflight_timeout, 0.1)
    except BaseException as exc:
        failure = str(exc) or type(exc).__name__
    finally:
        su.reload_nginx = original_reload
    rollback = next((row for row in reversed(timeline.rows) if row.get('event') == 'rollback_done'), None)
    parsed = {'present': rollback is not None, 'ok': rollback.get('ok') if rollback else None, 'errors': (rollback.get('errors') if rollback else []) or []}
    code = CHILD_ROLLBACK_FAILED if parsed['present'] and parsed['ok'] is False and parsed['errors'] else CHILD_REAL_FAILURE
    return code, failure, {'scenario': scenario, 'failure_message': failure, 'injection': injection, 'rollback_parsed': parsed, 'event_names': [row.get('event') for row in timeline.rows], 'rollback_event': rollback}
def child_main(args):
    report = {'scenario': args.internal_attempt, 'child_exit_code': None, 'failure_message': '', 'injection': {}, 'rollback_parsed': {}, 'boundary_ok': False}
    try:
        state = json.loads(Path(args.state).read_text(encoding='utf-8'))
        _assert_state_boundary(state)
        report['boundary_ok'] = True
        code, failure, payload = _child_attempt(args.internal_attempt, state, args)
        report.update(payload); report.update({'child_exit_code': code, 'failure_message': failure})
        return code
    except _Boundary as exc: report.update({'error': 'boundary_violation: ' + str(exc), 'child_exit_code': EXIT_PRECONDITION}); return EXIT_PRECONDITION
    except BaseException as exc: report.update({'error': repr(exc)[:500], 'child_exit_code': CHILD_REAL_FAILURE}); return CHILD_REAL_FAILURE
    finally:
        Path(args.child_report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
# ─────────── CLI ───────────
def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', default='/home/gateway-lab/lab/r4-failure-runs/run'); parser.add_argument('--qa-file', default='/home/gateway-lab/lab/current-qa.json')
    parser.add_argument('--host-port', type=int, default=443); parser.add_argument('--env-script', default=str(Path(__file__).with_name('r3l2_env.py')))
    parser.add_argument('--switch-script', default=str(Path(__file__).with_name('switch_upstream.py'))); parser.add_argument('--scenarios', nargs='*', default=None)
    parser.add_argument('--probe-interval-ms', type=int, default=20); parser.add_argument('--preflight-timeout', type=float, default=5.0)
    parser.add_argument('--front-timeout', type=float, default=10.0); parser.add_argument('--env-timeout', type=float, default=420.0)
    parser.add_argument('--stream-timeout', type=float, default=120.0); parser.add_argument('--internal-attempt', choices=SCENARIOS, default=None)
    parser.add_argument('--state', default=None); parser.add_argument('--events', default=None); parser.add_argument('--child-report', default=None); return parser
def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.internal_attempt:
        if not (args.state and args.events and args.child_report): print('internal attempt requires --state/--events/--child-report', flush=True); return EXIT_PRECONDITION
        return child_main(args)
    drill = Drill(args)
    try: code = drill.run()
    except SystemExit as exc: code = exc.code if isinstance(exc.code, int) else EXIT_PRECONDITION
    print(json.dumps({'exit_code': code, 'verdict': drill.report.get('verdict'), 'manual_intervention': drill.report.get('manual_intervention'), 'report_path': str(drill.out / 'drill-report.json')}, ensure_ascii=False, indent=2), flush=True)
    return code
if __name__ == '__main__':
    raise SystemExit(main())
