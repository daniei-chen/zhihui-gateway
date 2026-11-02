"""Control-plane mutations must not turn malformed input or unknown results green."""
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request

from server.qtgcore.routers import compat


def request(raw):
    consumed = False
    async def receive():
        nonlocal consumed
        if consumed:
            return {'type': 'http.disconnect'}
        consumed = True
        return {'type': 'http.request', 'body': raw, 'more_body': False}
    return Request({'type': 'http', 'method': 'PUT', 'path': '/accounts/trae:synthetic/disabled',
                    'query_string': b'', 'headers': []}, receive)


class MutationResultTests(unittest.IsolatedAsyncioTestCase):
    def test_success_requires_explicit_consistent_boolean_confirmation(self):
        for value in ({}, None, [], 'ok', {'ok': 'false'}, {'success': 1}, {'ok': False},
                      {'ok': True, 'success': False}, {'ok': True, 'pending': True},
                      {'ok': True, 'state': 'unknown'}):
            with self.subTest(value=value):
                self.assertFalse(compat._res_ok(value))
        for value in ({'ok': True}, {'success': True}, {'ok': True, 'success': True}):
            self.assertTrue(compat._res_ok(value))

    async def test_invalid_disabled_payload_never_calls_adapter(self):
        adapter = SimpleNamespace(set_account=mock.AsyncMock(return_value={'ok': True}))
        for raw in (b'{', b'null', b'[]', b'{}', b'{"disabled":"false"}', b'{"disabled":0}'):
            with self.subTest(raw=raw), mock.patch.object(compat, '_rt', return_value=object()), \
                    mock.patch.object(compat, '_adapter_for', return_value=adapter):
                with self.assertRaises(HTTPException) as error:
                    await compat.compat_account_disabled(request(raw), 'trae:synthetic')
                self.assertEqual(error.exception.status_code, 400)
                adapter.set_account.assert_not_awaited()

    async def test_valid_disabled_input_dispatches_one_precise_action(self):
        for disabled in (False, True):
            adapter = SimpleNamespace(set_account=mock.AsyncMock(return_value={'ok': True}))
            with mock.patch.object(compat, '_rt', return_value=object()), \
                    mock.patch.object(compat, '_adapter_for', return_value=adapter):
                result = await compat.compat_account_disabled(
                    request(json.dumps({'disabled': disabled}).encode()), 'trae:synthetic')
            self.assertIs(result['success'], True)
            self.assertIs(result['disabled'], disabled)
            adapter.set_account.assert_awaited_once_with('synthetic', not disabled)

    async def test_refresh_and_disable_unknown_results_never_report_completion(self):
        for raw_result in ({}, [], {'ok': 'false'}, {'ok': True, 'pending': True}, None):
            with self.subTest(result=raw_result):
                adapter = SimpleNamespace(refresh_account=mock.AsyncMock(return_value=raw_result),
                                          set_account=mock.AsyncMock(return_value=raw_result))
                with mock.patch.object(compat, '_rt', return_value=object()), \
                        mock.patch.object(compat, '_adapter_for', return_value=adapter):
                    refreshed = await compat.compat_account_refresh(request(b'{}'), 'trae:synthetic')
                    disabled = await compat.compat_account_disabled(request(b'{"disabled":true}'), 'trae:synthetic')
                self.assertIs(refreshed['ok'], False)
                self.assertIs(disabled['success'], False)
                self.assertNotEqual(refreshed['message'], '已刷新')
                adapter.refresh_account.assert_awaited_once_with('synthetic')
                adapter.set_account.assert_awaited_once_with('synthetic', False)


if __name__ == '__main__':
    unittest.main()
