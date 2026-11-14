"""Contract tests for the production staged-release signing entrypoint."""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/prepare-managed-stage.sh"


class ManagedStageHelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8")

    def test_shell_syntax_is_valid(self):
        if shutil.which("sh") is None:
            self.skipTest("POSIX shell is required for the shell syntax check")
        result = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_signing_order_and_trusted_preflight_are_bound(self):
        steps = [
            "create-web-manifest.py",
            "create-managed-release.py",
            "sign-release-ed25519.py",
            "web_preflight.py",
            "preflight_candidate.py",
        ]
        positions = [self.text.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("--rebuild", self.text)
        self.assertIn("--key \"$key\"", self.text)
        self.assertIn("--trust \"$trust\"", self.text)

    def test_helper_requires_root_and_never_contains_private_key_material(self):
        self.assertIn('[ "$(id -u)" = 0 ]', self.text)
        self.assertIn("key_mode=$(stat -c '%a'", self.text)
        self.assertNotIn("BEGIN PRIVATE KEY", self.text)
        self.assertNotIn("systemctl", self.text)


if __name__ == "__main__":
    unittest.main()
