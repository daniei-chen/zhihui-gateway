"""Retire fixed legacy mutation entrypoints without deleting their history.

Root administrators can intentionally execute historical source; this prevents
accidental use of documented CLIs and the old scheduler, not malicious root.
The coordinated adapter uses a separate library with no publication CLI.
"""
import hashlib
import json
import os
from pathlib import Path
import stat

from launch_guard import protected
from state_guard import Rejected, _json

EXPECTED = {
    'opt/zhihui-release/bin/zh-release-zd': '44f4e28cb0202fef0ebff95e561ed521187e4289e7730c617612fe67c97bdae0',
    'opt/zhihui-release/bin/zh-release-zd-v2': '44f4e28cb0202fef0ebff95e561ed521187e4289e7730c617612fe67c97bdae0',
    'opt/zhihui-release/bin/zh-release-zd.ingress-v2': '94aad69f4d72e37751cbe2d8923082d9d16406a2f3a26cc3396a6e15a15f6764',
    'opt/zhihui-release/bin/zh-frontend-release': '4d36343ec9ee8c805031ccb491a8b238ef3fe9bebe02fee475613bbf82ed72b7',
    'opt/zhihui-release/bin/zh-frontend-release-v2': 'b52bcb819eaf9c510e51ed6f82d7177c02d9a22e65ed20dd662a251d8c52d391',
    'opt/zhihui-release/bin/zh-frontend-release-v3': '60836753a6fe48a0c68bce5d68ab75b869d17159b1fb00e36464ee9b14e1b744',
    'opt/zhihui-release/bin/zh-frontend-forward': 'c1884dffca88a39306e4bf39e2087e9640986c302ea86d377f356b07e5246f7d',
    'opt/zhihui-release/bin/zh-frontend-forward-v3': '43d99d0f491960fe3a30d16b4639ab82d507906eac7d6acf0d0d3a780002e74c',
    'opt/zhihui-release/bin/frontend_release.py': '58f8e4509b1b13356fb4e9044adf31b2a09ba8515e61ec04ce5982404475bd23',
    'opt/zhihui-release/bin/frontend_release_v2.py': 'cde94020fc3b84d2da29418a93999de9df241b534e5f46aeaf2557bae34e89c9',
    'opt/zhihui-release/bin/frontend_release_v3.py': 'b5adffe38c021a8849fd39d2484d2cdf428f94472c1fabab1518310551757b21',
    'opt/zhihui-release/bin/frontend_forward.py': 'a17e3dface86ac63259aabfa6b39cb15eeb46b7623211c9f2a202afe2aed635f',
    'opt/zhihui-release/bin/frontend_forward_v3.py': '603df6ee253b7096026b707b0f80a8f2d58ad0bef5736846012332b924dab8dc',
}
BLOCKED = b'''#!/bin/sh
# Retained history: /opt/zhihui-release/writer-state/retired-entrypoints
echo 'Legacy independent publishing is retired. Use the coordinated signed writer migration workflow; never restore business data for code rollback.' >&2
exit 78
'''
PY_BLOCKED = b'''#!/usr/bin/python3
"""Legacy publication disabled; exact originals retained in writer-state."""
raise SystemExit('Legacy independent publishing is retired; use the coordinated signed writer workflow')
'''


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def replacement(name):
    return PY_BLOCKED if name.endswith('.py') else BLOCKED


