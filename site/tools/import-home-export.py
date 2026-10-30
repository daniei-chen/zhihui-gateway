"""Import an inspected homepage without replacing the preserved login page.

Usage: python site/tools/import-home-export.py <public-directory>
The source ZIP and editable project remain separate, unmodified evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def rewrite(text: str, files: set[str], prefix: str) -> str:
    # Prefix only existing assets. /models is also a protected console route.
    for name in sorted(files, key=len, reverse=True):
        text = text.replace('/' + name, prefix + name)
    text = text.replace("'/vendor/draco/'", repr(prefix + 'vendor/draco/'))
    text = text.replace('"/vendor/draco/"', json.dumps(prefix + 'vendor/draco/'))
    text = text.replace('https://zhihui.kaogong.art/dashboard/', '/dashboard/')
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('public', type=Path)
    args = parser.parse_args()
    source = args.public.resolve(strict=True)
    editable_public = (ROOT / 'home-source/public').resolve()
    if source != editable_public and (source == ROOT or ROOT.is_relative_to(source) or source.is_relative_to(ROOT)):
        raise ValueError('source must be outside the repository site tree')
    inputs = [p for p in source.rglob('*') if p.is_file()]
    if any(p.is_symlink() or not p.resolve().is_relative_to(source) for p in inputs):
        raise ValueError('symlink source rejected')
    names = {p.relative_to(source).as_posix() for p in inputs}
    if 'index.html' not in names:
        raise ValueError('homepage export missing')
    login = ROOT / 'login/index.html'
    preservation = json.loads((ROOT / 'login/preservation.json').read_bytes())
    login_raw = login.read_bytes()
    login_sha = hashlib.sha256(login_raw).hexdigest()
    if (preservation.get('policy') != 'preserve-production-layout-and-behavior'
            or preservation.get('candidate_sha256') != login_sha):
        raise ValueError('preserved login drift; homepage import cannot replace it')
    digest = hashlib.sha256(b'zhihui-home-import-v2\0')
    for path in sorted(inputs):
        digest.update(path.relative_to(source).as_posix().encode() + b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    revision = digest.hexdigest()
    # The existing server sends immutable year-long caching on /home-assets/.
    # A content-derived directory avoids reusing URLs for different bytes.
    prefix = '/home-assets/r-' + revision[:20] + '/'
    output = ROOT / 'home'
    output.mkdir(exist_ok=True)
    written = []
    for path in sorted(inputs):
        name = path.relative_to(source).as_posix()
        if name == 'login/index.html':
            continue
        raw = path.read_bytes()
        if path.suffix in {'.html', '.js', '.css', '.json', '.webmanifest'}:
            text = rewrite(raw.decode('utf-8'), names, prefix)
            raw = text.encode('utf-8')
        if name == 'index.html':
            target = output / 'index.html'
        else:
            target = output / ('r-' + revision[:20]) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target != output / 'index.html' and target.read_bytes() != raw:
            raise ValueError('immutable asset drift: ' + str(target))
        target.write_bytes(raw)
        written.append({'path': target.relative_to(ROOT).as_posix(), 'bytes': len(raw),
                        'sha256': hashlib.sha256(raw).hexdigest(),
                        'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {'source_archive_sha256': 'f328157a8bb8c0db48ff8adb315e43f9a34d5855cbd19e64f270b1cf72134e07',
                'source_tree_sha256': revision,
                'asset_prefix': prefix, 'production_deployed': False,
                'login_contract': 'Preserved production login; homepage importer must never replace it',
                'login': {'path': 'login/index.html', 'preservation_manifest': 'login/preservation.json',
                          'sha256': login_sha},
                'files': written}
    (ROOT / 'home-import.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'files': len(written), 'bytes': sum(row['bytes'] for row in written),
                      'production_deployed': False}))


if __name__ == '__main__':
    main()
