"""filelock：按绝对路径串行化「读—改—写」（issue #210 / #187 的丢更新）。

这层保护的价值只有一种证据能证明：**把锁换成 `nullcontext()`，同一段并发代码
就必须坏掉**。所以这里的用例成对出现 —— 同一交错下，有锁的得到确定结果，
没锁的必须丢更新。真实线程、真实子进程、真实文件，不用 mock 替身。

`writing=True` 的语义按 `credentiallease.mutation()` 的实际实现断言，不臆造：

* 它另开 `<文件名>.refresh.lock` 拿**内核**锁（POSIX `flock` / Windows
  `msvcrt.locking`），拿不到且预算用尽时抛 `OSError(EBUSY)`；
* `timeout=0` 是「不排队」：别人持锁就当场拒绝，这正是管理写入不阻塞事件循环的
  做法；
* 同一 owner（同一线程 / 同一 asyncio 任务）对同一路径重入时直接放行，不再碰
  内核锁，所以嵌套 `writing=True` 不会被自己判成 busy；
* `writing=False` 只有进程内 RLock，别的进程持着内核锁也照样能读。
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import filelock  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

# 子进程里的持锁者：拿真内核锁、报一声 HELD，然后一直持到父进程关掉 stdin。
# 父进程靠 HELD 这行确认「锁真的在别人手里」，不靠 sleep 猜。
_HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from server.services import credentiallease
with credentiallease.mutation(Path(sys.argv[2]), timeout=15):
    print('HELD', flush=True)
    sys.stdin.readline()
print('RELEASED', flush=True)
"""


def _read_modify_write(path: Path, *, pause: float = 0.0) -> int:
    """账号文件写者的真实形状：整份读 → 改一个键 → 原子替换。

    原子替换只保证文件不会写坏，不保证更新不丢 —— 快照是在 `pause` 之前读的。
    """
    data = json.loads(path.read_text(encoding='utf-8'))
    if pause:
        time.sleep(pause)
    data['count'] += 1
    staging = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    staging.write_text(json.dumps(data), encoding='utf-8')
    os.replace(staging, path)
    return data['count']


