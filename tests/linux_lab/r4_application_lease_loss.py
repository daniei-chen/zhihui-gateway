"""Real application lease-loss drill (disposable local VM only).

场景（都是真实 Manager 进程，不是进程内单测）：

* ``takeover``：交接控制器把 owner/fence 改成新持有者（等价于 B 接管）；
  旧进程必须停止新准入、不得把 owner 改回去；旧 fence 退出也不能覆盖新持有者。
* ``expiry``：对进程 SIGSTOP 超过实例租约 TTL，恢复后迟到的第一次心跳必须被
  ``lease_until > now`` 拒绝 → 停止准入；随后正常退出仍要释放自己的租约行。

判定口径：只认行为断言（readyz 状态、真实推理请求被 503 gateway_draining 拒绝、
DB 行的 owner/fence/role/lease_until），不看"日志里写了 lost"。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import signal
import socket
import time
from types import SimpleNamespace

from r3l2_handoff import HandoffExperiment, Instance, public_instance_info

LAB = Path('/home/gateway-lab/lab')


def guard_paths(repo: Path, out: Path, qa_file: Path) -> dict:
    if (socket.gethostname() != 'zhihui-local-lab' or Path.home() != LAB.parent
            or not repo.is_relative_to(LAB / 'snapshots')
            or not out.is_relative_to(LAB) or not qa_file.is_relative_to(LAB)):
        raise RuntimeError('Restricted to disposable local VM snapshots and lab evidence')
    if out.exists():
        raise RuntimeError('Refusing to overwrite existing lease-loss evidence')
    qa = json.loads(qa_file.read_text())
    if qa['production_connected'] or Path(qa['workspace']).resolve() != repo:
        raise RuntimeError('QA source/profile mismatch')
    return qa


def wait_readyz(instance: Instance, code: int, timeout: float) -> bool:
    import httpx
    deadline = time.monotonic() + timeout
    with httpx.Client(base_url=instance.url, timeout=3.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                if client.get('/api/readyz').status_code == code:
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(.1)
    return False


def row_summary(row: dict | None) -> dict:
    row = row or {}
    return {'owner': row.get('owner'), 'fence': row.get('fence'),
            'role': row.get('role'), 'lease_until': row.get('lease_until')}


def run_takeover(experiment: HandoffExperiment, report: dict) -> bool:
    instance = Instance(experiment, 'takeover')
    try:
        if not instance.start():
            raise AssertionError('Real application did not become ready (takeover)')
        iid, fence = instance.info['instance_id'], int(instance.info['fence'])
        before = experiment.instance_row(iid)
        takeover_owner = 'synthetic-handoff-controller'
        experiment.sqlite_write(
            'UPDATE instance_leases SET owner=?, fence=fence+1 '
            'WHERE instance_id=? AND fence=?', (takeover_owner, iid, fence))
        # 心跳 1s，最多等 10s 让旧进程发现失权并进入 draining。
        draining = wait_readyz(instance, 503, timeout=10)
        probe = experiment.drain_rejected_probe(
            instance, instance.info['keys'], label='takeover:admission')
        row = experiment.instance_row(iid)
        after = row_summary(row)
        instance.stop()
        final = row_summary(experiment.instance_row(iid))
        record = {
            'before': row_summary(before), 'after': after, 'after_exit': final,
            'readyz_became_503': draining,
            'new_admission_rejected': probe.get('rejected'),
            'probe_status': probe.get('status'), 'probe_code': probe.get('code'),
            'takeover_owner_preserved': after.get('owner') == takeover_owner,
            'takeover_fence_preserved': int(after.get('fence') or 0) == fence + 1,
            'old_fence_did_not_overwrite_after_exit': (
                final.get('owner') == takeover_owner
                and int(final.get('fence') or 0) == fence + 1),
        }
        record['pass'] = bool(
            draining and probe.get('rejected') and record['takeover_owner_preserved']
            and record['takeover_fence_preserved']
            and record['old_fence_did_not_overwrite_after_exit'])
        report['branches']['takeover'] = record
        return record['pass']
    finally:
        if instance.proc is not None and instance.proc.poll() is None:
            instance.stop()


def run_expiry(experiment: HandoffExperiment, report: dict, freeze_sec: float) -> bool:
    instance = Instance(experiment, 'expiry')
    try:
        if not instance.start():
            raise AssertionError('Real application did not become ready (expiry)')
        iid, fence = instance.info['instance_id'], int(instance.info['fence'])
        if not wait_readyz(instance, 200, timeout=10):
            raise AssertionError('readyz was not 200 before the freeze')
        pid = instance.proc.pid
        os.kill(pid, signal.SIGSTOP)
        frozen_at = time.monotonic()
        time.sleep(freeze_sec)
        os.kill(pid, signal.SIGCONT)
        resumed_at = time.monotonic()
        # 恢复后的第一次心跳（1s 间隔）应因 lease_until 过期被拒绝 → 停止准入。
        draining = wait_readyz(instance, 503, timeout=25)
        probe = experiment.drain_rejected_probe(
            instance, instance.info['keys'], label='expiry:admission')
        row = experiment.instance_row(iid)
        after = row_summary(row)
        instance.stop()
        final_row = experiment.instance_row(iid) or {}
        final = row_summary(final_row)
        record = {
            'freeze_sec': freeze_sec,
            'frozen_at_ms': round((frozen_at - experiment.origin) * 1000, 3),
            'resumed_at_ms': round((resumed_at - experiment.origin) * 1000, 3),
            'readyz_became_503': draining,
            'new_admission_rejected': probe.get('rejected'),
            'probe_status': probe.get('status'), 'probe_code': probe.get('code'),
            'row_after_resume': after,
            'owner_unchanged_before_exit': after.get('owner') == instance.info['owner'],
            'fence_unchanged_before_exit': int(after.get('fence') or 0) == fence,
            'released_on_exit': (final.get('role') == 'stopped'
                                 and not final.get('owner')
                                 and int(final.get('lease_until') or 0) == 0
                                 and int(final.get('fence') or 0) == fence),
            'after_exit': final,
        }
        # 过期行未被迟到心跳复活：恢复探测时 lease_until 不应被续到未来。
        record['lease_not_revived_during_probe'] = (
            int(after.get('lease_until') or 0) <= int(time.time()))
        record['pass'] = bool(
            draining and probe.get('rejected') and record['owner_unchanged_before_exit']
            and record['fence_unchanged_before_exit']
            and record['lease_not_revived_during_probe']
            and record['released_on_exit'])
        report['branches']['expiry'] = record
        return record['pass']
    finally:
        if instance.proc is not None and instance.proc.poll() is None:
            instance.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qa-file', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--freeze-sec', type=float, default=35.0,
                        help='SIGSTOP window; must exceed the lease TTL')
    parser.add_argument('--branches', nargs='*', default=['takeover', 'expiry'])
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[2]
    out = Path(args.out).resolve()
    qa_file = Path(args.qa_file).resolve()
    qa = guard_paths(repo, out, qa_file)
    if args.freeze_sec <= 30:
        parser.error('freeze window must exceed the 30s instance TTL')
    out.mkdir(parents=True, exist_ok=False)
    options = SimpleNamespace(repo=str(repo), out=str(out), chunk_delay=.15,
                              chunk_count=400, instance_ttl=30)
    experiment = HandoffExperiment(options)
    report = {'schema': 'zhihui-application-lease-loss/1',
              'source_sha256': qa['source_sha256'], 'production_connected': False,
              'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'status': 'running', 'branches': {}}
    try:
        experiment.prepare()
        experiment.timeline.emit('lease_loss_drill_begin',
                                 branches=list(args.branches))
        results = {}
        if 'takeover' in args.branches:
            results['takeover'] = run_takeover(experiment, report)
        if 'expiry' in args.branches:
            results['expiry'] = run_expiry(experiment, report, args.freeze_sec)
        report['results'] = results
        report['status'] = 'passed' if results and all(results.values()) else 'failed'
        if report['status'] != 'passed':
            raise AssertionError('At least one lease-loss branch failed')
    except Exception as exc:  # noqa: BLE001
        report.setdefault('status', 'failed')
        report['status'] = 'failed'
        report['error_type'] = type(exc).__name__
        report['error'] = str(exc)[:500]
    finally:
        if experiment.provider is not None:
            experiment.provider.stop()
        experiment.timeline.close()
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (out / 'lease-loss-report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)
    return 0 if report.get('status') == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
