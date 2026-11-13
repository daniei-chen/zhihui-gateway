#!/usr/bin/python3
"""Fixed first-migration CLI. Installation and signing remain separate steps."""
import importlib.util
import os
from pathlib import Path
import re
import runpy
import sys
import hashlib
from types import SimpleNamespace

INSTALL = Path('/opt/zhihui-release/writer-guard')
CACHE = Path('/opt/zhihui-release/python-cache')
MAINTENANCE = Path('/opt/zhihui-release/container-maintenance/maintenance.py')
MAINTENANCE_SHA = '7e2768235ee872dc6e9501a97254e96abf825355617086696f83d20677f38f72'


def arguments(argv):
    if (len(argv) == 3 and argv[0] == 'prepare-first'
            and all(re.fullmatch('[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', v) for v in argv[1:])):
        return argv
    if argv == ['run-first'] or argv == ['status']:
        return argv
    if (len(argv) == 3 and argv[0] == 'prepare'
            and all(re.fullmatch('[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', v) for v in argv[1:])):
        return argv
    if (len(argv) == 2 and argv[0] in ('run', 'status')
            and re.fullmatch('[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', argv[1])):
        return argv
    raise ValueError('finite first-migration command required')


def main(argv=None):
    argv = arguments(sys.argv[1:] if argv is None else argv)
    if (os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.no_site
            or not sys.dont_write_bytecode or Path(sys.pycache_prefix or '') != CACHE
            or Path(__file__).absolute() != INSTALL / 'migration_entry.py'):
        raise ValueError('isolated installed root entry required')
    # Import no release or mutable working-directory code. The entrypoint is
    # root-owned; it validates the complete installed closure before sys.path.
    target = INSTALL / 'writer_entry.py'
    for path in (target, *target.parents):
        info = path.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or path.is_symlink():
            raise ValueError('unsafe installed entry')
    spec = importlib.util.spec_from_file_location('writer_entry', target)
    entry = importlib.util.module_from_spec(spec)
    sys.modules['writer_entry'] = entry
    spec.loader.exec_module(entry)
    entry.verify_host_closure()
    os.chdir(INSTALL)
    sys.path.insert(0, str(INSTALL))
    from state_guard import Guard, Paths
    from launch_guard import RuntimeLock
    from coordination import Coordination
    from migration import Controller, Journal
    from first_install import FirstInstall
    from service_gate import ServiceGate
    from systemd_adapter import Systemd
    from startup import Startup
    from ingress import Ingress
    from writer_inventory import LinuxInventory, Quiescence
    from runtime_identity import RuntimeIdentity
    from host_adapter import Host
    from frontend_adapter import Frontend
    from retire_legacy import Retirement
    entry.protected(MAINTENANCE)
    if hashlib.sha256(MAINTENANCE.read_bytes()).hexdigest() != MAINTENANCE_SHA:
        raise ValueError('maintenance dependency drift')
    maintenance = SimpleNamespace(**runpy.run_path(str(MAINTENANCE)))
    os.umask(0o077)
    paths = Paths.production()
    authority = Coordination(paths)
    with authority.acquire() as permit:
        systemd, inventory = Systemd(), LinuxInventory()
        gate = ServiceGate(paths, authority, systemd.command)
        quiet_gate = SimpleNamespace(validate=lambda: gate.verify_blocked(permit, systemd.conditions))
        quiet = Quiescence(inventory, start_gate=quiet_gate)
        lock = RuntimeLock(paths, quiet)
        # Legacy inspection is deliberately unusable by Launcher.execute().
        # It establishes predecessor identity only; the candidate descriptor
        # forces the strict web closure even through this inspection authority.
        guard = Guard(paths, entry.HostReleaseVerifier(inspect_legacy=True), lock)
        subsequent = argv[0] == 'prepare' or len(argv) == 2
        journal = Journal(paths, authority, argv[-1] if subsequent else None)
        http = maintenance.Host()
        ingress_path = journal.root / 'ingress.json' if subsequent else paths.floor.parent / 'ingress.json'
        ingress = Ingress(maintenance, http, ingress_path, authority)
        frontend = Frontend.production(journal, authority)
        startup = Startup(paths, authority, gate, systemd)
        retirement = Retirement(paths, authority, journal)
        host = Host(guard, lock, journal, authority, permit, gate, ingress, systemd, inventory,
                    RuntimeIdentity(), http, startup, frontend, retirement)
        first = None if subsequent else FirstInstall(guard, journal)
        controller = Controller(guard, lock, journal, host, first_install=first)
        startup.verify_tools()
        if argv[0] in ('prepare-first', 'prepare'):
            gate.install(permit)
            result = controller.prepare(permit, argv[2], paths.releases / argv[1])
        elif argv[0] in ('run-first', 'run'):
            result = controller.run(permit)
        else:
            result = journal.load(permit)
        import json
        print(json.dumps({key: result[key] for key in ('transaction', 'phase', 'epoch', 'candidate',
                                                       'candidate_sha256', 'failure', 'compensation_confirmed')},
                         sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('WRITER-MIGRATION-BLOCKED: inspect retained private journals; no automatic legacy fallback',
              file=sys.stderr)
        raise SystemExit(1)
