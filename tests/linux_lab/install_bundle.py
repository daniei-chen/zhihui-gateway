"""Install a verified QA snapshot inside the disposable LOCAL Ubuntu VM only."""
import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import tarfile
from pathlib import Path
from urllib.request import urlopen


def fetch(name, checksum, target):
    destination = target / name
    if not destination.exists():
        partial = destination.with_suffix(destination.suffix + '.part')
        with urlopen('http://10.0.2.2:18181/artifacts/' + name, timeout=30) as response, partial.open('wb') as output:
            for chunk in iter(lambda: response.read(1024 * 1024), b''):
                output.write(chunk)
        with partial.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != checksum:
                raise RuntimeError('Transfer checksum mismatch')
        partial.replace(destination)
    with destination.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != checksum:
            raise RuntimeError('Cached artifact failed verification')
    return destination


def main():
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != Path('/home/gateway-lab'):
        raise RuntimeError('This installer is restricted to the disposable local VM')
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--node-archive', required=True)
    parser.add_argument('--node-sha256', required=True)
    parser.add_argument('--state-file', default=None,
                        help='Named QA profile under the local lab; leave current-qa.json unchanged')
    args = parser.parse_args()
    if not re.fullmatch(r'workspace-[a-f0-9]{16}\.tar\.gz', args.archive):
        raise ValueError('Invalid snapshot name')
    if not re.fullmatch(r'node-v[0-9]+\.[0-9]+\.[0-9]+-linux-x64\.tar\.xz', args.node_archive):
        raise ValueError('Invalid Node archive name')
    for checksum in (args.sha256, args.node_sha256):
        if not re.fullmatch('[a-f0-9]{64}', checksum):
            raise ValueError('Invalid checksum')
    root = Path('/home/gateway-lab/lab')
    blobs = root / 'blobs'
    blobs.mkdir(parents=True, exist_ok=True)
    archive = fetch(args.archive, args.sha256, blobs)
    snapshot = root / 'snapshots' / args.sha256[:16]
    snapshot.mkdir(parents=True, exist_ok=True)
    marker = snapshot / 'source-verified.json'
    if not marker.exists():
        with tarfile.open(archive, 'r:gz') as tar:
            if any(not member.name.startswith('workspace/') or not (member.isfile() or member.isdir())
                   for member in tar.getmembers()):
                raise RuntimeError('Unexpected source archive member')
            tar.extractall(snapshot, filter='data')
        marker.write_text(json.dumps({'sha256': args.sha256}), encoding='utf-8')
    workspace = snapshot / 'workspace'
    node_archive = fetch(args.node_archive, args.node_sha256, blobs)
    node_root = root / 'tools'
    node_root.mkdir(exist_ok=True)
    node_dir = node_root / args.node_archive.removesuffix('.tar.xz')
    if not (node_dir / 'bin/node').exists():
        with tarfile.open(node_archive, 'r:xz') as tar:
            tar.extractall(node_root, filter='data')
    requirements = [workspace / 'manager/server/requirements.hashes.txt',
                    workspace / 'tests/linux_lab/requirements-dev.hashes.txt']
    dependency_hash = hashlib.sha256(b''.join(path.read_bytes() for path in requirements)).hexdigest()
    venv = root / 'venvs' / dependency_hash[:16]
    ready = venv / 'qa-ready.json'
    if not ready.exists():
        subprocess.run(['/usr/bin/python3', '-m', 'venv', str(venv)], check=True)
        command = [str(venv / 'bin/python'), '-m', 'pip', 'install', '--disable-pip-version-check',
                   '--no-input', '--only-binary=:all:', '--require-hashes', '--index-url', 'https://pypi.org/simple']
        for requirement in requirements:
            command.extend(['-r', str(requirement)])
        env = {**os.environ, 'PIP_CONFIG_FILE': '/dev/null', 'PIP_EXTRA_INDEX_URL': ''}
        subprocess.run(command, env=env, check=True)
        subprocess.run([str(venv / 'bin/python'), '-m', 'pip', 'check'], check=True)
        ready.write_text(json.dumps({'requirements_sha256': dependency_hash}), encoding='utf-8')
    state = {'workspace': str(workspace), 'source_sha256': args.sha256,
             'python': str(venv / 'bin/python'), 'node_bin': str(node_dir / 'bin'),
             'venv': str(venv), 'host': socket.gethostname(), 'production_connected': False}
    state_file = Path(args.state_file).resolve() if args.state_file else root / 'current-qa.json'
    if not state_file.is_relative_to(root):
        raise RuntimeError('QA state must remain inside the disposable lab')
    payload = json.dumps(state, indent=2)
    if args.state_file and state_file.exists() and state_file.read_text() != payload:
        raise RuntimeError('Refusing to overwrite a different named QA profile')
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(payload, encoding='utf-8')
    print('LOCAL_QA_ENVIRONMENT_READY', json.dumps(state), flush=True)


if __name__ == '__main__':
    main()
