"""Retain a native Next export's real build directory in file-only archives."""
import argparse
import json
from pathlib import Path
import re
import tarfile


def check(web, apply=False):
    web = Path(web)
    output = web / 'out'
    if output.is_symlink() or getattr(output, 'is_junction', lambda: False)():
        raise ValueError('export link rejected')
    build = (web / '.next/BUILD_ID').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', build):
        raise ValueError('invalid native build identity')
    candidates = sorted(path.name for path in (output / '_next').iterdir()
                        if path.is_dir() and path.name != 'static')
    if candidates not in ([], [build]) or not (output / '_next/static' / build).is_dir():
        raise ValueError('native export namespace is not unique')
    namespace = output / '_next' / build
    if namespace.is_symlink() or getattr(namespace, 'is_junction', lambda: False)():
        raise ValueError('namespace link rejected')
    if apply:
        if (output / 'manifest.sig').exists():
            raise ValueError('never modify an already signed export; prepare a new candidate')
        namespace.mkdir(exist_ok=True)
    if not namespace.is_dir() or list(namespace.iterdir()):
        raise ValueError('native namespace must exist and remain empty; do not put markers outside static scopes')
    return {'build_id': build, 'namespace_materialized': True,
            'archive_must_preserve_empty_directories': True,
            'scope': 'shape only; trusted publisher still verifies Flight, signatures and complete pool preparation'}


def check_archive(archive_path, build, prefix='web/out'):
    wanted = (prefix.rstrip('/') + '/' if prefix else '') + '_next/' + build
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        if not any(member.name.rstrip('/') == wanted and member.isdir() for member in members):
            raise ValueError('archive dropped the native empty build directory')
        if any(member.isfile() and member.name.startswith(wanted + '/') for member in members):
            raise ValueError('archive contains an asset outside allowed static scopes')
    return {'native_directory_preserved': True, 'build_id': build}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(check(args.web, args.apply)))
