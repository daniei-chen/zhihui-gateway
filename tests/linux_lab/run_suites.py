"""Run the frozen source snapshot in the disposable, local Linux VM only."""
from __future__ import annotations

import datetime as dt
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


def main() -> int:
    root = Path('/home/gateway-lab/lab')
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != root.parent:
        raise RuntimeError('Only the disposable local VM may run these suites')
    parser = argparse.ArgumentParser()
    parser.add_argument('--qtg-first', action='store_true',
                        help='Validate QTG authorization changes before the longer Manager suite')
    parser.add_argument('--wait-for-lock-seconds',type=int,default=0,
                        help='Queue behind the current local suite without interrupting it')
    parser.add_argument('--expected-source-sha256',default=None,
                        help='Fence a queued run to the requested frozen snapshot')
    parser.add_argument('--qa-file', default=None)
    args = parser.parse_args()
    if not 0 <= args.wait_for_lock_seconds <= 3600:
        parser.error('Lock wait must be between 0 and 3600 seconds')
    targets = ('qtg', 'manager-assets') if args.qtg_first else ('manager-assets', 'qtg')
    # Capture before waiting: a later install must not silently retarget the run.
    qa_file = Path(args.qa_file).resolve() if args.qa_file else root / 'current-qa.json'
    if not qa_file.is_relative_to(root):
        raise RuntimeError('QA profile escapes the local lab')
    config = json.loads(qa_file.read_text())
    workspace = Path(config['workspace']).resolve()
    if not workspace.is_relative_to(root / 'snapshots') or config['production_connected']:
        raise RuntimeError('Unexpected QA snapshot or connection boundary')
    if args.expected_source_sha256 and config['source_sha256'] != args.expected_source_sha256:
        raise RuntimeError('Queued verification source does not match the requested snapshot')
    lock = (root / 'suites.lock').open('a+')
    deadline=time.monotonic()+args.wait_for_lock_seconds
    waiting=False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic()>=deadline:
                print('An existing Linux verification run is still active', flush=True)
                return 75
            if not waiting:
                print('WAITING_FOR_LOCAL_SUITE_LOCK '+config['source_sha256'],flush=True)
                waiting=True
            time.sleep(min(5,max(0,deadline-time.monotonic())))
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run_dir = root / 'suite-runs' / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    state = {
        'pid': os.getpid(),
        'start_ticks': Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19],
        'started_at': stamp,
        'source_sha256': config['source_sha256'],
        'qa_file': str(qa_file),
        'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'workspace': str(workspace),
        'run_dir': str(run_dir),
        'status': 'running',
        'targets': list(targets),
        'results': [],
    }
    state_path = root / 'suites-state.json'

    def save() -> None:
        data = json.dumps(state, indent=2)
        (run_dir / 'state.json').write_text(data)
        state_path.write_text(data)

    save()
    env = {**os.environ, 'TZ': 'Asia/Shanghai', 'PYTHONUTF8': '1',
           'PYTHONDONTWRITEBYTECODE': '1',
           'PATH': config['node_bin'] + os.pathsep + os.environ.get('PATH', '')}
    for target in targets:
        command = [config['python'], '-X', 'utf8', 'tests/zhihui_harness/run_tests.py',
                   '--target', target, '--keep', '--verbose']
        if target == 'manager-assets':
            command.append('--require-built-web')
        log_path = run_dir / (target + '.log')
        print('START', target, str(log_path), flush=True)
        started = time.monotonic()
        with log_path.open('w') as output:
            process = subprocess.Popen(command, cwd=workspace, env=env,
                                       stdout=output, stderr=subprocess.STDOUT)
            state['current_target'] = target
            state['child_pid'] = process.pid
            save()
            result = process.wait()
        pointer = workspace / 'evidence' / ('latest-' + target + '.json')
        record = {'target': target, 'exit_code': result,
                  'elapsed_s': round(time.monotonic() - started, 2), 'log': str(log_path)}
        if pointer.exists():
            record['evidence'] = json.loads(pointer.read_text())
        state['results'].append(record)
        save()
        print('FINISH', json.dumps(record), flush=True)
    state['status'] = 'completed'
    state['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    state.pop('current_target', None)
    state.pop('child_pid', None)
    save()
    return 1 if any(item['exit_code'] for item in state['results']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
