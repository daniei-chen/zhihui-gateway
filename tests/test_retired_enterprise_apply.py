"""The retired patch entry point must reject before any deployment side effect."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'manager/docs/enterprise-support/apply.sh'
BASH = shutil.which('bash')


@unittest.skipUnless(BASH, 'Bash is required for the retired shell entry point')
class RetiredEnterpriseApplyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='retired-enterprise-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.trace = self.root / 'commands.log'
        self.upstream = self.root / 'upstream'
        self.upstream.mkdir()
        (self.upstream / 'go.mod').write_text('module audit.invalid/fixture\n')
        (self.upstream / 'config.json').write_text('{"api_key":"synthetic-do-not-read"}\n')
        self.patches = self.root / 'patches'
        self.patches.mkdir()
        (self.patches / 'tencent.py').write_text('existing fixture\n')
        for command in ('docker', 'cp', 'go', 'python3', 'curl', 'dirname', 'date',
                        'mkdir', 'grep', 'git', 'sleep', 'head', 'tail'):
            shim = self.bin / command
            shim.write_text(
                f'#!{BASH}\n'
                'printf "%s\\n" "$0 $*" >> "$AUDIT_COMMAND_TRACE"\n'
                'exit 97\n'
            )
            shim.chmod(0o700)
        # A minimal environment prevents shell startup hooks and inherited
        # deployment credentials from participating in this regression.
        self.env = {
            'PATH': str(self.bin),
            'AUDIT_COMMAND_TRACE': str(self.trace),
            'UPSTREAM_DIR': str(self.upstream),
            'PATCH_DIR': str(self.patches),
            'MANAGER_CONTAINER': 'audit-synthetic-never-call-docker',
            'LC_ALL': 'C',
        }

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): (
                'directory' if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
            )
            for path in self.root.rglob('*')
        }

    def assert_rejected(self, arguments, *, sourced=False):
        before = self.snapshot()
        command = [BASH, '--noprofile', '--norc']
        if sourced:
            command += ['-c', 'source "$1" "${@:2}"', 'retired-source', str(SCRIPT)]
        else:
            command += [str(SCRIPT)]
        result = subprocess.run(command + arguments, cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn('RETIRED', result.stderr)
        self.assertIn('docs/PRODUCT-RELEASE.md', result.stderr)
        self.assertIn('docs/release-signing.md', result.stderr)
        self.assertFalse(self.trace.exists(), 'a deployment command was reached')
        self.assertEqual(self.snapshot(), before, 'the rejected call modified private fixture state')

    def test_all_direct_targets_reject_without_side_effects(self):
        for arguments in ([], ['all'], ['upstream'], ['manager'], ['invalid'], ['--help']):
            with self.subTest(arguments=arguments):
                self.assert_rejected(arguments)

    def test_sourced_calls_reject_without_side_effects(self):
        for arguments in ([], ['all'], ['upstream'], ['manager'], ['invalid']):
            with self.subTest(arguments=arguments):
                self.assert_rejected(arguments, sourced=True)


if __name__ == '__main__':
    unittest.main()
