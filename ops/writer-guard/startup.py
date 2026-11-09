"""Verify the installed host closure and fixed guarded systemd drop-ins."""
import hashlib
import os
from pathlib import Path
import re
import stat

from launch_guard import protected
from state_guard import Rejected, _json
from systemd_adapter import MAIN, JOB, launch_dropin

SUFFIX = 'zzzzzz-writer-start.conf'


class Startup:
    def __init__(self, paths, authority, gate, systemd):
        self.paths, self.authority, self.gate, self.systemd = paths, authority, gate, systemd
        self.install_root = paths.root / 'opt/zhihui-release/writer-guard'
        self.manifest = self.install_root / 'host-files.json'

    def verify_tools(self):
        protected(self.manifest, self.paths)
        info = self.manifest.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
            raise Rejected('unsafe installed host manifest')
        value = _json(self.manifest.read_bytes(), 'host file closure')
        if (type(value) is not dict or set(value) != {'schema', 'version', 'files'}
                or value['schema'] != 'zhihui.writer-host-files' or type(value['version']) is not int
                or value['version'] != 1 or type(value['files']) is not dict
                or not {'writer_entry.py', 'launch_guard.py', 'state_guard.py', 'startup.py'} <= value['files'].keys()):
            raise Rejected('invalid installed host manifest')
        actual = set()
        for path in self.install_root.iterdir():
            protected(path, self.paths)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Rejected('unexpected installed host entry')
            if path.name != self.manifest.name:
                actual.add(path.name)
        if actual != set(value['files']):
            raise Rejected('installed host file closure differs')
        for name, item in value['files'].items():
            if (not re.fullmatch(r'[a-z][a-z0-9_]*\.py', name) or type(item) is not dict
                    or set(item) != {'sha256', 'mode'} or type(item['mode']) is not int
                    or item['mode'] not in (0o644, 0o755)
                    or not re.fullmatch('[a-f0-9]{64}', str(item['sha256']))):
                raise Rejected('invalid installed host member')
            path = self.install_root / name
            if (hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']
                    or os.name == 'posix' and stat.S_IMODE(path.stat().st_mode) != item['mode']):
                raise Rejected('installed host tool content or mode drift')

    def path(self, unit):
        if unit not in (MAIN, JOB):
            raise Rejected('unsupported guarded startup unit')
        return self.paths.root / 'etc/systemd/system' / (unit + '.d') / SUFFIX

    def install(self, permit):
        if self.authority.validate(permit) is not None:
            raise Rejected('startup installation ownership required')
        self.verify_tools()
        self.gate.verify_blocked(permit, self.systemd.conditions)
        for unit in (MAIN, JOB):
            self.gate._exclusive(self.path(unit), launch_dropin(unit), 0o644)
        self.systemd.command(['daemon-reload'])
        for unit in (MAIN, JOB):
            self.verify(unit)

    def verify(self, unit):
        self.verify_tools()
        path = self.path(unit)
        protected(path, self.paths)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or path.read_bytes() != launch_dropin(unit)
                or os.name == 'posix' and stat.S_IMODE(info.st_mode) != 0o644):
            raise Rejected('installed startup drop-in differs')
        self.systemd.verify_start(unit)
        condition = 'ConditionPathExists=!' + str(self.gate.marker(unit))
        if condition not in self.systemd.conditions(unit):
            raise Rejected('guarded service lost persistent start condition')
