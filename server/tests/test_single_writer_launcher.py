"""单写者契约：`serve.py` 必须拒绝多 worker，并且一个数据目录只允许一个实例。

为什么值得钉住：`filelock` 的互斥是**进程内** RLock（只有 `writing=True` 才用内核锁），
限额/准入/密钥槽位也都在进程内存里。所以第二个实例不是「多一份容量」，而是把
issue #210/#187 的丢更新重新引进来，外加额度按进程放大。这两条以前只写在注释里，
没有任何东西拦得住 —— 这里把它变成会失败的测试。

用的是真子进程争抢，不是 mock：租约的价值恰恰在于「另一个进程」这个前提。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import instancelease  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

# 子进程持锁者：拿真租约、报 pid、然后一直持到父进程关掉 stdin。
_HOLDER = """
import sys, json
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from server.services import instancelease
lease = instancelease.acquire(Path(sys.argv[2]))
print(json.dumps({'pid': __import__('os').getpid()}), flush=True)
sys.stdin.readline()
lease.release()
print('RELEASED', flush=True)
"""


def _launcher(data_dir: Path, *extra: str, timeout: float = 60) -> subprocess.CompletedProcess:
    env = {**os.environ, 'WB_DATA_DIR': str(data_dir), 'PYTHONIOENCODING': 'utf-8'}
    return subprocess.run(
        [sys.executable, '-B', '-m', 'server.serve', '--host', '127.0.0.1', '--port', '0', *extra],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=timeout)


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _inflight(port: int) -> dict | None:
    """只回环的只读探针：200 即代表进程已经在服务，且不碰数据库写路径。"""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health/inflight',
                                    timeout=1.0) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode('utf-8', 'replace'))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return None


def _wait_inflight(port: int, log_path: Path, limit: float = 240.0) -> dict:
    began = time.monotonic()
    while time.monotonic() - began < limit:
        got = _inflight(port)
        if got is not None:
            return got
        time.sleep(0.25)
    tail = ''
    try:
        tail = log_path.read_text(encoding='utf-8', errors='replace')[-1500:]
    except OSError:
        pass
    raise AssertionError(f':{port} 在 {limit:.0f}s 内没有开始服务；日志尾部：\n{tail}')


class _TempDataDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix='swo65-lease-test-',
                                                ignore_cleanup_errors=True)
        self.data = Path(self._tmp.name) / 'data'

    def tearDown(self) -> None:
        self._tmp.cleanup()


class WorkerCountTests(_TempDataDir):
    """`--workers N>1` 必须被当场拒绝，并说明原因（不是文档里写一句）。"""

    def test_multi_worker_is_refused_with_the_reason(self) -> None:
        result = _launcher(self.data, '--workers', '2')
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn('必须单进程', output)
        self.assertIn('额度放大 N 倍', output)
        self.assertIn('#210', output)
        self.assertNotIn('unrecognized arguments', output,
                         '只是参数没认出来 —— 等于没解释为什么不能加 worker')

    def test_single_worker_still_starts_past_the_flag_parsing(self) -> None:
        """`--workers 1` 不能被新参数误伤：它应当走到租约这一步。"""
        with instancelease.acquire(self.data):
            result = _launcher(self.data, '--workers', '1')
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn('拒绝启动', output)
        self.assertNotIn('必须单进程', output)


class InstanceLeaseTests(_TempDataDir):
    """同一数据目录上第二个实例必须拒绝服务，并说出是谁占着。"""

    def test_launcher_refuses_while_another_process_holds_the_lease(self) -> None:
        holder = subprocess.Popen(
            [sys.executable, '-c', _HOLDER, str(ROOT), str(self.data)],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        try:
            line = holder.stdout.readline()
            pid = json.loads(line)['pid']
            result = _launcher(self.data, timeout=90)
        finally:
            try:
                holder.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                holder.wait(timeout=20)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=30)
            holder.stdout.close()
            holder.stderr.close()

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn('拒绝启动', output)
        self.assertIn(f'pid={pid}', output, '拒绝消息没说清是谁占着租约')
        self.assertNotIn('未知进程', output, 'holder 元数据读不到 —— 被拒绝的一方无从排查')

    def test_stale_files_do_not_block_a_new_instance(self) -> None:
        """上一个进程被 kill 之后，留下的租约文件不能把服务挡在门外。"""
        self.data.mkdir(parents=True)
        instancelease.holder_path(self.data).write_text(
            json.dumps({'pid': 999999, 'host': 'gone', 'started': 0}), encoding='utf-8')
        instancelease.lease_path(self.data).write_text('', encoding='utf-8')
        with instancelease.acquire(self.data):
            self.assertIn('pid=%d' % os.getpid(),
                          instancelease._holder_text(self.data))

    def test_lease_released_by_a_dying_holder_can_be_taken_again(self) -> None:
        holder = subprocess.Popen(
            [sys.executable, '-c', _HOLDER, str(ROOT), str(self.data)],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        json.loads(holder.stdout.readline())
        with self.assertRaises(instancelease.LeaseBusy):
            instancelease.acquire(self.data)
        holder.kill()
        holder.wait(timeout=30)
        holder.stdout.close()
        holder.stdin.close()
        holder.stderr.close()

        deadline = time.monotonic() + 20
        while True:
            try:
                with instancelease.acquire(self.data):
                    break
            except instancelease.LeaseBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def test_holder_metadata_names_the_live_holder(self) -> None:
        with instancelease.acquire(self.data):
            text = instancelease._holder_text(self.data)
        self.assertIn('pid=%d' % os.getpid(), text)
        self.assertIn('host=', text)
        self.assertIn('启动于', text)

    @unittest.skipUnless(os.name != 'nt', 'Windows 上创建符号链接需要额外权限')
    def test_symlinked_lease_is_refused(self) -> None:
        self.data.mkdir(parents=True)
        target = Path(self._tmp.name) / 'elsewhere.lease'
        target.write_text('', encoding='utf-8')
        instancelease.lease_path(self.data).symlink_to(target)
        with self.assertRaises(RuntimeError):
            instancelease.acquire(self.data)


class _Serving:
    """一个**真的在服务**的 `serve.py` 实例：真端口、真进程、真数据目录。"""

    def __init__(self, data_dir: Path, port: int | None = None) -> None:
        self.port = port or _free_port()
        self.log_path = Path(data_dir).parent / f'instance-{self.port}.log'
        self._log = self.log_path.open('w', encoding='utf-8', errors='replace')
        env = {**os.environ, 'WB_DATA_DIR': str(data_dir),
               'WB_STATIC_DIR': str(Path(data_dir).parent / 'web'),
               'WB_ADMIN_PASSWORD': 'single-writer-test-9f',
               'PYTHONIOENCODING': 'utf-8'}
        self.proc = subprocess.Popen(
            [sys.executable, '-B', '-m', 'server.serve',
             '--host', '127.0.0.1', '--port', str(self.port)],
            cwd=str(ROOT), env=env, stdout=self._log, stderr=subprocess.STDOUT)

    def wait_ready(self) -> dict:
        return _wait_inflight(self.port, self.log_path)

    def log_tail(self, size: int = 1500) -> str:
        try:
            return self.log_path.read_text(encoding='utf-8', errors='replace')[-size:]
        except OSError:
            return ''

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=30)
        self._log.close()


class LeaseLifetimeTests(_TempDataDir):
    """租约必须覆盖**整个进程生命周期**，不是「启动那一刻握一下」。

    为什么单独一条：把 `with lease:` 写成「先 `release()` 再进 with」——一次看起来
    完全无害的重构（比如为了「别一直占着锁」）——会让租约在启动后立刻失效，而
    上面那些用例全都还是绿的：它们的第二个实例是被**测试进程自己**持有的租约挡住的，
    跟服务进程有没有一直握着租约无关。这条用例盯的正是那个差别。
    """

    def test_a_serving_instance_keeps_the_lease_until_it_exits(self) -> None:
        first = _Serving(self.data)
        try:
            first.wait_ready()
            for attempt in (1, 2):
                blocked = _launcher(self.data, timeout=90)
                output = blocked.stdout + blocked.stderr
                self.assertNotEqual(blocked.returncode, 0, output)
                self.assertIn('拒绝启动', output)
                self.assertIn(f'pid={first.proc.pid}', output,
                              f'第 {attempt} 次被拒绝时没说清是谁占着')
                self.assertIsNone(first.proc.poll(),
                                  '第一个实例在自己还活着的时候就丢了租约')
                self.assertIsNotNone(_inflight(first.port),
                                     f'第 {attempt} 次尝试之后第一个实例已经不在服务了')
                with self.assertRaises(instancelease.LeaseBusy):
                    instancelease.acquire(self.data)
                time.sleep(1.0)
        finally:
            first.stop()

        port = _free_port()
        successor = _Serving(self.data, port)
        try:
            info = successor.wait_ready()
            self.assertEqual(info.get('total_in_flight'), 0)
        finally:
            successor.stop()



if __name__ == "__main__":
    unittest.main()
