"""systemd socket activation 的取 fd 逻辑。

为什么值得测：这段代码的失败模式是**服务起不来**（全站不可用），而且只在
装了 socket 单元的生产机上才会走到。没测就等于把「装完能不能起」赌在运气上。

三个必须守住的分支：
* 没有 systemd 变量 → 返回 None，走原来的自己 bind（装与不装都安全）；
* `LISTEN_PID` 与当前进程不符 → 返回 None（变量被父进程继承，fd 3 不是我们的）；
* fd 数量不是 1 → 直接退出，不猜。
"""
from __future__ import annotations

import os
import socket
import unittest
from pathlib import Path
from unittest import mock

from server import serve

#: 仓库根（子进程的 cwd 要给对，否则 import 不到 server 包）。
ROOT = Path(__file__).resolve().parents[2]


class InheritedListenerTest(unittest.TestCase):
    def setUp(self) -> None:
        for name in ('LISTEN_PID', 'LISTEN_FDS', 'LISTEN_FDNAMES'):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)

    def test_no_systemd_environment_returns_none(self) -> None:
        self.assertIsNone(serve.inherited_listener())

    def test_foreign_pid_is_ignored(self) -> None:
        # 变量是从父进程继承来的：fd 3 不是监听 socket，不能用。
        os.environ['LISTEN_PID'] = str(os.getpid() + 1)
        os.environ['LISTEN_FDS'] = '1'
        self.assertIsNone(serve.inherited_listener())

    def test_own_pid_without_fds_is_ignored(self) -> None:
        os.environ['LISTEN_PID'] = str(os.getpid())
        self.assertIsNone(serve.inherited_listener())

    def test_more_than_one_fd_refuses_to_guess(self) -> None:
        os.environ['LISTEN_PID'] = str(os.getpid())
        os.environ['LISTEN_FDS'] = '2'
        with self.assertRaises(SystemExit):
            serve.inherited_listener()

    def test_valid_activation_returns_socket_and_scrubs_environment(self) -> None:
        os.environ['LISTEN_PID'] = str(os.getpid())
        os.environ['LISTEN_FDS'] = '1'
        os.environ['LISTEN_FDNAMES'] = 'workbuddy-web'
        fake = mock.Mock(spec=socket.socket)
        with mock.patch.object(serve.socket, 'fromfd', return_value=fake) as fromfd:
            listener = serve.inherited_listener()
        self.assertIs(listener, fake)
        fromfd.assert_called_once_with(3, socket.AF_INET, socket.SOCK_STREAM)
        fake.set_inheritable.assert_called_once_with(False)
        # 契约只在本进程内成立：不能把"我拿到了激活 socket"传给子进程。
        self.assertNotIn('LISTEN_PID', os.environ)
        self.assertNotIn('LISTEN_FDS', os.environ)
        self.assertNotIn('LISTEN_FDNAMES', os.environ)


class MainWiringTest(unittest.TestCase):
    """`main()` 必须把 socket 交给 uvicorn，而不是继续自己 bind。"""

    def _run_main(self, listener: object) -> tuple[object, mock.Mock]:
        server = mock.Mock()
        with mock.patch.object(serve, 'inherited_listener', return_value=listener), \
                mock.patch.object(serve.uvicorn, 'Config') as config_cls, \
                mock.patch.object(serve, 'DrainServer', return_value=server), \
                mock.patch.object(serve.signal, 'signal'):
            serve.main(['--host', '127.0.0.1', '--port', '7866',
                        '--timeout-graceful-shutdown', '75'])
        return config_cls, server

    def test_socket_activation_passes_listener_to_uvicorn(self) -> None:
        fake = mock.Mock(spec=socket.socket)
        config_cls, server = self._run_main(fake)
        server.run.assert_called_once_with(sockets=[fake])
        # 拿到了 fd 就不该再让 uvicorn 自己按 host/port bind。
        kwargs = config_cls.call_args.kwargs
        self.assertNotIn('host', kwargs)
        self.assertNotIn('port', kwargs)

    def test_without_activation_keeps_binding_by_port(self) -> None:
        config_cls, server = self._run_main(None)
        server.run.assert_called_once_with()
        kwargs = config_cls.call_args.kwargs
        self.assertEqual(kwargs.get('host'), '127.0.0.1')
        self.assertEqual(kwargs.get('port'), 7866)


@unittest.skipIf(os.name != 'posix', 'fd 传递是 POSIX 语义，Windows 上不适用')
class RealFdHandoverTest(unittest.TestCase):
    """真把监听 socket 摆到 fd 3 上，看子进程能不能拿到。

    上面的用例把 `socket.fromfd` 打了桩，验的是"我们有没有按约定调用"；这一条
    验的是"约定本身在真实进程间成立"。区别很实际：`fromfd` 的 family/type 参数
    只在创建 socket 对象时有意义，一旦传错（比如照抄 uvicorn 源码里的 AF_UNIX）
    就可能拿到一个能构造、但 accept 行为诡异的对象 —— 打桩测不出来。

    只在 Linux CI 跑（门禁在 ubuntu 上）。本地 Windows 跳过。
    """

    def test_child_process_picks_up_inherited_listener(self) -> None:
        import subprocess
        import sys
        import textwrap

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        port = listener.getsockname()[1]

        # 子进程里把监听 socket 挪到 fd 3，再执行 inherited_listener()。
        script = textwrap.dedent('''
            import os, socket, sys
            os.environ['LISTEN_PID'] = str(os.getpid())
            os.environ['LISTEN_FDS'] = '1'
            from server.serve import inherited_listener
            sock = inherited_listener()
            if sock is None:
                print('NONE'); sys.exit(0)
            print('PORT', sock.getsockname()[1], sock.family == socket.AF_INET)
        ''')
        proc = subprocess.run(
            [sys.executable, '-c', script], capture_output=True, text=True, timeout=60,
            close_fds=False,
            preexec_fn=lambda: os.dup2(listener.fileno(), 3),
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f'PORT {port} True', proc.stdout, proc.stdout + proc.stderr)


if __name__ == '__main__':
    unittest.main()
