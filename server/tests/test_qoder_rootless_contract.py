from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class QoderRootlessContractTest(unittest.TestCase):
    def test_launcher_verifies_release_before_setpriv(self):
        source = (ROOT / 'deploy/qoder-rootless-production.py').read_text()
        self.assertIn("verifier['verify_release'](current_release())", source)
        self.assertIn("'--reuid=10001'", source)
        self.assertIn("'--regid=10001'", source)
        self.assertIn("'--clear-groups'", source)
        self.assertIn("'--no-new-privs'", source)
        self.assertIn("'--inh-caps=-all'", source)
        self.assertIn("'--ambient-caps=-all'", source)
        self.assertIn("'--host', '127.0.0.1'", source)
        self.assertIn("'--port', PORT", source)
        self.assertNotIn("qoder_trust.py'), 'start'", source)

    def test_systemd_template_clears_old_execstart_and_limits_writes(self):
        unit = (ROOT / 'deploy/qoder2-engine-rootless.conf').read_text()
        self.assertIn('ExecStart=\nExecStart=/opt/qoder-release/bin/qoder-rootless-production.py', unit)
        self.assertIn('ReadOnlyPaths=/opt/qoder-release', unit)
        self.assertIn('InaccessiblePaths=/opt/qoder-release/keys', unit)
        self.assertIn('ReadWritePaths=/opt/workbuddy-manager/engines/accounts', unit)
        self.assertIn('CapabilityBoundingSet=CAP_SETUID CAP_SETGID', unit)
        self.assertIn('NoNewPrivileges=no', unit)
        self.assertNotIn('User=10001', unit)


if __name__ == '__main__':
    unittest.main()
