"""R4 批次C：业务写者交接失败演练 —— ``b_start_failure``（仅 disposable local VM）。

串行协议（全程复用 ``r3l2_handoff`` 的真实类与只读 DB 连接，不复制其实现）：

t0  A serving 并完成一条真实推理（request_logs 落账）
t1  POST /api/system/drain {action:begin} → readyz 503（停止新准入）
t2  已准入长流继续跑完 → 在途归零；drain 后新请求被 503 gateway_draining 拒绝
t3  A 正常退出 → 应用自身 release 被真实观察（role=stopped、owner 空、lease_until=0、
    fence 不变；未观察到即失败，不拿日志冒充）
t4  B 用**同一个 launcher 与同一份 env**启动，只把 ``R3L2_MANAGER_ROOT`` 指向实验内
    不存在的路径 → 真实进程真实非零退出；验证无身份文件、DB 无 B 的 claim 行、
    B 端口无服务
t5  明确"恢复 A"的命令级步骤并执行：启动 A2（新进程新身份）→ readyz 200 →
    一条真实推理 200 且落账 → 账本四表对账（不重不漏、tokens 一致）

判定只认行为断言；报告 JSON 的 branches.b_start_failure 携带各步证据。退出码：
0=pass，1=fail，2=边界（未注入、未创建 out）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import posixpath
import socket
import sqlite3
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from r3l2_handoff import HandoffExperiment, Instance, public_instance_info

LAB = Path('/home/gateway-lab/lab')
EXIT_PASS, EXIT_FAIL, EXIT_BOUNDARY = 0, 1, 2
READY_TIMEOUT = 3.0


# ─────────── 纯函数：判定/边界/账本/命令构造（Windows 可单测） ───────────
def _inside(child, parent):
    return bool(child and parent) and PurePosixPath(posixpath.normpath(str(child))).is_relative_to(
        PurePosixPath(posixpath.normpath(str(parent))))


def guard_violations(facts):
    """边界事实 → 违规列表；IO 由调用方采集，本函数纯逻辑。"""
    bad, lab = [], facts.get('lab') or str(LAB)
    if facts.get('hostname') != 'zhihui-local-lab': bad.append('hostname_not_lab')
    if facts.get('home') != '/home/gateway-lab': bad.append('home_not_gateway_lab')
    if not _inside(facts.get('repo') or '', lab + '/snapshots'): bad.append('repo_outside_snapshots')
    if not _inside(facts.get('out') or '', lab): bad.append('out_outside_lab')
    if not _inside(facts.get('qa_file') or '', lab): bad.append('qa_outside_lab')
    if facts.get('qa_exists') is not True: bad.append('qa_file_missing')
    if facts.get('out_exists') is not False: bad.append('out_exists_refuse_overwrite')
    if facts.get('production_connected') is not False: bad.append('production_connected_not_false')
    if facts.get('qa_workspace') != facts.get('repo'): bad.append('qa_workspace_mismatch')
    return bad


def row_summary(row):
    row = row or {}
    return {'instance_id': row.get('instance_id'), 'owner': row.get('owner'),
            'fence': row.get('fence'), 'role': row.get('role'),
            'lease_until': row.get('lease_until')}


def release_observed(row, fence):
    """A 的真实 release 必须逐字段成立；任何一项缺失都算未观察。"""
    reasons = []
    row = row or {}
    if not row: reasons.append('release_row_missing')
    if row.get('role') != 'stopped': reasons.append('role_not_stopped')
    if row.get('owner'): reasons.append('owner_not_empty')
    if int(row.get('lease_until') or 0) != 0: reasons.append('lease_until_not_zero')
    if int(row.get('fence') or 0) != int(fence or 0): reasons.append('fence_changed')
    return {'observed': not reasons, 'reasons': reasons}


def new_instance_rows(before, after):
    seen = {row.get('instance_id') for row in before or []}
    return [row for row in after or [] if row.get('instance_id') not in seen]


def b_start_failure_observed(ev):
    """B 必须是真实启动失败：非零退出 + 无身份文件 + DB 无新 claim 行 + 端口无服务。"""
    reasons = []
    rc = ev.get('returncode')
    if rc is None: reasons.append('b_exit_code_missing')
    elif int(rc) == 0: reasons.append('b_exit_code_zero')
    if ev.get('identity_file_present') is not False: reasons.append('b_identity_file_present')
    if ev.get('new_instance_rows'): reasons.append('b_claimed_instance_row')
    if ev.get('readyz_status') == 200: reasons.append('b_serving_200')
    if ev.get('timed_out'): reasons.append('b_start_timeout')
    return {'failed': not reasons, 'reasons': reasons}


def a_baseline_observed(ev):
    """A 必须先在 serving 状态完成一条真实推理并落账。"""
    reasons = []
    if ev.get('readyz_status') != 200: reasons.append('a_readyz_not_200')
    inference = ev.get('inference') or {}
    if inference.get('status') != 200: reasons.append('a_inference_not_200')
    if inference.get('terminal') != 'completed': reasons.append('a_inference_not_completed')
    if ev.get('log_landed') is not True: reasons.append('a_request_log_missing')
    return {'ok': not reasons, 'reasons': reasons}


def a_drain_observed(ev):
    """drain 必须真实停止新准入、拒新请求，并让已准入流**正常完成**（在途归零）。"""
    reasons = []
    if ev.get('begin_status') != 200: reasons.append('drain_begin_not_200')
    if ev.get('readyz_503') is not True: reasons.append('readyz_not_503')
    if ev.get('in_flight_accepted') is not True: reasons.append('in_flight_not_accepted')
    terminal = ev.get('in_flight_terminal')
    if terminal == 'stream_cut': reasons.append('inflight_stream_cut')
    elif terminal != 'completed': reasons.append('in_flight_terminal_not_completed')
    if (ev.get('rejected_after_drain') or {}).get('rejected') is not True:
        reasons.append('new_admission_not_rejected')
    return {'ok': not reasons, 'reasons': reasons}


def a2_recovery_observed(ev):
    """恢复 A 必须：readyz 200 + 一条推理 200 且完成并落账 + 新身份 + 真实监听端口。"""
    reasons = []
    if ev.get('readyz_status') != 200: reasons.append('a2_readyz_not_200')
    if ev.get('inference_status') != 200: reasons.append('a2_inference_not_200')
    if ev.get('inference_terminal') != 'completed': reasons.append('a2_inference_not_completed')
    if ev.get('log_landed') is not True: reasons.append('a2_request_log_missing')
    if ev.get('new_instance_id') is not True: reasons.append('a2_reused_old_identity')
    port = ev.get('port')
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        reasons.append('a2_port_invalid')
    return {'ok': not reasons, 'reasons': reasons}


def ledger_verdict(ledger):
    reasons = []
    for key in ('duplicate_request_ids', 'unexpected_outcome_without_log',
                'settled_reservation_without_log', 'open_or_unknown_reservations'):
        if int((ledger or {}).get(key) or 0) != 0:
            reasons.append(key + '=' + str((ledger or {}).get(key)))
    if int((ledger or {}).get('request_logs_tokens') or 0) != int((ledger or {}).get('usage_daily_tokens') or 0):
        reasons.append('tokens_mismatch')
    return {'ok': not reasons, 'reasons': reasons}


def verdict(evidence):
    branch = (evidence.get('branches') or {}).get('b_start_failure') or {}
    baseline = a_baseline_observed(branch.get('a_baseline') or {})
    drain = a_drain_observed(branch.get('a_drain') or {})
    release = release_observed((branch.get('a_release') or {}).get('row'),
                               (branch.get('a_release') or {}).get('fence_before'))
    b_fail = b_start_failure_observed(branch.get('b_start_failure') or {})
    a2 = a2_recovery_observed(branch.get('a2_recovery') or {})
    ledger = ledger_verdict(evidence.get('ledger') or {})
    checks = {'a_baseline_ok': baseline['ok'], 'a_drain_ok': drain['ok'],
              'a_released': release['observed'], 'b_start_failed': b_fail['failed'],
              'a2_recovered': a2['ok'], 'ledger_ok': ledger['ok']}
    reasons = ([f'a_baseline_ok:{r}' for r in baseline['reasons']]
               + [f'a_drain_ok:{r}' for r in drain['reasons']]
               + [f'a_released:{r}' for r in release['reasons']]
               + [f'b_start_failed:{r}' for r in b_fail['reasons']]
               + [f'a2_recovered:{r}' for r in a2['reasons']]
               + [f'ledger_ok:{r}' for r in ledger['reasons']])
    return {'pass': all(checks.values()), 'checks': checks, 'reasons': reasons}


def recovery_steps(facts):
    """命令级恢复步骤：不写"人工恢复"四个字了事。"""
    repo, root, db = facts['repo'], facts['root'], facts['db']
    data, provider, keys = facts['data_dir'], facts['provider'], facts['keys_file']
    port, spec = facts['a2_port'], facts['threeway_spec']
    launcher = str(Path(root) / 'launch_manager.py')
    return [
        f'1) 保留现场：cp -a {facts["out"]} /home/gateway-lab/lab/evidence/ ; tail -n 120 {root}/manager-B.log',
        f'2) 确认 B 未占用写者：sqlite3 {db} "SELECT instance_id,owner,role,fence,lease_until FROM instance_leases ORDER BY rowid;"（不应出现 B 的新行）',
        f'3) 启动恢复实例 A2（新进程新身份，同一 repo/env）：cd {repo} && env WB_DB={db} WB_DATA_DIR={data} '
        f'WB2API_BASE={provider} WB2API_KEY=synthetic-upstream WB2API_MODE=native '
        f'R3L2_MANAGER_ROOT={repo}/manager R3L2_THREEWAY_SPEC=\'{spec}\' '
        f'R3L2_KEYS_FILE={keys} R3L2_INSTANCE_FILE={root}/instance-A2-recover.json '
        f'R3L2_PORT={port} python3 -u {launcher}',
        f'4) 验证就绪：curl -fsS http://127.0.0.1:{port}/api/readyz（期望 200）',
        f'5) 验证推理与账本：POST /v1/chat/completions（Bearer 见 {keys} 的指纹记录），'
        f'然后 sqlite3 {db} "SELECT COUNT(*),COALESCE(SUM(prompt_tokens+completion_tokens),0) '
        f'FROM request_logs WHERE status=200;" 与 usage_daily 对比',
        '6) 若 A2 仍失败：不要启动 B、不要手工 UPDATE instance_leases 冒充 release；保留日志并上报。',
    ]


# ─────────── IO 辅助（仅 VM） ───────────
def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_instance_rows(experiment):
    conn = sqlite3.connect(f'file:{experiment.db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute('SELECT * FROM instance_leases ORDER BY rowid')]
    finally:
        conn.close()


def request_log_row(experiment, request_id):
    if not request_id:
        return None
    conn = sqlite3.connect(f'file:{experiment.db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute('SELECT request_id,status,prompt_tokens,'
                           'completion_tokens,endpoint,terminal_state '
                           'FROM request_logs WHERE request_id=?',
                           (request_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def probe_readyz(url):
    import httpx
    try:
        with httpx.Client(trust_env=False, timeout=READY_TIMEOUT) as client:
            return {'status': client.get(url + '/api/readyz').status_code, 'error': None}
    except Exception as exc:  # noqa: BLE001
        return {'status': None, 'error': type(exc).__name__ + ': ' + str(exc)[:200]}


def wait_frames(holder, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        progress = holder.get('progress') or {}
        if int(progress.get('frames') or 0) >= 1:
            return True
        time.sleep(0.02)
    return False


def settle_ledger(experiment, wait_s, label):
    started = time.monotonic()
    ledger = experiment.reconcile_ledger()
    while time.monotonic() - started < wait_s and not ledger_verdict(ledger)['ok']:
        time.sleep(0.25)
        ledger = experiment.reconcile_ledger()
    ledger = dict(ledger)
    ledger['settle_ms'] = round((time.monotonic() - started) * 1000, 3)
    ledger['settle_label'] = label
    return ledger


def snapshot_ledger(experiment, label):
    ledger = dict(experiment.reconcile_ledger())
    ledger['settle_label'] = label
    return ledger


# ─────────── 场景本体（仅 VM） ───────────
def run_branch(experiment, args):
    branch = {'scenario': 'b_start_failure', 'mode': 'serial-protocol'}
    a = b = a2 = None
    try:
        # t0：A serving + 一条真实推理落账
        a = Instance(experiment, 'A')
        if not a.start():
            raise RuntimeError('A 未 ready：见 manager-A.log')
        if not a.info:
            raise RuntimeError('A 没有写出实例身份文件：见 manager-A.log')
        keys, fence_a = a.info['keys'], int(a.info['fence'])
        experiment.timeline.emit('A_serving', info=public_instance_info(a.info))
        baseline = experiment.stream(a, 'chat', 'workbuddy', label='A:baseline', keys=keys)
        baseline_log = request_log_row(experiment, baseline.get('request_id'))
        branch['a_baseline'] = {
            'readyz_status': probe_readyz(a.url)['status'],
            'inference': {'status': baseline.get('status'), 'terminal': baseline.get('terminal'),
                          'request_id': baseline.get('request_id'),
                          'frames': baseline.get('frames')},
            'log_landed': baseline_log is not None,
            'log_row': baseline_log,
            'ledger_snapshot': snapshot_ledger(experiment, 'after_baseline')}

        # t1：在途长流被准入 → begin drain → readyz 503 → drain 后新请求被拒
        holder = experiment.spawn_stream(a, 'chat', 'workbuddy', label='A:in-flight', keys=keys)
        accepted = wait_frames(holder, timeout=60)
        begin = experiment.drain_action(a, 'begin')
        readyz_503 = experiment.wait_readyz(a, 503, timeout=args.drain_timeout)
        rejected = experiment.drain_rejected_probe(a, keys, label='A:drain-rejected')
        holder['thread'].join(timeout=args.inflight_timeout)
        in_flight = holder.get('stream') or {}
        in_flight_terminal = in_flight.get('terminal')
        branch['a_drain'] = {
            'begin_status': begin.get('status'), 'readyz_503': readyz_503,
            'in_flight_accepted': accepted,
            'in_flight_terminal': in_flight_terminal,
            'in_flight_drained': in_flight_terminal == 'completed',
            'in_flight': {k: in_flight.get(k) for k in
                          ('status', 'terminal', 'frames', 'request_id', 't_start_ms', 't_end_ms')},
            'rejected_after_drain': {'status': rejected.get('status'),
                                     'code': rejected.get('code'),
                                     'rejected': rejected.get('rejected')}}

        # t2：A 正常退出，观察应用自身 release
        release_error = None
        try:
            row = experiment.finalize_and_release(a, fence_a)
        except AssertionError as exc:
            release_error = str(exc)
            row = experiment.instance_row(a.info['instance_id'])
        exit_rc = a.proc.returncode if a.proc is not None else None
        release = release_observed(row, fence_a)
        experiment.timeline.emit('A_release_observed', observed=release['observed'],
                                 reasons=release['reasons'], exit_returncode=exit_rc)
        branch['a_release'] = {'observed': release['observed'], 'reasons': release['reasons'],
                               'row': row_summary(row), 'fence_before': fence_a,
                               'exit_returncode': exit_rc, 'release_error': release_error}

        # t3：B 真实启动失败（同 launcher/env，仅 manager root 指向不存在路径）
        rows_before = read_instance_rows(experiment)
        identity_file = experiment.root / 'instance-B.json'
        bad_root = experiment.root / 'missing-manager-root'
        real_source, experiment.source = experiment.source, bad_root
        # 真失败注入：同一个 launcher/env，manager root 指向不存在路径；同时移除
        # PYTHONPATH，避免宿主环境的搜索路径把真实 server 包又找回来。
        saved_pythonpath = experiment.env.pop('PYTHONPATH', None)
        b = Instance(experiment, 'B')
        timed_out = False
        try:
            b.start(wait_ready=False)
        finally:
            experiment.source = real_source
            if saved_pythonpath is not None:
                experiment.env['PYTHONPATH'] = saved_pythonpath
        try:
            returncode = b.proc.wait(timeout=args.b_start_timeout) if b.proc is not None else None
        except Exception:  # subprocess.TimeoutExpired
            timed_out = True
            returncode = None
            b.stop(timeout=30)
        readyz = probe_readyz(b.url)
        rows_after = read_instance_rows(experiment)
        new_rows = new_instance_rows(rows_before, rows_after)
        log_path = experiment.root / 'manager-B.log'
        log_tail = log_path.read_text(errors='replace')[-1200:] if log_path.is_file() else ''
        b_record = {
            'returncode': returncode, 'timed_out': timed_out,
            'manager_root': str(bad_root), 'port': b.port,
            'pythonpath_removed': saved_pythonpath is not None,
            'injected_error': 'R3L2_MANAGER_ROOT pointed at a non-existent path',
            'identity_file': str(identity_file),
            'identity_file_present': identity_file.is_file(),
            'info': public_instance_info(b.info),
            'readyz_status': readyz['status'], 'readyz_error': readyz['error'],
            'instance_rows_before': len(rows_before), 'instance_rows_after': len(rows_after),
            'new_instance_rows': [row_summary(row) for row in new_rows],
            'log_tail': log_tail}
        branch['b_start_failure'] = b_record
        experiment.timeline.emit('B_start_failed', returncode=returncode,
                                 identity_file_present=b_record['identity_file_present'],
                                 new_instance_rows=len(new_rows),
                                 readyz_status=readyz['status'])
        b.stop(timeout=30)

        # t4：恢复 A（A2 新进程新身份）→ readyz 200 → 推理 200 落账 → 对账
        a2 = Instance(experiment, 'A2')
        args.recovery_facts['a2_port'] = a2.port  # 报告里的恢复命令必须可执行，用真实端口
        ready = a2.start()
        a2_readyz = probe_readyz(a2.url)['status']
        recovery = experiment.stream(a2, 'chat', 'workbuddy', label='A2:recovery',
                                     keys=(a2.info or {}).get('keys') or keys)
        recovery_log = request_log_row(experiment, recovery.get('request_id'))
        a2_record = {
            'started': ready, 'readyz_status': a2_readyz, 'port': a2.port,
            'inference_status': recovery.get('status'),
            'inference_terminal': recovery.get('terminal'),
            'inference_request_id': recovery.get('request_id'),
            'log_landed': recovery_log is not None, 'log_row': recovery_log,
            'new_instance_id': bool(a2.info) and a2.info.get('instance_id')
                               != a.info['instance_id'],
            'info': public_instance_info(a2.info)}
        branch['a2_recovery'] = a2_record
        experiment.timeline.emit('A2_recovered', readyz=a2_readyz,
                                 inference_status=recovery.get('status'))

        ledger = settle_ledger(experiment, args.ledger_wait, 'after_a2_recovery')
        branch['ledger'] = ledger
        return branch
    finally:
        for instance in (a2, b, a):
            if instance is not None and instance.proc is not None and instance.proc.poll() is None:
                try:
                    instance.stop(timeout=30)
                except Exception:  # noqa: BLE001
                    pass


def build_report(args, branch, ledger, verdict_result, started_at):
    repo = Path(args.repo).resolve()
    return {
        'schema': 'zhihui-writer-handoff-failure/v1',
        'scenario': 'b_start_failure',
        'source_sha256': args.source_sha256,
        'production_connected': False,
        'started_at': started_at, 'finished_at': now_iso(),
        'repo': str(repo),
        'branches': {'b_start_failure': branch},
        'ledger': ledger,
        'verdict': verdict_result,
        'recovery_steps': recovery_steps(args.recovery_facts),
        'status': 'passed' if verdict_result['pass'] else 'failed',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--qa-file', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--repo', default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument('--chunk-delay', type=float, default=0.15)
    parser.add_argument('--chunk-count', type=int, default=20)
    parser.add_argument('--instance-ttl', type=int, default=30)
    parser.add_argument('--drain-timeout', type=float, default=45.0)
    parser.add_argument('--inflight-timeout', type=float, default=120.0)
    parser.add_argument('--b-start-timeout', type=float, default=60.0)
    parser.add_argument('--ledger-wait', type=float, default=10.0)
    args = parser.parse_args(argv)
    repo, out = Path(args.repo).resolve(), Path(args.out).resolve()
    qa_file = Path(args.qa_file).resolve()
    qa, qa_error = {}, None
    if qa_file.is_file():
        try:
            qa = json.loads(qa_file.read_text(encoding='utf-8'))
        except ValueError as exc:
            qa_error = str(exc)
    facts = {'hostname': socket.gethostname(), 'home': str(Path.home()), 'lab': str(LAB),
             'repo': str(repo), 'out': str(out), 'qa_file': str(qa_file),
             'qa_exists': qa_file.is_file(), 'out_exists': out.exists(),
             'production_connected': qa.get('production_connected'),
             'qa_workspace': str(Path(qa['workspace']).resolve()) if qa.get('workspace') else None}
    bad = guard_violations(facts)
    if qa_error:
        bad.append('qa_file_invalid')
    if bad:
        print(json.dumps({'error': 'boundary_or_precondition_violation',
                          'violations': bad, 'injected': False}, ensure_ascii=False), flush=True)
        return EXIT_BOUNDARY
    started_at = now_iso()
    options = SimpleNamespace(repo=str(repo), out=str(out), chunk_delay=args.chunk_delay,
                              chunk_count=args.chunk_count, instance_ttl=args.instance_ttl)
    experiment = HandoffExperiment(options)
    args.source_sha256 = qa.get('source_sha256')
    args.recovery_facts = {'repo': str(repo), 'root': str(experiment.root),
                           'db': str(experiment.db_path or ''),
                           'out': str(out), 'a2_port': '<free-port>'}
    report = {'schema': 'zhihui-writer-handoff-failure/v1', 'status': 'running',
              'production_connected': False, 'started_at': started_at}
    exit_code = EXIT_FAIL
    try:
        experiment.prepare()
        args.recovery_facts.update({
            'db': experiment.db_path, 'data_dir': experiment.env.get('WB_DATA_DIR', ''),
            'provider': experiment.provider.base_url if experiment.provider else '',
            'keys_file': str(experiment.root / 'keys.json'),
            'threeway_spec': json.dumps(experiment.threeway_spec, ensure_ascii=False)})
        experiment.timeline.emit('writer_handoff_failure_begin')
        branch = run_branch(experiment, args)
        ledger = branch.get('ledger') or {}
        result = verdict({'branches': {'b_start_failure': branch}, 'ledger': ledger})
        report = build_report(args, branch, ledger, result, started_at)
        exit_code = EXIT_PASS if report['status'] == 'passed' else EXIT_FAIL
        experiment.timeline.emit('writer_handoff_failure_done',
                                 status=report['status'], checks=result['checks'])
    except Exception as exc:  # noqa: BLE001
        report = {'schema': 'zhihui-writer-handoff-failure/v1', 'status': 'failed',
                  'production_connected': False, 'started_at': started_at,
                  'finished_at': now_iso(), 'error_type': type(exc).__name__,
                  'error': str(exc)[:800]}
        exit_code = EXIT_FAIL
    finally:
        if experiment.provider is not None:
            try:
                experiment.provider.stop()
            except Exception:  # noqa: BLE001
                pass
        experiment.timeline.close()
        report['finished_at'] = now_iso()
        report['timeline'] = str(out / 'handoff-timeline.ndjson')
        out.mkdir(parents=True, exist_ok=True)
        (out / 'writer-handoff-report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': report.get('status'),
                          'exit_code': exit_code,
                          'checks': (report.get('verdict') or {}).get('checks'),
                          'reasons': (report.get('verdict') or {}).get('reasons'),
                          'report': str(out / 'writer-handoff-report.json')},
                         ensure_ascii=False, indent=2), flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
