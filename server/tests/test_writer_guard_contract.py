"""The fixed host writer profile must actually expose the drain contract."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest import mock


OPS = Path(__file__).resolve().parents[2] / "ops" / "writer-guard"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))

import host_adapter  # noqa: E402
from launch_guard import writer_command, writer_environment  # noqa: E402


class WriterGuardContractTests(unittest.TestCase):
    def test_writer_uses_managed_drain_launcher_and_shared_admission(self):
        release = SimpleNamespace(root=Path("/opt/workbuddy-manager/releases/candidate"))
        command = writer_command()
        self.assertEqual(command[command.index("-m") + 1], "server.serve")
        self.assertIn("--timeout-graceful-shutdown", command)
        environment = writer_environment({}, release)
        self.assertEqual(environment["WB_SHARED_ADMISSION"], "1")


    def test_helper_keeps_shared_admission_but_disables_background_jobs(self):
        release = SimpleNamespace(root=Path("/opt/workbuddy-manager/releases/candidate"))
        command = writer_command(helper=True)
        self.assertEqual(command[command.index("--port") + 1], "7865")
        environment = writer_environment({}, release, helper=True)
        self.assertEqual(environment["WB_SHARED_ADMISSION"], "1")
        self.assertEqual(environment["WB_BACKGROUND_JOBS"], "0")


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
