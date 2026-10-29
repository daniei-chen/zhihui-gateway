"""Run the browser's pure schedule validator on synthetic form values."""
import shutil
import subprocess
import unittest
from pathlib import Path

class ScheduleFormValidationTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_invalid_schedule_is_rejected_before_submission(self):
        version = subprocess.run(['node', '--version'], capture_output=True, text=True, timeout=10)
        parts = version.stdout.strip().lstrip('v').split('.')
        if version.returncode or len(parts) < 2 or tuple(map(int, parts[:2])) < (22, 6):
            self.skipTest('node >= 22.6 is required for TypeScript stripping')
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(['node', '--experimental-strip-types', 'web/lib/schedule-validation.test.mjs'], cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ReleaseLockGateTest(unittest.TestCase):
    """K28：发布期间调度器必须跳过自愈（不得触发管理端动作）。

    修复前 `dailyplan._loop` 完全不看发布状态，发布过程中整点到了照样跑签到/
    领奖/做任务。这些是**写请求**，会让回退验证看到的状态不干净，也可能与正在
    切换的发布互相打断。
    """

    def setUp(self):
        import os
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = os.environ.get('ZH_RELEASE_LOCK_FILE')
        self.lock = Path(self._tmp.name) / 'zhihui-release.lock'

    def tearDown(self):
        import os
        if self._saved is None:
            os.environ.pop('ZH_RELEASE_LOCK_FILE', None)
        else:
            os.environ['ZH_RELEASE_LOCK_FILE'] = self._saved
        self._tmp.cleanup()

    def _reload(self):
        """环境变量在 import 期读取，改完必须重新加载模块。"""
        import importlib
        from server.services import dailyplan
        return importlib.reload(dailyplan)

    def test_no_lock_file_means_no_release(self):
        import os
        os.environ['ZH_RELEASE_LOCK_FILE'] = str(self.lock)
        dailyplan = self._reload()
        self.assertFalse(dailyplan.release_in_progress(),
                         '锁文件不存在时应判定"没有发布"，否则调度器永远停摆')

    def test_held_lock_blocks_scheduler(self):
        try:
            import fcntl
        except ImportError:
            self.skipTest('非 POSIX 平台没有 flock，无法测发布锁')
        import os
        os.environ['ZH_RELEASE_LOCK_FILE'] = str(self.lock)
        self.lock.touch()
        dailyplan = self._reload()
        fd = os.open(str(self.lock), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(dailyplan.release_in_progress(),
                            '别的进程持有发布锁时必须判定"正在发布"')
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertFalse(dailyplan.release_in_progress(),
                         '锁释放后调度器必须能恢复')

    def test_probe_does_not_take_the_lock(self):
        """只读探测不能占住锁，否则发布永远拿不到锁。"""
        try:
            import fcntl
        except ImportError:
            self.skipTest('非 POSIX 平台没有 flock')
        import os
        os.environ['ZH_RELEASE_LOCK_FILE'] = str(self.lock)
        self.lock.touch()
        dailyplan = self._reload()
        dailyplan.release_in_progress()
        fd = os.open(str(self.lock), os.O_RDWR)
        try:
            # 探测后仍应能立刻拿到独占锁
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def test_non_unix_platform_does_not_block(self):
        """没有 fcntl 的平台必须返回 False（不能静默停摆）。"""
        import builtins
        import os
        os.environ['ZH_RELEASE_LOCK_FILE'] = str(self.lock)
        dailyplan = self._reload()
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == 'fcntl':
                raise ImportError('no fcntl on this platform')
            return real_import(name, *a, **kw)

        builtins.__import__ = fake_import
        try:
            self.assertFalse(dailyplan.release_in_progress())
        finally:
            builtins.__import__ = real_import
