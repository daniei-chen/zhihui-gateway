"""The fixed host writer profile must actually expose the drain contract."""
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
import sys
import unittest
from unittest import mock


OPS = Path(__file__).resolve().parents[2] / "ops" / "writer-guard"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))

import host_adapter  # noqa: E402
import launch_guard  # noqa: E402
import migration  # noqa: E402
import service_gate  # noqa: E402
import systemd_adapter  # noqa: E402
from launch_guard import writer_command, writer_environment  # noqa: E402


class WriterGuardContractTests(unittest.TestCase):
    def test_writer_uses_managed_drain_launcher_and_shared_admission(self):
        release = SimpleNamespace(root=Path("/opt/workbuddy-manager/releases/candidate"))
        command = writer_command()
        self.assertEqual(command[command.index("-m") + 1], "server.serve")
        self.assertIn("--timeout-graceful-shutdown", command)
        environment = writer_environment({}, release)
        self.assertEqual(environment["WB_SHARED_ADMISSION"], "1")

    @unittest.skipUnless(hasattr(launch_guard.os, 'chown'), 'POSIX ownership contract only')
    def test_service_data_contract_normalizes_private_handoff_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            users = root / 'users.json'
            password = root / 'initial-admin-password.txt'
            users.write_text('{}', encoding='utf-8')
            password.write_text('secret', encoding='utf-8')
            users.chmod(0o644)
            password.chmod(0o644)
            root.chmod(0o755)
            launch_guard.normalize_service_data(
                root, uid=launch_guard.os.getuid(), gid=launch_guard.os.getgid())
            self.assertEqual(launch_guard.stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(launch_guard.stat.S_IMODE(users.stat().st_mode), 0o600)
            self.assertEqual(launch_guard.stat.S_IMODE(password.stat().st_mode), 0o600)


    def test_helper_keeps_shared_admission_but_disables_background_jobs(self):
        release = SimpleNamespace(root=Path("/opt/workbuddy-manager/releases/candidate"))
        command = writer_command(helper=True)
        self.assertEqual(command[command.index("--port") + 1], "7865")
        environment = writer_environment({}, release, helper=True)
        self.assertEqual(environment["WB_SHARED_ADMISSION"], "1")
        self.assertEqual(environment["WB_BACKGROUND_JOBS"], "0")

    def test_helper_is_a_real_guarded_systemd_profile(self):
        self.assertEqual(systemd_adapter.entry_command(systemd_adapter.HELPER)[-1], 'helper')
        self.assertIn('workbuddy-web-helper.service', service_gate.UNITS)
        self.assertIn('writer_entry.py helper',
                      systemd_adapter.launch_dropin(systemd_adapter.HELPER).decode())
        helper_unit = (Path(__file__).parents[2] / 'deploy' /
                       'workbuddy-web-helper.service').read_text(encoding='utf-8')
        self.assertNotIn('Requires=workbuddy-web.service', helper_unit)

    def test_maintenance_fence_leaves_exact_relay_roots_available(self):
        maintenance_source = (Path(__file__).parents[2] / 'ops' /
                              'container-maintenance' / 'maintenance.py').read_text(encoding='utf-8')
        rule = next(line.split('=', 1)[1].strip() for line in maintenance_source.splitlines()
                    if line.startswith('PUBLIC_RULE = '))
        self.assertNotIn('|v1|', rule)
        self.assertIn('(api|qoder|trae', rule)
        self.assertIn('workbuddy', rule)

    def test_rolling_controller_routes_before_drain_and_retires_helper_last(self):
        class Journal:
            def __init__(self):
                self.record = {'phase': 'prepared', 'mode': 'rolling', 'epoch': 1}

            def save(self, _authority, expected, phase, **_changes):
                self.assert_same(expected)
                self.record = {**expected, 'phase': phase, 'epoch': expected['epoch'] + 1}
                return self.record

            def assert_same(self, value):
                if value is not self.record:
                    raise AssertionError('controller lost journal CAS identity')

            def _authorized(self, _authority):
                return None

            def load(self, _authority):
                return self.record

        class Lock:
            @contextmanager
            def acquire(self, **_kwargs):
                yield object()

            def validate(self, _permit, **_kwargs):
                return None

        class Guard:
            def __init__(self, capability):
                self.capability = capability

            def observe(self, _permit):
                return object()

            def load_floor(self):
                return SimpleNamespace(capabilities=self.capability)

            def require_launch(self, *_args):
                return None

        class Host:
            def __init__(self):
                self.events = []

            def __getattr__(self, name):
                if name in {'point_rolling', 'start_helper', 'helper_ready',
                            'route_helper', 'fence', 'drain_old',
                            'stop_primary_rolling', 'start', 'ready',
                            'route_main', 'stop_helper', 'resume',
                            'verify_complete'}:
                    def effect(*_args):
                        self.events.append(name)
                    return effect
                raise AttributeError(name)

        capability = object()
        journal = Journal()
        host = Host()
        controller = migration.Controller.__new__(migration.Controller)
        controller.journal = journal
        controller.lock = Lock()
        controller.guard = Guard(capability)
        controller.host = host
        controller.first_install = None
        controller._release = lambda _record, _name: SimpleNamespace(capabilities=capability)

        result = controller._run_rolling(object(), journal.record, object())
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(host.events, [
            'point_rolling', 'start_helper', 'helper_ready', 'route_helper',
            'fence', 'drain_old', 'stop_primary_rolling', 'start', 'ready',
            'route_main', 'stop_helper', 'resume', 'verify_complete',
        ])


    def test_resume_ready_retries_transient_protected_state_change(self):
        host = host_adapter.Host.__new__(host_adapter.Host)
        expected = object()
        attempts = []
        host.ready_identity = expected

        def flaky_ready(record, candidate):
            attempts.append((record, candidate))
            if len(attempts) == 1:
                raise host_adapter.Rejected("unsafe protected file")
            return expected

        host._ready = flaky_ready
        with mock.patch.object(host_adapter.time, "monotonic", side_effect=[0.0, 0.0]), \
                mock.patch.object(host_adapter.time, "sleep"):
            host._resume_ready("record", "candidate")

        self.assertEqual(len(attempts), 2)

    def test_final_verification_ready_uses_the_same_retry_contract(self):
        host = host_adapter.Host.__new__(host_adapter.Host)
        expected = object()
        attempts = []

        def flaky_ready(record, candidate):
            attempts.append((record, candidate))
            if len(attempts) == 1:
                raise host_adapter.Rejected("observed state changed during inspection")
            return expected

        host._ready = flaky_ready
        with mock.patch.object(host_adapter.time, "monotonic", side_effect=[0.0, 0.0]), \
                mock.patch.object(host_adapter.time, "sleep"):
            self.assertIs(host._retry_ready("record", "candidate", "verify-ready"), expected)

        self.assertEqual(len(attempts), 2)
