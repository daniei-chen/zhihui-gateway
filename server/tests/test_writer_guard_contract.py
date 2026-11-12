"""The fixed host writer profile must actually expose the drain contract."""
from pathlib import Path
from types import SimpleNamespace
import sys


OPS = Path(__file__).resolve().parents[2] / "ops" / "writer-guard"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))

from launch_guard import writer_command, writer_environment  # noqa: E402


def test_writer_uses_managed_drain_launcher_and_shared_admission():
    release = SimpleNamespace(root=Path("/opt/workbuddy-manager/releases/candidate"))
    command = writer_command()
    assert command[command.index("-m") + 1] == "server.serve"
    assert "--timeout-graceful-shutdown" in command
    environment = writer_environment({}, release)
    assert environment["WB_SHARED_ADMISSION"] == "1"


def test_helper_keeps_shared_admission_but_disables_background_jobs():
    release = SimpleNamespace(root=Path("/opt/workbuddy-manager/releases/candidate"))
    command = writer_command(helper=True)
    assert command[command.index("--port") + 1] == "7865"
    environment = writer_environment({}, release, helper=True)
    assert environment["WB_SHARED_ADMISSION"] == "1"
    assert environment["WB_BACKGROUND_JOBS"] == "0"