class Retirement:
    def __init__(self, paths, authority, journal):
        self.paths, self.authority, self.journal = paths, authority, journal
        self.root = paths.floor.parent / 'retired-entrypoints'

    def owned(self, permit):
        if self.authority.validate(permit) is not None:
            raise Rejected('retirement requires coordinated publication ownership')

    def _read(self, path):
        protected(path, self.paths)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 131072
                    or info.st_uid != self.paths.root_uid):
                raise Rejected('unsafe legacy entrypoint')
            raw = handle.read(131073)
            after = os.fstat(handle.fileno())
        linked = path.lstat()
        def identity(value):
            return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns
        if len(raw) > 131072 or identity(info) != identity(after) or identity(info) != identity(linked):
            raise Rejected('legacy entrypoint changed during inspection')
        return raw

    def _intent(self):
        value = _json(self.journal._read(self.root / 'intent.json'), 'retirement intent')
        if value != {'schema': 'zhihui.retired-entrypoints', 'version': 1, 'files': EXPECTED,
                     'replacement_sha256': {name: digest(replacement(name)) for name in EXPECTED}}:
            raise Rejected('legacy retirement intent differs')

    def preflight(self, permit):
        """Read-only validation; a partial retirement must retain exact history."""
        self.owned(permit)
        prepared = self.root.exists() or self.root.is_symlink()
        if prepared:
            self._intent()
        for index, (name, sha) in enumerate(sorted(EXPECTED.items())):
            raw = self._read(self.paths.root / name)
            if prepared:
                if digest(self._read(self.root / (str(index) + '.source'))) != sha:
                    raise Rejected('retired entrypoint history drift')
                if raw == replacement(name):
                    continue
            if digest(raw) != sha:
                raise Rejected('unreviewed legacy publisher drift')

    def verify(self, permit):
        self.owned(permit)
        self._intent()
        for index, (name, sha) in enumerate(sorted(EXPECTED.items())):
            if self._read(self.paths.root / name) != replacement(name):
                raise Rejected('legacy mutation entrypoint remains enabled')
            if (os.name == 'posix' and
                    stat.S_IMODE((self.paths.root / name).stat().st_mode) != 0o750):
                raise Rejected('legacy retirement stub mode differs')
            if digest(self._read(self.root / (str(index) + '.source'))) != sha:
                raise Rejected('retired entrypoint history drift')

    def retire(self, permit):
        self.preflight(permit)
        if self.root.exists() or self.root.is_symlink():
            self._intent()
        else:
            originals = {}
            for name, sha in EXPECTED.items():
                raw = self._read(self.paths.root / name)
                if digest(raw) != sha:
                    raise Rejected('unreviewed legacy publisher drift')
                originals[name] = raw
            protected(self.paths.floor.parent, self.paths, directory=True)
            self.root.mkdir(mode=0o700)
            if os.name == 'posix':
                fd = os.open(self.root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            # Persist every original before changing any callable path. Missing
            # intent after directory creation requires inspection, never reset.
            for index, (name, _) in enumerate(sorted(EXPECTED.items())):
                self.journal._atomic(self.root / (str(index) + '.source'), originals[name])
            self.journal._atomic(self.root / 'intent.json', (json.dumps({
                'schema': 'zhihui.retired-entrypoints', 'version': 1, 'files': EXPECTED,
                'replacement_sha256': {name: digest(replacement(name)) for name in EXPECTED}},
                sort_keys=True) + '\n').encode())
        for index, (name, sha) in enumerate(sorted(EXPECTED.items())):
            self.owned(permit)
            original = self._read(self.root / (str(index) + '.source'))
            if digest(original) != sha:
                raise Rejected('retired original differs')
            path = self.paths.root / name
            raw = self._read(path)
            if raw == replacement(name):
                if os.name == 'posix' and stat.S_IMODE(path.stat().st_mode) != 0o750:
                    os.chmod(path, 0o750)
                    with path.open('r+b') as handle:
                        os.fsync(handle.fileno())
                continue
            if digest(raw) != sha:
                raise Rejected('unrelated legacy entrypoint edit; refusing overwrite')
            self.journal._atomic(path, replacement(name))
            # The private atomic writer creates 0600. Callable stubs have no
            # credentials and retain root-only execution via explicit 0750.
            os.chmod(path, 0o750)
            with path.open('r+b') as handle:
                os.fsync(handle.fileno())
        self.verify(permit)
