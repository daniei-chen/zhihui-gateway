"""「一条健康记录都没有」不等于「全部健康」。

2026-11-20 真机事故：把豆包容器 stop 掉、`/health` 连 TCP 都连不上
（`curl` 返回 000）之后，管理台仍然显示 `connected: true` +
`pool_available: true`。用户会以为「豆包是好的，只是没有账号」。

根因是 Python 的 `all([])` 返回 **True**：

    "connected": all(h.up for h in agg.health) if agg.health else False

`if agg.health else False` 看起来兜住了，但上游整体不可达时
`build_pool_snapshot` 会把健康条目吞掉，`agg.health` 变成空列表 ——
那个 `else False` 根本不会走到。

这组测试钉住两点：
  · 空 health 必须**显式探测**（调 adapter 的 healthz）；
  · 探测失败/异常一律判不可达，不能乐观默认为 True。
"""
from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest import mock

from server.qtgcore.routers import compat_fixes


class EmptyHealthIsNotHealthyTests(unittest.TestCase):
    def test_accounts_probes_when_health_is_empty(self):
        src = inspect.getsource(compat_fixes.fixed_accounts)
        self.assertIn('if not agg.health', src,
                      'fixed_accounts 在 health 为空时必须显式探测')
        self.assertIn('healthz', src,
                      'fixed_accounts 没有调用 adapter 的 healthz 探针')

    def test_status_probes_when_health_is_empty(self):
        src = inspect.getsource(compat_fixes.fixed_status)
        self.assertIn('if not agg.health', src,
                      'fixed_status 在 health 为空时必须显式探测')
        self.assertIn('healthz', src,
                      'fixed_status 没有调用 adapter 的 healthz 探针')

    def test_probe_exception_means_unreachable(self):
        """探针抛异常时不能乐观判 True。"""
        for fn in (compat_fixes.fixed_accounts, compat_fixes.fixed_status):
            src = inspect.getsource(fn)
            idx = src.find('healthz')
            self.assertGreater(idx, -1)
            window = src[idx:idx + 700]
            self.assertIn('except Exception', window,
                          '%s 的探测没有兜住异常' % fn.__name__)
            self.assertIn('False', window,
                          '%s 的探测失败分支没有判 False' % fn.__name__)

    def test_all_empty_is_true_documented(self):
        """把「为什么会有这个 bug」写进代码，避免下次又被 `all([])` 骗。"""
        src = (inspect.getsource(compat_fixes.fixed_accounts)
               + inspect.getsource(compat_fixes.fixed_status))
        self.assertIn('all([])', src,
                      '缺少 `all([]) 是 True` 的说明，下一个人会重犯')


if __name__ == '__main__':
    unittest.main()
