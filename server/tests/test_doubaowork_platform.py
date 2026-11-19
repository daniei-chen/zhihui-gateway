"""第四平台 doubaowork 在策略层、聚合层与兼容层的契约。

核心断言：**枚举里有 ≠ 默认就授权**。
新增平台最危险的后果不是「用不了」，而是「悄悄对所有旧密钥生效」。
"""
import json
import sqlite3
import unittest

from starlette.requests import Request

from server import migrations, platformpolicy
from server.qtgcore import authz, service
from server.qtgcore.models import AccountsResponse


def make_request(headers=None, path='/v1/models', query=b''):
    return Request({
        'type': 'http', 'method': 'GET', 'path': path,
        'query_string': query, 'headers': headers or [],
    })


RAW_READY = {'id': 'a1', 'name': '主号', 'group': 'g', 'enabled': True,
             'available': True, 'state': 'busy', 'cooldown_remaining_seconds': 0,
             'last_error': '', 'error_code': '', 'in_flight': 1}
RAW_COOLING = {'id': 'a2', 'name': '', 'group': '', 'enabled': True,
               'available': False, 'state': 'cooling',
               'cooldown_remaining_seconds': 30, 'last_error': '710022004',
               'error_code': 'need_verify', 'in_flight': 0}
RAW_DISABLED = dict(RAW_READY, id='a3', enabled=False, state='disabled', in_flight=0)
RAW_UNKNOWN = dict(RAW_READY, id='a4', state='weird-new-state', in_flight=0)


class PlatformEnumTests(unittest.TestCase):
    def test_all_platforms_include_doubaowork(self):
        self.assertIn('doubaowork', platformpolicy.PLATFORMS)

    def test_default_platforms_follow_the_new_three(self):
        """2026-11-19：豆包工作进默认集合，qoder 移除。

        这条测试守的是「权限放大」：新平台曾经不能自动进默认集合。
        但现在豆包是**主力平台**（顶替 qoder 的位置），它必须默认可用；
        而 qoder 已经整体摘除，留在默认集合里会让 normalize 产出
        一个 PLATFORMS 里不存在的平台。
        """
        self.assertIn('doubaowork', platformpolicy.DEFAULT_PLATFORMS)
        self.assertNotIn('qoder', platformpolicy.DEFAULT_PLATFORMS,
                         'qoder 已摘除，不得出现在默认授予集合')
        self.assertEqual(list(platformpolicy.DEFAULT_PLATFORMS),
                         ['doubaowork', 'trae', 'workbuddy'])
        # 默认集合必须是全集子集，否则 normalize 会产出非法平台
        self.assertTrue(set(platformpolicy.DEFAULT_PLATFORMS)
                        <= set(platformpolicy.PLATFORMS))

    def test_normalize_none_returns_full_enum(self):
        self.assertEqual(platformpolicy.normalize(None), list(platformpolicy.PLATFORMS))

    def test_normalize_rejects_unknown(self):
        with self.assertRaises(ValueError):
            platformpolicy.normalize(['not-a-platform'])

    def test_normalize_keeps_stable_order(self):
        got = platformpolicy.normalize(['workbuddy', 'doubaowork'])
        self.assertEqual(got, ['doubaowork', 'workbuddy'])

    def test_qoder_is_recognisable_but_not_served(self):
        """qoder 摘除后的语义是「可识别、但不可用」，不是「非法」。

        留 `_ACCEPTED` 里那一项是为了存量数据：历史密钥 / 旧 users.json 里
        可能还写着 `qoder`，`normalize` 抛 ValueError 会让鉴权链路 500 ——
        一个已下线的平台不该把在线服务搞挂。真正的可用性由 `PLATFORMS`
        和 `runtime.qoder is None` 决定。
        """
        self.assertNotIn('qoder', platformpolicy.PLATFORMS)
        self.assertIn('qoder', platformpolicy._ACCEPTED)
        self.assertEqual(platformpolicy.normalize(['qoder']), ['qoder'])
        # 但「不传 = 全部」时绝不能把 qoder 带回来
        self.assertNotIn('qoder', platformpolicy.normalize(None))
        # 真正非法的仍然要拒
        with self.assertRaises(ValueError):
            platformpolicy.normalize(['not-a-platform'])


class PlatformSelectTests(unittest.TestCase):
    def test_path_prefix(self):
        self.assertEqual(platformpolicy.select('/doubaowork/v1/chat/completions'),
                         'doubaowork')

    def test_header(self):
        self.assertEqual(platformpolicy.select('/v1/models', 'doubaowork'), 'doubaowork')

    def test_model_prefix(self):
        self.assertEqual(platformpolicy.select('/v1/models', '', 'doubaowork/m'),
                         'doubaowork')

    def test_disagreement_rejected(self):
        with self.assertRaises(platformpolicy.PlatformConflict):
            platformpolicy.select('/doubaowork/v1/models', 'qoder')

    def test_default_stays_workbuddy(self):
        self.assertEqual(platformpolicy.select('/v1/models'), 'workbuddy')


