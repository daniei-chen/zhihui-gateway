"""账号出口闸门的回归测试（2026-11-19 批量吊销事故后新增）。

这些测试守的是「不许再把账号池一次打光」这件事本身，不是某个实现细节：

  · 自动化客户端不得触发批量动作
  · 单次批量不得超过批大小上限
  · 每分钟动作数有硬上限
  · 两个动作之间有最小间隔
  · 被拦下时给出可执行的等待时间，而不是静默排队
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from fastapi import HTTPException

from server import accountguard


class _FakeRequest:
    """最小 Request 替身：闸门只读 headers。"""

    def __init__(self, headers: dict | None = None) -> None:
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


def _fresh_window(per_minute: int = 60, min_interval: float = 1.0) -> accountguard._SlidingWindow:
    return accountguard._SlidingWindow(per_minute, min_interval)


class AutomationBlockTests(unittest.TestCase):
    """AI / 脚本不得触发批量动作 —— 本次事故的直接诉求。"""

    def test_ai_header_blocks_batch(self):
        req = _FakeRequest({'x-agent-automation': '1'})
        with mock.patch.object(accountguard, 'AUTOMATION_TOKEN', ''):
            with self.assertRaises(HTTPException) as ctx:
                accountguard.guard_account_action(req, count=3, what='刷新积分', batch=True)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn('自动化客户端', ctx.exception.detail)

    def test_script_user_agent_blocks_batch(self):
        for ua in ('python-requests/2.31', 'curl/8.5.0', 'Go-http-client/1.1', 'axios/1.6'):
            req = _FakeRequest({'user-agent': ua})
            with mock.patch.object(accountguard, 'AUTOMATION_TOKEN', ''):
                with self.assertRaises(HTTPException, msg=ua) as ctx:
                    accountguard.guard_account_action(req, count=2, what='x', batch=True)
            self.assertEqual(ctx.exception.status_code, 403, ua)

    def test_browser_ua_is_not_automation(self):
        req = _FakeRequest({'user-agent': 'Mozilla/5.0 (Windows NT 10.0) Chrome/120'})
        with mock.patch.object(accountguard, '_WINDOW', _fresh_window()):
            accountguard.guard_account_action(req, count=2, what='x', batch=True)

    def test_automation_allowed_with_matching_token(self):
        req = _FakeRequest({'x-agent-automation': '1', 'x-automation-token': 'ok-token'})
        with mock.patch.object(accountguard, 'AUTOMATION_TOKEN', 'ok-token'), \
             mock.patch.object(accountguard, '_WINDOW', _fresh_window()):
            accountguard.guard_account_action(req, count=2, what='x', batch=True)

    def test_automation_rejected_with_wrong_token(self):
        req = _FakeRequest({'x-agent-automation': '1', 'x-automation-token': 'wrong'})
        with mock.patch.object(accountguard, 'AUTOMATION_TOKEN', 'ok-token'):
            with self.assertRaises(HTTPException) as ctx:
                accountguard.guard_account_action(req, count=2, what='x', batch=True)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_non_batch_action_is_not_blocked_for_automation(self):
        """单账号动作不拦：那不是把池子打光的形态。"""
        req = _FakeRequest({'x-agent-automation': '1'})
        with mock.patch.object(accountguard, '_WINDOW', _fresh_window()):
            accountguard.guard_account_action(req, count=1, what='单号测活', batch=False)


class BatchSizeTests(unittest.TestCase):
    """单次批量不得超过上限，超过要求分批。"""

    def test_oversized_batch_rejected(self):
        req = _FakeRequest()
        limit = accountguard.DEFAULT_BATCH_MAX
        with self.assertRaises(HTTPException) as ctx:
            accountguard.guard_account_action(req, count=limit + 1, what='刷新', batch=True)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn(str(limit), ctx.exception.detail)
        self.assertIn('分批', ctx.exception.detail)

    def test_batch_at_limit_accepted(self):
        req = _FakeRequest()
        with mock.patch.object(accountguard, '_WINDOW', _fresh_window()):
            accountguard.guard_account_action(
                req, count=accountguard.DEFAULT_BATCH_MAX, what='刷新', batch=True)


class SlidingWindowTests(unittest.TestCase):
    """滑动窗口 + 最小间隔。"""

    def test_per_minute_cap_enforced(self):
        win = _fresh_window(per_minute=3, min_interval=0.0)
        for i in range(3):
            ok, _, _ = win.check(now=float(i))
            self.assertTrue(ok, f'第 {i + 1} 个应放行')
            win.record(now=float(i))
        ok, reason, wait = win.check(now=3.0)
        self.assertFalse(ok)
        self.assertIn('上限', reason)
        self.assertGreater(wait, 0)

    def test_window_slides_after_a_minute(self):
        win = _fresh_window(per_minute=2, min_interval=0.0)
        win.record(now=0.0)
        win.record(now=1.0)
        self.assertFalse(win.check(now=2.0)[0])
        # 61 秒后第一条滑出窗口
        self.assertTrue(win.check(now=61.5)[0])

    def test_min_interval_enforced(self):
        win = _fresh_window(per_minute=100, min_interval=2.0)
        win.record(now=0.0)
        ok, reason, wait = win.check(now=0.5)
        self.assertFalse(ok)
        self.assertIn('不足', reason)
        self.assertAlmostEqual(wait, 1.5, places=3)
        self.assertTrue(win.check(now=2.0)[0])

    def test_retry_after_header_present(self):
        """被限流时必须告诉调用方等多久 —— 否则 AI 会立刻重试，更糟。"""
        req = _FakeRequest()
        win = _fresh_window(per_minute=1, min_interval=0.0)
        win.record(now=0.0)
        with mock.patch.object(accountguard, '_WINDOW', win), \
             mock.patch.object(accountguard.time, 'monotonic', return_value=1.0):
            with self.assertRaises(HTTPException) as ctx:
                accountguard.guard_account_action(req, count=1, what='x', batch=False)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn('Retry-After', ctx.exception.headers or {})

    def test_snapshot_shape(self):
        win = _fresh_window(per_minute=5, min_interval=1.0)
        win.record()
        snap = win.snapshot()
        self.assertEqual(snap['used_last_minute'], 1)
        self.assertEqual(snap['per_minute'], 5)
        self.assertIn('min_interval_sec', snap)


class DefaultsTests(unittest.TestCase):
    """默认参数必须比事故当天的实际调用温和得多。"""

    def test_per_minute_is_below_incident_rate(self):
        # 事故当天 activity 是 240 次/分钟（48 账号 × 5 条）。
        self.assertLess(accountguard.DEFAULT_PER_MINUTE, 240,
                        '每分钟上限必须低于事故当天的 240 次/分钟')
        self.assertGreaterEqual(accountguard.DEFAULT_MIN_INTERVAL, 0.5,
                                '最小间隔太短就挡不住「同一秒打多个账号」')

    def test_batch_max_is_small(self):
        self.assertLessEqual(accountguard.DEFAULT_BATCH_MAX, 20,
                             '单批上限过大等于没分批')

    def test_automation_blocked_by_default(self):
        # 未配置令牌时，自动化批量调用必须默认禁止。
        self.assertEqual(accountguard.AUTOMATION_TOKEN, '')


class ReadOnlyEndpointsAreNotGatedTests(unittest.TestCase):
    """**只读**接口不得接入闸门（2026-11-19 修正）。

    第一版把 `refresh-credits`（查询余额）也拦了，结果是用户点一次
    「刷新积分」就收到 429 —— 正常功能被防死，而那个动作根本不产生新的
    refreshToken，不具备「把账号刷死」的能力。

    这些测试直接扫源码：被 `guard_account_action` 覆盖的接口里，
    **不允许**出现只读的余额查询。这比逐个 mock 路由更稳——
    新加一个接入点时它会自动被检查到。
    """

    #: 只读接口的路径片段：出现即视为误拦。
    READ_ONLY = ('/accounts/refresh-credits',)

    #: 必须接入闸门的写操作（防止有人「顺手删掉」）。
    WRITE_PATHS = ('/accounts/refresh-tokens', '/accounts/checkin-all',
                   '/accounts/refresh-plan/run')

    def _source(self) -> str:
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        return (root / 'routers' / 'accounts.py').read_text(encoding='utf-8')

    def test_refresh_credits_is_not_gated(self):
        """余额查询是读操作，源码里不该再有它的闸门调用。"""
        src = self._source()
        for marker in self.READ_ONLY:
            # 找 marker 之后的 800 字符窗口里有没有闸门调用
            idx = src.find(marker)
            self.assertGreater(idx, 0, f'{marker} 不见了？')
            window = src[idx:idx + 900]
            self.assertNotIn(
                'guard_account_action', window,
                f'{marker} 是只读查询，不该过批量闸门 —— '
                '它不产生新 refreshToken，拦它只会让正常刷新积分 429',
            )

    def test_write_operations_are_gated(self):
        """写操作必须仍然过闸门。"""
        src = self._source()
        for marker in self.WRITE_PATHS:
            idx = src.find(marker)
            self.assertGreater(idx, 0, f'{marker} 不见了？')
            window = src[idx:idx + 2000]
            self.assertIn(
                'guard_account_action', window,
                f'{marker} 是会续期/触发登录的写操作，必须过闸门',
            )


if __name__ == '__main__':
    unittest.main()
