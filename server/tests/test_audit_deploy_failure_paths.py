"""Exercise release failure paths with temporary files and fake external commands.

No service, public endpoint, production path, or real model is contacted.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / 'deploy/publish-console.sh'
ROLLING = (ROOT / 'deploy/rollout-rolling.sh').read_text(encoding='utf-8')
PAGES = ('dashboard', 'accounts', 'keys', 'stats', 'portal', 'settings/system', 'claim', 'login')


def function(name: str) -> str:
    match = re.search(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}', ROLLING, re.M | re.S)
    if not match:
        raise AssertionError(f'Missing shell function {name}')
    return match.group()


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash') and shutil.which('flock'),
                     'Linux shell deployment tools are required')
class ConsolePublishFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pool = self.root / 'pool'
        self.pool.mkdir()
        self.old = self.root / 'old'
        self.old.mkdir()
        (self.old / 'index.html').write_text('<html>old</html>')
        self.current = self.root / 'current'
        self.current.symlink_to(self.old, target_is_directory=True)
        self.releases = self.root / 'releases'
        self.archive = self.root / 'web.tar.gz'
        markup = '<html><script src="/_next/static/chunks/new.js"></script></html>'
        self.files = {'out/index.html': markup,
                      'out/_next/static/chunks/new.js': 'console.log("candidate")'}
        self.files.update({f'out/{page}/index.html': markup for page in PAGES})
        self.write_archive()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.command('chown', 'pass\n')
        self.command('cp', '''
import json, os, subprocess, sys
from pathlib import Path
root = Path(os.environ['PUBLISH_TEST_ROOT'])
with (root / 'copy-observations.jsonl').open('a') as out:
    out.write(json.dumps({'current': os.readlink(root / 'current')}) + '\\n')
if os.environ.get('PUBLISH_TEST_FAIL_COPY') == '1':
    raise SystemExit(1)
raise SystemExit(subprocess.call([os.environ['PUBLISH_TEST_REAL_CP'], *sys.argv[1:]]))
''')
        self.command('curl', '''
import os, sys
from pathlib import Path
from urllib.parse import urlsplit
root = Path(os.environ['PUBLISH_TEST_ROOT'])
args = sys.argv[1:]
url = next(value for value in args if value.startswith('https://audit.invalid/'))
path = urlsplit(url).path
destination = args[args.index('-o') + 1]
mode = os.environ.get('PUBLISH_TEST_HTTP_MODE', '')
with (root / 'requests.txt').open('a') as out:
    out.write(path + '\\n')
if mode == 'network_failure':
    raise SystemExit(7)
if path.startswith('/_next/static/'):
    status = 404 if mode == 'asset_missing' else 200
    body = (root / 'pool' / path.lstrip('/')).read_bytes()
    if mode == 'asset_wrong_body':
        body = b'<html>unexpected fallback with HTTP 200</html>'
else:
    status = 500 if mode == 'page_failure' else 200
    body = (root / 'current' / path.strip('/') / 'index.html').read_bytes()
    if mode == 'empty_page':
        body = b'<html>temporary maintenance</html>'
    elif mode == 'stale_page':
        body = b'<html><script src="/_next/static/chunks/old.js"></script></html>'
if destination != '/dev/null':
    Path(destination).write_bytes(body)
print(status, end='')
''')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        PUBLISH_TEST_ROOT=str(self.root), PUBLISH_TEST_REAL_CP=shutil.which('cp'))

    def command(self, name: str, source: str):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + source, encoding='utf-8')
        path.chmod(0o700)

    def write_archive(self):
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, text in self.files.items():
                data = text.encode()
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(data))

    def run_publish(self, *extra, **env):
        return subprocess.run(
            ['bash', str(PUBLISH), '--web', str(self.archive), '--version', '1.6.27',
             '--static-pool', str(self.pool), '--releases-dir', str(self.releases),
             '--current-link', str(self.current), '--base-url', 'https://audit.invalid', *extra],
            cwd=self.root, env={**self.env, **env}, text=True, capture_output=True, timeout=25)

    def assert_old_serving(self):
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual((self.current / 'index.html').read_text(), '<html>old</html>')

    def test_resources_are_ready_before_switch_and_all_pages_are_verified(self):
        result = self.run_publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.current.resolve(), self.old)
        observations = [json.loads(line) for line in
                        (self.root / 'copy-observations.jsonl').read_text().splitlines()]
        self.assertTrue(observations)
        self.assertTrue(all(item['current'] == str(self.old) for item in observations))
        requests = (self.root / 'requests.txt').read_text().splitlines()
        self.assertEqual(sum(not value.startswith('/_next/static/') for value in requests), len(PAGES))
        self.assertEqual(requests.count('/_next/static/chunks/new.js'), 1)
        self.assertIn('全部通过', result.stdout)

    def test_failed_copy_preserves_old_pointer(self):
        result = self.run_publish(PUBLISH_TEST_FAIL_COPY='1')
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_serving()
        self.assertFalse((self.root / 'requests.txt').exists())

    def test_existing_asset_with_different_contents_blocks_switch(self):
        asset = self.pool / '_next/static/chunks/new.js'
        asset.parent.mkdir(parents=True)
        asset.write_text('a different existing immutable asset')
        result = self.run_publish()
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_serving()
        self.assertEqual(asset.read_text(), 'a different existing immutable asset')

    def test_http_failures_and_empty_or_stale_pages_roll_back(self):
        for mode in ('page_failure', 'empty_page', 'stale_page', 'asset_missing',
                     'asset_wrong_body', 'network_failure'):
            with self.subTest(mode=mode):
                result = self.run_publish(PUBLISH_TEST_HTTP_MODE=mode)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_old_serving()
                self.assertIn('已恢复原指针', result.stderr)
                self.assertNotIn('控制台已发布并通过校验', result.stdout)

    def test_missing_candidate_page_blocks_before_http_or_pointer_switch(self):
        del self.files['out/settings/system/index.html']
        self.write_archive()
        result = self.run_publish()
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_serving()
        self.assertFalse((self.root / 'requests.txt').exists())

    def test_signal_after_atomic_rename_restores_previous_pointer(self):
        # Interrupt the real rename's caller before the mv wrapper returns.
        # This catches setting SWITCHED only after atomic_link has completed.
        self.command('mv', '''
import os, signal, subprocess, sys
from pathlib import Path
result = subprocess.call([os.environ['PUBLISH_TEST_REAL_MV'], *sys.argv[1:]])
marker = Path(os.environ['PUBLISH_TEST_ROOT']) / 'signal-sent'
if result == 0 and not marker.exists():
    marker.write_text('sent')
    os.kill(os.getppid(), getattr(signal, os.environ['PUBLISH_TEST_SIGNAL']))
raise SystemExit(result)
''')
        for signal_name, exit_code in (('SIGINT', 130), ('SIGTERM', 143)):
            with self.subTest(signal=signal_name):
                (self.root / 'signal-sent').unlink(missing_ok=True)
                result = self.run_publish(PUBLISH_TEST_SIGNAL=signal_name,
                                          PUBLISH_TEST_REAL_MV=shutil.which('mv'))
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                self.assert_old_serving()
                self.assertIn('已恢复原指针', result.stderr)
                self.assertFalse((self.root / 'requests.txt').exists())

    def test_dry_run_has_no_writes_or_http_requests(self):
        result = self.run_publish('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_old_serving()
        self.assertFalse(self.releases.exists())
        self.assertFalse(Path(str(self.current) + '.publish.lock').exists())
        self.assertFalse((self.root / 'requests.txt').exists())

    def test_concurrent_publish_is_rejected_before_creating_release(self):
        import fcntl
        with Path(str(self.current) + '.publish.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_publish()
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_serving()
        self.assertFalse(self.releases.exists())

    def test_archive_path_escape_is_rejected_before_extracting(self):
        self.files['out/../outside.txt'] = 'must not be extracted'
        self.write_archive()
        result = self.run_publish()
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_serving()
        self.assertFalse((self.root / 'outside.txt').exists())


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'bash is required')
class RollingFailureTests(unittest.TestCase):
    def run_shell(self, source: str, *, env=None):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['bash', '-c', source], cwd=directory,
                                    env={**os.environ, **(env or {})}, text=True,
                                    capture_output=True, timeout=10)
            log = Path(directory) / 'rollout.jsonl'
            records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            switches = Path(directory) / 'switches'
            routes = switches.read_text().splitlines() if switches.exists() else []
            return result, records, routes

    def test_relay_restart_failure_keeps_verified_direct_target(self):
        result, _, routes = self.run_shell('''
set -uo pipefail
step() { :; }
die() { printf '%s\\n' "$*" >&2; exit 1; }
curl() { printf 200; }
switch_inference() { printf '%s\\n' "$1" >> switches; }
systemctl() { return 1; }
RELAY=fake-relay
''' + function('restart_relay') + '\nrestart_relay\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(routes, ['direct'])
        self.assertIn('保持在直连', result.stderr)

    def test_unready_direct_target_does_not_move_traffic(self):
        result, _, routes = self.run_shell('''
set -uo pipefail
step() { :; }
die() { exit 1; }
curl() { printf 503; }
switch_inference() { printf '%s\\n' "$1" >> switches; }
systemctl() { exit 99; }
RELAY=fake-relay
''' + function('restart_relay') + '\nrestart_relay\n')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(routes, [])

    def rollout_source(self, mode: str) -> str:
        return r'''
set -uo pipefail
step() { :; }
die() { printf '%s\n' "$*" >&2; exit 1; }
sleep() { :; }
seq() { printf 1; }
systemctl() { :; }
prestart_candidate() { :; }
route_switch() { :; }
db_scalar() {
    case "$1" in
        *'max(rowid)'*) printf 10;;
        *'http_status >= 500'*) printf 0;;
        *) printf '%s' "${TEST_REQUEST_COUNT:-4}";;
    esac
}
curl() {
    if [ -s rollout.jsonl ]; then
        printf 'premature rollout record\n' >&2
        return 99
    fi
    case "$*" in
        *'/api/release'*) printf '{"version":"1.6.27"}';;
        *'/readyz'*) printf '%s' "${TEST_READYZ_STATUS:-200}"; return "${TEST_CURL_EXIT:-0}";;
        *) printf '%s' "${TEST_PAGE_STATUS:-200}"; return "${TEST_CURL_EXIT:-0}";;
    esac
}
DB=fake.db
GATEWAY=fake-gateway
HELPER=fake-helper
STARTED_AT=test
RECORDED=0
ROLLOUT_LOG=rollout.jsonl
''' + '\n'.join(function(name) for name in ('record_rollout', 'check_pages', mode)) + f'\n{mode} 1.6.27\n'

    def test_required_http_checks_fail_both_rollout_modes(self):
        for mode in ('restart_gateway', 'rollout_blue_green'):
            for failure in ({'TEST_PAGE_STATUS': '500'}, {'TEST_READYZ_STATUS': '503'},
                            {'TEST_CURL_EXIT': '28'}):
                with self.subTest(mode=mode, failure=failure):
                    result, records, _ = self.run_shell(self.rollout_source(mode), env=failure)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]['exit'], 2)
                    self.assertIn('检查失败', records[0]['note'])
                    self.assertNotIn('premature rollout record', result.stderr)

    def test_success_records_follow_checks_and_limit_the_evidence_claim(self):
        for mode in ('restart_gateway', 'rollout_blue_green'):
            with self.subTest(mode=mode):
                result, records, _ = self.run_shell(self.rollout_source(mode))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['exit'], 0)
                self.assertEqual(records[0]['requests'], 4)
                self.assertFalse(records[0]['zero_downtime_verified'])
                self.assertEqual(records[0]['evidence_scope'], 'gateway_completed_requests')
                self.assertNotIn('premature rollout record', result.stderr)

    def test_failed_window_query_cannot_be_reported_as_zero_failures(self):
        for mode in ('restart_gateway', 'rollout_blue_green'):
            with self.subTest(mode=mode):
                result, records, _ = self.run_shell(self.rollout_source(mode),
                                                   env={'TEST_REQUEST_COUNT': 'unavailable'})
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(records[0]['exit'], 2)
                self.assertIsNone(records[0]['requests'])


if __name__ == '__main__':
    unittest.main()
