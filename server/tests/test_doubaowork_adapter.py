"""豆包工作适配器的契约与安全边界。

这些测试锁住三件事：
1. **不伪造**：上游没有的能力（签到、额度、用量）必须明确报「不支持」，
   不能返回看起来成功的结果；
2. **不猜**：上游响应缺字段/形状不对时报 UpstreamError，不降级成空值；
3. **不放大**：账号 id 进 URL 前必须校验，写操作必须回读确认。
"""
import json
import unittest
from unittest import mock

from server.qtgcore.adapters.doubaowork import DoubaoWorkAdapter
from server.qtgcore.config import Settings
from server.qtgcore.errors import UpstreamError


def settings(**over):
    return Settings(**over)


class DoubaoWorkAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_password_file_is_explicit(self):
        adapter = DoubaoWorkAdapter(settings(doubaowork_admin_password_file='/nonexistent/pw'))
        with self.assertRaisesRegex(UpstreamError, '管理密码缺失'):
            await adapter.authenticate()

    async def test_401_reports_wrong_password_not_generic_failure(self):
        adapter = DoubaoWorkAdapter(settings())
        adapter._credentials = lambda: 'wrong'
        resp = mock.Mock(status_code=401, text='nope')
        adapter._request = mock.AsyncMock(return_value=resp)
        with self.assertRaisesRegex(UpstreamError, '管理密码不正确'):
            await adapter.authenticate()

    async def test_login_does_not_recurse_into_ensure_auth(self):
        """登录请求必须 auth=False，否则 ensure_auth 会无限重登。"""
        adapter = DoubaoWorkAdapter(settings())
        adapter._credentials = lambda: 'pw'
        adapter._request = mock.AsyncMock(return_value=mock.Mock(status_code=200, text='{}'))
        await adapter.authenticate()
        self.assertIs(adapter._request.call_args.kwargs['auth'], False)


class DoubaoWorkReadTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        return DoubaoWorkAdapter(settings())

    async def test_overview_requires_data_envelope(self):
        for bad in ({}, {'data': None}, {'data': 'x'}, {'data': [1, 2]}, []):
            with self.subTest(bad=bad):
                adapter = self.adapter()
                adapter._admin = mock.AsyncMock(return_value=bad)
                with self.assertRaises(UpstreamError):
                    await adapter.overview()

    async def test_overview_passes_upstream_rows_through_unchanged(self):
        rows = [{'id': 'a', 'state': 'ready', 'enabled': True}]
        adapter = self.adapter()
        adapter._admin = mock.AsyncMock(return_value={'data': rows})
        out = await adapter.overview()
        self.assertEqual(out['accounts'], rows)
        self.assertEqual(out['checkin'], [])

    async def test_usage_is_empty_and_marked_unsupported(self):
        """上游没有用量端点：不补零、不造曲线。"""
        out = await self.adapter().usage(14)
        self.assertEqual(out['usage'], [])
        self.assertEqual(out['credits_daily'], [])
        self.assertIn('usage', out['unsupported'])
        self.assertIn('credits', out['unsupported'])

    async def test_checkin_reports_missing_credentials_without_faking(self):
        """签到已实现（走豆包官方网页接口），没有账号凭证时如实报错。

        这条测试守的是**不伪造**：既不能报成功，也不能退回到「上游不支持」
        （那是旧实现的说法，现在已经不成立）——签到是真的有，只是读不到凭证。
        """
        adapter = self.adapter()
        out = await adapter.checkin()
        self.assertIs(out['ok'], False)
        self.assertEqual(out['results'], [])
        self.assertIn('没有可签到的账号', out['message'])
        joined = json.dumps(out, ensure_ascii=False)
        for word in ('已提交', '签到成功', '已完成'):
            self.assertNotIn(word, joined)

    async def test_models_read_from_admin_config(self):
        adapter = self.adapter()
        adapter._admin = mock.AsyncMock(return_value={
            'data': None, 'version': '0.3.0',
            'models': [{'id': 'm1'}, {'id': 'm2'}, {'no_id': True}, 'plain'],
        })
        out = await adapter.models()
        ids = [m['id'] for m in out['data']]
        self.assertEqual(ids, ['m1', 'm2', 'plain'])

    async def test_models_missing_section_is_empty_not_error(self):
        adapter = self.adapter()
        adapter._admin = mock.AsyncMock(return_value={'version': '0.3.0'})
        self.assertEqual(await adapter.models(), {'data': []})

    async def test_health_never_raises(self):
        adapter = self.adapter()
        adapter.healthz = mock.AsyncMock(side_effect=RuntimeError('boom'))
        out = await adapter.health()
        self.assertIs(out['up'], False)
        self.assertEqual(out['engine'], 'doubaowork')
        self.assertIn('boom', out['last_error'])

    async def test_logs_tolerate_odd_rows(self):
        adapter = self.adapter()
        adapter._admin = mock.AsyncMock(return_value={'data': [
            'not-a-dict',
            {'started_at': 0, 'status': 'error', 'model': 'm', 'duration_ms': 5, 'error': 'e'},
            {'started_at': 1700000000, 'status': 'success', 'model': 'm'},
        ]})
        out = await adapter.logs()
        self.assertEqual(len(out['data']), 2)
        self.assertEqual(out['data'][0]['level'], 'error')
        self.assertTrue(out['data'][1]['ts'])


class DoubaoWorkWriteTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        return DoubaoWorkAdapter(settings())

    async def test_set_account_rejects_path_injection(self):
        for bad in ('../admin/password', 'a/b', '', 'x' * 200, 'a b'):
            with self.subTest(bad=bad):
                adapter = self.adapter()
                adapter._admin_data = mock.AsyncMock(return_value={'enabled': True})
                with self.assertRaises(ValueError):
                    await adapter.set_account(bad, True)
                adapter._admin_data.assert_not_awaited()

    async def test_set_account_rejects_non_boolean(self):
        adapter = self.adapter()
        with self.assertRaises(ValueError):
            await adapter.set_account('ok-id', 'true')  # 字符串不是 bool

    async def test_set_account_reports_readback_mismatch_as_unknown(self):
        """上游说改成了，回读却相反——必须报 unknown，不能报成功。"""
        adapter = self.adapter()
        adapter._admin_data = mock.AsyncMock(return_value={'id': 'a', 'enabled': False})
        out = await adapter.set_account('a', True)
        self.assertIs(out['ok'], False)
        self.assertEqual(out['state'], 'unknown')
        self.assertIs(out['changed'], False)

    async def test_set_account_confirmed(self):
        adapter = self.adapter()
        adapter._admin_data = mock.AsyncMock(return_value={'id': 'a', 'enabled': False})
        out = await adapter.set_account('a', False)
        self.assertIs(out['ok'], True)
        self.assertEqual(out['state'], 'completed')

    async def test_add_account_rejects_invalid_json(self):
        adapter = self.adapter()
        for bad in ('', 'not-json', '{'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    await adapter.add_account('n', bad)

    async def test_add_account_surfaces_upstream_errors(self):
        adapter = self.adapter()
        adapter._admin = mock.AsyncMock(return_value={
            'imported': [], 'errors': ['account 1: bad cookie'], 'created': 0, 'updated': 0})
        out = await adapter.add_account('n', '{"id":"x"}')
        self.assertIs(out['ok'], False)
        self.assertIn('bad cookie', out['error'])

    async def test_add_account_name_fills_only_missing(self):
        adapter = self.adapter()
        captured = {}

        async def fake_admin(method, path, **kwargs):
            captured.update(kwargs.get('json_body') or {})
            return {'imported': ['a', 'b'], 'errors': [], 'created': 2, 'updated': 0}

        adapter._admin = fake_admin
        await adapter.add_account('名叫N', json.dumps([{'id': 'a'}, {'id': 'b', 'name': '已有'}]))
        self.assertEqual(captured['accounts'][0]['name'], '名叫N')
        self.assertEqual(captured['accounts'][1]['name'], '已有')

    async def test_save_keys_defers_to_upstream_panel(self):
        out = await self.adapter().save_keys([{'name': 'x'}])
        self.assertIs(out['ok'], False)
        self.assertIn('面板', out['error'])

    async def test_oauth_paths_are_explicitly_unsupported(self):
        adapter = self.adapter()
        self.assertIs((await adapter.oauth_login_url())['ok'], False)
        self.assertIs((await adapter.oauth_finish('http://x'))['ok'], False)

    async def test_refresh_requires_id(self):
        self.assertIs((await self.adapter().refresh_account(''))['ok'], False)


if __name__ == '__main__':
    unittest.main()