class MigrationTests(unittest.TestCase):
    def test_historical_backfill_stays_three_platforms(self):
        """v5 补列只给当年那三平台；不能把新平台回填给历史密钥。"""
        conn = sqlite3.connect(':memory:')
        try:
            conn.execute('CREATE TABLE api_keys(id INTEGER PRIMARY KEY, name TEXT)')
            conn.execute("INSERT INTO api_keys VALUES(1, 'old')")
            migrations._key_platforms(conn)
            row = conn.execute('SELECT platforms FROM api_keys').fetchone()
            self.assertEqual(json.loads(row[0]),
                             ['workbuddy', 'qoder', 'trae'])
        finally:
            conn.close()


class AuthzPlatformTests(unittest.TestCase):
    def test_header_query_and_path_all_resolve(self):
        self.assertEqual(authz.platform_of(make_request([(b'x-qtg-platform', b'doubaowork')])),
                         'doubaowork')
        self.assertEqual(authz.platform_of(make_request(query=b'platform=doubaowork')),
                         'doubaowork')
        self.assertEqual(authz.platform_of(make_request(path='/doubaowork/v1/models')),
                         'doubaowork')

    def test_unresolved_keeps_legacy_sentinel(self):
        self.assertEqual(authz.platform_of(make_request()), '三平台')


class MappingTests(unittest.TestCase):
    def test_busy_maps_to_ready(self):
        a = service.map_doubaowork_account(RAW_READY)
        self.assertEqual(a.state, 'ready')
        self.assertEqual(a.in_flight, 1)
        self.assertEqual(a.platform, 'doubaowork')

    def test_cooling_carries_reason(self):
        a = service.map_doubaowork_account(RAW_COOLING)
        self.assertEqual(a.state, 'cooling')
        self.assertTrue(a.in_cooldown)
        self.assertEqual(a.note, '710022004')
        self.assertEqual(a.errors, 1)

    def test_disabled(self):
        self.assertEqual(service.map_doubaowork_account(RAW_DISABLED).state, 'disabled')

    def test_unknown_state_is_unknown_not_ready(self):
        self.assertEqual(service.map_doubaowork_account(RAW_UNKNOWN).state, 'unknown')

    def test_credits_are_zero_and_explained(self):
        a = service.map_doubaowork_account(RAW_READY)
        self.assertEqual((a.credits_remain, a.credits_total, a.credits_used),
                         (0.0, 0.0, 0.0))
        self.assertIn('上游不提供额度', a.note)

    def test_no_invented_fields(self):
        a = service.map_doubaowork_account(RAW_READY)
        self.assertIsNone(a.cooldown_until)
        self.assertIsNone(a.expires_at)
        self.assertFalse(a.checked_today)
        self.assertEqual(a.realm, '')


class PayloadTests(unittest.TestCase):
    def build(self):
        agg = service.Aggregate()
        agg.doubaowork_accounts = [
            service.map_doubaowork_account(r)
            for r in (RAW_READY, RAW_COOLING, RAW_DISABLED)
        ]
        return agg, service.accounts_payload(agg)

    def test_response_has_field(self):
        self.assertIn('doubaowork', AccountsResponse.model_fields)

    def test_payload_carries_rows_and_summary(self):
        _, payload = self.build()
        self.assertEqual(len(payload.doubaowork), 3)
        self.assertIn('doubaowork', payload.summary)
        self.assertEqual(payload.summary['doubaowork'].total, 3)
        self.assertEqual(payload.summary['all'].total, 3)

    def test_all_accounts_includes_fourth_platform(self):
        agg, _ = self.build()
        self.assertEqual(len(service.all_accounts(agg)), 3)

    def test_serialization_keeps_zero_credits(self):
        _, payload = self.build()
        dumped = payload.model_dump()
        self.assertEqual(dumped['doubaowork'][0]['credits_remain'], 0.0)

    def test_summary_credits_do_not_inflate(self):
        _, payload = self.build()
        self.assertEqual(payload.summary['all'].credits, 0)


class CompatUpstreamIdTests(unittest.TestCase):
    def test_doubaowork_takes_first_slot(self):
        """2026-11-19：豆包工作顶替 qoder 的 1 号位置。"""
        from server.qtgcore.routers import compat_fixes
        self.assertEqual(compat_fixes._PLAT_TO_UPSTREAM.get('doubaowork'), 1)
        self.assertEqual(compat_fixes.UPSTREAM_IDS.get(1), 'doubaowork')
        self.assertEqual(compat_fixes.UPSTREAM_NAMES.get(1), '豆包工作')
        # qoder 完全退出编号表
        self.assertNotIn('qoder', compat_fixes._PLAT_TO_UPSTREAM)
        self.assertNotIn('qoder', compat_fixes.UPSTREAM_IDS.values())

    def test_rows_of_returns_doubaowork(self):
        from server.qtgcore.routers import compat_fixes
        agg = service.Aggregate()
        agg.doubaowork_accounts = [service.map_doubaowork_account(RAW_READY)]
        self.assertEqual(len(compat_fixes._rows_of(agg, 1)), 1)
        self.assertEqual(len(compat_fixes._rows_of(agg, None)), 1)


if __name__ == '__main__':
    unittest.main()