@contextmanager
def _cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextmanager
def _kernel_lock_holder(path: Path, release_after: float | None = None):
    """起一个**真子进程**持住内核锁；yield 出去时它确实在持有。

    `release_after` 给了就让子进程在那么多秒后释放（用来验证等待预算），
    没给就一直持到退出这个 `with`。无论哪条路，子进程都是自己正常退出的。
    """
    proc = subprocess.Popen(
        [sys.executable, '-c', _HOLDER, str(ROOT), str(path)],
        cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    timer = None
    try:
        line = proc.stdout.readline()
        if line.strip() != 'HELD':
            proc.kill()
            proc.wait(timeout=30)
            raise AssertionError('子进程没拿到内核锁：stdout=%r stderr=%r'
                                 % (line, proc.stderr.read()[:2000]))
        if release_after is not None:
            def release() -> None:
                try:
                    proc.stdin.write('\n')
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
            timer = threading.Timer(release_after, release)
            timer.daemon = True
            timer.start()
        yield proc
    finally:
        if timer is not None:
            timer.cancel()
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()


class _TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix='filelock-test-')
        self.dir = Path(self._tmp.name)
        self.path = self.dir / 'account.json'
        self.path.write_text(json.dumps({'count': 0, 'proxy': '', 'weight': 1}),
                             encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _count(self, path: Path | None = None) -> int:
        target = path or self.path
        return json.loads(target.read_text(encoding='utf-8'))['count']


class LostUpdateTests(_TempDirTest):
    """模块存在的理由：并发写者不丢更新。"""

    def test_eight_threads_by_twentyfive_bumps_keep_every_update(self) -> None:
        writers, rounds = 8, 25
        start = threading.Barrier(writers)
        failures: list[BaseException] = []

        def worker() -> None:
            try:
                start.wait(30)
                for _ in range(rounds):
                    with filelock.file_lock(self.path):
                        _read_modify_write(self.path, pause=0.002)
            except BaseException as exc:          # noqa: BLE001 —— 线程里的异常要带回主线程
                failures.append(exc)

        threads = [threading.Thread(target=worker, name=f'writer-{i}')
                   for i in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)

        self.assertEqual([str(exc) for exc in failures], [])
        self.assertFalse([t.name for t in threads if t.is_alive()], '有写者没退出')
        self.assertEqual(self._count(), writers * rounds,
                         '丢更新：%d 次读-改-写只留下 %d 次' % (writers * rounds, self._count()))
        self.assertEqual(sorted(p.name for p in self.dir.glob('*.tmp')), [],
                         '临时文件没被替换掉')

    def test_the_same_interleaving_loses_an_update_without_the_lock(self) -> None:
        """对照组：证明上面那条断言真的依赖 file_lock，不是文件 IO 恰好串行。

        两个写者都先读、都读到旧快照（Barrier 保证谁都没写），再各自写回 ——
        后写者用旧快照盖掉前一个的 `count`。这里必须只剩 1 次。
        """
        both_read = threading.Barrier(2)
        write_io = threading.Lock()          # 只串行化写入动作，不串行化「读—改」这个决定
        failures: list[BaseException] = []

        def writer() -> None:
            try:
                with nullcontext():              # 等价于「没有这层保护」
                    data = json.loads(self.path.read_text(encoding='utf-8'))
                    both_read.wait(30)
                    data['count'] += 1
                    staging = self.path.with_name(f'{self.path.name}.{threading.get_ident()}.tmp')
                    staging.write_text(json.dumps(data), encoding='utf-8')
                    with write_io:
                        os.replace(staging, self.path)
            except BaseException as exc:          # noqa: BLE001
                failures.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        self.assertEqual([str(exc) for exc in failures], [])
        self.assertEqual(self._count(), 1,
                         '两个写者没有互相覆盖 —— 这个对照组失效了，'
                         '上面「有锁不丢更新」的用例也就证明不了任何东西')


class ReentrancyTests(_TempDirTest):
    """同一路径可重入（RLock），但重入不等于放行别的线程。"""

    def test_nested_acquisition_on_the_same_path_does_not_deadlock(self) -> None:
        seen: list[str] = []

        def worker() -> None:
            with filelock.file_lock(self.path):
                with filelock.file_lock(self.path):
                    with filelock.file_lock(self.path):
                        seen.append('inner')

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(15)
        self.assertFalse(thread.is_alive(),
                         '同线程重入把自己锁死了：RLock 被换成了 Lock')
        self.assertEqual(seen, ['inner'])

    def test_reentrant_holder_still_excludes_other_threads(self) -> None:
        # 重入侧跑在守护线程里：RLock 一旦被换成 Lock，这里必须是「超时判失败」，
        # 而不是把整个测试进程挂死。
        nested = threading.Event()
        release = threading.Event()
        entered = threading.Event()

        def reentrant() -> None:
            with filelock.file_lock(self.path):
                with filelock.file_lock(self.path):
                    nested.set()
                    release.wait(30)

        def other() -> None:
            with filelock.file_lock(self.path):
                entered.set()

        reentrant_thread = threading.Thread(target=reentrant, daemon=True)
        reentrant_thread.start()
        self.assertTrue(nested.wait(20), '同线程重入没进到内层：RLock 被换成了 Lock')
        other_thread = threading.Thread(target=other, daemon=True)
        other_thread.start()
        other_thread.join(0.4)
        self.assertFalse(entered.is_set(), '重入把锁变成了空操作')
        release.set()
        reentrant_thread.join(20)
        self.assertFalse(reentrant_thread.is_alive())
        self.assertTrue(entered.wait(20), '外层退出后另一个线程仍然进不来')
        other_thread.join(20)

    def test_nested_writing_lock_is_not_refused_as_busy(self) -> None:
        sidecar = self.path.with_name(self.path.name + '.refresh.lock')
        seen: list[bool] = []

        def worker() -> None:
            with filelock.file_lock(self.path, writing=True):
                with filelock.file_lock(self.path, writing=True):
                    seen.append(sidecar.exists())

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(20)
        self.assertFalse(thread.is_alive(),
                         '嵌套 writing=True 卡住了：重入被判成竞争')
        self.assertEqual(seen, [True], '写路径没有落到 credentiallease 的内核锁上')


class IndependentPathTests(_TempDirTest):
    """锁的粒度是单个文件：不同路径必须能并行。"""

    def test_two_paths_hold_their_critical_sections_at_the_same_time(self) -> None:
        first = self.dir / 'first.json'
        second = self.dir / 'second.json'
        for path in (first, second):
            path.write_text(json.dumps({'count': 0}), encoding='utf-8')
        overlap = threading.Barrier(2)
        failures: list[BaseException] = []

        def worker(path: Path) -> None:
            try:
                with filelock.file_lock(path):
                    # 只有两段临界区真重叠，两边才能一起过这个栅栏。
                    overlap.wait(5)
            except BaseException as exc:          # noqa: BLE001
                failures.append(exc)

        threads = [threading.Thread(target=worker, args=(path,))
                   for path in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertEqual([repr(exc) for exc in failures], [],
                         '两个不同路径的临界区没能重叠：锁退化成了一把全局锁')

    def test_same_path_critical_sections_never_overlap(self) -> None:
        holding = threading.Event()
        release = threading.Event()
        contender_entered = threading.Event()
        spans: dict[str, tuple[float, float]] = {}

        def holder() -> None:
            with filelock.file_lock(self.path):
                holding.set()
                begin = time.monotonic()
                release.wait(15)
                spans['holder'] = (begin, time.monotonic())

        def contender() -> None:
            with filelock.file_lock(self.path):
                spans['contender'] = (time.monotonic(), time.monotonic())
                contender_entered.set()

        holder_thread = threading.Thread(target=holder, daemon=True)
        holder_thread.start()
        self.assertTrue(holding.wait(15), '持锁者没进临界区')
        contender_thread = threading.Thread(target=contender, daemon=True)
        contender_thread.start()
        contender_thread.join(0.4)
        self.assertFalse(contender_entered.is_set(),
                         '同一路径的临界区重叠了：前一个还没退出，后一个就拿到了锁')
        release.set()
        holder_thread.join(30)
        self.assertTrue(contender_entered.wait(15), '持锁者退出后后到者仍然进不来')
        contender_thread.join(15)
        self.assertLessEqual(spans['holder'][1], spans['contender'][0],
                             '后到者的进入时间早于持锁者的退出时间')


class WritingLockTests(_TempDirTest):
    """`writing=True` 委托 credentiallease.mutation：真内核锁，跨进程生效。"""

    def test_a_second_process_cannot_take_a_writing_lock(self) -> None:
        with _kernel_lock_holder(self.path) as proc:
            started = time.monotonic()
            with self.assertRaises(OSError) as caught:
                with filelock.file_lock(self.path, writing=True, timeout=0):
                    self.fail('另一个进程持着内核锁，这里不该拿到写锁')
            elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.errno, errno.EBUSY,
                         '拒绝的理由必须是「忙」：%r' % (caught.exception,))
        self.assertLess(elapsed, 3, 'timeout=0 应该当场拒绝，不该排队')
        self.assertEqual(proc.returncode, 0, '持锁者自己挂了，这次竞争不算数')

        # 竞争消失后同一把锁必须拿得回来 —— 证明拒绝的是竞争，不是永久坏掉。
        with filelock.file_lock(self.path, writing=True, timeout=10):
            pass

    def test_writing_lock_with_a_budget_waits_for_the_holder(self) -> None:
        with _kernel_lock_holder(self.path, release_after=0.6) as proc:
            started = time.monotonic()
            with filelock.file_lock(self.path, writing=True, timeout=10):
                waited = time.monotonic() - started
        self.assertEqual(proc.returncode, 0)
        self.assertGreaterEqual(waited, 0.3,
                                '给了预算却立刻就拿到了锁：内核锁根本没被争用')
        self.assertLess(waited, 9)

    def test_plain_read_lock_does_not_need_the_kernel_lock(self) -> None:
        with _kernel_lock_holder(self.path):
            started = time.monotonic()
            with filelock.file_lock(self.path):
                pass
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5,
                        'writing=False 也被内核锁挡住了：只读快照会跟着写者一起卡住')

    def test_only_the_writing_lock_creates_the_sidecar(self) -> None:
        sidecar = self.path.with_name(self.path.name + '.refresh.lock')
        with filelock.file_lock(self.path):
            pass
        self.assertFalse(sidecar.exists(), '只读路径不该碰内核锁文件')
        with filelock.file_lock(self.path, writing=True):
            self.assertTrue(sidecar.exists(), '写路径没有落到 credentiallease 的内核锁上')

    @unittest.skipUnless(os.name != 'nt', 'Windows 上创建符号链接需要额外权限')
    def test_a_symlinked_credential_is_refused(self) -> None:
        link = self.dir / 'linked.json'
        link.symlink_to(self.path)
        with self.assertRaises(OSError) as caught:
            with filelock.file_lock(link, writing=True):
                self.fail('符号链接不该被当作凭证写入目标')
        self.assertEqual(caught.exception.errno, errno.EINVAL)


