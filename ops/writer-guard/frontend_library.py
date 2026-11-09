#!/usr/bin/python3
"""Root-owned, exact-input frontend publisher. Never imports a candidate app."""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import urllib.parse
import uuid


SAFE_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root',
            'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_FILES = 20000
BUILD_RE = re.compile(r'[A-Za-z0-9_-]{8,64}\Z')
SHA_RE = re.compile(r'[a-f0-9]{64}\Z')
METADATA = {'manifest.sha256', 'manifest.sig'}
ORIGINAL_BUILD = 'OgGJqnHFJqfP4M2FIdziD'
RECONSTRUCTED_FROM = '32858e288964fd44b0baa046e5dff94d0f1ef136'
INJECTION = b"        sub_filter '</head>' '<style>.skip-link{display:none!important}</style></head>';\n"
DISABLED_SYNC = b'''#!/bin/sh
echo 'Unsafe blind-copy/mtime frontend sync retired. Use /opt/zhihui-release/bin/zh-frontend-release with an approved exact-input descriptor.' >&2
exit 78
'''


class Layout:
    def __init__(self, prefix=Path('/')):
        self.prefix = prefix
        self.tools = prefix / 'opt/zhihui-release/bin'
        self.inputs = prefix / 'opt/zhihui-release/frontend-input-v3'
        self.bootstrap = self.tools / 'trusted_bootstrap.py'
        self.manager = prefix / 'opt/workbuddy-manager'
        self.manager_release = self.manager / 'releases/current'
        self.web = self.manager / 'web'
        self.trust = self.manager / 'release-trust-ed25519.pem'
        self.key = prefix / 'opt/zhihui-release/keys/release-priv.pem'
        self.cache = prefix / 'opt/zhihui-release/python-cache'
        self.releases = prefix / 'var/www/zhihui-console-releases'
        self.current = prefix / 'var/www/zhihui-console-current'
        self.pool = prefix / 'var/www/zhihui-next-pool'
        self.nginx = prefix / 'etc/nginx/sites-enabled/zhihui.kaogong.art'
        self.sync = self.tools / 'zh-console-next-sync'
        self.lock = prefix / 'run/zh-release.lock'
        self.saved = prefix / 'root/backups/audit-20261029/frontend-release'
        self.work = prefix / 'var/www/.zh-frontend-transactions'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def load_json(raw):
    def unique(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError('duplicate JSON object key')
            result[name] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def safe_relative(name):
    if not isinstance(name, str):
        raise ValueError('path must be text')
    path = PurePosixPath(name)
    if (not name or len(name) > 512 or path.is_absolute()
            or '..' in path.parts or path.as_posix() != name or '\\' in name
            or any(ord(c) < 33 or ord(c) > 126 for c in name)):
        raise ValueError('unsafe relative path: ' + repr(name))
    return path


def protected(path, leaf_link=False):
    path = path.absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            if item != path or not leaf_link:
                raise ValueError('unexpected symlink: ' + str(item))
        elif info.st_mode & 0o022:
            raise ValueError('writable trust path: ' + str(item))
        if info.st_uid != 0:
            raise ValueError('non-root trust path: ' + str(item))
    if path.is_symlink():
        protected(path.resolve(strict=True))


def protected_mkdir(path, mode=0o700):
    missing = []
    item = path
    while not item.exists() and not item.is_symlink():
        missing.append(item)
        item = item.parent
    protected(item)
    for item in reversed(missing):
        item.mkdir(mode=mode)
    protected(path)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, raw, mode=0o644):
    protected(path.parent)
    if path.exists() or path.is_symlink():
        protected(path)
    temporary = path.with_name(path.name + '.front-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            os.fchmod(handle.fileno(), mode)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_link(path, target):
    protected(path.parent)
    if path.exists() or path.is_symlink():
        protected(path, leaf_link=True)
        if not path.is_symlink():
            raise ValueError('expected pointer symlink')
    temporary = path.with_name(path.name + '.front-' + uuid.uuid4().hex)
    temporary.symlink_to(target)
    os.replace(temporary, path)
    fsync_dir(path.parent)


def tree_files(root, *, signed=False):
    found = {}
    protected(root)
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        safe_relative(relative)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError('unsafe tree entry: ' + relative)
        protected(path)
        if path.is_file() and (not signed or relative not in METADATA):
            if info.st_nlink != 1:
                raise ValueError('hard-linked tree file: ' + relative)
            if info.st_size > MAX_FILE or len(found) >= MAX_FILES:
                raise ValueError('tree footprint limit exceeded')
            found[relative] = {'sha256': file_sha(path), 'size': info.st_size}
    if sum(item['size'] for item in found.values()) > MAX_ARCHIVE:
        raise ValueError('tree total size limit exceeded')
    return found


def normalize_modes(root):
    root.chmod(0o755)
    for path in root.rglob('*'):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('cannot normalize symlink')
        os.chown(path, 0, 0)
        path.chmod(0o755 if path.is_dir() else 0o644)


def durable_tree(root):
    directories = [root]
    for path in root.rglob('*'):
        if path.is_dir():
            directories.append(path)
        elif path.is_file():
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        fsync_dir(directory)


def manifest_bytes(files):
    return ''.join('%s  %d  %s\n' % (item['sha256'], item['size'], name)
                   for name, item in sorted(files.items())).encode()


def strict_manifest(root):
    manifest = root / 'manifest.sha256'
    protected(manifest)
    listed = {}
    for raw in manifest.read_text().splitlines():
        parts = raw.split('  ')
        if len(parts) != 3:
            raise ValueError('bad signed manifest format')
        digest, size, name = parts
        safe_relative(name)
        if not SHA_RE.fullmatch(digest) or not re.fullmatch(r'0|[1-9][0-9]*', size) or name in METADATA or name in listed:
            raise ValueError('invalid signed manifest entry')
        listed[name] = {'sha256': digest, 'size': int(size)}
    actual = tree_files(root, signed=True)
    if not listed or actual != listed:
        raise ValueError('strict signed closure mismatch')
    return listed


def read_map(path):
    protected(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('oversized source/output manifest')
    data = load_json(path.read_text())
    if set(data) != {'schema', 'files'} or data['schema'] != 1 or not isinstance(data['files'], dict):
        raise ValueError('invalid source/output manifest')
    if not 0 < len(data['files']) <= MAX_FILES:
        raise ValueError('source/output manifest count')
    for name, item in data['files'].items():
        safe_relative(name)
        if (not isinstance(item, dict) or set(item) != {'sha256', 'size'}
                or not isinstance(item['sha256'], str) or not SHA_RE.fullmatch(item['sha256'])
                or type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE):
            raise ValueError('invalid source/output file entry')
    return data['files']


def extract_tar(archive, destination, expected):
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError('archive too large')
    destination.mkdir(mode=0o755)
    seen, total, entries = set(), 0, 0
    # Plain tar only: no gzip bomb, PAX/GNU override, link, sparse or device entry.
    with archive.open('rb') as raw:
        offset = 0
        while True:
            header = raw.read(512)
            if len(header) != 512:
                raise ValueError('truncated tar header')
            if header == b'\0' * 512:
                if raw.read(512) != b'\0' * 512 or any(raw.read()):
                    raise ValueError('invalid tar trailer')
                break
            member = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
            if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE):
                raise ValueError('tar links/extensions/special entries forbidden')
            name = member.name[:-1] if member.isdir() and member.name.endswith('/') else member.name
            relative = safe_relative(name)
            if relative.parts[0] != 'out' or name in seen or member.linkname:
                raise ValueError('tar root/duplicate/link forbidden')
            seen.add(name)
            entries += 1
            if entries > MAX_FILES * 2 or member.size > MAX_FILE or member.size < 0:
                raise ValueError('tar count/member size limit')
            if member.isdir() and member.size:
                raise ValueError('directory has tar payload')
            total += member.size
            if total > MAX_ARCHIVE:
                raise ValueError('tar expanded size limit')
            tail = PurePosixPath(*relative.parts[1:])
            path = destination / str(tail)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                if len(relative.parts) == 1 or tail.as_posix() not in expected:
                    raise ValueError('unexpected archive file')
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with path.open('xb') as handle:
                    remaining = member.size
                    while remaining:
                        data = raw.read(min(1024 * 1024, remaining))
                        if not data:
                            raise ValueError('truncated tar payload')
                        handle.write(data)
                        remaining -= len(data)
                path.chmod(0o644)
            padding = (-member.size) % 512
            if len(raw.read(padding)) != padding:
                raise ValueError('truncated tar padding')
            offset += 512 + member.size + padding
    normalize_modes(destination)
    if tree_files(destination) != expected:
        raise ValueError('extracted output hash/closure mismatch')


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value and (name == 'src' or tag == 'link' and name == 'href'):
                self.urls.append(value)


def validate_assets(root, build, *, identity_path=None):
    identity_path = Path(identity_path) if identity_path is not None else Path(__file__).resolve().with_name('frontend_build_identity_v2.py')
    protected(identity_path)
    identity = runpy.run_path(str(identity_path))
    identity['check_output'](root, build)
    checked = 0
    for path in root.rglob('*'):
        if not path.is_file() or path.suffix not in ('.html', '.css', '.js', '.txt'):
            continue
        text = path.read_text(encoding='utf-8')
        urls = []
        if path.suffix == '.html':
            parser = References()
            parser.feed(text)
            urls += parser.urls
        if path.suffix == '.css':
            urls += re.findall(r'url\(\s*[\'"]?([^\s\)\'\"]+)', text)
        urls += re.findall(r'(?<![A-Za-z0-9_])(/_next/static/[^\s\'"<>\\]+?\.(?:js|css|woff2?|png|svg|webp|jpg|gif|json))', text)
        for url in urls:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme or parsed.netloc or url.startswith(('data:', '#')):
                continue
            name = urllib.parse.unquote(parsed.path)
            if name.startswith('/_next/'):
                target = root / name.lstrip('/')
            elif path.suffix == '.css' and not name.startswith('/'):
                target = path.parent / name
            else:
                continue  # Existing favicon/home aliases are outside this release.
            target = target.resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                raise ValueError('missing/escaping asset in %s: %s' % (path.relative_to(root), url))
            checked += 1
    return checked


class Operations:
    def run(self, args, *, check=True):
        return subprocess.run([str(arg) for arg in args], env=SAFE_ENV, check=check,
                              capture_output=True, text=True, timeout=120)

    def sign(self, key, manifest, signature):
        self.run(['/usr/bin/openssl', 'pkeyutl', '-sign', '-inkey', key,
                  '-rawin', '-in', manifest, '-out', signature])

    def exchange(self, first, second):
        # Same-filesystem atomic directory exchange; no fallback with a gap.
        libc = ctypes.CDLL(None, use_errno=True)
        call = libc.renameat2
        call.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        if call(-100, os.fsencode(first), -100, os.fsencode(second), 2) != 0:
            raise OSError(ctypes.get_errno(), 'renameat2 exchange failed')
        fsync_dir(first.parent)
        fsync_dir(second.parent)

    def nginx_reload(self):
        self.run(['/usr/sbin/nginx', '-t'])
        self.run(['/usr/bin/systemctl', 'reload', 'nginx'])

    def full_preflight(self, publisher):
        p = publisher.p
        self.run(['/usr/bin/python3', '-I', '-B', '-S', '-X',
                  'pycache_prefix=' + str(p.cache), p.bootstrap,
                  '--root', p.manager_release, '--web'])


class Authority:
    def __init__(self, layout):
        self.p = layout
        protected(layout.bootstrap)
        protected(layout.manager_release, leaf_link=True)
        protected(layout.trust)
        boot = runpy.run_path(str(layout.bootstrap))
        self.root = layout.manager_release.resolve(strict=True)
        # Verify every release byte including all three checkers BEFORE import.
        boot['verify_release'](self.root, layout.trust)
        source = runpy.run_path(str(self.root / 'deploy/source_preflight.py'))
        source['check'].__globals__['TRUST'] = layout.trust
        source['check'](self.root)
        self.web = runpy.run_path(str(self.root / 'deploy/web_preflight.py'))
        self.web['check'].__globals__['TRUST'] = layout.trust
        self.pool = runpy.run_path(str(self.root / 'deploy/web_pool_preflight.py'))

    def tree(self, path, *, strict=True):
        ok, detail = self.pool['verify_tree'](path, self.p.trust)
        if not ok:
            raise ValueError('signed tree rejected: %s: %s' % (path.name, detail))
        if strict:
            strict_manifest(path)

    def console(self, path, *, strict=True):
        self.web['check'](path, quiet=True)
        self.tree(path, strict=strict)


class Publisher:
    def __init__(self, layout=None, operations=None):
        self.p = layout or Layout()
        self.ops = operations or Operations()
        self.fd = None
        self.auth = None
        self.journal = None
        self.tx = None
        self.work = None

    def __enter__(self):
        if os.geteuid() != 0:
            raise PermissionError('root required')
        os.umask(0o077)
        protected(self.p.lock.parent)
        self.fd = os.open(self.p.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(self.fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
            os.close(self.fd)
            raise ValueError('unsafe release lock')
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def record(self, phase, **fields):
        self.journal.update(fields, phase=phase, updatedAt=time.time())
        atomic_bytes(self.tx / 'journal.json', json.dumps(self.journal, indent=2).encode() + b'\n', 0o600)

    def initial(self):
        for path in (self.p.web, self.p.releases, self.p.pool, self.p.pool / 'static',
                     self.p.nginx, self.p.sync, self.p.cache, self.p.web / 'current.json'):
            protected(path)
        protected(self.p.current, leaf_link=True)
        protected(self.p.web / 'out', leaf_link=True)
        if not self.p.current.is_symlink() or not (self.p.web / 'out').is_symlink():
            raise ValueError('expected serving pointer chain')
        if Path(os.readlink(self.p.web / 'out')) != self.p.current:
            raise ValueError('unexpected web/out pointer')
        current = self.p.current.resolve(strict=True)
        if current.parent != self.p.releases or not BUILD_RE.fullmatch(current.name):
            raise ValueError('unexpected current console target')
        data = load_json((self.p.web / 'current.json').read_text())
        if data.get('buildId') != current.name:
            raise ValueError('current pointer/json mismatch')
        supported = data.get('supportedBuilds', [current.name])
        if not isinstance(supported, list) or not all(isinstance(x, str) and BUILD_RE.fullmatch(x) for x in supported):
            raise ValueError('invalid supported build list')
        self.auth = Authority(self.p)
        self.ops.full_preflight(self)
        self.auth.console(current, strict=False)  # Legacy BUILD_INFO can be unsigned.
        return current, data

    def inputs(self, approved):
        if not SHA_RE.fullmatch(approved):
            raise ValueError('invalid approved input hash')
        descriptor = self.p.inputs / 'release-input.json'
        protected(descriptor)
        if descriptor.stat().st_size > 16384 or file_sha(descriptor) != approved:
            raise ValueError('unapproved release input descriptor')
        data = load_json(descriptor.read_text())
        keys = {'schema', 'newBuildId', 'previousBuildId', 'sourceManifestSha256',
                'outManifestSha256', 'archiveSha256', 'sourceKind', 'reconstructedFrom',
                'previousCurrentJsonSha256', 'previousNginxSha256'}
        if (set(data) != keys or data['schema'] != 3 or data['sourceKind'] != 'reconstructed-source'
                or data['reconstructedFrom'] != RECONSTRUCTED_FROM
                or not all(isinstance(data[x], str) and BUILD_RE.fullmatch(data[x]) for x in ('newBuildId', 'previousBuildId'))
                or data['newBuildId'] in {ORIGINAL_BUILD, data['previousBuildId']}
                or not all(isinstance(data[x], str) and SHA_RE.fullmatch(data[x])
                           for x in ('previousCurrentJsonSha256', 'previousNginxSha256'))):
            raise ValueError('unsupported frontend release descriptor')
        for filename, field in (('console-out.tar', 'archiveSha256'),
                                ('source-files.json', 'sourceManifestSha256'), ('out-files.json', 'outManifestSha256')):
            path = self.p.inputs / filename
            protected(path)
            if path.stat().st_size > (MAX_ARCHIVE if filename.endswith('.tar') else 16 * 1024 * 1024):
                raise ValueError('input file exceeds fixed size limit: ' + filename)
            if not isinstance(data[field], str) or not SHA_RE.fullmatch(data[field]) or file_sha(path) != data[field]:
                raise ValueError('input file digest mismatch: ' + filename)
        source = read_map(self.p.inputs / 'source-files.json')
        output = read_map(self.p.inputs / 'out-files.json')
        if any(name in METADATA | {'BUILD_INFO.json'} for name in output):
            raise ValueError('input must not supply publisher-owned signature/provenance metadata')
        if not source or sum(item['size'] for item in output.values()) > MAX_ARCHIVE:
            raise ValueError('invalid source/output footprint')
        descriptor_after = file_sha(descriptor)
        if descriptor_after != approved:
            raise ValueError('approved descriptor changed while reading')
        return data, output

    def signed(self, root):
        protected(self.p.key)
        if stat.S_IMODE(self.p.key.stat().st_mode) != 0o600:
            raise ValueError('signing key requires mode 0600')
        normalize_modes(root)
        atomic_bytes(root / 'manifest.sha256', manifest_bytes(tree_files(root, signed=True)))
        signature = root / 'manifest.sig'
        if signature.exists():
            signature.unlink()
        self.ops.sign(self.p.key, root / 'manifest.sha256', signature)
        signature.chmod(0o644)
        self.auth.tree(root)
        durable_tree(root)

    def prepare(self, approved, *, validation_only=False):
        current, old_json = self.initial()
        data, expected = self.inputs(approved)
        if current.name != data['previousBuildId']:
            raise ValueError('current build differs from approved predecessor')
        if file_sha(self.p.web / 'current.json') != data['previousCurrentJsonSha256']:
            raise ValueError('current.json differs from approved predecessor')
        if file_sha(self.p.nginx) != data['previousNginxSha256']:
            raise ValueError('nginx differs from approved predecessor')
        build = data['newBuildId']
        target = self.p.releases / build
        if target.exists() or target.is_symlink():
            raise ValueError('new console destination already exists')
        transaction = build + '-' + (uuid.uuid4().hex[:16] if validation_only else approved[:16])
        protected_mkdir(self.p.saved)
        protected_mkdir(self.p.work)
        self.tx, self.work = self.p.saved / transaction, self.p.work / transaction
        if self.tx.exists() or self.work.exists():
            raise ValueError('transaction already exists; inspect/rollback instead of overwrite')
        self.tx.mkdir(mode=0o700)
        self.work.mkdir(mode=0o700)
        nginx = self.p.nginx.read_bytes()
        if current.name == ORIGINAL_BUILD:
            if nginx.count(INJECTION) != 1:
                raise ValueError('exact legacy skip-link injection must occur once')
            new_nginx = nginx.replace(INJECTION, b'', 1)
        else:
            if INJECTION in nginx or self.p.sync.read_bytes() != DISABLED_SYNC:
                raise ValueError('nonlegacy predecessor must retain the safe serving policy')
            new_nginx = nginx
        for name, raw in (('nginx-before', nginx), ('current-before.json', (self.p.web / 'current.json').read_bytes()),
                          ('sync-before', self.p.sync.read_bytes()), ('nginx-after', new_nginx)):
            atomic_bytes(self.tx / name, raw, 0o600)
        self.journal = {'schema': 1, 'transaction': transaction, 'approvedInputSha256': approved,
                        'oldBuild': current.name, 'newBuild': build, 'oldPointer': os.readlink(self.p.current),
                        'beforeSha': {name: file_sha(self.tx / name) for name in ('nginx-before', 'current-before.json', 'sync-before')},
                        'nginxAfterSha': sha(new_nginx), 'poolCommitted': [], 'pointerChanged': False,
                        'productionTouched': False}
        self.journal['validationOnly'] = validation_only
        self.journal['publisherVersion'] = 3
        self.record('preparing')
        out = self.work / 'console'
        extract_tar(self.p.inputs / 'console-out.tar', out, expected)
        references = validate_assets(out, build)
        info = {'buildId': build, 'sourceKind': 'reconstructed-source',
                'reconstructedFrom': RECONSTRUCTED_FROM,
                'sourceCommit': None, 'originalCommitReproduced': False,
                'sourceManifestSha256': data['sourceManifestSha256'],
                'outManifestSha256': data['outManifestSha256'], 'archiveSha256': data['archiveSha256'],
                'publishedBy': 'trusted-frontend-release-v3', 'assetReferencesChecked': references}
        atomic_bytes(out / 'BUILD_INFO.json', json.dumps(info, indent=2).encode() + b'\n')
        self.signed(out)
        self.auth.console(out)
        pool_stages = self.prepare_pool(out, build)
        used = sum(path.stat().st_size for path in self.work.rglob('*') if path.is_file())
        if shutil.disk_usage(self.p.releases).free < max(64 * 1024 * 1024, used):
            raise ValueError('insufficient free space for durable publish/rollback headroom')
        supported = sorted(set(old_json.get('supportedBuilds', [])) | {current.name, build})
        new_json = dict(old_json, buildId=build, supportedBuilds=supported,
                        sourceKind='reconstructed-source', sourceManifestSha256=data['sourceManifestSha256'])
        raw = json.dumps(new_json, indent=2).encode() + b'\n'
        atomic_bytes(self.tx / 'current-after.json', raw, 0o600)
        self.record('prepared', currentAfterSha=sha(raw), stagedScopes=list(pool_stages))
        return out, pool_stages, new_nginx, raw

    def prepare_pool(self, out, build):
        source = out / '_next/static'
        if not source.is_dir():
            raise ValueError('static output missing')
        scopes = {}
        for directory in sorted(source.iterdir()):
            if not directory.is_dir() or directory.is_symlink() or directory.name not in {'chunks', 'css', 'media', build}:
                raise ValueError('unexpected static scope: ' + directory.name)
            new_files = tree_files(directory)
            if not new_files or any(name in METADATA for name in new_files):
                raise ValueError('invalid unsigned static scope')
            existing = self.p.pool / 'static' / directory.name
            stage = self.work / ('pool-' + directory.name)
            if existing.exists() or existing.is_symlink():
                protected(existing)
                self.auth.tree(existing)
                old_files = tree_files(existing, signed=True)
                for name, meta in new_files.items():
                    if name in old_files and old_files[name] != meta:
                        raise ValueError('pool path content collision: %s/%s' % (directory.name, name))
                shutil.copytree(existing, stage)
            else:
                stage.mkdir(mode=0o755)
            for name in new_files:
                target = stage / name
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    shutil.copyfile(directory / name, target)
            self.signed(stage)
            scopes[directory.name] = stage
        if build not in scopes:
            raise ValueError('new build scope absent')
        expected = tree_files(out / '_next')
        for name in expected:
            parts = PurePosixPath(name).parts
            if len(parts) < 3 or parts[0] != 'static' or parts[1] not in scopes:
                raise ValueError('output has unhandled _next asset outside signed static scopes: ' + name)
        return scopes

    def apply(self, approved, *, prepare_only=False):
        try:
            out, scopes, nginx, current_json = self.prepare(approved, validation_only=prepare_only)
            if prepare_only:
                self.record('validated-not-published')
                return
            self.record('retiring-unsafe-sync', productionTouched=True)
            atomic_bytes(self.p.sync, DISABLED_SYNC, 0o750)
            self.record('committing-pool')
            for name, stage in scopes.items():
                target = self.p.pool / 'static' / name
                self.record('committing-pool', nextScope=name)
                if target.exists():
                    self.ops.exchange(stage, target)
                else:
                    os.rename(stage, target)
                    fsync_dir(target.parent)
                self.auth.tree(target)
                self.journal['poolCommitted'].append(name)
                self.record('committing-pool')
            target = self.p.releases / self.journal['newBuild']
            os.rename(out, target)
            fsync_dir(target.parent)
            self.auth.console(target)
            self.record('switching-pointer')
            atomic_link(self.p.current, target)
            self.record('switching-json', pointerChanged=True)
            atomic_bytes(self.p.web / 'current.json', current_json)
            atomic_bytes(self.p.nginx, nginx)
            self.record('reloading-nginx')
            self.ops.nginx_reload()
            self.ops.full_preflight(self)
            self.auth.console(target)
            self.record('published', poolPreservesOldFiles=True, mutableApplicationStateUntouched=True)
        except BaseException as error:
            if self.journal is None:
                raise
            if not self.journal.get('productionTouched'):
                self.record('rejected-before-publication', error=type(error).__name__ + ': ' + str(error))
                raise
            self.record('publication-failed', error=type(error).__name__ + ': ' + str(error))
            try:
                self.rollback(self.journal['transaction'])
            except BaseException as rollback_error:
                self.record('rollback-failed', rollbackError=type(rollback_error).__name__ + ': ' + str(rollback_error))
                raise RuntimeError('frontend publication and rollback failed; inspect private journal; no state or old assets were deleted') from rollback_error
            raise RuntimeError('frontend publication failed; previous console/json/nginx verified restored') from error

    def load(self, transaction):
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,64}-[a-f0-9]{16}', transaction):
            raise ValueError('invalid transaction identifier')
        self.tx = self.p.saved / transaction
        protected(self.tx / 'journal.json')
        self.journal = load_json((self.tx / 'journal.json').read_text())
        if self.journal.get('schema') != 1 or self.journal.get('transaction') != transaction:
            raise ValueError('invalid private transaction journal')
        if self.journal.get('publisherVersion') != 3:
            raise ValueError('v3 does not manage another publisher version transaction')
        for name, expected in self.journal['beforeSha'].items():
            if name not in {'nginx-before', 'current-before.json', 'sync-before'}:
                raise ValueError('unknown backup item')
            protected(self.tx / name)
            if file_sha(self.tx / name) != expected:
                raise ValueError('private backup changed')
        self.auth = Authority(self.p)

    def rollback(self, transaction):
        self.load(transaction)
        old, new = self.journal['oldBuild'], self.journal['newBuild']
        if not BUILD_RE.fullmatch(old) or not BUILD_RE.fullmatch(new):
            raise ValueError('invalid journal build identity')
        target = self.p.releases / old
        self.auth.console(target, strict=False)
        protected(self.p.current, leaf_link=True)
        if not self.p.current.is_symlink() or self.p.current.resolve() not in {target, self.p.releases / new}:
            raise ValueError('refusing to overwrite an unrelated later console publication')
        protected(self.p.nginx)
        if file_sha(self.p.nginx) not in {self.journal['beforeSha']['nginx-before'], self.journal['nginxAfterSha']}:
            raise ValueError('refusing to undo unrelated nginx edits')
        protected(self.p.web / 'current.json')
        if file_sha(self.p.web / 'current.json') not in {
                self.journal['beforeSha']['current-before.json'], self.journal.get('currentAfterSha')}:
            raise ValueError('refusing to undo unrelated current.json state')
        self.record('rolling-back')
        atomic_link(self.p.current, target)
        atomic_bytes(self.p.web / 'current.json', (self.tx / 'current-before.json').read_bytes())
        atomic_bytes(self.p.nginx, (self.tx / 'nginx-before').read_bytes())
        self.ops.nginx_reload()
        self.ops.full_preflight(self)
        self.auth.console(target, strict=False)
        self.record('rolled-back', restoredBuild=old, poolAdditionsRetained=True,
                    mutableApplicationStateUntouched=True)


def main():
    raise SystemExit('Independent frontend publication is retired; use the coordinated writer workflow')


if __name__ == '__main__':
    main()
