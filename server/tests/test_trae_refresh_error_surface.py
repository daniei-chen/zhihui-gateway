"""`refresh_account` 必须把上游的拒绝原因带出来。

2026-11-20 真机事故：Trae 的「一键刷新令牌」显示「刷新结果未确认，请核对账号状态」，
而真实原因是上游 `POST /api/cmd/refresh_jwt` 返回
`401 {"error":"未登录或会话已过期","ok":false}` —— **管理面会话过期**。

根因：`refresh_account` 用 `required=False` 调 `_cmd`，而 `base._json` 在
`required=False` 时把 4xx 静默转成 None，错误详情全部丢失。

这类错误不会让任何测试失败（返回结构合法，只是没有 error 字段），
所以用测试钉住：上游说什么，调用方就要能看到什么。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from server.qtgcore.adapters.trae2 import Trae2Adapter
from server.qtgcore.errors import UpstreamError


def _adapter_with(response):
    """构造一个只替换 `_request` 的适配器（绕过 __init__ 的配置依赖）。"""
    adapter = Trae2Adapter.__new__(Trae2Adapter)
    adapter.name = 'trae2'
    adapter._request = mock.AsyncMock(return_value=response)
    return adapter


def _resp(status, payload):
    return SimpleNamespace(status_code=status,
                           text=json.dumps(payload, ensure_ascii=False))


class RefreshAccountErrorSurfaceTests(unittest.TestCase):
    def _run(self, response):
        adapter = _adapter_with(response)
        return asyncio.run(adapter.refresh_account('1234567890'))

    def test_401_surfaces_upstream_error_text(self):
        out = self._run(_resp(401, {'error': '未登录或会话已过期', 'ok': False}))
        self.assertFalse(out['ok'])
        self.assertEqual('auth_required', out.get('state'))
        self.assertIn('未登录或会话已过期', out.get('error', ''))

    def test_401_offers_actionable_hint(self):
        out = self._run(_resp(401, {'error': '未登录或会话已过期', 'ok': False}))
        self.assertTrue(out.get('hint'), '401 必须给出下一步该做什么')

    def test_500_surfaces_upstream_error_text(self):
        out = self._run(_resp(500, {'error': '内部错误：数据库锁定', 'ok': False}))
        self.assertFalse(out['ok'])
        self.assertEqual('failed', out.get('state'))
        self.assertIn('数据库锁定', out.get('error', ''))

    def test_body_ok_false_with_error_is_surfaced(self):
        out = self._run(_resp(200, {'ok': False, 'error': 'JWT 尚未到期，无需刷新'}))
        self.assertFalse(out['ok'])
        self.assertIn('无需刷新', out.get('error', ''))

    def test_not_due_is_marked_not_needed_not_failed(self):
        """「JWT 还没到期」是省一次操作，不是失败 —— 不能算进 failed 计数。"""
        out = self._run(_resp(200, {'ok': False, 'error': 'JWT 剩余有效期 203.3 小时，暂无需刷新'}))
        self.assertFalse(out['ok'])
        self.assertEqual('not_needed', out.get('state'))

    def test_success_body_is_passed_through(self):
        out = self._run(_resp(200, {'ok': True, 'expires_at': 123}))
        self.assertTrue(out['ok'])
        self.assertEqual(123, out.get('expires_at'))

    def test_missing_uid_is_rejected_without_calling_upstream(self):
        adapter = _adapter_with(_resp(200, {'ok': True}))
        out = asyncio.run(adapter.refresh_account(''))
        self.assertFalse(out['ok'])
        adapter._request.assert_not_awaited()

    def test_transport_error_is_surfaced(self):
        adapter = Trae2Adapter.__new__(Trae2Adapter)
        adapter.name = 'trae2'
        adapter._request = mock.AsyncMock(side_effect=UpstreamError('连接被拒绝'))
        out = asyncio.run(adapter.refresh_account('1234567890'))
        self.assertFalse(out['ok'])
        self.assertIn('连接被拒绝', out.get('error', ''))

    def test_never_returns_bare_ok_false(self):
        """核心断言：失败时必须带原因，不能只有一个 `{"ok": False}`。"""
        for resp in (
            _resp(401, {'error': '会话过期', 'ok': False}),
            _resp(500, {'error': 'boom', 'ok': False}),
            _resp(200, {'ok': False, 'error': '拒绝'}),
            _resp(200, {}),
        ):
            out = self._run(resp)
            if out.get('ok') is False:
                self.assertTrue(str(out.get('error') or '').strip(),
                                '失败结果必须带 error 字段，实际拿到：%r' % (out,))


if __name__ == '__main__':
    unittest.main()
