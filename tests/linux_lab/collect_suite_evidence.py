"""Collect one Linux QA suite-run from the LOCAL QEMU lab VM into repo evidence.

Host-side companion to ``tests/linux_lab/run_suites.py``.  It never connects to
anything but the disposable local VM (127.0.0.1 / localhost) and copies, then
re-hashes, the artefacts a run produced:

  * ``suite-run`` mode (formal regression): ``state.json`` plus every target log
    from ``/home/gateway-lab/lab/suite-runs/<stamp>/`` and, for every target the
    state file lists, the run's ``stats-<target>.json``, ``junit-<target>.xml``
    and ``latest-<target>.json`` from the frozen workspace evidence tree;
  * ``manual-run`` mode (e.g. the ``--collect-only`` rehearsal): the same stats /
    junit / latest files for one ``run_tests.py`` run id, plus optional extra
    log files.

``summary.json`` is derived from the stats JSON that ``run_tests.py`` writes
(itself parsed from the JUnit XML), never by grepping logs for numbers.

Usage (host, from anywhere)::

    python -X utf8 tests/linux_lab/collect_suite_evidence.py --latest
    python -X utf8 tests/linux_lab/collect_suite_evidence.py --run 20261025T061500Z
    python -X utf8 tests/linux_lab/collect_suite_evidence.py \
        --run-id 20261025-061500-abcd1234 --target qtg \
        --workspace /home/gateway-lab/lab/snapshots/<sha16>/workspace \
        --log-remote /home/gateway-lab/lab/rehearsal/collect-only.log

Defaults (host/port/user/key) come from ``zhihui-linux-lab/lab-config.json``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[2]
LAB = REPO.parent / 'zhihui-linux-lab'
LAB_CONFIG = LAB / 'lab-config.json'
EVIDENCE_ROOT = REPO / 'evidence' / 'linux-r0'
REMOTE_LAB = '/home/gateway-lab/lab'
SCHEMA = 'zhihui-linux-suite-evidence/1'
RUN_NAME_RE = re.compile(r'[0-9]{8}T[0-9]{6}Z')
RUN_ID_RE = re.compile(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{8}')
TARGET_RE = re.compile(r'[a-z0-9-]+')
FORBIDDEN_HOSTS = {'203.0.113.10'}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


class Lab:
    """Minimal ssh/scp wrapper pinned to the disposable local VM."""

    def __init__(self, host: str, port: int, user: str, key: Path):
        if host in FORBIDDEN_HOSTS or host not in {'127.0.0.1', 'localhost'}:
            raise SystemExit(f'Refusing to talk to {host!r}: only the local QEMU lab VM is allowed')
        if not key.is_file():
            raise SystemExit(f'SSH key not found: {key}')
        self.host, self.port, self.user, self.key = host, port, user, key
        self.base = ['ssh', '-i', str(key), '-o', 'StrictHostKeyChecking=no',
                     '-o', 'UserKnownHostsFile=NUL', '-o', 'BatchMode=yes',
                     '-o', 'ConnectTimeout=15', '-p', str(port), f'{user}@{host}']

    def run(self, command: str) -> subprocess.CompletedProcess:
        return subprocess.run(self.base + [command], capture_output=True, text=True,
                              encoding='utf-8', errors='replace')

    def popen_scp(self, remote: str, local: Path) -> subprocess.CompletedProcess:
        return subprocess.run(['scp', '-q', '-i', str(self.key), '-o', 'StrictHostKeyChecking=no',
                               '-o', 'UserKnownHostsFile=NUL', '-o', 'BatchMode=yes',
                               '-P', str(self.port), f'{self.user}@{self.host}:{remote}', str(local)],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')


def probe_remote(lab: Lab, paths: list[str]) -> dict[str, bool]:
    """One round trip: which of the candidate remote paths exist as regular files?"""
    quoted = ' '.join(shlex.quote(path) for path in paths)
    script = (f'for p in {quoted}; do if [ -f "$p" ]; then printf "F %s\\n" "$p"; '
              f'else printf "M %s\\n" "$p"; fi; done')
    result = lab.run(script)
    if result.returncode != 0 and not result.stdout.strip():
        raise SystemExit(f'remote probe failed: {result.stderr.strip()[:2000]}')
    state = {path: False for path in paths}
    for line in result.stdout.splitlines():
        if line.startswith('F '):
            state[line[2:].strip()] = True
    return state


def remote_sha256(lab: Lab, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    result = lab.run('sha256sum -- ' + ' '.join(shlex.quote(path) for path in paths))
    if result.returncode != 0:
        raise SystemExit(f'remote sha256 failed: {result.stderr.strip()[:2000]}')
    mapping = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            mapping[parts[1].strip()] = parts[0].strip()
    return mapping


def collect_files(lab: Lab, remote_paths: list[str], dest: Path, missing: list[dict],
                  transfers: list[dict]) -> dict[str, Path]:
    """Copy remote files into dest (flat), verify sha256 both ends, record provenance."""
    present = probe_remote(lab, remote_paths)
    wanted = [path for path in remote_paths if present.get(path)]
    for path in remote_paths:
        if not present.get(path):
            missing.append({'remote': path, 'reason': 'not present on VM'})
    checksums = remote_sha256(lab, wanted)
    local_map: dict[str, Path] = {}
    for path in wanted:
        target = dest / PurePosixPath(path).name
        copied = lab.popen_scp(path, target)
        if copied.returncode != 0:
            missing.append({'remote': path, 'reason': f'scp failed: {copied.stderr.strip()[:500]}'})
            continue
        local_hash = sha256_file(target)
        remote_hash = checksums.get(path)
        record = {'remote': path, 'local': target.name, 'bytes': target.stat().st_size,
                  'sha256': local_hash, 'remote_sha256': remote_hash,
                  'verified': remote_hash == local_hash}
        transfers.append(record)
        if not record['verified']:
            raise SystemExit(f'transfer verification failed for {path}: {local_hash} != {remote_hash}')
        local_map[path] = target
    return local_map


def probe_vm(lab: Lab) -> dict:
    result = lab.run('hostname; readlink -f /home/gateway-lab; python3 -V 2>&1; date -u +%Y-%m-%dT%H:%M:%SZ')
    if result.returncode != 0:
        raise SystemExit(f'SSH to the lab VM failed: {result.stderr.strip()[:2000]}')
    lines = result.stdout.splitlines()
    hostname = lines[0].strip() if lines else ''
    if hostname != 'zhihui-local-lab':
        raise SystemExit(f'Refusing to collect from unexpected host {hostname!r}')
    return {'hostname': hostname, 'host': lab.host, 'port': lab.port, 'user': lab.user,
            'python': lines[2].strip() if len(lines) > 2 else None,
            'vm_utc_time': lines[3].strip() if len(lines) > 3 else None}


def stats_summary(stats_payload: dict) -> dict:
    stats = stats_payload.get('stats') or {}
    failures = [{'id': item.get('id'), 'type': item.get('type'), 'message': item.get('message')}
                for item in stats.get('failures', [])]
    errors = [{'id': item.get('id'), 'type': item.get('type'), 'message': item.get('message')}
              for item in stats.get('error_items', [])]
    skips = [{'id': item.get('id'), 'reason': item.get('reason')} for item in stats.get('skips', [])]
    counts = {key: stats.get(key, 0) for key in
              ('tests', 'passed', 'failed', 'errors', 'skipped', 'xfailed', 'xpassed')}
    return {
        'scope': stats_payload.get('scope'),
        'target': stats_payload.get('target'),
        'summary': stats_payload.get('summary'),
        'counts': counts,
        'pytest_exit': stats_payload.get('pytest_exit'),
        'verification_exit': stats_payload.get('verification_exit'),
        'elapsed_s': stats_payload.get('elapsed_s'),
        'view_sha256': stats_payload.get('view_sha256'),
        'view_sha256_after': stats_payload.get('view_sha256_after'),
        'view_changed_during_tests': stats_payload.get('view_changed_during_tests'),
        'requested_test_paths': stats_payload.get('requested_test_paths'),
        'pytest_args': stats_payload.get('pytest_args'),
        'failures': failures,
        'error_items': errors,
        'skips': skips,
        'stats_missing': bool(stats.get('missing')),
        'stats_parse_error': stats.get('parse_error'),
    }


def collect_only_hint(log_path: Path | None) -> dict | None:
    """Informational only: pytest's own terminal line for collection runs."""
    if log_path is None or not log_path.is_file():
        return None
    collected = None
    errors = 0
    method = None
    per_file = 0
    text = log_path.read_text(encoding='utf-8', errors='replace')
    for line in text.splitlines():
        for pattern in (r'collected (\d+) items?', r'(\d+) tests? collected', r'(\d+) items? collected'):
            match = re.search(pattern, line)
            if match:
                collected = int(match.group(1))
                method = 'pytest "N tests collected" terminal line'
        match = re.search(r'(\d+) errors? during collection', line)
        if match:
            errors = int(match.group(1))
        match = re.match(r'^\s*(\S+\.py):\s+(\d+)\s*$', line)
        if match:
            per_file += int(match.group(2))
    if collected is None and per_file:
        collected = per_file
        method = 'sum of pytest per-file collection counts (terminal output)'
    return {'collected_items': collected, 'collection_errors': errors, 'method': method,
            'source': 'pytest terminal output (informational only; pass/fail counts come from stats/JUnit)'}


