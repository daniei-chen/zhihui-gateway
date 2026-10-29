"""Product source acquisition must not consume ambient Git trust settings."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('repository_source', Path(__file__).resolve().parents[1] / 'tools/repository_source.py')
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


class RepositorySourceTests(unittest.TestCase):
    def test_rejects_nonimmutable_revision_without_creating_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'new'
            for revision in ['main', 'abc1234', 'A' * 40, '../v0.0.2']:
                with self.assertRaises(ValueError):
                    source.acquire(target, revision)
                self.assertFalse(target.exists())

    def test_never_overwrites_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError), patch.object(source, 'git') as git:
                source.acquire(Path(temp), 'a' * 40)
            git.assert_not_called()

    def test_ignores_git_url_rewrites_environment_helpers_and_hooks(self):
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_DIR': 'untrusted', 'GIT_CONFIG_GLOBAL': 'untrusted'}), \
                patch.object(source.subprocess, 'run') as run:
            run.return_value.stdout = 'ok\n'
            self.assertEqual(source.git(Path('.'), 'status'), 'ok')
            command = run.call_args.args[0]
            env = run.call_args.kwargs['env']
            self.assertIn('http.followRedirects=false', command)
            self.assertIn('protocol.allow=never', command)
            self.assertIn('credential.helper=', command)
            self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
            self.assertNotIn('GIT_DIR', env)
            self.assertNotIn('GIT_CONFIG_COUNT', env)

    def test_moving_tag_is_not_installed_and_partial_tree_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'new'
            def fake_git(directory, *args):
                return 'b' * 40 if args[0] == 'rev-parse' else ''
            with patch.object(source, 'git', side_effect=fake_git), self.assertRaises(ValueError):
                source.acquire(target, 'a' * 40, 'v0.0.2')
            self.assertTrue(target.is_dir())


if __name__ == '__main__':
    unittest.main()
