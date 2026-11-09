"""Coordinated immutable console/pool activation under the writer admission fence.

Reuse the frozen frontend publisher's validated trees, signatures and atomic
exchange primitives, but never reacquire its publication mutex or edit Nginx.
Pool updates only add immutable assets. Failure retains history and is retried
from the durable intent; no previous assets or business state are restored.
"""
import hashlib
import json
from pathlib import Path
import shutil

from state_guard import Rejected, _json

FRONTEND_SHA = 'aac69af307f069cfeb618fb9db8c4b6227e13a31cfd5d420dba2aece8a068513'
IDENTITY_SHA = '73249d4695c452950a2bc921c40180cadcece057892b5ff1b11a76341877e339'


class SignedTrees:
    def __init__(self, frontend, layout, ops):
        self.f, self.p, self.ops = frontend, layout, ops

    def tree(self, root, **kwargs):
        for name in ('manifest.sha256', 'manifest.sig'):
            self.f.protected(root / name)
        result = self.ops.run(['/usr/bin/openssl', 'pkeyutl', '-verify', '-pubin', '-inkey',
                               self.p.trust, '-rawin', '-in', root / 'manifest.sha256',
                               '-sigfile', root / 'manifest.sig'])
        if result.returncode:
            raise Rejected('frontend signature rejected')
        return self.f.strict_manifest(root)


