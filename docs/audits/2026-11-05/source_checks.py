#!/usr/bin/env python3
"""Read-only syntax and narrow secret-pattern inventory; never import app code.

Python AST parsing, node --check and bash -n do not prove security or runtime
correctness. Regex families omit passwords, many provider formats, compressed
assets and secrets assembled dynamically. Matches report locations, not values.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess

REPO = Path(__file__).resolve().parents[3]
EXCLUDED = 'docs/audits/2026-11-05/'
PATTERNS = {
    'private_pem': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]{64,}-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'github_token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'aws_access_id': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'long_sk_token': re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{48,}\b'),
}


def inspect(path: str) -> dict:
    absolute = REPO / path
    if absolute.is_symlink() or not absolute.is_file():
        return {'path': path, 'not_read': 'symlink or absent'}
    raw = absolute.read_bytes()
    result = {'path': path, 'sha256': hashlib.sha256(raw).hexdigest(),
              'bytes': len(raw), 'checks': [], 'secret_pattern_candidates': []}
    for family, expression in PATTERNS.items():
        for match in expression.finditer(raw):
            result['secret_pattern_candidates'].append({
                'family': family, 'line': raw[:match.start()].count(b'\n') + 1,
            })
    suffix = absolute.suffix.lower()
    if suffix == '.py':
        try:
            ast.parse(raw.decode('utf-8-sig'), filename=path)
            result['checks'].append({'kind': 'python_ast', 'ok': True})
        except (SyntaxError, UnicodeDecodeError) as error:
            result['checks'].append({'kind': 'python_ast', 'ok': False,
                                     'error_type': type(error).__name__,
                                     'line': getattr(error, 'lineno', None)})
    command = None
    if suffix in ('.js', '.mjs', '.cjs'):
        command, kind = ['node', '--check', str(absolute)], 'node_check'
    elif suffix == '.sh':
        command, kind = ['bash', '-n', str(absolute)], 'bash_syntax'
    if command:
        try:
            checked = subprocess.run(command, cwd=REPO, capture_output=True, timeout=30)
            result['checks'].append({'kind': kind, 'ok': checked.returncode == 0,
                                     'exit': checked.returncode})
        except (OSError, subprocess.TimeoutExpired) as error:
            result['checks'].append({'kind': kind, 'ok': False,
                                     'error_type': type(error).__name__})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw_paths = subprocess.check_output(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=REPO)
    paths = sorted({path.decode() for path in raw_paths.split(b'\0') if path
                    and not path.decode().startswith(EXCLUDED)})
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(inspect, paths))
    checks = [item for row in rows for item in row.get('checks', [])]
    candidates = [{'path': row['path'], **item} for row in rows
                  for item in row.get('secret_pattern_candidates', [])]
    receipt = {
        'scope': 'tracked and nonignored candidate paths, excluding this audit evidence directory',
        'interpretation': 'syntax and four regex families only; no proof of vulnerability absence',
        'paths': len(rows), 'check_counts': dict(Counter(item['kind'] for item in checks)),
        'check_failures': sum(not item['ok'] for item in checks),
        'secret_pattern_families': list(PATTERNS),
        'secret_pattern_candidates': candidates,
        'files': rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in receipt.items() if key != 'files'}, ensure_ascii=False))
    return int(bool(receipt['check_failures']))


if __name__ == '__main__':
    raise SystemExit(main())
