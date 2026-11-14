"""Recovery housekeeping must wait for the producer without weakening safety."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    path = ROOT / "deploy" / "recovery_housekeeping.py"
    spec = importlib.util.spec_from_file_location("recovery_housekeeping", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RecoveryHousekeepingLockTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix" and hasattr(os, "fork"),
                         "requires POSIX flock and fork")
    def test_waits_for_backup_producer_then_acquires(self) -> None:
        module = _load_module()
        module.LOCK_WAIT_SECONDS = 2
        module.LOCK_RETRY_SECONDS = 0.05
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.lock"
            parent_fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            child = None
            try:
                import fcntl
                fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                child = os.fork()
                if child == 0:
                    child_fd = os.open(path, os.O_RDWR)
                    try:
                        module.acquire_backup_lock(child_fd)
                        os._exit(0)
                    except BaseException:
                        os._exit(2)
                    finally:
                        os.close(child_fd)
                module.time.sleep(0.12)
                fcntl.flock(parent_fd, fcntl.LOCK_UN)
                _, status = os.waitpid(child, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            finally:
                if child is not None:
                    try:
                        os.waitpid(child, os.WNOHANG)
                    except ChildProcessError:
                        pass
                os.close(parent_fd)


if __name__ == "__main__":
    unittest.main()
