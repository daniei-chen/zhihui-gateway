"""Verify that the LOCAL QEMU lab VM holds a byte-identical copy of a frozen archive.

RV06 requires the Linux evidence to be tied to the *installed* tree, not just to
an archive hash.  This script hashes every bundled file inside the VM snapshot
and compares it with the archive's own per-file manifest; it also confirms that
``current-qa.json`` points at the expected ``sha256``-derived snapshot directory.

The same file runs on both sides:

  * host:  ``python -X utf8 tests/linux_lab/verify_snapshot_install.py --archive workspace-<16hex>.tar.gz``
           (copies itself plus the manifest into ``/tmp`` on the VM, runs the
           remote check, fetches the JSON result, writes
           ``evidence/linux-r0/<stamp>/install-verification.json``)
  * VM:    ``/usr/bin/python3 /tmp/verify_snapshot_install.py --remote-check --manifest ... --workspace ...``

Only the disposable local VM (127.0.0.1 / localhost) may be contacted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[2]
LAB = REPO.parent / 'zhihui-linux-lab'
LAB_CONFIG = LAB / 'lab-config.json'
ARTIFACTS = LAB / 'cloud-init' / 'artifacts'
EVIDENCE_ROOT = REPO / 'evidence' / 'linux-r0'
REMOTE_LAB = '/home/gateway-lab/lab'
SCHEMA = 'zhihui-install-verification/1'
ARCHIVE_RE = re.compile(r'workspace-[a-f0-9]{16}\.tar\.gz')
SHA_RE = re.compile(r'[0-9a-f]{64}')
SPOT_CHECK_PATHS = ['manager/server/observability.py', 'qtg/app/qtg/manager_api.py',
                    'manager/server/db.py', 'manager/server/migrations.py',
                    'tests/zhihui_harness/run_tests.py', 'tests/linux_lab/run_suites.py']
FORBIDDEN_HOSTS = {'203.0.113.10'}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- VM side
def remote_check(manifest_path: Path, workspace: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    expected = {entry['path']: entry for entry in manifest['source_manifest']}
    expected_dirs = set(manifest['source_directories'])
    started = time.monotonic()
    modified, missing = [], []
    hashes = {}
    for relative, entry in expected.items():
        path = workspace / relative
        if not path.is_file():
            missing.append(relative)
            continue
        actual = sha256_file(path)
        hashes[relative] = actual
        if actual != entry['sha256']:
            modified.append({'path': relative, 'manifest_sha256': entry['sha256'], 'installed_sha256': actual})
    present_files = set()
    present_dirs = set()
    for directory, dirs, files in __import__('os').walk(workspace):
        here = Path(directory)
        rel_dir = here.relative_to(workspace).as_posix()
        if rel_dir != '.':
            present_dirs.add(rel_dir)
        for filename in files:
            present_files.add((here / filename).relative_to(workspace).as_posix())
    extra = sorted(present_files - set(expected))
    spot = {}
    for relative in SPOT_CHECK_PATHS:
        if relative in expected:
            spot[relative] = {'manifest_sha256': expected[relative]['sha256'],
                              'installed_sha256': hashes.get(relative),
                              'match': hashes.get(relative) == expected[relative]['sha256']}
    return {
        'verified': not modified and not missing and not extra,
        'workspace': str(workspace),
        'archive_sha256': manifest['archive_sha256'],
        'manifest_sha256': sha256_file(manifest_path),
        'file_count_expected': len(expected),
        'file_count_present': len(present_files),
        'directory_count_expected': len(expected_dirs),
        'directory_count_present': len(present_dirs),
        'missing_from_install': missing[:50],
        'missing_count': len(missing),
        'modified_in_install': modified[:50],
        'modified_count': len(modified),
        'extra_files_in_install': extra[:50],
        'extra_count': len(extra),
        'spot_checks': spot,
        'elapsed_s': round(time.monotonic() - started, 2),
    }


# ------------------------------------------------------------------------- host side
def ssh_base(config: dict) -> list[str]:
    host = config.get('ssh_host', '127.0.0.1')
    if host in FORBIDDEN_HOSTS or host not in {'127.0.0.1', 'localhost'}:
        raise SystemExit(f'Refusing to talk to {host!r}: only the local QEMU lab VM is allowed')
    key = Path(config.get('ssh_key_path', str(LAB / 'lab_ed25519')))
    if not key.is_file():
        raise SystemExit(f'SSH key not found: {key}')
    return ['ssh', '-i', str(key), '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=NUL',
            '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-p', str(config.get('ssh_port', 22224)),
            f"{config.get('ssh_user', 'gateway-lab')}@{host}"]


def scp_base(config: dict) -> list[str]:
    key = Path(config.get('ssh_key_path', str(LAB / 'lab_ed25519')))
    return ['scp', '-q', '-i', str(key), '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=NUL', '-o', 'BatchMode=yes',
            '-P', str(config.get('ssh_port', 22224))]


def host_verify(args: argparse.Namespace) -> int:
    config = json.loads(LAB_CONFIG.read_text(encoding='utf-8')) if LAB_CONFIG.is_file() else {}
    if args.manifest:
        manifest_path = Path(args.manifest)
        archive_name = manifest_path.name.removesuffix('.manifest.json')
    else:
        candidates = []
        if args.archive:
            if not ARCHIVE_RE.fullmatch(args.archive):
                raise SystemExit(f'Invalid archive name: {args.archive!r}')
            candidates = [ARTIFACTS / (args.archive + '.manifest.json')]
        else:
            if not SHA_RE.fullmatch(args.sha256 or ''):
                raise SystemExit('Pass --archive, --manifest or a 64-hex --sha256')
            for candidate in sorted(ARTIFACTS.glob('workspace-*.tar.gz.manifest.json')):
                document = json.loads(candidate.read_text(encoding='utf-8'))
                if document.get('archive_sha256') == args.sha256:
                    candidates = [candidate]
                    break
        if not candidates or not candidates[0].is_file():
            raise SystemExit('No manifest found for the requested archive')
        manifest_path = candidates[0]
        archive_name = manifest_path.name.removesuffix('.manifest.json')
    if not ARCHIVE_RE.fullmatch(archive_name):
        raise SystemExit(f'Unexpected manifest name: {manifest_path.name}')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    archive_sha256 = manifest['archive_sha256']

    ssh = ssh_base(config)
    scp = scp_base(config)
    started = time.monotonic()
    probe = subprocess.run(ssh + ['hostname; cat /home/gateway-lab/lab/current-qa.json'], capture_output=True,
                           text=True, encoding='utf-8', errors='replace')
    if probe.returncode != 0:
        raise SystemExit(f'SSH to the lab VM failed: {probe.stderr.strip()[:2000]}')
    lines = probe.stdout.splitlines()
    hostname = lines[0].strip()
    if hostname != 'zhihui-local-lab':
        raise SystemExit(f'Refusing to verify unexpected host {hostname!r}')
    current_qa = json.loads('\n'.join(lines[1:]))
    workspace = PurePosixPath(args.workspace or current_qa['workspace'])
    if not str(workspace).startswith(REMOTE_LAB + '/snapshots/'):
        raise SystemExit(f'Workspace must be a lab snapshot: {workspace}')
    if current_qa.get('source_sha256') != archive_sha256:
        raise SystemExit('current-qa.json source_sha256 does not match the frozen archive: '
                         f'{current_qa.get("source_sha256")} != {archive_sha256}')
    expected_snapshot = REMOTE_LAB + '/snapshots/' + archive_sha256[:16]
    if str(workspace) != expected_snapshot + '/workspace':
        raise SystemExit(f'workspace {workspace} is not the sha256-derived snapshot {expected_snapshot}/workspace')

    remote_tmp = f'/tmp/verify-{archive_sha256[:12]}'
    destination_prefix = f"{config.get('ssh_user', 'gateway-lab')}@{config.get('ssh_host', '127.0.0.1')}"
    mk = subprocess.run(ssh + [f'mkdir -p {remote_tmp}'], capture_output=True, text=True)
    if mk.returncode != 0:
        raise SystemExit(f'Could not create {remote_tmp} on the VM')
    for local, remote in ((Path(__file__), f'{remote_tmp}/verify_snapshot_install.py'),
                          (manifest_path, f'{remote_tmp}/manifest.json')):
        copied = subprocess.run(scp + [str(local), f'{destination_prefix}:{remote}'],
                                capture_output=True, text=True)
        if copied.returncode != 0:
            raise SystemExit(f'scp failed for {local}: {copied.stderr.strip()[:1000]}')
    remote = subprocess.run(ssh + [f'/usr/bin/python3 {remote_tmp}/verify_snapshot_install.py --remote-check '
                                   f'--manifest {remote_tmp}/manifest.json --workspace {shlex.quote(str(workspace))}'],
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    if remote.returncode != 0:
        raise SystemExit(f'remote verification failed: {remote.stderr.strip()[:4000]}')
    result = json.loads(remote.stdout)
    result.update({
        'schema': SCHEMA,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'generated_at_local': datetime.now().astimezone().isoformat(),
        'verified_by': 'tests/linux_lab/verify_snapshot_install.py',
        'verified_by_sha256': sha256_file(Path(__file__)),
        'host': {'hostname': hostname, 'ssh': config.get('ssh_host'), 'port': config.get('ssh_port')},
        'archive': {'name': archive_name, 'sha256': archive_sha256,
                    'manifest_path': str(manifest_path), 'manifest_sha256': sha256_file(manifest_path)},
        'current_qa': current_qa,
        'expected_snapshot_dir': expected_snapshot,
        'total_elapsed_s': round(time.monotonic() - started, 2),
    })
    if args.out:
        output = Path(args.out)
        if EVIDENCE_ROOT.resolve() not in output.resolve().parents:
            raise SystemExit(f'--out must live under {EVIDENCE_ROOT}')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        result['written'] = str(output)
    print(json.dumps({key: result[key] for key in
                      ('verified', 'workspace', 'file_count_expected', 'file_count_present',
                       'modified_count', 'missing_count', 'extra_count', 'spot_checks',
                       'total_elapsed_s', 'written') if key in result}, ensure_ascii=False, indent=1))
    return 0 if result['verified'] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--remote-check', action='store_true', help='internal: run inside the VM')
    parser.add_argument('--manifest', help='local path to <archive>.manifest.json')
    parser.add_argument('--archive', help='archive file name, e.g. workspace-<16hex>.tar.gz')
    parser.add_argument('--sha256', help='archive sha256 to look up in the artifacts manifests')
    parser.add_argument('--workspace', help='remote workspace to verify (default: current-qa.json)')
    parser.add_argument('--out', help=f'write the report under {EVIDENCE_ROOT}')
    args = parser.parse_args(argv)
    if args.remote_check:
        if not args.manifest or not args.workspace:
            parser.error('--remote-check requires --manifest and --workspace')
        result = remote_check(Path(args.manifest), Path(args.workspace))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    return host_verify(args)


if __name__ == '__main__':
    raise SystemExit(main())
