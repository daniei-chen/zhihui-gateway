"""维护围栏的监听者判定：rolling 交接期间新旧实例必然并存。

`maintenance.py` 依赖 `fcntl`，只能在 Linux 上导入，所以这里用**源码合同**锁定
判定结构，而不是在 Windows 上假装跑一遍。

原缺陷：`listeners()` 一看到 7866 就要求"期望端口是 7866 且 pid 匹配"，
于是在检查 helper（`expected_port=7865`）时，把仍在 7866 正常服务的旧 primary
判成 `primary_listener_identity_mismatch` —— 线上表现为 151 次重试（约 90 秒）
后整次滚动交接失败并围栏。
"""
from __future__ import annotations

import unittest
from pathlib import Path

SOURCE = (Path(__file__).parents[2] / 'ops' / 'container-maintenance' / 'maintenance.py')


def _listeners_body() -> str:
    text = SOURCE.read_text(encoding='utf-8')
    start = text.index('def listeners(')
    end = text.index('def upstream(', start)
    return text[start:end]


class ListenerGateContractTests(unittest.TestCase):
    def test_helper_check_no_longer_rejects_the_live_primary(self):
        body = _listeners_body()
        # 原 bug 的判定核心：任何"期望端口不是 7866"的调用都会因为看到 7866 而失败。
        self.assertNotIn('expected_port != 7866', body)
        # 修复后：只有 expected_port == 7866 时才把该行计入并校验 pid。
        self.assertIn('if expected_port == 7866:', body)
        self.assertIn("if expected_port == 7865:", body)

    def test_helper_must_not_own_the_primary_listener(self):
        body = _listeners_body()
        # helper 的 pid 出现在 7866 行里必须是错误，不能被"容忍并存"吞掉。
        self.assertIn("raise Failure('primary_listener_identity_mismatch')", body)
        self.assertIn('if len(live) != 1:', body)

    def test_helper_listener_requires_allow_helper(self):
        body = _listeners_body()
        self.assertIn("raise Failure('helper_7865_still_listening')", body)
        self.assertIn("raise Failure('helper_listener_not_loopback')", body)

    def test_both_listeners_are_required_to_be_loopback(self):
        body = _listeners_body()
        self.assertIn("raise Failure('primary_listener_not_loopback')", body)
        self.assertIn('127.0.0.1:7866', body)
        self.assertIn('127.0.0.1:7865', body)


if __name__ == '__main__':
    unittest.main()
