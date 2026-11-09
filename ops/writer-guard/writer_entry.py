#!/usr/bin/python3
"""Fixed host entrypoint; install only with the reviewed migration controller.

Invoke -I -S -B with the protected pycache prefix. No release-owned code is
imported before floor admission. This file never initializes or advances state.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import stat
import subprocess
import sys

INSTALL = Path('/opt/zhihui-release/writer-guard')
BOOTSTRAP = Path('/opt/zhihui-release/bin/trusted_bootstrap.py')
TRUST = Path('/opt/workbuddy-manager/release-trust-ed25519.pem')
VENV = Path('/opt/workbuddy-manager/venv')
CACHE = Path('/opt/zhihui-release/python-cache')


class EntryRejected(RuntimeError):
    pass


def protected(path, *, links=False):
    path = Path(path).absolute()
    targets = (path, path.resolve(strict=True)) if links else (path,)
    for target in targets:
        for item in (target, *target.parents):
            info = item.lstat()
            if (info.st_uid != 0 or not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022
                    or not links and stat.S_ISLNK(info.st_mode)):
                raise EntryRejected('unsafe host execution path')


def load_host_module(name):
    if name not in ('state_guard', 'launch_guard'):
        raise EntryRejected('unrecognized host module')
    path = INSTALL / (name + '.py')
    protected(path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify_host_closure():
    marker = INSTALL / 'host-files.json'
    protected(marker)
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
        raise EntryRejected('unsafe installed host manifest')
    def unique(pairs):
        value = {}
        for name, item in pairs:
            if name in value:
                raise EntryRejected('duplicate installed host manifest key')
            value[name] = item
        return value
    value = json.loads(marker.read_bytes(), object_pairs_hook=unique)
    if (type(value) is not dict or set(value) != {'schema', 'version', 'files'}
            or value['schema'] != 'zhihui.writer-host-files' or type(value['version']) is not int
            or value['version'] != 1 or type(value['files']) is not dict
            or not {'writer_entry.py', 'state_guard.py', 'launch_guard.py'} <= value['files'].keys()):
        raise EntryRejected('invalid installed host manifest')
    if {p.name for p in INSTALL.iterdir()} != set(value['files']) | {marker.name}:
        raise EntryRejected('installed host closure differs')
    for name, item in value['files'].items():
        if (type(name) is not str or not re.fullmatch(r'[a-z][a-z0-9_]*\.py', name)
                or type(item) is not dict or set(item) != {'sha256', 'mode'}
                or type(item['mode']) is not int or item['mode'] not in (0o644, 0o755)
                or type(item['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', item['sha256'])):
            raise EntryRejected('invalid installed host manifest member')
        path = INSTALL / name
        protected(path)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != item['mode']
                or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']):
            raise EntryRejected('installed host code drift')


class HostReleaseVerifier:
    def __init__(self, *, inspect_legacy=False):
        if type(inspect_legacy) is not bool:
            raise EntryRejected('explicit inspection policy required')
        self.inspect_legacy = inspect_legacy
        protected(BOOTSTRAP)
        self.bootstrap = runpy.run_path(str(BOOTSTRAP))

    @staticmethod
    def _regular(path, *, executable=False):
        protected(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or executable and not info.st_mode & 0o111:
            raise EntryRejected('unsafe release member')

    def _source_closure(self, root, manifest):
        actual = set()
        excluded = {'deploy/managed-release.json', 'deploy/managed-release.sig'}
        for name in ('server', 'deploy'):
            base = root / name
            protected(base)
            if not base.is_dir():
                raise EntryRejected('missing source closure')
            for path in base.rglob('*'):
                protected(path)
                if path.is_dir():
                    if path.name == '__pycache__':
                        raise EntryRejected('release contains executable bytecode cache')
                    continue
                self._regular(path)
                if path.suffix in ('.pyc', '.pyo'):
                    raise EntryRejected('release contains executable bytecode cache')
                relative = path.relative_to(root).as_posix()
                if relative not in excluded:
                    actual.add(relative)
        expected = {p for p in manifest['files'] if p.startswith(('server/', 'deploy/'))}
        if actual != expected:
            raise EntryRejected('source closure differs from signed release')

    def _web_closure(self, root):
        base = root / 'web/out'
        protected(base)
        manifest, signature = base / 'manifest.sha256', base / 'manifest.sig'
        for path in (manifest, signature, TRUST):
            self._regular(path)
        before = manifest.read_bytes()
        result = subprocess.run(['/usr/bin/openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(TRUST),
                                 '-rawin', '-in', str(manifest), '-sigfile', str(signature)],
                                capture_output=True, timeout=30,
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        if result.returncode or manifest.read_bytes() != before:
            raise EntryRejected('web signature failed or manifest drifted')
        expected = set()
        for line in before.decode('utf-8').splitlines():
            parts = line.split('  ')
            if len(parts) != 3:
                raise EntryRejected('invalid signed web manifest')
            digest, size, relative = parts
            p = PurePosixPath(relative)
            if (not re.fullmatch('[0-9a-f]{64}', digest) or not re.fullmatch('[0-9]+', size)
                    or not p.parts or p.is_absolute() or '..' in p.parts or '\\' in relative
                    or p.as_posix() != relative or relative in expected
                    or relative in ('manifest.sha256', 'manifest.sig')):
                raise EntryRejected('invalid signed web member')
            path = base / relative
            self._regular(path)
            raw = path.read_bytes()
            if len(raw) != int(size) or hashlib.sha256(raw).hexdigest() != digest:
                raise EntryRejected('signed web content mismatch')
            expected.add(relative)
        if not {'index.html', 'login/index.html'} <= expected:
            raise EntryRejected('required signed web entries missing')
        actual = set()
        for path in base.rglob('*'):
            protected(path)
            if not path.is_dir():
                self._regular(path)
                actual.add(path.relative_to(base).as_posix())
        if actual - {'manifest.sha256', 'manifest.sig'} != expected:
            raise EntryRejected('web closure differs from signed manifest')

    @staticmethod
    def _runtime():
        for path in (Path('/usr/bin/setpriv'), Path('/usr/bin/openssl'), VENV):
            protected(path, links=True)
        for path in VENV.rglob('*'):
            protected(path, links=True)
        python = VENV / 'bin/python'
        protected(python, links=True)
        if not python.is_file() or not os.access(python, os.X_OK):
            raise EntryRejected('fixed Python runtime missing')
        protected(CACHE)

    def verify_release(self, root):
        manifest = self.bootstrap['verify_release'](root)
        self._source_closure(root, manifest)
        # The controller must inspect a signed legacy predecessor to initialize
        # its floor. This is inspection only, never launch permission.
        if not self.inspect_legacy or 'server/writer-capabilities.json' in manifest['files']:
            self._web_closure(root)
        self._runtime()
        return manifest


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (['main'], ['helper'], ['reconcile']):
        raise EntryRejected('only fixed main/helper/reconcile profiles are accepted')
    if (os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.no_site
            or not sys.dont_write_bytecode or Path(sys.pycache_prefix or '') != CACHE
            or Path(__file__).absolute() != INSTALL / 'writer_entry.py'):
        raise EntryRejected('isolated fixed host entrypoint required')
    protected(Path(__file__))
    verify_host_closure()
    state = load_host_module('state_guard')
    launch = load_host_module('launch_guard')
    source = dict(os.environ)
    # Reject injection before even the external verifier launches openssl.
    dummy = state.VerifiedRelease(Path('/opt/workbuddy-manager/releases/unresolved'),
                                  '', 'unresolved', state.Capabilities(), object())
    launch.writer_environment(source, dummy, helper=argv == ['helper'], reconcile=argv == ['reconcile'])
    os.environ.clear()
    os.environ.update({'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'})
    paths = state.Paths.production()
    lock = launch.RuntimeLock(paths)
    guard = state.Guard(paths, HostReleaseVerifier(), lock)
    launcher = launch.Launcher(guard, lock)
    with launcher.admitted() as (release, lease):
        launcher.execute(release, lease, source, helper=argv == ['helper'], reconcile=argv == ['reconcile'])


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never include environment values, user documents or provider tokens.
        print('WRITER-LAUNCH-BLOCKED: host release/state/runtime contract rejected', file=sys.stderr)
        raise SystemExit(1)
