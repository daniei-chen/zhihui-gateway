"""豆包签到模块的契约测试（对齐上游 DoubaoWork-Daily 的协议语义）。

不发真实网络请求：用 httpx.MockTransport 钉死响应，验证
launch → detail → claim → 回查 这条链路在各种分支下的判定。
"""
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from server.qtgcore.adapters import doubao_checkin as dc


def write_account(directory: str, uid: str, cookies, *, name: str = '',
                  enabled: bool = True) -> None:
    doc = {'id': uid, 'name': name or uid, 'enabled': enabled, 'cookies': cookies}
    (Path(directory) / f'{uid}.json').write_text(json.dumps(doc), encoding='utf-8')


def launch_body(group: str = dc.V3_GROUP) -> dict:
    return {'code': 0, 'data': {'ug_campaign_info': {'campaign_group_code': group}}}


def detail_body(*, checked: bool, items=('signin-1',), group: str = dc.V3_GROUP,
                total: int = 1200, reward: int = 200, days: int = 3) -> dict:
    tasks = []
    for item in items:
        tasks.append({
            'campaign_code': item,
            'campaign_group_code': group,
            'task_status': 1,
            'daily_checkin': {
                'checked_in_today': checked,
                'continuous_days': days,
                'reward': {'reward_amount': reward},
            },
        })
    return {'code': 0, 'data': {'progress': {'total_amount': total}, 'tasks': tasks}}


def claim_ok() -> dict:
    return {'code': 0, 'data': {'checked_in': True, 'biz_error': {'code': 0}}}


class AccountFileTests(unittest.TestCase):
    def test_reads_session_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_account(tmp, 'a1', {'sessionid': 's1', 'ttwid': 't'}, name='主号')
            write_account(tmp, 'a2', {'sessionid_ss': 's2'})
            rows = dc.read_accounts(tmp)
        self.assertEqual([r['id'] for r in rows], ['a1', 'a2'])
        self.assertEqual(rows[0]['name'], '主号')

    def test_skips_without_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_account(tmp, 'bad', {'ttwid': 'x'})
            write_account(tmp, 'ok', {'sessionid': 'y'})
            self.assertEqual([r['id'] for r in dc.read_accounts(tmp)], ['ok'])

    def test_skips_broken_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'broken.json').write_text('{nope', encoding='utf-8')
            write_account(tmp, 'ok', {'sessionid': 'y'})
            self.assertEqual([r['id'] for r in dc.read_accounts(tmp)], ['ok'])

    def test_cookie_string_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'c.json').write_text(json.dumps(
                {'id': 'c', 'cookie': 'sessionid=a; ttwid=b'}), encoding='utf-8')
            self.assertEqual(dc.read_accounts(tmp)[0]['cookies'],
                             {'sessionid': 'a', 'ttwid': 'b'})

    def test_missing_dir(self):
        self.assertEqual(dc.read_accounts('/nonexistent/x'), [])