def write_summary(dest: Path, summary: dict) -> Path:
    output = dest / 'summary.json'
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return output


def default_destination(name: str) -> Path:
    return EVIDENCE_ROOT / name


def suite_run(args, lab: Lab) -> int:
    if args.run:
        run_name = args.run
    else:
        listing = lab.run(f'ls -1 {REMOTE_LAB}/suite-runs 2>/dev/null | grep -E "^[0-9]{{8}}T[0-9]{{6}}Z$" | sort | tail -1')
        run_name = listing.stdout.strip()
        if not run_name:
            raise SystemExit('No suite-run found on the VM')
    if not RUN_NAME_RE.fullmatch(run_name):
        raise SystemExit(f'Invalid suite-run stamp: {run_name!r}')
    run_dir = f'{REMOTE_LAB}/suite-runs/{run_name}'
    dest = Path(args.dest) if args.dest else default_destination(run_name)
    if dest.exists() and (dest / 'summary.json').exists() and not args.force:
        raise SystemExit(f'Evidence already collected at {dest} (use --force to re-collect)')
    dest.mkdir(parents=True, exist_ok=True)

    missing: list[dict] = []
    transfers: list[dict] = []
    state_path = collect_files(lab, [f'{run_dir}/state.json'], dest, missing, transfers)
    if f'{run_dir}/state.json' not in state_path:
        raise SystemExit(f'Could not fetch {run_dir}/state.json')
    state = load_json(dest / 'state.json')

    targets = [result.get('target') for result in state.get('results', []) if result.get('target')]
    log_paths = [result['log'] for result in state.get('results', []) if result.get('log')]
    # An interrupted run keeps its in-flight target in state['current_target']; that log
    # exists on disk but never reaches state['results'], so collect it explicitly.
    extra_logs = [f'{run_dir}/{target}.log' for target in state.get('targets', [])
                  if f'{run_dir}/{target}.log' not in log_paths]
    if extra_logs:
        present_logs = probe_remote(lab, extra_logs)
        log_paths.extend(path for path in extra_logs if present_logs.get(path))
    collect_files(lab, log_paths, dest, missing, transfers)

    per_target: dict[str, dict] = {}
    totals = {'tests': 0, 'passed': 0, 'failed': 0, 'errors': 0, 'skipped': 0,
              'xfailed': 0, 'xpassed': 0}
    target_order = []
    for result in state.get('results', []):
        target = result.get('target')
        if not target:
            continue
        target_order.append(target)
        evidence = result.get('evidence') or {}
        workspace = state.get('workspace', '')
        run_id = evidence.get('run_id')
        stats_remote = evidence.get('stats')
        if not stats_remote and run_id:
            stats_remote = f'{workspace}/evidence/runs/{run_id}/stats-{target}.json'
        stats_dir = str(PurePosixPath(stats_remote).parent) if stats_remote else None
        junit_remote = f'{stats_dir}/junit-{target}.xml' if stats_dir else None
        latest_remote = f'{workspace}/evidence/latest-{target}.json'
        candidates = [path for path in (stats_remote, junit_remote, latest_remote) if path]
        collected = collect_files(lab, candidates, dest, missing, transfers)
        entry = {'target': target, 'exit_code': result.get('exit_code'),
                 'elapsed_s': result.get('elapsed_s'), 'log': PurePosixPath(result.get('log', '')).name,
                 'run_id': run_id,
                 'stats': PurePosixPath(stats_remote).name if stats_remote else None,
                 'junit': PurePosixPath(junit_remote).name if junit_remote else None,
                 'latest': PurePosixPath(latest_remote).name if latest_remote else None}
        stats_local = collected.get(stats_remote)
        if stats_local is not None:
            payload = load_json(stats_local)
            entry.update(stats_summary(payload))
            entry['stats_sha256'] = sha256_file(stats_local)
            for key, value in entry['counts'].items():
                totals[key] += value
            if entry.get('scope') == 'collect-only':
                entry['collect_only'] = collect_only_hint(dest / entry['log'] if entry['log'] else None)
        else:
            entry['stats_missing'] = True
        per_target[target] = entry

    source_sha256 = state.get('source_sha256')
    expected = args.expected_source_sha256
    summary = {
        'schema': SCHEMA,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'generated_at_local': datetime.now().astimezone().isoformat(),
        'mode': 'suite-run',
        'collected_by': 'tests/linux_lab/collect_suite_evidence.py',
        'collected_by_sha256': sha256_file(Path(__file__)),
        'vm': probe_vm(lab),
        'run': {
            'stamp': run_name,
            'remote_dir': run_dir,
            'state': state,
            'source_sha256': source_sha256,
            'workspace': state.get('workspace'),
            'driver_sha256': state.get('driver_sha256'),
            'status': state.get('status'),
            'started_at': state.get('started_at'),
            'finished_at': state.get('finished_at'),
            'targets': target_order,
            'timings_s': [{'target': item.get('target'), 'exit_code': item.get('exit_code'),
                           'elapsed_s': item.get('elapsed_s')} for item in state.get('results', [])],
            'wall_clock_s': round(sum(item.get('elapsed_s') or 0 for item in state.get('results', [])), 2),
            'all_exit_zero': all((item.get('exit_code') == 0) for item in state.get('results', [])),
            'interrupted': state.get('status') != 'completed',
            'current_target_at_collection': state.get('current_target'),
        },
        'expected_source_sha256': expected,
        'source_sha256_match': (source_sha256 == expected) if expected else None,
        'per_target': per_target,
        'totals': totals,
        'failures': [{'target': target, **failure}
                     for target, entry in per_target.items()
                     for failure in entry.get('failures', [])],
        'errors': [{'target': target, **item}
                   for target, entry in per_target.items()
                   for item in entry.get('error_items', [])],
        'evidence_files': transfers,
        'missing_evidence': missing,
        'notes': [],
    }
    if missing:
        summary['notes'].append(f'{len(missing)} expected evidence file(s) were absent on the VM')
    if any(entry.get('stats_missing') for entry in per_target.values()):
        summary['notes'].append('At least one target has no stats file; totals only cover collected targets')
    output = write_summary(dest, summary)
    print(json.dumps({'status': 'COLLECTED', 'mode': 'suite-run', 'run': run_name,
                      'dest': str(dest), 'summary': str(output),
                      'source_sha256': source_sha256, 'source_sha256_match': summary['source_sha256_match'],
                      'totals': totals, 'failures': len(summary['failures']),
                      'missing': len(missing)}, ensure_ascii=False), flush=True)
    return 0


