"""Freeze the current working tree into an immutable, manifest-backed candidate bundle.

This is the R0 (RV06) freeze step for the LOCAL Linux QA VM.  It wraps
``tests/linux_lab/prepare_test_bundle.py`` (the reviewed bundler) and adds the
evidence the review asked for:

  * ``evidence/linux-r0/<stamp>/frozen-candidate.json`` with the archive name,
    sha256 and byte count, ``input_manifest_sha256``, the per-file manifest path
    and its own sha256, a fingerprint of **every** ``.py`` in the frozen bundle,
    ``git HEAD`` and a ``git status`` summary, plus UTC/local timestamps;
  * archive content verification against its own manifest, so a pre-existing
    archive whose bytes do not match the manifest can never be published as the
    candidate (``prepare_test_bundle.py`` skips writing an archive that already
    exists under the same name);
  * idempotent behaviour: the same working tree always yields the same archive
    name, and a re-run reuses the existing evidence document instead of writing
    a second copy.  Different content yields a different archive, and a
    same-name/different-content archive aborts instead of being overwritten.

Usage (host, from anywhere)::

    python -X utf8 tests/linux_lab/freeze_candidate.py
    python -X utf8 tests/linux_lab/freeze_candidate.py --stamp 20261025T050000Z
    python -X utf8 tests/linux_lab/freeze_candidate.py --allow-online   # refresh pins

Never contacts production; the bundler only reads the local tree and the
already-pinned Node archive under ``zhihui-linux-lab/cloud-init/artifacts``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LINUX_LAB = Path(__file__).resolve().parent
PREPARE = LINUX_LAB / 'prepare_test_bundle.py'
LAB = REPO.parent / 'zhihui-linux-lab'
ARTIFACTS = LAB / 'cloud-init' / 'artifacts'
EVIDENCE_ROOT = REPO / 'evidence' / 'linux-r0'
SCHEMA = 'zhihui-frozen-candidate/1'
ARCHIVE_RE = re.compile(r'workspace-[a-f0-9]{16}\.tar\.gz')
SHA_RE = re.compile(r'[0-9a-f]{64}')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd or REPO), capture_output=True, text=True,
                          encoding='utf-8', errors='replace')


def git_summary(repo: Path) -> dict:
    head = run(['git', 'rev-parse', 'HEAD'], repo)
    branch = run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], repo)
    status = run(['git', 'status', '--porcelain'], repo)
    tracked = run(['git', 'ls-files'], repo)
    if head.returncode != 0 or status.returncode != 0:
        return {'available': False, 'error': (head.stderr or status.stderr or 'git failed').strip()[-2000:]}
    lines = [line for line in status.stdout.splitlines() if line.strip()]
    tracked_modified = sum(1 for line in lines if line[:2] in (' M', 'M ', 'MM', 'AM', 'RM', 'R '))
    deleted = sum(1 for line in lines if 'D' in line[:2])
    added = sum(1 for line in lines if 'A' in line[:2] or line.startswith('??'))
    untracked = sum(1 for line in lines if line.startswith('??'))
    tracked_py = sorted(path for path in tracked.stdout.splitlines() if path.endswith('.py'))
    untracked_py = sorted(line[3:].strip() for line in lines
                          if line.startswith('??') and line[3:].strip().endswith('.py'))
    return {
        'available': True,
        'head': head.stdout.strip(),
        'branch': branch.stdout.strip(),
        'tracked_modified': tracked_modified,
        'untracked': untracked,
        'staged_added': added - untracked,
        'deleted': deleted,
        'dirty': bool(lines) or head.stdout.strip() == '',
        'porcelain_lines': len(lines),
        'status_lines': lines,
        'tracked_file_count': len(tracked.stdout.splitlines()),
        'tracked_python_files': tracked_py,
        'untracked_python_files': untracked_py,
    }


def verify_archive(archive: Path, manifest: dict) -> dict:
    """Hash every member of the archive and compare with its own source manifest."""
    expected_files = {entry['path']: entry for entry in manifest['source_manifest']}
    expected_dirs = set(manifest['source_directories'])
    seen_files: dict[str, str] = {}
    seen_dirs: set[str] = set()
    unexpected: list[str] = []
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar:
            name = member.name
            if not (name == 'workspace' or name.startswith('workspace/')):
                unexpected.append(name)
                continue
            relative = name[len('workspace/'):]
            if member.isdir():
                if relative:
                    seen_dirs.add(relative.rstrip('/'))
            elif member.isfile():
                extracted = tar.extractfile(member)
                if extracted is None:
                    unexpected.append(name + ' (unreadable)')
                    continue
                digest = hashlib.sha256()
                for chunk in iter(lambda: extracted.read(1024 * 1024), b''):
                    digest.update(chunk)
                seen_files[relative] = digest.hexdigest()
            else:
                unexpected.append(name + ' (not file/dir)')
    mismatches = []
    for path, entry in expected_files.items():
        actual = seen_files.pop(path, None)
        if actual is None:
            mismatches.append({'path': path, 'problem': 'missing from archive'})
        elif actual != entry['sha256']:
            mismatches.append({'path': path, 'problem': 'content diff',
                               'manifest_sha256': entry['sha256'], 'archive_sha256': actual})
    for path in seen_files:
        mismatches.append({'path': path, 'problem': 'archive file not in manifest'})
    missing_dirs = sorted(expected_dirs - seen_dirs)
    extra_dirs = sorted(seen_dirs - expected_dirs)
    return {
        'verified': not mismatches and not unexpected and not missing_dirs and not extra_dirs,
        'files_checked': len(expected_files),
        'dirs_checked': len(expected_dirs),
        'unexpected_members': unexpected[:50],
        'file_mismatches': mismatches[:50],
        'file_mismatch_count': len(mismatches),
        'missing_directories': missing_dirs[:50],
        'extra_directories': extra_dirs[:50],
    }


def python_fingerprint(manifest: dict) -> dict:
    files = {entry['path']: entry['sha256'] for entry in manifest['source_manifest']
             if entry['path'].endswith('.py')}
    payload = json.dumps(sorted(files.items()), sort_keys=True).encode()
    return {
        'algorithm': 'sha256',
        'definition': 'sha256 over sorted (path, sha256) pairs of every .py file inside the frozen archive',
        'aggregate_sha256': hashlib.sha256(payload).hexdigest(),
        'file_count': len(files),
        'total_bytes': sum(entry['bytes'] for entry in manifest['source_manifest']
                           if entry['path'].endswith('.py')),
        'files': files,
    }


def load_evidence(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return None


def find_existing(archive_sha256: str, manifest_sha256: str) -> Path | None:
    if not EVIDENCE_ROOT.is_dir():
        return None
    for candidate in sorted(EVIDENCE_ROOT.glob('*/frozen-candidate.json')):
        document = load_evidence(candidate)
        if not document:
            continue
        archive = document.get('archive') or {}
        if archive.get('sha256') == archive_sha256 and document.get('input_manifest_sha256') == manifest_sha256:
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', default=str(REPO), help='execution tree to freeze (default: this repo)')
    parser.add_argument('--stamp', default=None, help='evidence directory name (default: UTC timestamp)')
    parser.add_argument('--allow-online', action='store_true',
                        help='fetch fresh QA/Node pins instead of reusing the reviewed offline pins')
    parser.add_argument('--skip-archive-verify', action='store_true',
                        help='skip re-hashing every archive member (not recommended)')
    parser.add_argument('--new-evidence', action='store_true',
                        help='write a new evidence document even if one already exists for this archive')
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if repo != REPO:
        raise SystemExit(f'This freeze step is pinned to {REPO} (got {repo})')
    global ARTIFACTS, EVIDENCE_ROOT
    ARTIFACTS = repo.parent / 'zhihui-linux-lab' / 'cloud-init' / 'artifacts'
    EVIDENCE_ROOT = repo / 'evidence' / 'linux-r0'
    generated = datetime.now(timezone.utc)
    local = generated.astimezone()

    # -- 1. produce / reuse the content-addressed archive --------------------
    command = [sys.executable, '-X', 'utf8', str(PREPARE)]
    if not args.allow_online:
        command.append('--offline')
    result = run(command)
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-4000:])
        sys.stderr.write(result.stderr[-4000:])
        raise SystemExit(f'prepare_test_bundle.py failed with exit {result.returncode}')
    bundle = json.loads(result.stdout.strip().splitlines()[-1])
    archive = ARTIFACTS / bundle['archive']
    if not ARCHIVE_RE.fullmatch(archive.name):
        raise SystemExit(f'Unexpected archive name from bundler: {archive.name}')
    manifest_path = ARTIFACTS / (archive.name + '.manifest.json')
    current_manifest_path = ARTIFACTS / 'bundle-manifest.json'
    if not manifest_path.is_file() or not current_manifest_path.is_file():
        raise SystemExit('Bundler did not write the expected manifest files')
    manifest_text = manifest_path.read_text(encoding='utf-8')
    if manifest_text != current_manifest_path.read_text(encoding='utf-8'):
        raise SystemExit('bundle-manifest.json diverged from the immutable per-archive manifest')
    manifest = json.loads(manifest_text)
    manifest_sha256 = hashlib.sha256(manifest_text.encode('utf-8')).hexdigest()

    # -- 2. immutable-archive guards ---------------------------------------
    if not archive.is_file():
        raise SystemExit(f'Bundler reported {archive.name} but the file is missing')
    archive_bytes = archive.stat().st_size
    archive_sha256 = sha256_file(archive)
    if archive_sha256 != manifest['archive_sha256']:
        raise SystemExit('EXISTING ARCHIVE CONTENT DOES NOT MATCH ITS MANIFEST: '
                         f'{archive.name} {archive_sha256} != {manifest["archive_sha256"]}')
    if archive_bytes != manifest['archive_bytes']:
        raise SystemExit('EXISTING ARCHIVE SIZE DOES NOT MATCH ITS MANIFEST: '
                         f'{archive_bytes} != {manifest["archive_bytes"]}')
    if bundle['archive_sha256'] != archive_sha256:
        raise SystemExit('Bundler-reported sha256 disagrees with the file on disk')

    verification = {'verified': True, 'skipped': True, 'files_checked': 0, 'dirs_checked': 0}
    if not args.skip_archive_verify:
        verification = verify_archive(archive, manifest)
        if not verification['verified']:
            raise SystemExit('Archive content does not match its own manifest; refusing to publish: '
                             + json.dumps(verification, ensure_ascii=False)[:2000])

    # -- 3. evidence document ---------------------------------------------
    fingerprint = python_fingerprint(manifest)
    git = git_summary(repo)
    bundled_py = set(fingerprint['files'])
    tracked_py = set(git.get('tracked_python_files') or [])
    untracked_py = set(git.get('untracked_python_files') or [])
    document = {
        'schema': SCHEMA,
        'generated_at': generated.isoformat(),
        'generated_at_local': local.isoformat(),
        'repo': str(repo),
        'archive': {
            'name': archive.name,
            'path': str(archive),
            'relative_path': archive.relative_to(repo.parent).as_posix(),
            'sha256': archive_sha256,
            'bytes': archive_bytes,
            'manifest_path': str(manifest_path),
            'manifest_relative_path': manifest_path.relative_to(repo.parent).as_posix(),
            'manifest_sha256': manifest_sha256,
            'manifest_bytes': manifest_path.stat().st_size,
            'file_count': len(manifest['source_manifest']),
            'directory_count': len(manifest['source_directories']),
        },
        'input_manifest_sha256': manifest['input_manifest_sha256'],
        'node': {'archive': manifest['node_archive'], 'sha256': manifest['node_sha256']},
        'canonical_targets': manifest.get('canonical_targets', []),
        'python_fingerprint': fingerprint,
        'python_tree': {
            'git_tracked_python_files': len(tracked_py),
            'tracked_python_not_in_bundle': sorted(tracked_py - bundled_py),
            'untracked_python_files_bundled_by_walk': sorted(untracked_py & bundled_py),
            'note': ('The frozen bundle is produced by walking the working tree, so untracked files are '
                     'included; tracked .py files missing from the bundle would indicate an exclusion gap.'),
        },
        'git': git,
        'verification': verification,
        'frozen_by': 'tests/linux_lab/freeze_candidate.py',
        'frozen_by_sha256': sha256_file(Path(__file__)),
        'reused': False,
    }

    existing = None if args.new_evidence else find_existing(archive_sha256, manifest['input_manifest_sha256'])
    if existing is not None:
        document['reused'] = True
        document['reused_from'] = str(existing)
        print(json.dumps({'status': 'REUSED', 'archive': archive.name, 'sha256': archive_sha256,
                          'evidence': str(existing)}, ensure_ascii=False), flush=True)
        return 0

    stamp = args.stamp or generated.strftime('%Y%m%dT%H%M%SZ')
    if not re.fullmatch(r'[A-Za-z0-9._-]+', stamp):
        raise SystemExit(f'Invalid stamp: {stamp!r}')
    target_dir = EVIDENCE_ROOT / stamp
    if target_dir.exists() and any(target_dir.iterdir()):
        raise SystemExit(f'Evidence directory already exists and is not empty: {target_dir}')
    target_dir.mkdir(parents=True, exist_ok=True)
    document['evidence_dir'] = str(target_dir)
    output = target_dir / 'frozen-candidate.json'
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({
        'status': 'FROZEN',
        'archive': archive.name,
        'sha256': archive_sha256,
        'bytes': archive_bytes,
        'input_manifest_sha256': manifest['input_manifest_sha256'],
        'manifest': str(manifest_path),
        'python_fingerprint_sha256': fingerprint['aggregate_sha256'],
        'python_files': fingerprint['file_count'],
        'git_head': git.get('head'),
        'git': {k: git.get(k) for k in ('branch', 'tracked_modified', 'untracked', 'deleted')},
        'archive_verification': {'verified': verification['verified'],
                                 'files_checked': verification['files_checked']},
        'node': document['node'],
        'evidence': str(output),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
