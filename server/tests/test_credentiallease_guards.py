"""`credentiallease._open` 的四道文件守卫，每条都要有一条真能红的用例。

## 为什么单独一个模块

`_open()` 在打开 `<文件名>.refresh.lock` 之后连做四道检查，任何一道不过都拒绝
这次凭证写入：

| 检查 | 行 | 生效平台 |
| --- | --- | --- |
| 凭证文件本身是符号链接 → `EINVAL` | `credentiallease.py:25-26` | 全平台 |
| 锁 sidecar 是符号链接 → `EINVAL` | `:31-32` | 全平台 |
| 锁不是普通文件、或 `st_nlink != 1` → `EINVAL` | `:37-38` | **全平台** |
| 锁的 `st_mode & 0o077`（组/其他可写）→ `EPERM` | `:40-41` | 仅 POSIX |
| 锁的属主与凭证不一致 → `EPERM` | `:44-45` | 仅 POSIX |

2026-11-28 的覆盖度审计：把这些检查逐条删掉再跑 77 条相关用例（`test_filelock`
/ `test_v007_refresh` / `test_merge_read_integrity` / `test_token_renew` /
`test_refresh_all_tokens` / `test_refresh_plan`），**全绿** —— 也就是说这几道
守卫此前一条测试都没有；唯一的例外是凭证符号链接，它只有一条 Linux-only 的
用例（`test_filelock.py` 里 `skipUnless(os.name != 'nt')`）。

`test_portal_closeout.py` 里也有 `0o644 → ValueError` 和硬链接 → `ValueError`
的用例，看起来正好覆盖权限位与 nlink —— 那打的是 `recovery` 的快照校验器，
不是 `_open` 这里。**同名判据不等于同一实现**，改一处不会让另一处红。

## 四条用例的平台分布

- 硬链接、属主：**全平台跑**。`st_nlink` 是唯一在 Windows 上也生效的检查，
  而它恰好此前零覆盖，所以这两条必须在两个平台上都真跑。
- 权限位、符号链接：`skipUnless(os.name != 'nt')`。`0o077` 检查本身就在
  `if os.name != 'nt'` 里，Windows 上创建符号链接也需要额外权限 —— 这两条
  在 Linux CI 上真跑，在 Windows 上如实跳过而不是假装通过。

## 属主那条为什么不 chown

真的把 sidecar `chown` 给别人需要 root，普通开发者与 CI 都跑不了。所以这条
用例把 `credentiallease` 看到的 `os` 换成一份 POSIX 形态的替身：`name='posix'`、
`geteuid()` 非 0（否则会走 `fchown` 分支）、`fstat()` 返回一个属主与凭证不同的
`stat_result`。被替换的只有 `_open` 读文件属性这一路，锁本身仍是真实的
`os.open` / `os.close`，所以它证的是 `_open` 的判定，不是 mock 之间的对话。
"""
from __future__ import annotations

import errno
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import credentiallease  # noqa: E402

POSIX_ONLY = 'Windows 上 permisssion 位与符号链接都不具备可测语义（检查本身也在 os.name != nt 分支里）'


class _LeaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix='credentiallease-guard-',
                                                ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)
        self.cred = self.dir / 'account.json'
        self.cred.write_text(json.dumps({'count': 0}), encoding='utf-8')
        self.sidecar = self.dir / 'account.json.refresh.lock'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _use_lease_once(self) -> None:
        """先正常拿一次锁，让 sidecar 按实现自己的方式出现（不手搓）。"""
        with credentiallease.mutation(self.cred):
            pass
        self.assertTrue(self.sidecar.exists(), '一次正常加锁之后 sidecar 应该存在')

    def _refusal(self, path: Path | None = None):
        with self.assertRaises(OSError) as caught:
            with credentiallease.mutation(path or self.cred):
                self.fail('这道守卫没拦住：临界区被放进去了')
        return caught.exception


class HardlinkGuardTests(_LeaseCase):
    """`st_nlink != 1`：锁文件被硬链接到别处，说明它在自己看不见的地方也被引用。"""

    def test_hardlinked_lock_sidecar_is_refused(self) -> None:
        self._use_lease_once()
        os.link(self.sidecar, self.dir / 'unrelated-name')
        self.assertEqual(os.stat(self.sidecar).st_nlink, 2)

        exc = self._refusal()
        self.assertEqual(exc.errno, errno.EINVAL, '拒绝的理由必须是「文件不安全」：%r' % (exc,))
        self.assertIn('unsafe credential lock', str(exc))

    def test_single_link_lock_sidecar_still_works(self) -> None:
        """反向对照：同一个 sidecar、nlink=1 时照样能进临界区。

        没有这条，上面那条在「任何加锁都被拒」的实现下也会绿。
        """
        self._use_lease_once()
        self.assertEqual(os.stat(self.sidecar).st_nlink, 1)
        with credentiallease.mutation(self.cred):
            pass