class PathNormalizationTests(_TempDirTest):
    """同一份文件的相对 / 绝对两种写法必须命中同一把锁。"""

    def test_relative_and_absolute_spellings_share_one_lock(self) -> None:
        entered = threading.Event()

        def worker(path: Path) -> None:
            with filelock.file_lock(path):
                entered.set()

        with _cwd(self.dir):
            relative = Path(self.path.name)
            self.assertNotEqual(str(relative), str(self.path))
            with filelock.file_lock(relative):
                thread = threading.Thread(target=worker, args=(self.path,), daemon=True)
                thread.start()
                thread.join(0.4)
                self.assertFalse(entered.is_set(),
                                 '相对/绝对被当成两把锁：同一份文件出现两个写者')
            self.assertTrue(entered.wait(15))
            thread.join(15)

    def test_absolute_holder_blocks_the_relative_spelling(self) -> None:
        entered = threading.Event()

        def worker(path: Path) -> None:
            with filelock.file_lock(path):
                entered.set()

        with _cwd(self.dir):
            relative = Path(self.path.name)
            with filelock.file_lock(self.path):
                thread = threading.Thread(target=worker, args=(relative,), daemon=True)
                thread.start()
                thread.join(0.4)
                self.assertFalse(entered.is_set(),
                                 '绝对路径持锁时相对写法溜进去了')
            self.assertTrue(entered.wait(15))
            thread.join(15)

    def test_writing_lock_normalizes_the_same_way(self) -> None:
        """两把 RLock 的话，后到者会当场 EBUSY；同一把锁才会排队。"""
        entered = threading.Event()
        failures: list[BaseException] = []

        def worker(path: Path) -> None:
            try:
                with filelock.file_lock(path, writing=True, timeout=0):
                    entered.set()
            except BaseException as exc:          # noqa: BLE001
                failures.append(exc)

        with _cwd(self.dir):
            relative = Path(self.path.name)
            with filelock.file_lock(relative, writing=True):
                thread = threading.Thread(target=worker, args=(self.path,), daemon=True)
                thread.start()
                thread.join(0.4)
                self.assertFalse(entered.is_set())
            thread.join(15)

        self.assertEqual([repr(exc) for exc in failures], [],
                         '绝对写法走了另一把锁，被内核锁判成 EBUSY：路径没归一')
        self.assertTrue(entered.is_set())


if __name__ == '__main__':
    unittest.main()
