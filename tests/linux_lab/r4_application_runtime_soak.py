"""Real application-owned lease/HTTP/SSE soak, disposable VM only."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import socket
import time

from r3l2_handoff import HandoffExperiment, Instance, public_instance_info


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qa-file', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--duration-sec', type=int, default=300)
    args = parser.parse_args(argv)
    lab = Path('/home/gateway-lab/lab')
    repo = Path(__file__).resolve().parents[2]
    out = Path(args.out).resolve()
    qa_file = Path(args.qa_file).resolve()
    if (socket.gethostname() != 'zhihui-local-lab' or Path.home() != lab.parent
            or not repo.is_relative_to(lab / 'snapshots')
            or not out.is_relative_to(lab) or not qa_file.is_relative_to(lab)):
        raise RuntimeError('Restricted to disposable local VM snapshots and lab evidence')
    if args.duration_sec < 300:
        parser.error('Real application soak requires at least 300 seconds')
    if out.exists():
        raise RuntimeError('Refusing to overwrite existing runtime soak evidence')
    qa = json.loads(qa_file.read_text())
    if qa['production_connected'] or Path(qa['workspace']).resolve() != repo:
        raise RuntimeError('QA source/profile mismatch')
    options = argparse.Namespace(repo=str(repo), out=str(out), chunk_delay=.15,
                                 chunk_count=400, instance_ttl=30)
    experiment = HandoffExperiment(options)
    report = {'schema': 'zhihui-application-runtime-soak/1',
              'source_sha256': qa['source_sha256'], 'production_connected': False,
              'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'status': 'running', 'automatic_application_keeper': True,
              'manual_heartbeat_calls': 0, 'samples': 0}
    instance = None
    stream = None
    trace = None
    try:
        experiment.prepare()
        experiment.env['ZH_INSTANCE_HEARTBEAT_SEC'] = '10'
        instance = Instance(experiment, 'runtime-soak')
        if not instance.start():
            raise AssertionError('Real application did not become ready')
        report['instance'] = public_instance_info(instance.info)
        iid, fence = instance.info['instance_id'], int(instance.info['fence'])
        initial = experiment.instance_row(iid)
        report['initial_lease_until'] = initial['lease_until']
        trace = (out / 'application-samples.ndjson').open('x', encoding='utf-8')
        origin = time.monotonic()
        next_probe = origin
        drain_began = None
        resumed = False
        expiries = set()
        while True:
            elapsed = time.monotonic() - origin
            if elapsed >= 110 and stream is None:
                stream = experiment.spawn_stream(instance, 'chat', 'workbuddy',
                    label='automatic-runtime:cross-drain', keys=instance.info['keys'])
            if elapsed >= 120 and drain_began is None:
                if not stream or (stream.get('progress') or {}).get('frames', 0) < 1:
                    raise AssertionError('No admitted SSE body before drain')
                record = experiment.drain_action(instance, 'begin')
                if record['status'] != 200:
                    raise AssertionError('Drain control failed')
                drain_began = record['t_end_ms']
                rejected = experiment.drain_rejected_probe(instance, instance.info['keys'])
                if not rejected['rejected']:
                    raise AssertionError('New inference was not rejected during drain')
                report['new_admission_rejected'] = True
            if elapsed >= 180 and not resumed:
                if experiment.drain_action(instance, 'resume')['status'] != 200:
                    raise AssertionError('Resume control failed')
                resumed = True
            row = experiment.instance_row(iid)
            now = int(time.time())
            expected = 'draining' if drain_began is not None and not resumed else 'serving'
            if (not row or row['owner'] != instance.info['owner']
                    or int(row['fence']) != fence or row['role'] != expected
                    or int(row['lease_until']) <= now):
                raise AssertionError('Automatic lease ownership/role/expiry invariant failed')
            expiries.add(int(row['lease_until']))
            if time.monotonic() >= next_probe:
                response = instance.session.get('/api/readyz')
                wanted = 503 if expected == 'draining' else 200
                if response.status_code != wanted:
                    raise AssertionError('HTTP readiness did not match drain phase')
                trace.write(json.dumps({'elapsed_sec': round(elapsed, 6),
                    'role': row['role'], 'fence': fence, 'lease_until': row['lease_until'],
                    'lease_remaining_sec': int(row['lease_until']) - now,
                    'readyz_status': response.status_code}) + '\n')
                trace.flush()
                report['samples'] += 1
                next_probe += 2
            if elapsed >= args.duration_sec:
                report['elapsed_sec'] = round(elapsed, 6)
                break
            time.sleep(.2)
        stream['thread'].join(timeout=15)
        body = stream.get('stream') or {}
        if (stream['thread'].is_alive() or body.get('terminal') != 'completed'
                or body.get('status') != 200 or body.get('t_end_ms', 0) <= drain_began):
            raise AssertionError('SSE did not complete across the real drain window')
        report['sse'] = body
        instance.stop()
        final = experiment.instance_row(iid)
        if (final['role'] != 'stopped' or final['owner'] or final['lease_until'] != 0
                or final['fence'] != fence):
            raise AssertionError('Application did not release its lease on shutdown')
        snapshot = json.loads(final['drain_snapshot'])
        keeper = snapshot.get('instance_runtime', {})
        # 300s/10s=30 次，但首次心跳在 10s、主循环在 elapsed>=300 退出可能与第 30 次
        # 心跳同刻，故下限取 duration/10-1；再用 lease_until 版本数做独立交叉印证，
        # 不以单一计数凑绿。
        minimum = max(2, args.duration_sec // 10 - 1)
        if (keeper.get('heartbeats', 0) < minimum or keeper.get('heartbeat_sec') != 10
                or len(expiries) < minimum):
            raise AssertionError('Insufficient real automatic heartbeat evidence')
        report.update(status='passed', automatic_heartbeats=keeper['heartbeats'],
                      heartbeat_evidence_minimum=minimum,
                      observed_lease_versions=len(expiries), keeper=keeper,
                      shutdown_release_verified=True,
                      ledger=experiment.reconcile_ledger())
        if report['ledger']['verdict'] != 'PASS':
            raise AssertionError('Ledger reconciliation failed')
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__, error=str(exc))
    finally:
        if instance is not None and instance.proc is not None and instance.proc.poll() is None:
            instance.stop()
        if trace is not None:
            trace.close()
        if experiment.provider is not None:
            experiment.provider.stop()
        experiment.timeline.close()
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (out / 'application-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
