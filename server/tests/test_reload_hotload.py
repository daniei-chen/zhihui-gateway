"""Account additions observe hot-load success; failure never restarts inference."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import reload as R  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._wait = R.HOT_RELOAD_WAIT_SECONDS
        self._poll = R.HOT_RELOAD_POLL_SECONDS
        # 测试里把窗口压到几十毫秒：判据是「等 / 不等」，不是等多久
        R.HOT_RELOAD_WAIT_SECONDS = 0.15
        R.HOT_RELOAD_POLL_SECONDS = 0.01
        R._pending_uids.clear()
        R._reload_worker = None
        self.restarts: list[int] = []

        async def _restart():
            self.restarts.append(1)
            return True, 'ok'

        self._restart_patch = mock.patch.object(R.wb2api, 'restart_container', _restart)
        self._restart_patch.start()
        self.addCleanup(self._restart_patch.stop)

    def tearDown(self) -> None:
        R.HOT_RELOAD_WAIT_SECONDS = self._wait
        R.HOT_RELOAD_POLL_SECONDS = self._poll
        R._pending_uids.clear()
        R._reload_worker = None

    def _status_patch(self, statuses: list[dict]) -> None:
        """按调用次序返回不同的 /status 快照；用完后一直用最后一个。"""
        seq = list(statuses)

        async def _get():
            return seq.pop(0) if len(seq) > 1 else seq[0]

        self._status = mock.patch.object(R.wb2api, 'get_status', _get)
        self._status.start()
        self.addCleanup(self._status.stop)

    async def _run(self) -> None:
        R.request_reload_or_restart('uid-new')
        # 等后台任务跑完（窗口 + 余量）
        if R._reload_worker is not None:
            await asyncio.wait_for(R._reload_worker, timeout=5)

    def test_hot_reload_appearing_skips_restart(self) -> None:
        """账号在窗口内被上游收录 → 不重启（这是新版上游的正常路径）。"""
        self._status_patch([
            {'connected': True, 'accounts': []},
            {'connected': True, 'accounts': [{'uid': 'uid-new'}]},
        ])
        asyncio.run(self._run())
        self.assertEqual(self.restarts, [], '热加载已生效却又重启了上游')

    def test_timeout_requires_attention_without_restart(self) -> None:
        self._status_patch([{'connected': True, 'accounts': [{'uid': 'other'}]}])
        asyncio.run(self._run())
        self.assertEqual(self.restarts, [])
        self.assertEqual(R.state()['account_reload']['reason'], 'account_not_observed')

    def test_unreachable_upstream_requires_attention_without_restart(self) -> None:
        self._status_patch([{'connected': False, 'error': 'connection refused'}])
        asyncio.run(self._run())
        self.assertEqual(self.restarts, [])
        self.assertEqual(R.state()['account_reload']['reason'], 'upstream_unavailable')

    def test_two_accounts_coalesce_observation_without_restarting(self) -> None:
        self._status_patch([{'connected': True, 'accounts': [{'uid': 'other'}]}])

        async def _both() -> None:
            R.request_reload_or_restart('uid-a')
            R.request_reload_or_restart('uid-b')
            if R._reload_worker is not None:
                await asyncio.wait_for(R._reload_worker, timeout=5)

        asyncio.run(_both())
        self.assertEqual(self.restarts, [])
        self.assertEqual(R.state()['account_reload']['pending_count'], 2)

    def test_no_event_loop_reports_false(self) -> None:
        """没有事件循环时返回 False，让调用方自己决定（与 request_restart 同口径）。"""
        self.assertFalse(R.request_reload_or_restart('uid-x'))
