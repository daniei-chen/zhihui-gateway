"""Authenticated encrypted snapshots on an independent, fast-forward Git branch.

Called by the root-owned backup application, never by a public HTTP endpoint.
Only ciphertext and nonsecret format metadata enter the repository.
"""
from __future__ import annotations
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

TARGET = 'git@github-backup:daniei-chen/WorkBuddy-API.git'
BRANCH = 'workbuddy-recovery-encrypted'
CERTIFICATE = Path('/root/backups/workbuddy-recovery/recovery-public.pem')
CONFIG = Path('/root/backups/workbuddy-recovery/offsite.json')
REPOSITORY = Path('/root/backups/workbuddy-offsite.git')
# C-007（2026-10-27 审计）：每日快照已 48.9 MB 且持续增长，原 50 MiB 上限
# 只剩约 3.5 MB 余量 —— 恢复异地备份后很快就会以
# "encrypted snapshot exceeds repository size limit" 失败。
# 提高到 256 MiB 给增长留足空间；这仍是一个硬上限，不是无限制。
MAX_CIPHER_BYTES = 256 * 1024 * 1024

#: 子进程（openssl / git）的超时，秒。
#:
#: 2026-10-26 实测：本机到备份仓库的上行约 **163 KiB/s**，10 MiB 探针 push 用了 63 s，
#: 因此 47 MiB 密文约需 292 s。原值 180 s 会在 push 中途掐断 git，git 返回 128，
#: 被包装成 `backup command git failed (128)` —— 看起来像权限问题，实际是超时。
#: 放宽到 900 s；失败仍然报错，不静默。
#:
#: 注意这只解决"慢"，不解决"大"：`MAX_CIPHER_BYTES` 仍是 50 MiB 硬上限，而当前密文
#: 已约 47.7 MiB，很快会撞线。上限策略（提高上限 / 截断 request_logs 窗口 / 改对象存储）
#: 需要业务方决定，见 docs/EXTERNAL-INPUTS-ONBOARDING.md 的 C3。
RUN_TIMEOUT = 900
MAX_COMMAND_OUTPUT = 1024 * 1024
MIN_FREE_BYTES = 256 * 1024 * 1024
READBACK_METHOD = 'independent-git-fetch-sha256-v1'


def _file_limit(limit):
    """Bound child-created files as well as command capture on Linux."""
    if os.name != 'posix':
        return None
    import resource

    def apply():
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    return apply

def _run(command, *, data=None, cwd=None, accepted=(0,)):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', LC_ALL='C.UTF-8')
    # Git diagnostics and command output are not an unbounded memory buffer.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.run(command, input=data, stdout=output,
                                 stderr=errors, cwd=cwd, env=env, timeout=RUN_TIMEOUT,
                                 preexec_fn=_file_limit(MAX_CIPHER_BYTES + 32 * 1024 * 1024))
        output.seek(0)
        process.stdout = output.read(MAX_COMMAND_OUTPUT + 1)
        if len(process.stdout) > MAX_COMMAND_OUTPUT:
            raise RuntimeError('backup command output exceeds metadata limit')
    if process.returncode not in accepted:
        # Credential helpers may include sensitive diagnostics. Never print them.
        raise RuntimeError(f'backup command {Path(command[0]).name} failed ({process.returncode})')
    return process


def _readback(repository: Path, target: str, commit: str, metadata: dict) -> dict:
    """Fetch from the receiver into an EMPTY object store, then hash its bytes.

    Neither ls-remote nor the producer's local object cache proves retrieval.
    Test/local targets exercise the exact download path but are never offsite
    evidence. The configured production target is the only approved location.
    """
    if shutil.disk_usage(repository.parent).free < metadata['cipher_bytes'] * 2 + MIN_FREE_BYTES:
        raise RuntimeError('insufficient free space for bounded backup readback')
    with tempfile.TemporaryDirectory(prefix='readback-', dir=repository.parent) as temporary:
        root = Path(temporary)
        fresh = root / 'receiver.git'
        _run(['git', 'init', '--bare', str(fresh)])
        command = ['git', '--git-dir', str(fresh)]
        _run(command + ['-c', 'fetch.fsckObjects=true', '-c', 'fetch.unpackLimit=1',
                        'fetch', '--depth=1', '--no-tags',
                        '--quiet', target, 'refs/heads/' + BRANCH])
        if _oid(_run(command + ['rev-parse', 'FETCH_HEAD']).stdout) != commit:
            raise RuntimeError('backup branch changed during independent readback')
        fetched = json.loads(_run(command + ['show', commit + ':snapshot.json']).stdout)
        if fetched != metadata:
            raise RuntimeError('downloaded backup receipt mismatch')
        size = int(_run(command + ['cat-file', '-s', commit + ':snapshot.cms']).stdout)
        if size != metadata['cipher_bytes'] or not 0 < size <= MAX_CIPHER_BYTES:
            raise RuntimeError('downloaded ciphertext size mismatch')
        cipher = root / 'snapshot.cms'
        with cipher.open('xb') as output, tempfile.TemporaryFile() as errors:
            process = subprocess.run(command + ['cat-file', 'blob', commit + ':snapshot.cms'],
                stdout=output, stderr=errors, timeout=RUN_TIMEOUT,
                env=dict(os.environ, GIT_TERMINAL_PROMPT='0'),
                preexec_fn=_file_limit(MAX_CIPHER_BYTES))
        if process.returncode or cipher.stat().st_size != size or _hash(cipher) != metadata['cipher_sha256']:
            raise RuntimeError('downloaded ciphertext digest mismatch')
    return {'method': READBACK_METHOD, 'verified': True, 'verified_at': int(time.time()),
            'commit': commit, 'cipher_sha256': metadata['cipher_sha256'],
            'cipher_bytes': size, 'source_archive_sha256': metadata['source_archive_sha256'],
            'approved_offsite_target': target == TARGET,
            'target_sha256': hashlib.sha256(target.encode()).hexdigest(),
            'restore_verified': False}

