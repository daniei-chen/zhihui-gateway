"""Recreate an ignored editable public/ tree from verified versioned output."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'home-import.json').read_text(encoding='utf-8'))
output = root / 'home-source/public'
if output.exists():
    raise ValueError('public already exists; preserve local edits instead of overwriting')
prefix = manifest['asset_prefix']
staged = []
for row in manifest['files']:
    source = (root / row['path']).resolve(strict=True)
    if not source.is_relative_to(root):
        raise ValueError('unsafe source')
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row['sha256']:
        raise ValueError('import drift: ' + row['path'])
    name = row['path']
    if name == 'home/index.html':
        relative = 'index.html'
    else:
        relative = name.removeprefix('home/' + prefix.removeprefix('/home-assets/'))
        if relative == name:
            raise ValueError('unexpected asset namespace')
    if Path(relative).suffix in {'.html', '.js', '.css', '.json', '.webmanifest'}:
        text = raw.decode('utf-8').replace(prefix, '/')
        raw = text.encode('utf-8')
    target = (output / relative).resolve()
    if not target.is_relative_to(output):
        raise ValueError('unsafe output')
    staged.append((target, raw))
for target, raw in staged:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
print(json.dumps({'files': len(staged), 'login_mode': 'separate-preserved-page-not-materialized',
                  'production_contacted': False}))
