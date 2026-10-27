"""Create a manifest-backed test bundle and pinned Linux QA dependencies."""
from __future__ import annotations

import concurrent.futures
import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
from pathlib import Path
from urllib.request import Request, urlopen

from download_prerequisites import LAB, download, fetch_json

REPO = Path(__file__).resolve().parents[2]
NODE_VERSION = '24.19.0'
QA_VERSIONS = {'pytest': '9.1.1', 'pytest-asyncio': '1.4.0', 'iniconfig': '2.3.0',
               'packaging': '26.3', 'pluggy': '1.6.0', 'Pygments': '2.21.0',
               'pydantic-settings': '2.15.0'}


def qa_requirement(pair):
    name, version = pair
    metadata = fetch_json(f'https://pypi.org/pypi/{name}/{version}/json')
    hashes = sorted({item['digests']['sha256'] for item in metadata['urls']
                     if item['packagetype'] == 'bdist_wheel' and not item.get('yanked')})
    if not hashes:
        raise RuntimeError('No publisher wheel hashes for ' + name)
    return f'{name}=={version} ' + ' '.join('--hash=sha256:' + h for h in hashes)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--offline',action='store_true',
        help='Reuse already-pinned QA hashes and a locally hash-verified Node archive; never fetch new pins')
    args=parser.parse_args()
    qa_lock = Path(__file__).with_name('requirements-dev.hashes.txt')
    node_name = f'node-v{NODE_VERSION}-linux-x64.tar.xz'
    artifacts = LAB / 'cloud-init' / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    if args.offline:
        requirements=[line for line in qa_lock.read_text(encoding='utf-8').splitlines()
                      if line.strip() and not line.startswith('#')]
        expected={f'{name}=={version}' for name,version in QA_VERSIONS.items()}
        if {line.split()[0] for line in requirements} != expected or any(
                not re.fullmatch(r'\S+ (?:--hash=sha256:[0-9a-f]{64})(?: --hash=sha256:[0-9a-f]{64})*',line)
                for line in requirements):
            raise RuntimeError('Offline QA lock does not match the reviewed pins')
        previous=json.loads((artifacts/'bundle-manifest.json').read_text(encoding='utf-8'))
        checksum=previous['node_sha256']
        node=artifacts/node_name
        if (previous['node_archive']!=node_name or not re.fullmatch('[0-9a-f]{64}',checksum)
                or not node.is_file() or hashlib.sha256(node.read_bytes()).hexdigest()!=checksum):
            raise RuntimeError('Offline Node archive does not match the existing verified pin')
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            requirements = list(pool.map(qa_requirement, QA_VERSIONS.items()))
        qa_lock.write_text('# QA versions match the reviewed Windows baseline; publisher wheel digests.\n'
                           + '\n'.join(requirements) + '\n', encoding='utf-8')
        sums_url = f'https://nodejs.org/dist/v{NODE_VERSION}/SHASUMS256.txt'
        with urlopen(Request(sums_url, headers={'User-Agent': 'zhihui-local-verification/1.0'}), timeout=30) as response:
            sums = response.read().decode('utf-8')
        checksum = next(line.split()[0] for line in sums.splitlines() if line.split()[-1] == node_name)
        if not re.fullmatch('[0-9a-f]{64}', checksum):
            raise RuntimeError('Invalid Node publisher digest')
        node = download({'name': node_name, 'algorithm': 'sha256', 'digest': checksum,
                         'url': f'https://nodejs.org/dist/v{NODE_VERSION}/{node_name}',
                         'publisher_record': sums_url})
    node_destination = artifacts / node_name
    if not node_destination.exists():
        shutil.copyfile(node['path'], node_destination)
    if hashlib.sha256(node_destination.read_bytes()).hexdigest() != checksum:
        raise RuntimeError('Node transfer artifact verification failed')
    excluded = {'.git', '__pycache__', '.pytest_cache', 'node_modules', '.next', '.venv', 'venv', '.deps'}
    roots = ['manager', 'qtg', 'tests', 'tools', 'ops', 'release', 'design', 'contracts',
             'recovery', 'tasks', 'baseline', 'src/three-platform']
    files = set()
    directories = set()
    for name in roots:
        for directory, dirs, names in os.walk(REPO / name):
            here = Path(directory)
            dirs[:] = [d for d in dirs if d not in excluded and not (here / d).is_symlink()
                       and not (here == REPO / 'src/three-platform' and d in {'server', 'data'})
                       and not (here == REPO / 'manager' and d == 'data')
                       and not (here == REPO / 'qtg' and d in {'var', 'secrets'})]
            directories.add(here.relative_to(REPO).as_posix())
            for filename in names:
                p = here / filename
                if p.is_symlink() or filename.endswith(('.pyc', '.pyo')):
                    continue
                if filename.startswith('.env') and not filename.endswith('.example'):
                    continue
                files.add(p)
    for name in ('.gitignore', 'README.md', 'HANDOVER.md'):
        if (REPO / name).exists():
            files.add(REPO / name)
    manifest = []
    content = []
    for path in sorted(files):
        if not path.resolve().is_relative_to(REPO.resolve()):
            raise RuntimeError('Bundle member escapes execution tree')
        data = path.read_bytes()
        relative = path.relative_to(REPO).as_posix()
        manifest.append({'path': relative, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        content.append((relative, data))
    input_hash = hashlib.sha256(json.dumps({'files': manifest, 'directories': sorted(directories)},
                                         sort_keys=True).encode()).hexdigest()
    archive = artifacts / f'workspace-{input_hash[:16]}.tar.gz'
    if not archive.exists():
        with archive.open('xb') as output, gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as tar:
                # Next export's _next/<buildId> marker may be an empty directory.
                # Losing it changes the artifact and makes a real publisher reject it.
                for relative in sorted(directories):
                    member = tarfile.TarInfo('workspace/' + relative)
                    member.type = tarfile.DIRTYPE
                    member.mtime = 0
                    member.mode = 0o755
                    tar.addfile(member)
                for relative, data in content:
                    member = tarfile.TarInfo('workspace/' + relative)
                    member.size = len(data)
                    member.mtime = 0
                    member.mode = 0o755 if relative.endswith('.sh') else 0o644
                    tar.addfile(member, io.BytesIO(data))
    package = {'source_manifest': manifest, 'source_directories': sorted(directories),
               'input_manifest_sha256': input_hash,
               'archive': archive.name, 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
               'archive_bytes': archive.stat().st_size, 'node_archive': node_name,
               'node_sha256': checksum, 'reference_server_excluded': True,
               'canonical_targets': ['manager-assets', 'qtg']}
    manifest_text = json.dumps(package, indent=2)
    versioned_manifest = artifacts / (archive.name + '.manifest.json')
    if versioned_manifest.exists() and versioned_manifest.read_text(encoding='utf-8') != manifest_text:
        raise RuntimeError('Immutable artifact manifest changed')
    versioned_manifest.write_text(manifest_text, encoding='utf-8')
    (artifacts / 'bundle-manifest.json').write_text(manifest_text, encoding='utf-8')
    print(json.dumps({k: package[k] for k in ('archive', 'archive_bytes', 'archive_sha256', 'node_archive')}), flush=True)


if __name__ == '__main__':
    main()
