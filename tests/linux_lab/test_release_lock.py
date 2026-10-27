"""Cross-process checks for the actual release mutex; run on POSIX, never production."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

TOOL = Path(__file__).resolve().parents[2] / 'ops/release-lock/release_lock.py'
CHILD = r'''
import os, pathlib, subprocess, sys, time
tool, root, mode = sys.argv[1:]
root = pathlib.Path(root)
(root / 'ready').write_text(str(os.getpid()))
fd = int(os.environ['ZH_RELEASE_LOCK_FD'])
if mode in ('stages', 'backward'):
    phases = ['drain', 'switch', 'verify', 'done'] if mode == 'stages' else ['drain', 'prepare']
    for phase in phases:
        result = subprocess.run([sys.executable, tool, 'stage', phase], pass_fds=(fd,))
        if result.returncode:
            raise SystemExit(result.returncode)
elif mode == 'background':
    child = subprocess.Popen([sys.executable, __file__, tool, str(root), 'hold'], pass_fds=(fd,),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (root / 'orphan').write_text(str(child.pid))
elif mode == 'fail':
    raise SystemExit(23)
else:
    deadline = time.monotonic() + 90
    while not (root / 'stop').exists() and time.monotonic() < deadline:
        time.sleep(.05)
'''


@unittest.skipUnless(os.name == 'posix', 'Kernel flock and /proc require Linux')
class ReleaseLockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zh-release-test-')
        self.root = Path(self.temp.name)
        self.script = self.root / 'child.py'
        self.script.write_text(CHILD)
        self.env = {**os.environ,
                    'ZH_RELEASE_LOCK_FILE': str(self.root / 'release.lock'),
                    'ZH_RELEASE_STATE_FILE': str(self.root / 'state.json')}
        self.processes = []

    def tearDown(self):
        (self.root / 'stop').touch()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
        self.temp.cleanup()

    def call(self, *args):
        return subprocess.run([sys.executable, str(TOOL), *args], env=self.env,
                              text=True, capture_output=True, timeout=60)

    def launch(self, mode='hold'):
        process = subprocess.Popen([sys.executable, str(TOOL), 'run', 'test-release', 'manager,web',
                                    '--', sys.executable, str(self.script), str(TOOL),
                                    str(self.root), mode], env=self.env,
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        self.until(lambda: (self.root / 'ready').exists() or process.poll() is not None)
        return process

    def until(self, check):
        deadline = time.monotonic() + 60
        while not check():
            if time.monotonic() > deadline:
                self.fail('Timed out waiting for the specific release test process')
            time.sleep(.05)

    def status(self):
        result = self.call('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_lock_covers_entire_child_and_inode_is_never_replaced(self):
        process = self.launch()
        inode = (self.root / 'release.lock').stat().st_ino
        self.assertTrue(self.status()['active'])
        blocked = self.call('run', 'second', 'qtg', '--', sys.executable, '-c', 'raise SystemExit(99)')
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        (self.root / 'stop').touch()
        process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(self.status()['active'])
        self.assertEqual((self.root / 'release.lock').stat().st_ino, inode)
        self.assertEqual(self.call('run', 'third', 'ops', '--', sys.executable, '-c', 'pass').returncode, 0)

    def test_non_child_cannot_mutate_stage(self):
        self.launch()
        denied = self.call('stage', 'done')
        self.assertEqual(denied.returncode, 64)
        self.assertEqual(self.status()['stage'], 'prepare')

    def test_child_records_verified_sequence(self):
        process = self.launch('stages')
        stdout, stderr = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        state = self.status()
        self.assertEqual([entry['stage'] for entry in state['history']],
                         ['prepare', 'drain', 'switch', 'verify', 'done'])
        self.assertEqual(state['status'], 'finished')
        self.assertEqual(state['exit_code'], 0)
        self.assertEqual((self.root / 'state.json').stat().st_mode & 0o777, 0o600)

    def test_backward_transition_is_rejected_and_failure_is_recorded(self):
        process = self.launch('backward')
        process.communicate(timeout=60)
        self.assertEqual(process.returncode, 64)
        state = self.status()
        self.assertEqual(state['stage'], 'drain')
        self.assertEqual(state['exit_code'], 64)

    def test_nonzero_child_never_becomes_done(self):
        process = self.launch('fail')
        process.communicate(timeout=60)
        self.assertEqual(process.returncode, 23)
        state = self.status()
        self.assertEqual((state['stage'], state['exit_code']), ('prepare', 23))
        self.assertFalse(state['active'])

    def test_sigterm_stops_child_and_releases_mutex(self):
        process = self.launch()
        child_pid = int((self.root / 'ready').read_text())
        process.terminate()
        process.communicate(timeout=60)
        self.assertEqual(process.returncode, 143)
        self.assertFalse(Path(f'/proc/{child_pid}').exists())
        self.assertFalse(self.status()['active'])

    def test_background_descendant_keeps_lock_even_after_parent_exit(self):
        process = self.launch('background')
        process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0)
        self.assertTrue(self.status()['active'])
        self.assertEqual(self.call('run', 'second', 'ops', '--', sys.executable, '-c', 'pass').returncode, 75)
        (self.root / 'stop').touch()
        self.until(lambda: not self.status()['active'])

    def test_unsafe_old_acquire_api_and_symlink_lock_fail_closed(self):
        self.assertEqual(self.call('acquire', 'old', 'manager').returncode, 64)
        self.assertEqual(self.call('release').returncode, 64)
        destination = self.root / 'unrelated'
        destination.write_text('preserve')
        (self.root / 'release.lock').symlink_to(destination)
        self.assertEqual(self.call('run', 'test', 'ops', '--', sys.executable, '-c', 'pass').returncode, 64)
        self.assertEqual(destination.read_text(), 'preserve')


if __name__ == '__main__':
    unittest.main(verbosity=2)
