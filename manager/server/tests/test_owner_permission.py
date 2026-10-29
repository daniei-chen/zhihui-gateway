"""N-01 回归：属主不可读的旧锁文件不能把启动打崩。

背景（2026-10-28 增量只读审计 N-01，P1）：`budgetowner.alive()` 原实现只捕获
`FileNotFoundError`。当 `.budget-owners/` 里存在**属主不是本进程 uid** 的旧锁文件
（root 手动运行 / 测试残留）时，`os.open` 抛 `PermissionError` 一路穿到
`main.py` 的 lifespan → 进程 exit 3。生产在 08:44:57 实际发生过一次，靠
systemd `Restart=always` 3 秒后重试成功（第二次生成了新的随机 owner 名，恰好避开）。

语义边界：
  * 打不开 ≠ 已死。返回 `None`（不确定）→ 由对账流程按 unknown 保守处理；
  * **绝不能**因为读不到就 `return False`（= 已死），那会去释放别人的预留。
"""
from __future__ import annotations

import pathlib
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import budgetowner, config  # noqa: E402


def _owner(hex32: str = '0' * 32) -> str:
    return '%s:%s' % (socket.gethostname(), hex32)


class OwnerLivenessTest(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = pathlib.Path(self._tmp.name) / 'm.db'
        budgetowner.close()

    def tearDown(self) -> None:
        budgetowner.close()
        config.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def test_permission_error_returns_none_not_raise(self) -> None:
        """属主不可读 → None（不确定），不抛。这是 N-01 的修复点。"""
        with mock.patch('os.open', side_effect=PermissionError(13, 'Permission denied')):
            self.assertIsNone(budgetowner.alive(_owner()))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(budgetowner.alive(_owner('1' * 32)))

    def test_other_oserror_returns_none(self) -> None:
        with mock.patch('os.open', side_effect=OSError(24, 'Too many open files')):
            self.assertIsNone(budgetowner.alive(_owner('2' * 32)))

    def test_live_owner_is_detected(self) -> None:
        """真正的存活 owner 仍要能判 True —— 修复不能把「活着」也吞成 None。"""
        identity = budgetowner.identity()
        self.assertIs(budgetowner.alive(identity), True)

    def test_foreign_host_is_unknown(self) -> None:
        self.assertIsNone(budgetowner.alive('some-other-host:' + '3' * 32))

    def test_malformed_identity_is_unknown(self) -> None:
        self.assertIsNone(budgetowner.alive('not-an-owner'))
        self.assertIsNone(budgetowner.alive(''))


if __name__ == '__main__':
    unittest.main()
