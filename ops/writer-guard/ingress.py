"""Bound, recoverable maintenance fence using the existing Nginx barrier code.

The prior container-maintenance journal is never overwritten. This journal owns
only the exact two config snapshots for this writer migration.
"""
import base64
import hashlib
import json
import os
import stat
from pathlib import Path

from state_guard import Rejected, _json


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class Ingress:
    def __init__(self, maintenance, host, path, authority):
        self.m, self.host, self.path, self.authority = maintenance, host, Path(path), authority

    def _authorized(self, permit):
        if self.authority.validate(permit) is not None:
            raise Rejected('ingress mutation requires publication and backup ownership')

    def _save(self, value):
        self.m.atomic(self.path, (json.dumps(value, sort_keys=True, indent=2) + '\n').encode(), 0o600)

    def load(self, permit):
        self._authorized(permit)
        self.m.trusted(self.path)
        witness = self.path.with_suffix('.initialized')
        self.m.trusted(witness)
        if (not stat.S_ISREG(witness.stat().st_mode) or witness.stat().st_nlink != 1
                or witness.stat().st_mode & 0o077 or witness.stat().st_size > 1024
                or witness.read_bytes() != b'zhihui.writer-ingress-v1\n'):
            raise Rejected('invalid ingress initialization witness')
        if (not stat.S_ISREG(self.path.stat().st_mode) or self.path.stat().st_nlink != 1
                or self.path.stat().st_size > 1024 * 1024 or self.path.stat().st_mode & 0o077):
            raise Rejected('private bounded ingress intent required')
        value = _json(self.path.read_bytes(), 'writer ingress')
        if (type(value) is not dict or set(value) != {'schema', 'version', 'files', 'master', 'old_workers', 'phase'}
                or value['schema'] != 'zhihui.writer-ingress' or type(value['version']) is not int
                or value['version'] != 1 or value['phase'] not in
                ('prepared', 'fence-writing', 'fence-reloading', 'fenced',
                 'resume-writing', 'resume-reloading', 'resumed')
                or type(value['files']) is not list or len(value['files']) != 2):
            raise Rejected('invalid writer ingress intent')
        for item, path, rule, count in zip(value['files'], (self.m.PUBLIC, self.m.LOOPBACK),
                                          (self.m.PUBLIC_RULE, self.m.LOCAL_RULE), (2, 1)):
            if (type(item) is not dict or set(item) != {'path', 'mode', 'original', 'fenced', 'original_sha', 'fenced_sha'}
                    or item['path'] != str(path) or type(item['mode']) is not int
                    or item['mode'] & ~0o755 or item['mode'] & 0o022):
                raise Rejected('ingress config binding mismatch')
            original, fenced = (base64.b64decode(item[k], validate=True) for k in ('original', 'fenced'))
            if (digest(original) != item['original_sha'] or digest(fenced) != item['fenced_sha']
                    or self.m.fenced_bytes(original, rule, count) != fenced):
                raise Rejected('ingress snapshot bytes changed')
        identities = [value['master'], *value['old_workers']]
        if any(type(row) is not dict or not {'pid', 'starttime'} <= row.keys()
               or type(row['pid']) is not int or row['pid'] <= 1
               or type(row['starttime']) is not int or row['starttime'] < 0 for row in identities):
            raise Rejected('invalid ingress generation identity')
        return value

    def prepare(self, permit):
        self._authorized(permit)
        if self.path.exists() or self.path.is_symlink():
            return self.load(permit)
        witness = self.path.with_suffix('.initialized')
        if witness.exists() or witness.is_symlink():
            raise Rejected('ingress intent missing after initialization; do not resnapshot')
        self.m.trusted(self.path.parent)
        self.host.upstream()
        # The previous maintenance window must be terminal, with exact original
        # bytes restored. Do not remove/reuse its durable journal.
        if self.m.JOURNAL.exists():
            previous = self.m.load()
            if previous['phase'] not in ('resumed', 'restored-before-cutover'):
                raise Rejected('prior maintenance transaction is unfinished')
            for item in previous['files']:
                if digest(Path(item['path']).read_bytes()) != item['original_sha256']:
                    raise Rejected('prior maintenance config restoration drift')
        master, workers = self.host.workers()
        files = []
        for path, rule, count in ((self.m.PUBLIC, self.m.PUBLIC_RULE, 2),
                                  (self.m.LOOPBACK, self.m.LOCAL_RULE, 1)):
            self.m.trusted(path)
            original = path.read_bytes()
            fenced = self.m.fenced_bytes(original, rule, count)
            files.append({'path': str(path), 'mode': path.stat().st_mode & 0o777,
                          'original': base64.b64encode(original).decode(),
                          'fenced': base64.b64encode(fenced).decode(),
                          'original_sha': digest(original), 'fenced_sha': digest(fenced)})
        value = {'schema': 'zhihui.writer-ingress', 'version': 1, 'files': files,
                 'master': master, 'old_workers': workers, 'phase': 'prepared'}
        fd = os.open(witness, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(b'zhihui.writer-ingress-v1\n')
            handle.flush()
            os.fsync(handle.fileno())
        self._save(value)
        return self.load(permit)

    def _check_files(self, value, variant=None):
        for item in value['files']:
            path = Path(item['path'])
            self.m.trusted(path)
            actual = digest(path.read_bytes())
            allowed = {item[variant + '_sha']} if variant else {item['original_sha'], item['fenced_sha']}
            if actual not in allowed:
                raise Rejected('unrelated Nginx change; refusing to overwrite')

    def apply(self, permit, variant):
        if variant not in ('original', 'fenced'):
            raise Rejected('fixed ingress variant required')
        value = self.load(permit)
        self.host.upstream()
        self._check_files(value)
        prefix = 'resume' if variant == 'original' else 'fence'
        value['phase'] = prefix + '-writing'
        self._save(value)
        for item in value['files']:
            self._authorized(permit)
            self._check_files(value)
            self.m.atomic(Path(item['path']), base64.b64decode(item[variant]), item['mode'])
        self.host.command('nginx_test')
        master, workers = self.host.workers()
        if master != value['master']:
            raise Rejected('Nginx master changed during writer migration')
        known = {(row['pid'], row['starttime']): row for row in value['old_workers'] + workers}
        value['old_workers'] = list(known.values())
        value['phase'] = prefix + '-reloading'
        self._save(value)
        self._authorized(permit)
        self.host.command('nginx_reload')
        self.host.barrier({'nginx_master': value['master'], 'old_workers': value['old_workers']})
        self._check_files(value, variant)
        value['phase'] = 'resumed' if variant == 'original' else 'fenced'
        self._save(value)

    def verify(self, permit, *, fenced):
        value = self.load(permit)
        if value['phase'] != ('fenced' if fenced else 'resumed'):
            raise Rejected('ingress phase not confirmed')
        self._check_files(value, 'fenced' if fenced else 'original')
        self.host.upstream()
        self.host.barrier({'nginx_master': value['master'], 'old_workers': value['old_workers']}, timeout=0)
