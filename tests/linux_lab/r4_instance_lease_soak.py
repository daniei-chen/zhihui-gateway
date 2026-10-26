"""Real 300-second explicit lease keeper + drain exercise, disposable VM only.

This does not prove that the production app lifespan owns an instance keeper.
No patched clocks, shortened TTLs, production paths, or production credentials.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration-sec', type=int, default=300)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    if args.duration_sec < 300:
        parser.error('A passing real soak requires at least 300 wall-clock seconds')
    lab = Path('/home/gateway-lab/lab').resolve()
    repo = Path(__file__).resolve().parents[2]
    out = Path(args.out).resolve()
    if (socket.gethostname() != 'zhihui-local-lab' or Path.home() != lab.parent
            or not repo.is_relative_to(lab) or not out.is_relative_to(lab)):
        raise RuntimeError('Restricted to the disposable local VM and its lab directory')
    if out.exists():
        raise RuntimeError('Refusing to overwrite an existing soak evidence directory')
    out.mkdir(parents=True)
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(repo / 'tests'), str(repo / 'manager')]
    from fake_provider.server import FakeProvider
    from zhihui_harness.isolation import IsolatedEnv

    provider = FakeProvider().start()
    saved_env = dict(os.environ)
    report = {
        'schema': 'zhihui-r4-instance-soak/1', 'production_connected': False,
        'scope': 'explicit-experimental-keeper-and-drain-functions',
        'production_lifespan_heartbeat_verified': False,
        'http_entry_or_live_streams_verified': False,
        'requested_duration_sec': args.duration_sec,
        'heartbeat_sec': 10, 'lease_ttl_sec': 30,
        'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'status': 'running', 'heartbeats': 0, 'samples': 0,
        'limitations': ['Explicit experimental keeper; not production lifecycle integration',
                        'No HTTP entry, in-flight SSE, or dual-instance writer protocol exercised'],
    }
    db = None
    iso = None
    trace = None
    started = None
    try:
        iso = IsolatedEnv.create(repo, keep=True, base_dir=out)
        env = iso.build_env(provider, extra={'ZH_INSTANCE_HEARTBEAT_SEC': '10',
                                           'ZH_INSTANCE_LEASE_TTL_SEC': '30'})
        problems = iso.validate(env)
        if problems:
            raise RuntimeError('Isolation environment validation failed')
        os.environ.update(env)
        from server import config, db, drain, instancelease
        config.ensure_dirs()
        instancelease.reset_identity()
        drain.reset_for_tests()
        db.connect()
        if instancelease.HEARTBEAT_SEC != 10 or instancelease.LEASE_TTL_SEC != 30:
            raise AssertionError('Real soak must use default 10s heartbeat / 30s TTL')
        claimed, fence = instancelease.claim(ttl_seconds=30)
        if not claimed:
            raise AssertionError('Initial isolated instance claim failed')
        me = instancelease.identity()
        report['identity_sha256_16'] = hashlib.sha256(me['instance_id'].encode()).hexdigest()[:16]
        report['initial_fence'] = fence
        trace = (out / 'samples.ndjson').open('x', encoding='utf-8')
        started = time.monotonic()
        next_heartbeat = started + 10
        next_sample = started
        max_gap = 0.0
        last_heartbeat = started
        draining = False
        while True:
            mono = time.monotonic()
            elapsed = mono - started
            wall = int(time.time())
            row = instancelease.describe()
            if not row or row['owner'] != me['owner'] or int(row['fence']) != fence:
                raise AssertionError('Ownership or fence changed during the real soak')
            if int(row['lease_until']) <= wall:
                raise AssertionError('Lease expired before the next real heartbeat')
            if mono >= next_heartbeat:
                if not instancelease.heartbeat(fence=fence, ttl_seconds=30):
                    raise AssertionError('Real heartbeat rejected')
                max_gap = max(max_gap, mono - last_heartbeat)
                last_heartbeat = mono
                report['heartbeats'] += 1
                next_heartbeat += 10
                row = instancelease.describe()
            if elapsed >= 120 and not draining:
                drain.begin()
                draining = True
                row = instancelease.describe()
            expected_role = 'draining' if draining else 'serving'
            if row['role'] != expected_role or drain.is_draining() != draining:
                raise AssertionError('Lease role and local drain state disagree')
            if mono >= next_sample:
                trace.write(json.dumps({'elapsed_sec': round(elapsed, 6),
                    'wall_epoch': wall, 'role': row['role'], 'fence': row['fence'],
                    'lease_until': row['lease_until'],
                    'lease_remaining_sec': int(row['lease_until']) - wall,
                    'draining': drain.is_draining()}) + '\n')
                trace.flush()
                report['samples'] += 1
                next_sample += 1
            if elapsed >= args.duration_sec:
                break
            time.sleep(min(.2, max(.001, args.duration_sec - elapsed)))
        if report['heartbeats'] < args.duration_sec // 10:
            raise AssertionError('Insufficient real heartbeat observations')
        if not drain.resume():
            raise AssertionError('Local drain resume rejected')
        row = instancelease.describe()
        if (drain.is_draining() or row['role'] != 'serving'
                or int(row['fence']) != fence or row['owner'] != me['owner']
                or int(row['lease_until']) <= int(time.time())):
            raise AssertionError('Resume did not retain both local and lease state')
        report.update(status='passed', elapsed_sec=round(time.monotonic() - started, 6),
                      max_observed_heartbeat_gap_sec=round(max_gap, 6),
                      final_role=row['role'], final_fence=row['fence'],
                      resume_verified=True)
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        if started is not None:
            report['elapsed_sec'] = round(time.monotonic() - started, 6)
    finally:
        if trace is not None:
            trace.close()
        if db is not None:
            with db._lock:
                db._close_connection_locked()
        provider.stop()
        os.environ.clear()
        os.environ.update(saved_env)
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
