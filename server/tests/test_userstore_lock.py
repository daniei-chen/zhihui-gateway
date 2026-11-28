"""`server/userstore.locked()` 的第一份契约测试。

被测的三层保护（`server/userstore.py`）：

* 进程内：模块级 RLock（`:11`、`:18`）罩住整段读改写；
* 跨进程：每个 target 一个 `.<name>.lock`，`flock` / `msvcrt.locking` 非阻塞
  抢占，失败后每 20ms 重试，5 秒 deadline 到点抛
  `RuntimeError('用户存储正在修改，请稍后重试')`（`:33`~`:47`）；
* 安全：锁文件拒绝 symlink（`:24`）与 `nlink != 1`（`:29`），两者都抛
  `RuntimeError('unsafe user-store lock')`。

断言必须真的争锁：把文件锁 mock 掉以后，「丢更新」和「跨进程互斥」两件事都
无从证明。所以下面全是真实线程与真实子进程。跨进程的同步不用 sleep 猜时间：
子进程**在持锁状态下**写一个 ready 文件，父进程等到文件出现才开始尝试 ——
这等价于跨进程的 Event/Barrier，确保父进程撞上的是别人正持有的锁。

一个实测到的边界：`_mutex` 是模块级 RLock 且横跨 `yield`（`:18` 进、`:50` 出），
所以**同一进程**里两个不同 target 也会串行 —— 进程内不存在「不同 target 临界区
重叠」这回事。target 粒度只能在跨进程观测，见
`test_different_targets_overlap_across_processes`；这一点已在 PR 里单独说明。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import userstore  # noqa: E402

BUSY = '用户存储正在修改，请稍后重试'
UNSAFE = 'unsafe user-store lock'

#: 子进程：拿到 target 的文件锁后写 ready，并一直持锁到父进程写 release。
#: 30 秒硬超时只是为了防止父进程崩掉后留下孤儿进程。
_HOLDER = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from server import userstore
target, ready, release = Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
with userstore.locked(target):
    ready.write_text('locked', encoding='utf-8')
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not release.exists():
        time.sleep(0.02)
'''


class UserStoreLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='userstore-lock-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.target = self.dir / 'users.json'
        self.holders: list[tuple[subprocess.Popen, Path]] = []
        self.addCleanup(self._stop_holders)

    def _stop_holders(self):
        for proc, release in self.holders:
            if proc.poll() is None:
                try:
                    release.write_text('release', encoding='utf-8')
                except OSError:
                    pass
        for proc, _ in self.holders:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def _start_holder(self, target: Path, tag: str = ''):
        """起一个真实子进程持住 target 的锁，返回 (进程, ready, release)。"""
        ready = self.dir / f'holder-ready{tag}'
        release = self.dir / f'holder-release{tag}'
        proc = subprocess.Popen(
            [sys.executable, '-c', _HOLDER, str(ROOT), str(target), str(ready), str(release)],
            cwd=str(ROOT), env={**os.environ, 'PYTHONPATH': str(ROOT)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace')
        self.holders.append((proc, release))
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.01)
        if not ready.exists():
            detail = proc.stderr.read().strip() if proc.poll() is not None else ''
            self.fail(f'子进程没能拿到锁（exit={proc.poll()}）：{detail}')
        return proc, ready, release

    def _atomic_write(self, payload: dict) -> None:
        """按生产写法（`security.save_users`）原子替换，让失败只可能是丢更新。"""
        tmp = self.target.with_name(f'.users.json.{os.getpid()}-{threading.get_ident()}.tmp')
        tmp.write_text(json.dumps(payload), encoding='utf-8')
        os.replace(tmp, self.target)

    def test_concurrent_read_modify_write_loses_no_updates(self):
        """8 线程 × 25 次「读 → 改 → 写」，最终必须是 200，不是「差不多 200」。"""
        self._atomic_write({'count': 0})
        threads = 8
        per_thread = 25
        errors: list[BaseException] = []

        def bump():
            try:
                for _ in range(per_thread):
                    with userstore.locked(self.target):
                        data = json.loads(self.target.read_text(encoding='utf-8'))
                        time.sleep(0.001)
                        self._atomic_write({'count': data['count'] + 1})
            except BaseException as exc:  # 线程里的异常必须带回主线程断言
                errors.append(exc)

        pool = [threading.Thread(target=bump, daemon=True) for _ in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join(120)
        self.assertFalse([thread for thread in pool if thread.is_alive()],
                         '线程没有在 120 秒内结束（疑似死锁）')
        self.assertEqual(errors, [])
        final = json.loads(self.target.read_text(encoding='utf-8'))
        self.assertEqual(final['count'], threads * per_thread,
                         f'丢更新：{threads} 线程 × {per_thread} 次只记到 {final["count"]}')

    def test_cross_process_holder_forces_deadline_runtime_error(self):
        """别人持锁时 locked() 必须在 ~5 秒 deadline 抛 RuntimeError，退出后立刻能进。"""
        started = time.monotonic()
        proc, _, release = self._start_holder(self.target)
        outcome: dict[str, object] = {}

        def attempt():
            begin = time.monotonic()
            try:
                with userstore.locked(self.target):
                    outcome['result'] = 'acquired'
            except BaseException as exc:
                outcome['result'] = exc
            outcome['elapsed'] = time.monotonic() - begin

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(12)
        self.assertFalse(worker.is_alive(), '锁被别人持有时 locked() 没有按 deadline 返回')
        self.assertIsInstance(outcome.get('result'), RuntimeError,
                              f'期望 RuntimeError，实际 {outcome.get("result")!r}')
        self.assertEqual(str(outcome['result']), BUSY)
        elapsed = float(outcome['elapsed'])
        self.assertGreaterEqual(elapsed, 4.5, f'太早放弃：{elapsed:.2f}s')
        self.assertLess(elapsed, 9.0, f'没有在 ~5 秒内放弃：{elapsed:.2f}s')
        self.assertIsNone(proc.poll(), '抛异常时子进程本应仍持着锁')

        release.write_text('release', encoding='utf-8')
        self.assertEqual(proc.wait(timeout=30), 0)
        reentry = time.monotonic()
        with userstore.locked(self.target):
            pass
        self.assertLess(time.monotonic() - reentry, 2.0, '子进程退出后锁没有被释放')
        self.assertLess(time.monotonic() - started, 10.0, '跨进程用例超过 10 秒预算')

    def test_same_thread_reentrancy_on_same_target(self):
        """同线程重入同一 target 直接放行（靠线程本地标记），不能自己等自己。"""
        events: dict[str, object] = {}
        finished = threading.Event()

        def enter():
            try:
                with userstore.locked(self.target):
                    with userstore.locked(self.target):
                        events['depth'] = 'inner'
            except BaseException as exc:
                events['error'] = exc
            finally:
                finished.set()

        worker = threading.Thread(target=enter, daemon=True)
        worker.start()
        self.assertTrue(finished.wait(10), '同线程重入同一个 target 死锁了')
        self.assertNotIn('error', events)
        self.assertEqual(events.get('depth'), 'inner')

    def test_different_targets_overlap_across_processes(self):
        """锁 A 的进程在持锁时，另一个 target 必须能进 —— 每个 target 有自己的锁文件。

        进程内做不到这一点（`_mutex` 横跨 yield），所以用两个进程证明临界区真的
        同时存在：子进程拿着 users.json 的锁，父进程在同一时刻进入 other-users.json
        的临界区并走完，期间子进程还活着、ready 文件还在。
        """
        other = self.dir / 'other-users.json'
        proc, ready, release = self._start_holder(self.target, tag='-a')
        begin = time.monotonic()
        with userstore.locked(other):
            held = time.monotonic() - begin
            self.assertIsNone(proc.poll(), '子进程应仍在持锁')
            self.assertTrue(ready.exists(), '子进程的持锁标记不该消失')
        self.assertLess(held, 2.0, '锁另一个 target 被无关的锁文件挡住了（像是全局锁）')
        self.assertTrue((self.dir / '.users.json.lock').is_file())
        self.assertTrue((self.dir / '.other-users.json.lock').is_file(),
                        '每个 target 应有自己的 .<name>.lock')
        release.write_text('release', encoding='utf-8')
        self.assertEqual(proc.wait(timeout=30), 0)

    def test_symlinked_lock_file_is_refused(self):
        """symlink 锁文件必须拒绝：否则跟随链接就会锁到任意目标上。"""
        lock_path = self.dir / '.users.json.lock'
        victim = self.dir / 'not-a-lock.txt'
        victim.write_text('1', encoding='utf-8')
        try:
            os.symlink(victim, lock_path)
        except (OSError, NotImplementedError) as exc:
            reason = getattr(exc, 'winerror', None)
            if os.name != 'nt':
                # POSIX 上普通用户建 symlink 不会失败。真失败了说明这条安全断言
                # 在 Linux CI 上会变成空门（skip 不算跑过），必须红。
                self.fail(f'POSIX 上无法创建 symlink，安全断言未被验证：{exc!r}')
            self.skipTest(
                f'当前平台不允许普通用户创建 symlink（{type(exc).__name__}'
                f'{f", WinError {reason}" if reason else ""}）；该用例由 Linux CI 执行')
        with self.assertRaises(RuntimeError) as caught:
            with userstore.locked(self.target):
                pass
        self.assertEqual(str(caught.exception), UNSAFE)

    def test_hardlinked_lock_file_is_refused(self):
        """nlink != 1 的锁文件必须拒绝：同一个锁可以被两个名字删改。"""
        lock_path = self.dir / '.users.json.lock'
        lock_path.write_text('1', encoding='utf-8')
        try:
            os.link(lock_path, self.dir / 'lock-copy')
        except OSError as exc:
            if os.name != 'nt':
                self.fail(f'POSIX 上无法创建硬链接，nlink 断言未被验证：{exc!r}')
            self.skipTest(f'当前平台不允许创建硬链接（{type(exc).__name__}: {exc}）')
        with self.assertRaises(RuntimeError) as caught:
            with userstore.locked(self.target):
                pass
        self.assertEqual(str(caught.exception), UNSAFE)


if __name__ == '__main__':
    unittest.main()