class Frontend:
    def __init__(self, frontend, identity, layout, work, journal, authority):
        self.f, self.identity, self.p = frontend, identity, layout
        self.work, self.journal, self.authority = Path(work), journal, authority
        self.intent = self.work / 'frontend.json'
        self.ops = frontend.Operations()
        self.auth = SignedTrees(frontend, layout, self.ops)

    @classmethod
    def production(cls, journal, authority):
        import runpy
        root = Path('/opt/zhihui-release/bin')
        modules = []
        for path, sha in ((Path('/opt/zhihui-release/writer-guard/frontend_library.py'), FRONTEND_SHA),
                          (root / 'frontend_build_identity_v2.py', IDENTITY_SHA)):
            # This helper is loaded only after the installed host closure was
            # checked; keep its existing dependency frozen independently too.
            from writer_entry import protected
            protected(path)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != sha:
                raise Rejected('frozen frontend dependency drift')
            modules.append(type('FrozenModule', (), runpy.run_path(str(path))))
        f, identity = modules
        return cls(f, identity, f.Layout(), journal.root / 'frontend', journal, authority)

    def _owned(self, permit):
        if self.authority.validate(permit) is not None:
            raise Rejected('frontend requires coordinated publication ownership')

    def build(self, candidate):
        out = candidate.root / 'web/out'
        directories = [p.name for p in (out / '_next').iterdir()
                       if p.is_dir() and p.name != 'static' and self.f.BUILD_RE.fullmatch(p.name)]
        if len(directories) != 1:
            raise Rejected('one immutable console build identity required')
        build = directories[0]
        self.identity.check_output(out, build)
        self.auth.tree(out)
        identity_path = Path('/opt/zhihui-release/bin/frontend_build_identity_v2.py')
        if self.p.prefix != Path('/'):
            identity_path = Path(self.identity.__file__)
        self.f.validate_assets(out, build, identity_path=identity_path)
        return build

    def _current(self):
        self.f.protected(self.p.web / 'current.json')
        raw = (self.p.web / 'current.json').read_bytes()
        if len(raw) > 8192:
            raise Rejected('console metadata exceeds bound')
        value = _json(raw, 'console current')
        if (type(value) is not dict or not isinstance(value.get('buildId'), str)
                or not self.f.BUILD_RE.fullmatch(value['buildId'])
                or type(value.get('supportedBuilds', [])) is not list
                or len(value.get('supportedBuilds', [])) > 128
                or any(type(x) is not str or not self.f.BUILD_RE.fullmatch(x)
                       for x in value.get('supportedBuilds', []))):
            raise Rejected('invalid console metadata')
        return raw, value

    def preflight(self, candidate):
        build = self.build(candidate)
        self.f.protected(self.p.current, leaf_link=True)
        self.f.protected(self.p.web / 'out', leaf_link=True)
        if (not self.p.current.is_symlink() or not (self.p.web / 'out').is_symlink()
                or Path((self.p.web / 'out').readlink()) != self.p.current):
            raise Rejected('unexpected console pointer chain')
        current = self.p.current.resolve(strict=True)
        if current.parent != self.p.releases or not self.f.BUILD_RE.fullmatch(current.name):
            raise Rejected('console pointer outside fixed releases')
        self.auth.tree(current)
        _, value = self._current()
        if value.get('buildId') != current.name:
            raise Rejected('console current pointer/metadata mismatch')
        if build == current.name:
            raise Rejected('new console build must not overwrite existing build')
        self.f.protected(self.p.sync)
        if self.p.sync.read_bytes() != self.f.DISABLED_SYNC:
            raise Rejected('unsafe legacy console synchronization enabled')
        self.f.protected(self.p.pool / 'static')
        self.f.protected(self.work.parent)
        if len({p.stat().st_dev for p in (self.work.parent, self.p.releases, self.p.pool / 'static')}) != 1:
            raise Rejected('atomic console/pool publication requires one filesystem')
        for scope in (candidate.root / 'web/out/_next/static').iterdir():
            if not scope.is_dir() or scope.name not in {'chunks', 'css', 'media', build}:
                raise Rejected('unsupported console static scope')
            old = self.p.pool / 'static' / scope.name
            if old.exists():
                previous = self.auth.tree(old)
                for name, item in self.f.tree_files(scope).items():
                    if name in previous and previous[name] != item:
                        raise Rejected('existing immutable static path collision')

    def _load(self):
        value = _json(self.journal._read(self.intent), 'coordinated frontend intent')
        if (type(value) is not dict or set(value) != {'schema', 'candidate', 'build', 'old_build',
                                                     'old_current_sha', 'new_current', 'phase', 'scopes'}
                or value['schema'] != 'zhihui.writer-frontend'
                or value['phase'] not in ('prepared', 'assets', 'pointer', 'complete')
                or not all(type(value[k]) is str and self.f.BUILD_RE.fullmatch(value[k]) for k in ('build', 'old_build'))
                or not all(type(value[k]) is str and self.f.SHA_RE.fullmatch(value[k])
                           for k in ('candidate', 'old_current_sha'))
                or type(value['scopes']) is not dict
                or value['build'] not in value['scopes']
                or set(value['scopes']) - {'chunks', 'css', 'media', value['build']}
                or any(type(x) is not str or not self.f.SHA_RE.fullmatch(x) for x in value['scopes'].values())
                or type(value['new_current']) is not dict
                or value['new_current'].get('buildId') != value['build']
                or value['new_current'].get('writerManifestSha256') != value['candidate']):
            raise Rejected('invalid coordinated frontend intent')
        return value

    def _save(self, value):
        self.journal._atomic(self.intent, (json.dumps(value, sort_keys=True) + '\n').encode())

    def activate(self, permit, record, candidate):
        self._owned(permit)
        build = self.build(candidate)
        if self.intent.exists():
            value = self._load()
            if value['candidate'] != candidate.manifest_sha256 or value['build'] != build:
                raise Rejected('frontend intent candidate mismatch')
        else:
            self.preflight(candidate)
            self.work.mkdir(mode=0o700, exist_ok=True)
            old = self.p.current.resolve(strict=True)
            before, current = self._current()
            updated = dict(current, buildId=build,
                           supportedBuilds=sorted(set(current.get('supportedBuilds', [])) | {old.name, build}))
            # Never attribute a new console to the predecessor's source hash.
            for key in ('sourceManifestSha256', 'sourceCommit', 'sourceKind', 'reconstructedFrom'):
                updated.pop(key, None)
            updated.update(sourceKind='signed-writer-release', writerReleaseId=candidate.release_id,
                           writerManifestSha256=candidate.manifest_sha256)
            # Stage and sign add-only asset unions using the already-installed
            # publisher. Never enter its lock-taking context manager.
            publisher = self.f.Publisher(self.p, self.ops)
            publisher.work, publisher.auth = self.work, self.auth
            scopes = publisher.prepare_pool(candidate.root / 'web/out', build)
            value = {'schema': 'zhihui.writer-frontend', 'candidate': candidate.manifest_sha256,
                     'build': build, 'old_build': old.name,
                     'old_current_sha': hashlib.sha256(before).hexdigest(), 'new_current': updated,
                     'phase': 'prepared', 'scopes': {name: hashlib.sha256((path / 'manifest.sha256').read_bytes()).hexdigest()
                                                      for name, path in scopes.items()}}
            self.journal._atomic(self.work / 'current-before.json', before)
            self._save(value)
        self.f.protected(self.p.current, leaf_link=True)
        if self.p.current.resolve(strict=True) not in tuple(self.p.releases / x for x in (value['old_build'], build)):
            raise Rejected('console pointer drift during coordinated migration')
        before, _ = self._current()
        new = (json.dumps(value['new_current'], sort_keys=True, indent=2) + '\n').encode()
        if hashlib.sha256(before).hexdigest() not in (value['old_current_sha'], hashlib.sha256(new).hexdigest()):
            raise Rejected('console metadata changed outside coordinated migration')
        for name, expected in value['scopes'].items():
            self._owned(permit)
            stage, target = self.work / ('pool-' + name), self.p.pool / 'static' / name
            if target.exists() or target.is_symlink():
                self.auth.tree(target)
            if target.exists() and hashlib.sha256((target / 'manifest.sha256').read_bytes()).hexdigest() == expected:
                self.auth.tree(target)
                continue
            self.auth.tree(stage)
            if hashlib.sha256((stage / 'manifest.sha256').read_bytes()).hexdigest() != expected:
                raise Rejected('staged static union changed')
            if target.exists():
                existing = self.auth.tree(target)
                staged = self.auth.tree(stage)
                if any(staged.get(k) != v for k, v in existing.items()):
                    raise Rejected('static union would delete or change old assets')
                self.ops.exchange(stage, target)
            else:
                stage.rename(target)
                self.f.fsync_dir(target.parent)
        value['phase'] = 'assets'
        self._save(value)
        target = self.p.releases / build
        if not target.exists():
            # Copy to a private staging name; failures do not expose partial HTML.
            staged = self.work / 'console'
            if staged.exists():
                raise Rejected('partial console staging retained; inspect before retry')
            shutil.copytree(candidate.root / 'web/out', staged)
            self.f.normalize_modes(staged)
            self.f.durable_tree(staged)
            self.auth.tree(staged)
            staged.rename(target)
            self.f.fsync_dir(target.parent)
        self._owned(permit)
        if self.auth.tree(target) != self.auth.tree(candidate.root / 'web/out'):
            raise Rejected('installed console differs from signed candidate')
        value['phase'] = 'pointer'
        self._save(value)
        self._owned(permit)
        self.f.atomic_link(self.p.current, target)
        self._owned(permit)
        self.f.atomic_bytes(self.p.web / 'current.json', new)
        self.verify(candidate)
        value['phase'] = 'complete'
        self._save(value)

    def verify(self, candidate):
        build = self.build(candidate)
        target = self.p.releases / build
        self.f.protected(self.p.current, leaf_link=True)
        self.f.protected(self.p.web / 'out', leaf_link=True)
        _, current = self._current()
        if (self.p.current.resolve(strict=True) != target
                or (self.p.web / 'out').readlink() != self.p.current
                or self.auth.tree(target) != self.auth.tree(candidate.root / 'web/out')
                or current.get('buildId') != build
                or current.get('writerManifestSha256') != candidate.manifest_sha256
                or current.get('writerReleaseId') != candidate.release_id):
            raise Rejected('console activation identity mismatch')
        for scope in (candidate.root / 'web/out/_next/static').iterdir():
            installed = self.auth.tree(self.p.pool / 'static' / scope.name)
            if any(installed.get(name) != item for name, item in self.f.tree_files(scope).items()):
                raise Rejected('console assets missing from signed static pool')