def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):digest.update(block)
    return digest.hexdigest()

def _oid(raw):
    value = raw.decode('ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{40}', value):raise RuntimeError('invalid Git object identity')
    return value

def encrypt(snapshot: Path, certificate: Path, output: Path):
    if not snapshot.is_file() or snapshot.is_symlink():raise RuntimeError('invalid snapshot')
    if not certificate.is_file() or certificate.is_symlink():raise RuntimeError('invalid recovery certificate')
    _run(['openssl','cms','-encrypt','-binary','-in',str(snapshot),'-out',str(output),
          '-outform','DER','-aes-256-gcm','-recip',str(certificate),
          '-keyopt','rsa_padding_mode:oaep','-keyopt','rsa_oaep_md:sha256'])
    output.chmod(0o600)
    if output.stat().st_size>MAX_CIPHER_BYTES:raise RuntimeError('encrypted snapshot exceeds repository size limit')

def publish(snapshot: Path, certificate: Path, repository: Path, target=TARGET):
    """The optional target supports isolated tests; production uses the fixed target."""
    snapshot,certificate,repository=map(Path,(snapshot,certificate,repository))
    target = str(target)
    repository.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if repository.is_symlink():raise RuntimeError('backup repository must not be a symlink')
    if not repository.exists():_run(['git','init','--bare',str(repository)])
    repository.chmod(0o700)
    def git(*args, data=None):return _run(['git','--git-dir',str(repository),*args],data=data)
    bare=git('rev-parse','--is-bare-repository').stdout.strip()
    if bare!=b'true':raise RuntimeError('backup repository must be bare')
    remote=_run(['git','ls-remote','--exit-code',target,'refs/heads/'+BRANCH],accepted=(0,2))
    parent=None
    if remote.returncode==0:
        parent=_oid(remote.stdout.split()[0])
        git('fetch','--no-tags',target,'refs/heads/'+BRANCH+':refs/remotes/backup/encrypted')
        if _oid(git('rev-parse','refs/remotes/backup/encrypted').stdout)!=parent:
            raise RuntimeError('backup branch changed during fetch; retry later')
    with tempfile.TemporaryDirectory(prefix='encrypted-',dir=repository.parent) as temporary:
        cipher=Path(temporary)/'snapshot.cms'
        source_hash = _hash(snapshot)
        encrypt(snapshot,certificate,cipher)
        if _hash(snapshot) != source_hash:
            raise RuntimeError('snapshot changed during encryption')
        metadata={'format':'workbuddy-recovery-cms-v1','created_at':int(time.time()),
                  'cipher_sha256':_hash(cipher),'cipher_bytes':cipher.stat().st_size,
                  'source_archive_sha256':source_hash,
                  'certificate_sha256':_hash(certificate),'encryption':'AES-256-GCM / RSA-OAEP-SHA256'}
        blobs={
          'snapshot.cms':_oid(git('hash-object','-w',str(cipher)).stdout),
          'snapshot.json':_oid(git('hash-object','-w','--stdin',data=json.dumps(metadata,sort_keys=True).encode()).stdout),
          'README.md':_oid(git('hash-object','-w','--stdin',data=b'Encrypted WorkBuddy recovery snapshot. Recovery private key is held offline by the operator. No plaintext application data is stored on this branch.\n').stdout),
        }
        tree=_oid(git('mktree',data=''.join(f'100644 blob {digest}\t{name}\n' for name,digest in sorted(blobs.items())).encode()).stdout)
        command=['git','--git-dir',str(repository),'-c','user.name=WorkBuddy Backup',
                 '-c','user.email=backup@localhost','commit-tree',tree]
        if parent:command+=['-p',parent]
        commit=_oid(_run(command,data=b'Encrypted application recovery snapshot\n').stdout)
        # Normal push is intentional: races fail; no force and no main mutation.
        git('push',target,commit+':refs/heads/'+BRANCH)
        remote=_run(['git','ls-remote','--exit-code',target,'refs/heads/'+BRANCH])
        if _oid(remote.stdout.split()[0])!=commit:raise RuntimeError('remote backup verification failed')
        readback = _readback(repository, target, commit, metadata)
        git('update-ref','refs/heads/'+BRANCH,commit)
    return dict(metadata, commit=commit, branch=BRANCH,
                verified_remote=readback['approved_offsite_target'], readback=readback,
                complete_restore_verified=False)

def configured_publish(snapshot: Path):
    configuration=json.loads(CONFIG.read_text())
    if configuration.get('target')!=TARGET or configuration.get('branch')!=BRANCH:
        raise RuntimeError('unexpected production backup target')
    if configuration.get('certificate_sha256')!=_hash(CERTIFICATE):
        raise RuntimeError('recovery certificate identity mismatch')
    return publish(snapshot,CERTIFICATE,REPOSITORY)