class ProtocolTests(unittest.TestCase):
    def test_activity_group_from_launch(self):
        self.assertEqual(dc.activity_group(launch_body()), dc.V3_GROUP)
        self.assertEqual(dc.activity_group({'code': 0, 'data': {}}), '')

    def test_detail_path_switches_by_group(self):
        self.assertEqual(dc.detail_path(dc.V3_GROUP), dc.DETAIL_V3_PATH)
        self.assertEqual(dc.detail_path('other_group'), dc.DETAIL_LIST_PATH)

    def test_walk_finds_only_daily_tasks(self):
        payload = detail_body(checked=False, items=('a', 'b'))
        payload['data']['tasks'].append({'campaign_code': 'no-daily', 'task_status': 1})
        rows = dc.walk_daily_tasks(payload, dc.V3_GROUP)
        self.assertEqual([r['item'] for r in rows], ['a', 'b'])

    def test_biz_error_outranks_top_level_code(self):
        """回执里 biz_error 非 0 必须当失败——第一版只看顶层 code，会假成功。"""
        raw = {'code': 0, 'data': {'biz_error': {'code': 710012001, 'message': 'expired'}}}
        with self.assertRaises(dc.CheckinError) as ctx:
            dc._raise_for(raw)
        self.assertEqual(ctx.exception.code, 710012001)

    def test_not_open_yet_is_flagged_and_retryable(self):
        """710010702 = 「每日签到尚未开放」（服务端按天开窗，时刻逐日漂移）。

        真机记录（2026-11-02）：10:45 仍拒、10:55 即放行。所以它**不该**被当成
        账号坏了，也不该让人去客户端点按钮 —— 正确反应是稍后重试。
        """
        with self.assertRaises(dc.CheckinError) as ctx:
            dc._raise_for({'code': 0, 'data': {'biz_error': {'code': dc._NOT_OPEN_YET}}})
        self.assertTrue(ctx.exception.not_open_yet)
        self.assertIn('尚未开放', str(ctx.exception))

    def test_task_status_3_means_checked(self):
        """`task_status` 判据：3=已签 / 1=未签（agent-checkin 真机实录）。

        只认 `daily_checkin.checked_in_today` 的话，没有该字段的账号会被误判成
        「没有签到任务」——把「能签」报成「没得签」。
        """
        payload = {'code': 0, 'data': {'tasks': [
            {'campaign_code': 'daily_signin', 'campaign_group_code': dc.V3_GROUP,
             'task_status': 3}]}}
        rows = dc.walk_daily_tasks(payload, dc.V3_GROUP)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['checked'])

        payload['data']['tasks'][0]['task_status'] = 1
        rows = dc.walk_daily_tasks(payload, dc.V3_GROUP)
        self.assertFalse(rows[0]['checked'])


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class CheckinFlowTests(unittest.IsolatedAsyncioTestCase):
    def account(self):
        return {'id': 'a1', 'name': '主号', 'enabled': True,
                'cookies': {'sessionid': 's1', 'ttwid': 't1'}}

    async def test_launch_without_group_falls_back_to_known_group(self):
        """launch 拿不到活动组时**必须回退**到已知组继续读详情。

        2026-11-16 真机实测：已登录网页里 `POST /alice/user/launch` 返回
        `{"code":710010702,"msg":"系统错误"}`，而同一时刻 `v3/detail/` 返回 200
        且带完整任务表。把 launch 当唯一发现途径 = 永远签不到。
        """
        paths = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path.endswith('/alice/user/launch'):
                return httpx.Response(200, json={'code': 0, 'data': {}})
            return httpx.Response(200, json=detail_body(checked=True))

        async with client_for(handler) as client:
            out = await dc.checkin_one(client, self.account())
        self.assertEqual(out['status'], 'already')
        self.assertTrue(any(dc.DETAIL_V3_PATH in p for p in paths), f'应回退到 v3 详情: {paths}')

    async def test_launch_erroring_still_reads_detail(self):
        """launch 直接报 710010702（真机现象）时，仍要靠 detail 拿到状态。"""
        def handler(request):
            if request.url.path.endswith('/alice/user/launch'):
                return httpx.Response(200, json={'code': dc._NOT_OPEN_YET, 'msg': '系统错误'})
            return httpx.Response(200, json=detail_body(checked=True))

        async with client_for(handler) as client:
            out = await dc.checkin_one(client, self.account())
        self.assertEqual(out['status'], 'already')
        self.assertTrue(out['ok'])

    async def test_already_when_checked_today(self):
        calls = []

        def handler(request):
            path = request.url.path
            calls.append(path)
            if path.endswith('/alice/user/launch'):
                return httpx.Response(200, json=launch_body())
            return httpx.Response(200, json=detail_body(checked=True))

        async with client_for(handler) as client:
            out = await dc.checkin_one(client, self.account())
        self.assertEqual(out['status'], 'already')
        self.assertTrue(out['ok'])
        self.assertFalse(any('landing/checkin' in p for p in calls), '已签到时不得提交领取')

    async def test_claimed_requires_recheck_confirmation(self):
        state = {'n': 0}

        def handler(request):
            path = request.url.path
            if path.endswith('/alice/user/launch'):
                return httpx.Response(200, json=launch_body())
            if 'landing/checkin' in path:
                return httpx.Response(200, json=claim_ok())
            # 第一次详情=未签；提交后的回查=已签
            state['n'] += 1
            return httpx.Response(200, json=detail_body(checked=state['n'] > 1))

        async with client_for(handler) as client:
            out = await dc.checkin_one(client, self.account(), recheck_delays=(0.0,))
        self.assertEqual(out['status'], 'claimed')
        self.assertTrue(out['ok'])
        self.assertEqual(out['reward'], 200)

    async def test_claim_accepted_but_unconfirmed_is_confirming_not_success(self):
        def handler(request):
            path = request.url.path
            if path.endswith('/alice/user/launch'):
                return httpx.Response(200, json=launch_body())
            if 'landing/checkin' in path:
                return httpx.Response(200, json=claim_ok())
            return httpx.Response(200, json=detail_body(checked=False))  # 回查始终未勾选

        async with client_for(handler) as client:
            out = await dc.checkin_one(client, self.account(), recheck_delays=(0.0, 0.0))
        self.assertEqual(out['status'], 'confirming')
        self.assertFalse(out['ok'], '回查没确认就不能报成功')
        self.assertIn('不会重复提交', out['message'])

    async def test_not_open_yet_is_not_counted_as_failure(self):
        def handler(request):
            path = request.url.path
            if path.endswith('/alice/user/launch'):
                return httpx.Response(200, json=launch_body())
            if 'landing/checkin' in path:
                return httpx.Response(200, json={'code': 0, 'data': {
                    'biz_error': {'code': dc._NOT_OPEN_YET, 'message': 'not open'}}})
            return httpx.Response(200, json=detail_body(checked=False))

        async with client_for(handler) as client:
            out = await dc.checkin_one(client, self.account())
        self.assertEqual(out['status'], 'not_open_yet')
        self.assertTrue(out.get('retryable'))
        self.assertIn('稍后重试', out['message'])

    async def test_multiple_daily_tasks_is_inactive_not_guessed(self):
        def handler(request):
            path = request.url.path
            if path.endswith('/alice/user/launch'):
                return httpx.Response(200, json=launch_body())
            return httpx.Response(200, json=detail_body(checked=False, items=('a', 'b')))

        async with client_for(handler) as client:
            out = await dc.checkin_one(client, self.account())
        self.assertEqual(out['status'], 'inactive')
        self.assertIn('无法确定', out['message'])

    async def test_no_daily_task_is_inactive(self):
        def handler(request):
            path = request.url.path
            if path.endswith('/alice/user/launch'):
                return httpx.Response(200, json=launch_body())
            return httpx.Response(200, json={'code': 0, 'data': {'tasks': []}})

        async with client_for(handler) as client:
            out = await dc.checkin_one(client, self.account())
        self.assertEqual(out['status'], 'inactive')

    async def test_launch_failure_is_reported(self):
        def handler(request):
            return httpx.Response(200, json={'code': 710012001, 'message': 'not logged in'})

        async with client_for(handler) as client:
            out = await dc.checkin_one(client, self.account())
        self.assertEqual(out['status'], 'fail')
        self.assertIn('登录态已失效', out['message'])

    async def test_http_error_on_detail(self):
        def handler(request):
            if request.url.path.endswith('/alice/user/launch'):
                return httpx.Response(200, json=launch_body())
            return httpx.Response(502, text='bad')

        async with client_for(handler) as client:
            out = await dc.checkin_one(client, self.account())
        self.assertEqual(out['status'], 'fail')
        self.assertIn('HTTP 502', out['message'])

    async def test_non_v3_group_uses_list_path(self):
        seen = []

        def handler(request):
            seen.append(request.url.path)
            if request.url.path.endswith('/alice/user/launch'):
                return httpx.Response(200, json=launch_body('some_other_group'))
            return httpx.Response(200, json=detail_body(checked=True, group='some_other_group'))

        async with client_for(handler) as client:
            await dc.checkin_one(client, self.account())
        self.assertTrue(any(dc.DETAIL_LIST_PATH in p for p in seen), seen)


class AllTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = await dc.checkin_all(tmp)
        self.assertFalse(out['ok'])
        self.assertIn('没有可签到的账号', out['message'])

    async def test_aggregates_and_writes_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = Path(tmp) / 'accounts'
            accounts.mkdir()
            write_account(accounts, 'a1', {'sessionid': 's1'})
            write_account(accounts, 'a2', {'sessionid': 's2'})
            history = Path(tmp) / 'hist.jsonl'
            original = dc.checkin_one

            async def fake(client, account, **_kw):
                uid = account['id']
                if uid == 'a1':
                    return {'uid': uid, 'name': uid, 'ok': True, 'status': 'claimed',
                            'reward': 200, 'message': '签到成功 +200 额度'}
                return {'uid': uid, 'name': uid, 'ok': True, 'status': 'already',
                        'reward': None, 'message': '今日已签到'}

            dc.checkin_one = fake
            try:
                out = await dc.checkin_all(str(accounts), gap_seconds=0,
                                           history_path=str(history))
            finally:
                dc.checkin_one = original
            # 必须在临时目录还活着的时候读，否则文件已经被删了
            rows = dc.read_history(str(history), days=30)
            blob = Path(history).read_text(encoding='utf-8')

        self.assertEqual((out['succeeded'], out['already'], out['failed']), (1, 1, 0))
        self.assertEqual(out['reward_total'], 200)
        self.assertTrue(out['ok'])
        self.assertIn('新签 1 个', out['message'])
        self.assertIn('+200', out['message'])
        self.assertEqual(len(rows), 2)
        self.assertNotIn('sessionid', blob)
        self.assertNotIn('ttwid', blob)


if __name__ == '__main__':
    unittest.main()
