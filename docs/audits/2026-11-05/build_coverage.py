#!/usr/bin/env python3
"""Merge explicit human review receipts with reproducible syntax checks.

Never infer human review from passing tests, filenames or authored ownership.
Audit deliverables themselves are excluded from the source-review denominator.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
BASE = '4c6b4cb8cb790698e0753909745dfe8a7c78dd9f'
EXCLUDE = HERE.relative_to(REPO).as_posix() + '/'
LEVELS = {
    'manual-full': 'manual-full', 'full-read': 'manual-full',
    'full': 'manual-full', 'FULL': 'manual-full', 'FULL-DOC': 'manual-full',
    'manual-full-patch': 'manual-full',
    'manual-targeted': 'manual-targeted', 'partial': 'manual-targeted',
    'PARTIAL': 'manual-targeted', 'targeted-read': 'manual-targeted',
    'partial-read': 'manual-targeted',
    'automated': 'automated-only', 'AUTOMATED': 'automated-only',
    'static-lint/typecheck/build-only': 'automated-only',
    'static/lint/typecheck/build-only': 'automated-only',
    'inventory-only': 'inventory-only',
}
RANK = {'inventory-only': 0, 'automated-only': 1,
        'manual-targeted': 2, 'manual-full': 3}
PREFIXES = ('manager/', 'src/', 'site/', 'tools/', 'tests/', 'docs/',
            'ops/', 'design/', '.github/')


def git(*args: str) -> bytes:
    return subprocess.check_output(['git', *args], cwd=REPO)


def category(path: str) -> str:
    if path.startswith('manager/server/') and '/tests/' not in path and path.endswith('.py'):
        return 'manager-production-python'
    if path.startswith('src/three-platform/web/'):
        if '.test.' in path or '/tests/' in path:
            return 'frontend-tests'
        if path.endswith(('.tsx', '.ts', '.jsx', '.js', '.mjs', '.cjs')):
            return 'frontend-source-and-config'
        return 'frontend-other'
    if '/tests/' in '/' + path or '.test.' in path or Path(path).name.startswith('test_'):
        return 'tests-and-fixtures'
    if path.endswith('.patch'):
        return 'public-patches-not-complete-engine'
    if path.startswith('site/'):
        return 'historical-site-source-and-assets'
    if path.endswith(('.py', '.sh', '.ps1', '.cmd', '.bat', '.js', '.mjs', '.cjs')):
        return 'other-tools-and-runtime-source'
    return 'docs-config-and-other-assets'


def main() -> None:
    baseline = {}
    for entry in git('ls-tree', '-r', '-z', BASE).split(b'\0'):
        if not entry:
            continue
        meta, path = entry.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        baseline[path.decode()] = {'mode': mode, 'kind': kind, 'oid': oid}

    paths = {p.decode() for p in git('ls-files', '--cached', '--others',
                                   '--exclude-standard', '-z').split(b'\0') if p}
    paths = sorted((paths | set(baseline)) - {p for p in paths if p.startswith(EXCLUDE)})
    paths = [p for p in paths if not p.startswith(EXCLUDE)]
    levels = {p: 'inventory-only' for p in paths}
    evidence = defaultdict(set)
    claims = defaultdict(list)

    for source in sorted((HERE / 'reviews').iterdir()):
        if source.suffix not in ('.tsv', '.txt'):
            continue
        for line in source.read_text(encoding='utf-8').splitlines():
            if line.startswith('#'):
                continue
            fields = line.split('\t')
            level = next((LEVELS[x] for x in fields if x in LEVELS), None)
            path = next((x.split(' (')[0] for x in fields if x.startswith(PREFIXES)), None)
            if not level or path not in levels:
                continue
            if RANK[level] > RANK[levels[path]]:
                levels[path] = level
            evidence[path].add(source.name)
            claims[path].append(level)

    checks = json.loads((HERE / 'evidence/source-checks.json').read_text())
    by_path = {row['path']: row for row in checks['files']}
    for path, row in by_path.items():
        if path not in levels:
            continue
        if row.get('checks'):
            if RANK[levels[path]] < RANK['automated-only']:
                levels[path] = 'automated-only'
            evidence[path].add('source-checks.json:' + ','.join(c['kind'] for c in row['checks']))

    # Read exact baseline Git objects in one subprocess, without trusting another checkout.
    objects = list(dict.fromkeys(meta['oid'] for meta in baseline.values() if meta['kind'] == 'blob'))
    payload = subprocess.run(['git', 'cat-file', '--batch'], cwd=REPO,
                             input=('\n'.join(objects) + '\n').encode(),
                             capture_output=True, check=True).stdout
    stream = io.BytesIO(payload)
    hashes = {}
    for _ in objects:
        oid, kind, length = stream.readline().decode().split()
        raw = stream.read(int(length))
        assert stream.read(1) == b'\n'
        hashes[oid] = hashlib.sha256(raw).hexdigest()

    rows = []
    for path in paths:
        file = REPO / path
        raw = file.read_bytes() if file.is_file() and not file.is_symlink() else None
        candidate_hash = hashlib.sha256(raw).hexdigest() if raw is not None else ''
        check_hash = by_path.get(path, {}).get('sha256')
        if check_hash and candidate_hash != check_hash:
            raise RuntimeError('Source changed after syntax receipt: ' + path)
        base = baseline.get(path, {})
        rows.append({
            'path': path, 'category': category(path), 'review_level': levels[path],
            'baseline_present': str(bool(base)).lower(),
            'candidate_present': str(raw is not None).lower(),
            'lines': len(raw.splitlines()) if raw is not None else '',
            'bytes': len(raw) if raw is not None else '',
            'baseline_git_blob': base.get('oid', ''),
            'baseline_sha256': hashes.get(base.get('oid'), ''),
            'candidate_sha256': candidate_hash,
            'evidence': ';'.join(sorted(evidence[path])),
        })
    with (HERE / 'coverage.tsv').open('w', encoding='utf-8', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]), delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)
    grouped = defaultdict(Counter)
    for row in rows:
        grouped[row['category']][row['review_level']] += 1
    summary = {
        'baseline_commit': BASE, 'baseline_paths': len(baseline),
        'candidate_source_paths': sum(row['candidate_present'] == 'true' for row in rows),
        'source_union_paths': len(rows),
        'excludes': EXCLUDE,
        'interpretation': 'manual-full means entire public file read, not formal proof; patches do not cover private engine source; test execution is not human review',
        'review_levels': dict(Counter(row['review_level'] for row in rows)),
        'categories': {key: dict(value) for key, value in sorted(grouped.items())},
        'manager_production_python_lines': sum(int(row['lines'] or 0) for row in rows if row['category'] == 'manager-production-python'),
        'human_full_lines_all_file_types': sum(int(row['lines'] or 0) for row in rows if row['review_level'] == 'manual-full'),
    }
    (HERE / 'coverage-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