class OwnershipGuardTests(_LeaseCase):
    """属主不一致：锁文件属于别人，写进去等于替别人改凭证。"""

    def test_lock_owned_by_another_user_is_refused(self) -> None:
        self._use_lease_once()
        source = self.cred.stat()
        foreign_uid = (source.st_uid + 12345) % (2 ** 32)

        # 只替换 `_open` 读属性那一路：文件是真的，`os.open` / `os.close` 也是真的。
        fake_stat = os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, foreign_uid, 0, 0, 0, 0, 0))
        fake_os = types.SimpleNamespace(
            name='posix',                       # 走属主分支（Windows 上该分支不执行）
            O_CREAT=os.O_CREAT, O_RDWR=os.O_RDWR,
            O_NOFOLLOW=getattr(os, 'O_NOFOLLOW', 0),
            O_NONBLOCK=getattr(os, 'O_NONBLOCK', 0),
            open=os.open, close=os.close, lseek=os.lseek,
            geteuid=lambda: 1000,               # 非 0：不走 fchown 那条
            fchown=lambda *args: None,
            fstat=lambda descriptor: fake_stat,
        )
        with mock.patch.object(credentiallease, 'os', fake_os):
            exc = self._refusal()

        self.assertEqual(exc.errno, errno.EPERM, '属主不一致必须按权限拒绝：%r' % (exc,))
        self.assertIn('ownership mismatch', str(exc))

    def test_matching_owner_is_not_refused(self) -> None:
        """反向对照：属主与凭证一致（且权限位干净）时不该被这条拦住。"""
        self._use_lease_once()
        source = self.cred.stat()
        fake_stat = os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, source.st_uid, 0, 0, 0, 0, 0))
        fake_os = types.SimpleNamespace(
            name='posix', O_CREAT=os.O_CREAT, O_RDWR=os.O_RDWR,
            O_NOFOLLOW=getattr(os, 'O_NOFOLLOW', 0),
            O_NONBLOCK=getattr(os, 'O_NONBLOCK', 0),
            open=os.open, close=os.close, lseek=os.lseek,
            geteuid=lambda: 1000, fchown=lambda *args: None,
            fstat=lambda descriptor: fake_stat,
        )
        # 这里只证明 `_open` 的属主判定不误伤；真正的加锁走 POSIX，Windows 上没有 fcntl。
        if os.name == 'nt':
            self.skipTest('Windows 上没有 fcntl，对照只验到 _open 的属主判定')
        with mock.patch.object(credentiallease, 'os', fake_os):
            with credentiallease.mutation(self.cred):
                pass


class PermissionBitsPatchedGuardTests(_LeaseCase):
    """同一条 `0o077` 检查，用 POSIX 形态的 `fstat` 替身在任何平台证明它咬得住。

    真实 chmod 的那两条在下面（仅 POSIX）：一条证明 Linux 上真跑，这一条保证
    我在这台 Windows 上也能把「删掉检查 → 必须红」证出来，而不是靠 CI 猜。
    """

    def _fake_os(self, mode: int):
        source = self.cred.stat()
        fake_stat = os.stat_result((mode, 0, 0, 1, source.st_uid, 0, 0, 0, 0, 0))
        return types.SimpleNamespace(
            name='posix', O_CREAT=os.O_CREAT, O_RDWR=os.O_RDWR,
            O_NOFOLLOW=getattr(os, 'O_NOFOLLOW', 0),
            O_NONBLOCK=getattr(os, 'O_NONBLOCK', 0),
            open=os.open, close=os.close, lseek=os.lseek,
            geteuid=lambda: 1000, fchown=lambda *args: None,
            fstat=lambda descriptor: fake_stat,
        )

    def test_group_readable_lock_is_refused(self) -> None:
        self._use_lease_once()
        with mock.patch.object(credentiallease, 'os', self._fake_os(stat.S_IFREG | 0o644)):
            exc = self._refusal()
        self.assertEqual(exc.errno, errno.EPERM)
        self.assertIn('unsafe credential lock permissions', str(exc))

    def test_private_mode_is_not_refused(self) -> None:
        if os.name == 'nt':
            self.skipTest('放行后的真实加锁在 Windows 上走 msvcrt，替身 os 只到 _open')
        self._use_lease_once()
        with mock.patch.object(credentiallease, 'os', self._fake_os(stat.S_IFREG | 0o600)):
            with credentiallease.mutation(self.cred):
                pass


@unittest.skipUnless(os.name != 'nt', POSIX_ONLY)
class PermissionBitsGuardTests(_LeaseCase):
    """`st_mode & 0o077`：锁文件对组/其他可写，别人能替你把锁改成任意状态。"""

    def test_publicly_readable_lock_is_refused(self) -> None:
        self._use_lease_once()
        os.chmod(self.sidecar, 0o644)       # 组/其他可读 —— 命中 0o077
        exc = self._refusal()
        self.assertEqual(exc.errno, errno.EPERM, '权限过宽必须按权限拒绝：%r' % (exc,))
        self.assertIn('unsafe credential lock permissions', str(exc))

    def test_private_lock_is_not_refused(self) -> None:
        self._use_lease_once()
        os.chmod(self.sidecar, 0o600)
        with credentiallease.mutation(self.cred):
            pass


class SymlinkGuardTests(_LeaseCase):
    """凭证本身是符号链接：写进去等于顺着链接改到别处。

    不按平台 skip，而是试着建链接：建不出来（Windows 默认需要额外权限）才跳过，
    这样开发者开了开发者模式的 Windows 上也会真跑。
    """

    def test_symlinked_credential_is_refused(self) -> None:
        link = self.dir / 'linked.json'
        try:
            link.symlink_to(self.cred)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('本机建不出符号链接（%s），Linux CI 上会真跑' % exc)
        exc = self._refusal(link)
        self.assertEqual(exc.errno, errno.EINVAL)
        self.assertIn('must not be a symlink', str(exc))


if __name__ == '__main__':
    unittest.main()
