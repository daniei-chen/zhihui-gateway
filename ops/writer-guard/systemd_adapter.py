"""Bounded systemd D-Bus reads and fixed service commands; no shell."""
import json
import re
import subprocess

from state_guard import Rejected

MAIN = 'workbuddy-web.service'
HELPER = 'workbuddy-web-helper.service'
JOB = 'wb-budget-autoreconcile.service'
TIMER = 'wb-budget-autoreconcile.timer'
UNITS = (MAIN, HELPER, JOB, TIMER)
ENTRY = '/opt/zhihui-release/writer-guard/writer_entry.py'
PREFIX = ['/usr/bin/python3', '-I', '-S', '-B', '-X',
          'pycache_prefix=/opt/zhihui-release/python-cache', ENTRY]


def entry_command(unit):
    if unit not in (MAIN, HELPER, JOB):
        raise Rejected('unsupported guarded service')
    if unit == HELPER:
        return PREFIX + ['helper']
    return PREFIX + ['main' if unit == MAIN else 'reconcile']


def launch_dropin(unit):
    command = entry_command(unit)
    return ('[Service]\nUser=root\nGroup=root\nWorkingDirectory=/opt/workbuddy-manager\n'
            'ExecCondition=\nExecStartPre=\nExecStartPost=\nExecStart=\nExecStart=' + ' '.join(command) + '\n'
            'ExecReload=\nExecStop=\nExecStopPost=\n'
            'Environment=PYTHONDONTWRITEBYTECODE=1\nEnvironment=PYTHONNOUSERSITE=1\n'
            'Environment=PYTHONSAFEPATH=1\n'
            'Environment=PYTHONPYCACHEPREFIX=/opt/zhihui-release/python-cache\n'
            'Environment=PYTHONPATH=/opt/workbuddy-manager\n'
            'KillMode=control-group\nNoNewPrivileges=yes\n').encode()


class Systemd:
    def __init__(self, runner=None):
        self.runner = runner or subprocess.run

    def _run(self, command, *, timeout=15):
        result = self.runner(command, capture_output=True, timeout=timeout,
                             env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
            raise Rejected('systemd operation failed or response exceeded bounds')
        return result.stdout

    def object_path(self, unit):
        if unit not in UNITS:
            raise Rejected('unknown systemd unit')
        # A freshly installed inactive unit is visible to ``systemctl show``
        # but may not yet have a manager object.  ``GetUnit`` only looks up
        # already loaded objects and therefore rejects the first rolling
        # handoff before the helper can start.  LoadUnit is bounded to the
        # fixed allowlist above and returns the same immutable object path
        # while making the manager load the unit for subsequent reads.
        result = json.loads(self._run(['/usr/bin/busctl', '--system', '--json=short', 'call',
                            'org.freedesktop.systemd1', '/org/freedesktop/systemd1',
                            'org.freedesktop.systemd1.Manager', 'LoadUnit', 's', unit]))
        if (result.get('type') != 'o' or type(result.get('data')) is not list or len(result['data']) != 1
                or type(result['data'][0]) is not str
                or not re.fullmatch(r'/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+', result['data'][0])):
            raise Rejected('invalid systemd object identity')
        return result['data'][0]

    def property(self, unit, interface, name, kind):
        if interface not in ('Unit', 'Service', 'Timer') or not re.fullmatch('[A-Za-z]+', name):
            raise Rejected('invalid systemd property selector')
        result = json.loads(self._run(['/usr/bin/busctl', '--system', '--json=short', 'get-property',
                          'org.freedesktop.systemd1', self.object_path(unit),
                          'org.freedesktop.systemd1.' + interface, name]))
        if result.get('type') != kind or 'data' not in result:
            raise Rejected('invalid systemd property type')
        return result['data']

    def conditions(self, unit):
        rows = self.property(unit, 'Unit', 'Conditions', 'a(sbbsi)')
        if type(rows) is not list:
            raise Rejected('invalid systemd conditions')
        normalized = []
        for row in rows:
            if (type(row) is not list or len(row) != 5 or type(row[0]) is not str
                    or type(row[1]) is not bool or type(row[2]) is not bool
                    or type(row[3]) is not str or type(row[4]) is not int):
                raise Rejected('invalid systemd condition entry')
            if not row[1]:
                normalized.append(row[0] + '=' + ('!' if row[2] else '') + row[3])
        return normalized

    def show(self, unit):
        if unit not in UNITS:
            raise Rejected('unknown systemd unit')
        names = ('Id', 'LoadState', 'ActiveState', 'SubState', 'MainPID', 'ExecMainPID',
                 'ExecMainCode', 'ExecMainStatus', 'ControlGroup', 'KillMode', 'User',
                 'Group', 'Type', 'NeedDaemonReload', 'UnitFileState')
        raw = self._run(['/usr/bin/systemctl', 'show', unit, '--no-pager', '-p', ','.join(names)])
        value = {}
        for line in raw.decode('utf-8', errors='strict').splitlines():
            key, sep, item = line.partition('=')
            if not sep or key not in names or key in value:
                raise Rejected('invalid systemd service state')
            value[key] = item
        required = {'Id', 'LoadState', 'ActiveState', 'SubState', 'UnitFileState', 'NeedDaemonReload'}
        if unit != TIMER:
            required.update({'MainPID', 'ControlGroup', 'KillMode', 'User', 'Group', 'Type'})
        if not required <= value.keys() or value['Id'] != unit:
            raise Rejected('systemd service identity mismatch')
        return value

    def verify_start(self, unit):
        value = self.show(unit)
        if (value['LoadState'] != 'loaded' or value['User'] != 'root' or value['Group'] != 'root'
                or value['KillMode'] != 'control-group' or value['NeedDaemonReload'] != 'no'
                or value['Type'] != ('oneshot' if unit == JOB else 'simple')):
            raise Rejected('guarded systemd startup contract differs')
        rows = self.property(unit, 'Service', 'ExecStart', 'a(sasbttttuii)')
        expected = entry_command(unit)
        if (type(rows) is not list or len(rows) != 1 or type(rows[0]) is not list
                or len(rows[0]) != 10 or rows[0][:3] != [expected[0], expected, False]):
            raise Rejected('systemd ExecStart is not the fixed guarded entry')
        for name in ('ExecCondition', 'ExecStartPre', 'ExecStartPost', 'ExecReload', 'ExecStop', 'ExecStopPost'):
            if self.property(unit, 'Service', name, 'a(sasbttttuii)') != []:
                raise Rejected('unguarded additional startup command')

    def command(self, argv):
        allowed = (['daemon-reload'], ['stop', TIMER], ['stop', MAIN], ['stop', HELPER],
                   ['stop', JOB], ['start', MAIN], ['start', HELPER], ['start', JOB],
                   ['start', TIMER], ['reset-failed', MAIN], ['reset-failed', HELPER])
        if argv not in allowed:
            raise Rejected('unsupported systemd mutation')
        self._run(['/usr/bin/systemctl', *argv], timeout=105)
