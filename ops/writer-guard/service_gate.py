"""Fail-closed persistent systemd start gates; installation is an explicit step.

Runtime masking does not override an existing /etc/systemd/system unit. Use a
fixed negative ConditionPathExists drop-in plus root-owned marker instead.
Markers survive reboot; release them only after verified guarded ExecStart.
"""
from pathlib import Path
import os
import stat
import subprocess

from launch_guard import protected
from state_guard import Rejected

UNITS = ('workbuddy-web.service', 'workbuddy-web-helper.service',
         'wb-budget-autoreconcile.service')
SUFFIX = 'zzzzz-writer-migration-gate.conf'


def gate_bytes(marker):
    return ('[Unit]\nConditionPathExists=!' + str(marker) + '\n').encode()


class ServiceGate:
    def __init__(self, paths, authority, command=None, *, include_helper=True):
        self.paths, self.authority = paths, authority
        self.command = command or self._systemctl
        self.state = paths.floor.parent
        self.units = (UNITS if include_helper else
                      ('workbuddy-web.service', 'wb-budget-autoreconcile.service'))

    def marker(self, unit):
        if unit not in self.units:
            raise Rejected('unknown writer gate unit')
        return self.state / (unit + '.start-blocked')

    def dropin(self, unit):
        self.marker(unit)
        return self.paths.root / 'etc/systemd/system' / (unit + '.d') / SUFFIX

    @staticmethod
    def _systemctl(argv):
        allowed = (['daemon-reload'], ['stop', 'wb-budget-autoreconcile.timer'],
                   ['stop', 'workbuddy-web.service'], ['stop', 'workbuddy-web-helper.service'],
                   ['stop', 'wb-budget-autoreconcile.service'])
        if argv not in allowed:
            raise Rejected('unsupported gate service operation')
        result = subprocess.run(['/usr/bin/systemctl', *argv], capture_output=True, timeout=100,
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode:
            raise Rejected('writer gate service operation failed')

    def _authorized(self, permit):
        if self.authority.validate(permit) is not None:
            raise Rejected('writer gate ownership required')

    def _sync(self, parent):
        if os.name == 'posix':
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _exclusive(self, path, raw, mode):
        protected(path.parent, self.paths, directory=True)
        if path.exists() or path.is_symlink():
            protected(path, self.paths)
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.read_bytes() != raw
                    or os.name == 'posix' and stat.S_IMODE(info.st_mode) != mode):
                raise Rejected('writer gate conflicts with existing path')
            return
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
        with os.fdopen(fd, 'wb') as handle:
            if os.name == 'posix':
                os.fchmod(handle.fileno(), mode)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        self._sync(path.parent)

    def install(self, permit):
        self._authorized(permit)
        protected(self.paths.root / 'etc/systemd/system', self.paths, directory=True)
        protected(self.state, self.paths, directory=True)
        for unit in self.units:
            path = self.dropin(unit)
            if not path.parent.exists():
                path.parent.mkdir(mode=0o755)
                self._sync(path.parent.parent)
            self._exclusive(path, gate_bytes(self.marker(unit)), 0o644)
        self.command(['daemon-reload'])

    def block(self, permit):
        self._authorized(permit)
        for unit in self.units:
            path = self.dropin(unit)
            protected(path, self.paths)
            if path.read_bytes() != gate_bytes(self.marker(unit)):
                raise Rejected('gate must be installed before stopping writers')
            self._exclusive(self.marker(unit), b'zhihui.writer-start-blocked\n', 0o600)
        # Marker persistence precedes stopping the timer/processes. A crash
        # must never permit the old raw ExecStart to restart on reboot.
        self.command(['daemon-reload'])
        self.command(['stop', 'wb-budget-autoreconcile.timer'])

    def verify_blocked(self, permit, conditions):
        self._authorized(permit)
        for unit in self.units:
            path, marker = self.dropin(unit), self.marker(unit)
            protected(path, self.paths)
            protected(marker, self.paths)
            if path.read_bytes() != gate_bytes(marker) or marker.read_bytes() != b'zhihui.writer-start-blocked\n':
                raise Rejected('writer start gate changed')
            # `conditions` comes from a trusted systemctl adapter, not caller
            # booleans. Require the exact negated file condition in live config.
            if ('ConditionPathExists=!' + str(marker)) not in conditions(unit):
                raise Rejected('systemd has not loaded the writer start gate')

    def release(self, permit, unit, guarded_start):
        self._authorized(permit)
        marker = self.marker(unit)
        # The adapter must verify fixed ExecStart, drop-ins and installed tool
        # closure before removing a persistent boot/start fence.
        if guarded_start.verify(unit) is not None:
            raise Rejected('guarded startup verifier must raise on failure')
        protected(marker, self.paths)
        if marker.read_bytes() != b'zhihui.writer-start-blocked\n':
            raise Rejected('writer start gate changed')
        marker.unlink()
        self._sync(marker.parent)