def manual_run(args, lab: Lab) -> int:
    if not RUN_ID_RE.fullmatch(args.run_id):
        raise SystemExit(f'Invalid run id: {args.run_id!r}')
    if not args.target:
        raise SystemExit('--target is required with --run-id')
    for target in args.target:
        if not TARGET_RE.fullmatch(target):
            raise SystemExit(f'Invalid target: {target!r}')
    workspace = args.workspace
    if not workspace:
        config = lab.run(f'cat {REMOTE_LAB}/current-qa.json')
        if config.returncode != 0:
            raise SystemExit('Could not read current-qa.json; pass --workspace explicitly')
        workspace = json.loads(config.stdout)['workspace']
    if not workspace.startswith(REMOTE_LAB + '/snapshots/'):
        raise SystemExit(f'Workspace must be a lab snapshot: {workspace!r}')
    default_name = f'{args.run_id}'
    dest = Path(args.dest) if args.dest else default_destination(default_name)
    if dest.exists() and (dest / 'summary.json').exists() and not args.force:
        raise SystemExit(f'Evidence already collected at {dest} (use --force to re-collect)')
    dest.mkdir(parents=True, exist_ok=True)

    stats_dir = f'{workspace}/evidence/runs/{args.run_id}'
    candidates: list[str] = []
    for target in args.target:
        candidates.extend([f'{stats_dir}/stats-{target}.json',
                           f'{stats_dir}/junit-{target}.xml',
                           f'{workspace}/evidence/latest-{target}.json'])
    candidates.extend(args.log_remote or [])
    missing: list[dict] = []
    transfers: list[dict] = []
    collected = collect_files(lab, candidates, dest, missing, transfers)

    per_target: dict[str, dict] = {}
    totals = {'tests': 0, 'passed': 0, 'failed': 0, 'errors': 0, 'skipped': 0,
              'xfailed': 0, 'xpassed': 0}
    for target in args.target:
        stats_remote = f'{stats_dir}/stats-{target}.json'
        entry = {'target': target,
                 'stats': PurePosixPath(stats_remote).name,
                 'junit': f'junit-{target}.xml',
                 'latest': f'latest-{target}.json'}
        stats_local = collected.get(stats_remote)
        if stats_local is not None:
            payload = load_json(stats_local)
            entry.update(stats_summary(payload))
            entry['stats_sha256'] = sha256_file(stats_local)
            for key, value in entry['counts'].items():
                totals[key] += value
            logs = [path for path in (args.log_remote or [])]
            if logs:
                local_log = collected.get(logs[0])
                entry['terminal_log'] = local_log.name if local_log else None
                entry['terminal_hint'] = collect_only_hint(local_log)
        else:
            entry['stats_missing'] = True
        per_target[target] = entry

    config = lab.run(f'cat {REMOTE_LAB}/current-qa.json')
    current = json.loads(config.stdout) if config.returncode == 0 else {}
    source_sha256 = args.expected_source_sha256 or current.get('source_sha256')
    expected = args.expected_source_sha256
    summary = {
        'schema': SCHEMA,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'generated_at_local': datetime.now().astimezone().isoformat(),
        'mode': 'manual-run',
        'collected_by': 'tests/linux_lab/collect_suite_evidence.py',
        'collected_by_sha256': sha256_file(Path(__file__)),
        'vm': probe_vm(lab),
        'run': {
            'run_id': args.run_id,
            'workspace': workspace,
            'stats_dir': stats_dir,
            'source_sha256': source_sha256,
            'source_sha256_origin': ('--expected-source-sha256' if expected
                                     else 'current-qa.json at collection time'),
            'current_qa': current,
            'targets': list(args.target),
        },
        'expected_source_sha256': expected,
        'source_sha256_match': (source_sha256 == expected) if expected else None,
        'per_target': per_target,
        'totals': totals,
        'failures': [{'target': target, **failure}
                     for target, entry in per_target.items()
                     for failure in entry.get('failures', [])],
        'errors': [{'target': target, **item}
                   for target, entry in per_target.items()
                   for item in entry.get('error_items', [])],
        'evidence_files': transfers,
        'missing_evidence': missing,
        'notes': [],
    }
    if missing:
        summary['notes'].append(f'{len(missing)} expected evidence file(s) were absent on the VM')
    output = write_summary(dest, summary)
    print(json.dumps({'status': 'COLLECTED', 'mode': 'manual-run', 'run_id': args.run_id,
                      'dest': str(dest), 'summary': str(output),
                      'source_sha256': source_sha256, 'source_sha256_match': summary['source_sha256_match'],
                      'totals': totals, 'failures': len(summary['failures']),
                      'missing': len(missing)}, ensure_ascii=False), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    global EVIDENCE_ROOT
    config = load_json(LAB_CONFIG) if LAB_CONFIG.is_file() else {}
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--latest', action='store_true', help='collect the newest suite-run on the VM')
    parser.add_argument('--run', help='suite-run stamp directory, e.g. 20261025T061500Z')
    parser.add_argument('--run-id', help='manual run_tests.py run id, e.g. 20261025-061500-abcd1234')
    parser.add_argument('--target', action='append', default=[], help='target for manual-run mode (repeatable)')
    parser.add_argument('--workspace', help='remote workspace path for manual-run mode')
    parser.add_argument('--log-remote', action='append', default=[], help='extra remote log to collect (repeatable)')
    parser.add_argument('--dest', help=f'local evidence dir (default under {EVIDENCE_ROOT})')
    parser.add_argument('--evidence-root', default=None,
                        help='evidence root to write under (default evidence/linux-r0); '
                             'use evidence/linux-r2 for later candidates')
    parser.add_argument('--expected-source-sha256', default=None)
    parser.add_argument('--force', action='store_true', help='re-collect into an existing evidence dir')
    parser.add_argument('--ssh-host', default=config.get('ssh_host', '127.0.0.1'))
    parser.add_argument('--ssh-port', type=int, default=int(config.get('ssh_port', 22224)))
    parser.add_argument('--ssh-user', default=config.get('ssh_user', 'gateway-lab'))
    parser.add_argument('--ssh-key', default=config.get('ssh_key_path', str(LAB / 'lab_ed25519')))
    args = parser.parse_args(argv)

    if args.evidence_root:
        resolved_root = Path(args.evidence_root).resolve()
        repo_evidence = (REPO / 'evidence').resolve()
        if resolved_root != repo_evidence and repo_evidence not in resolved_root.parents:
            raise SystemExit(f'--evidence-root must live under {repo_evidence}')
        EVIDENCE_ROOT = resolved_root

    if args.dest:
        resolved = Path(args.dest).resolve()
        evidence_root = EVIDENCE_ROOT.resolve()
        if resolved != evidence_root and evidence_root not in resolved.parents:
            raise SystemExit(f'--dest must live under {evidence_root} (got {resolved})')
        args.dest = str(resolved)
    lab = Lab(args.ssh_host, args.ssh_port, args.ssh_user, Path(args.ssh_key))
    if args.run or args.latest:
        return suite_run(args, lab)
    if args.run_id:
        return manual_run(args, lab)
    parser.error('choose --latest, --run <stamp> or --run-id <id>')


if __name__ == '__main__':
    raise SystemExit(main())
