"""U17：作用域合同（platform + realm + source_authority + config_generation）。

审计要三件事，这里逐条变成断言：

1. **作用域进缓存 key**：`aggregate` / `proxy.catalog` / `qoder.models` /
   `trae.models` 的 key 必须带 platform / realm / config_generation，而不是
   字面量 —— 否则配置改了还命中旧条目（表现为"改了不生效"）。
2. **冲突输入显式处理**：路径前缀 / `X-QTG-Platform` / 模型名前缀三者不一致时
   必须 400（`platform_conflict`），非法 realm 必须 400（`invalid_realm`）；
   客户端要了平台但本端点不按平台取数时必须**显式降级**并写进响应元数据，
   不能静默返回同一份数据。
3. **不串台**：不同 platform / realm / config_generation 的并发请求不得读到
   彼此的缓存。既有实现里 `modelcatalog` 已经按 realm 分键（这是对的），
   `aggregate` 等此前只有字面量 key（本轮补上作用域）—— 两类都用测试钉住。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, platformpolicy, security  # noqa: E402
from server.services import scope as scope_service  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PW = 'AdminPw123!u17'


# ────────────────────────────────────────────────────────────────────────
# 1. 平台冲突：三种来源不一致必须显式 400
# ────────────────────────────────────────────────────────────────────────
class PlatformConflictTest(unittest.TestCase):
    def test_single_source_wins(self) -> None:
        self.assertEqual(platformpolicy.select('/qoder/v1/models'), 'qoder')
        self.assertEqual(platformpolicy.select('/v1/models', 'trae'), 'trae')
        self.assertEqual(platformpolicy.select('/v1/models', '', 'qoder/glm-5.2'), 'qoder')
        self.assertEqual(platformpolicy.select('/v1/models'), 'workbuddy',
                         '没有任何信号时默认 workbuddy（既有行为）')

    def test_agreeing_sources_are_fine(self) -> None:
        self.assertEqual(
            platformpolicy.select('/qoder/v1/chat/completions', 'qoder', 'qoder/glm-5.2'),
            'qoder')

    def test_path_and_header_disagree(self) -> None:
        with self.assertRaises(platformpolicy.PlatformConflict) as ctx:
            platformpolicy.select('/qoder/v1/models', 'trae')
        self.assertEqual(ctx.exception.code, 'platform_conflict')

    def test_header_and_model_prefix_disagree(self) -> None:
        with self.assertRaises(platformpolicy.PlatformConflict) as ctx:
            platformpolicy.select('/v1/chat/completions', 'trae', 'qoder/glm-5.2')
        self.assertEqual(ctx.exception.code, 'platform_conflict')

    def test_unknown_header_is_rejected(self) -> None:
        with self.assertRaises(platformpolicy.PlatformConflict) as ctx:
            platformpolicy.select('/v1/models', 'qoder2')
        self.assertEqual(ctx.exception.code, 'invalid_platform')

    def test_middleware_turns_conflict_into_400(self) -> None:
        """数据面路径上的冲突要变成 400，而不是随便挑一个平台转发。"""
        async def app(scope, receive, send):  # pragma: no cover - 不该被调用
            raise AssertionError('冲突请求不该到达下游应用')

        middleware = platformpolicy.PlatformRoutingMiddleware(app)
        captured = {}

        async def send(message):
            if message['type'] == 'http.response.start':
                captured['status'] = message['status']
            elif message['type'] == 'http.response.body':
                captured['body'] = message.get('body', b'')

        async def receive():
            return {'type': 'http.request', 'body': b''}

        scope = {'type': 'http', 'method': 'POST', 'path': '/qoder/v1/chat/completions',
                 'headers': [(b'x-qtg-platform', b'trae')], 'query_string': b''}
        asyncio.run(middleware(scope, receive, send))
        self.assertEqual(captured.get('status'), 400)
        self.assertIn(b'platform_conflict', captured.get('body', b''))


# ────────────────────────────────────────────────────────────────────────
# 2. 作用域对象与缓存 key
# ────────────────────────────────────────────────────────────────────────
class ScopeKeyTest(unittest.TestCase):
    def test_cache_key_carries_every_dimension(self) -> None:
        scope = scope_service.Scope(platform='qoder', realm='global',
                                    source_authority='upstream',
                                    config_generation='abc123abc123')
        key = scope.cache_key('models')
        self.assertEqual(key, 'models|p=qoder|r=global|g=abc123abc123')
        for marker in ('p=qoder', 'r=global', 'g=abc123abc123'):
            self.assertIn(marker, key)

    def test_different_scope_never_shares_a_key(self) -> None:
        base = dict(realm='cn', source_authority='upstream',
                    config_generation='gen000000001')
        keys = {
            scope_service.Scope(platform='qoder', **base).cache_key('models'),
            scope_service.Scope(platform='trae', **base).cache_key('models'),
            scope_service.Scope(platform='qoder', **dict(base, realm='global')).cache_key('models'),
            scope_service.Scope(platform='qoder', **dict(base, config_generation='gen000000002')).cache_key('models'),
        }
        self.assertEqual(len(keys), 4, f'不同作用域必须得到不同 key：{keys}')

    def test_prefix_invalidation_still_works(self) -> None:
        """`cache.invalidate('aggregate')` 必须仍能清掉所有代/所有作用域的条目。"""
        from server.qtgcore.cache import TTLCache
        store = TTLCache()
        keys = [scope_service.Scope(platform=p, realm='all', config_generation=g).cache_key('aggregate')
                for p in ('all', 'qoder') for g in ('g1', 'g2')]
        for key in keys:
            store._entries[key] = type('E', (), {'value': 1, 'expires_at': 0.0, 'lock': None})()
        store._entries['other'] = type('E', (), {'value': 1, 'expires_at': 0.0, 'lock': None})()
        self.assertEqual(store.invalidate('aggregate'), 4)
        self.assertEqual(list(store._entries), ['other'])

    def test_scoped_key_defaults_to_all_platforms(self) -> None:
        key = scope_service.scoped_key('aggregate')
        self.assertTrue(key.startswith('aggregate|p=all|r=all|g='), key)
        generation = key.rsplit('g=', 1)[1]
        self.assertEqual(len(generation), 12, '配置指纹应为 12 位 hex')

    def test_fixed_scope_rejects_unknown_values(self) -> None:
        with self.assertRaises(scope_service.ScopeConflict):
            scope_service.fixed('qoder2')
        with self.assertRaises(scope_service.ScopeConflict):
            scope_service.fixed(realm='eu')
        with self.assertRaises(scope_service.ScopeConflict):
            scope_service.fixed(authority='vibes')


class ConfigGenerationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._cfg = Path(self._tmp.name) / 'config.json'
        self._cfg.write_text('{"server":{"listen":":7863"}}\n', encoding='utf-8')
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self._cfg
        scope_service.reset_generation()

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        scope_service.reset_generation()
        self._tmp.cleanup()

    def test_stable_within_memo_window(self) -> None:
        first = scope_service.config_generation(force=True)
        self.assertEqual(first, scope_service.config_generation())
        self.assertEqual(len(first), 12)

    def test_bump_changes_generation(self) -> None:
        before = scope_service.config_generation(force=True)
        after = scope_service.bump_generation()
        self.assertNotEqual(before, after, '显式推进一代必须换指纹')

    def test_upstream_config_change_changes_generation(self) -> None:
        before = scope_service.config_generation(force=True)
        self._cfg.write_text('{"server":{"listen":":7864"},"reasoning_as_content":false}\n',
                             encoding='utf-8')
        after = scope_service.config_generation(force=True)
        self.assertNotEqual(before, after,
                            '上游配置变了，缓存 key 必须跟着变（否则"改了不生效"）')

    def test_model_map_change_changes_generation(self) -> None:
        with mock.patch.object(scope_service, '_model_map_fingerprint',
                               side_effect=['aaa', 'bbb']):
            before = scope_service.config_generation(force=True)
            after = scope_service.config_generation(force=True)
        self.assertNotEqual(before, after)


# ────────────────────────────────────────────────────────────────────────
# 3. realm 与降级
# ────────────────────────────────────────────────────────────────────────
class RealmAndDegradeTest(unittest.TestCase):
    def _request(self, path='/api/read-api/version', headers=(), query=b''):
        from starlette.requests import Request
        return Request({'type': 'http', 'method': 'GET', 'path': path,
                        'headers': list(headers), 'query_string': query,
                        'server': ('testserver', 80), 'scheme': 'http',
                        'client': ('testclient', 1234)})

    def test_realm_values(self) -> None:
        self.assertEqual(scope_service.resolve_realm(None), 'all')
        self.assertEqual(scope_service.resolve_realm(''), 'all')
        self.assertEqual(scope_service.resolve_realm('cn'), 'cn')
        self.assertEqual(scope_service.resolve_realm(' GLOBAL '), 'global')

    def test_whitespace_is_normalised_not_rejected(self) -> None:
        """`realm=cn ` 这种只是多打了一个空格，归一化即可（不该 400 打扰用户）。"""
        self.assertEqual(scope_service.resolve_realm('cn '), 'cn')
        self.assertEqual(scope_service.resolve_realm('\tglobal\n'), 'global')

    def test_invalid_realm_is_explicit(self) -> None:
        """拼错/含糊的 realm 必须显式 400，而不是静默退化成"不过滤"。"""
        for bad in ('glboal', 'eu', 'cn:global', 'cnglobal', 1):
            with self.subTest(realm=bad):
                try:
                    got = scope_service.resolve_realm(bad)
                except scope_service.ScopeConflict as exc:
                    self.assertEqual(exc.code, 'invalid_realm')
                else:
                    self.fail(f'realm={bad!r} 应被拒，实际 {got!r}')

    def test_platform_header_is_degraded_not_ignored(self) -> None:
        """客户端要 qoder、端点却不按平台取数 → 显式降级（不是静默同数据）。"""
        request = self._request(headers=[(b'x-qtg-platform', b'qoder')])
        scope = scope_service.for_request(request, authority='local_db')
        self.assertEqual(scope.platform, 'qoder')
        self.assertEqual(scope.requested_platform, 'qoder')
        self.assertFalse(scope.honored)
        self.assertEqual(scope.degraded, ('platform_not_routable',))
        self.assertIn('platform_not_routable', scope.metadata()['degraded'])

    def test_platform_routed_endpoint_honours_the_platform(self) -> None:
        request = self._request(headers=[(b'x-qtg-platform', b'qoder')])
        scope = scope_service.for_request(request, authority='upstream',
                                          platform_routed=True)
        self.assertTrue(scope.honored)
        self.assertEqual(scope.degraded, ())
        self.assertNotIn('degraded', scope.metadata())

    def test_no_platform_request_means_all(self) -> None:
        scope = scope_service.for_request(self._request(), authority='local_db')
        self.assertEqual(scope.platform, 'all')
        self.assertTrue(scope.honored)
        self.assertIsNone(scope.metadata()['platform_requested'])

    def test_query_platform_is_a_fallback(self) -> None:
        request = self._request(query=b'platform=trae')
        self.assertEqual(scope_service.requested_platform(request), 'trae')

    def test_header_wins_over_query(self) -> None:
        request = self._request(headers=[(b'x-qtg-platform', b'qoder')],
                                query=b'platform=trae')
        self.assertEqual(scope_service.requested_platform(request), 'qoder')

    def test_unknown_platform_is_explicit(self) -> None:
        request = self._request(headers=[(b'x-qtg-platform', b'qoder2')])
        with self.assertRaises(scope_service.ScopeConflict) as ctx:
            scope_service.requested_platform(request)
        self.assertEqual(ctx.exception.code, 'invalid_platform')


# ────────────────────────────────────────────────────────────────────────
# 4. 串台验证
# ────────────────────────────────────────────────────────────────────────
class CacheIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        from server.qtgcore.cache import cache
        cache.invalidate()
        self.addCleanup(cache.invalidate)

    def test_concurrent_scopes_do_not_share_entries(self) -> None:
        """同一份数据、不同作用域并发请求：各自算各自的，不串。"""
        from server.qtgcore.cache import TTLCache
        store = TTLCache()
        calls: list[str] = []

        async def run() -> list:
            def factory(platform):
                async def build():
                    calls.append(platform)
                    await asyncio.sleep(0.01)
                    return f'payload-{platform}'
                return build

            scopes = [scope_service.Scope(platform=p, realm='all',
                                          config_generation='gen000000001')
                      for p in ('qoder', 'trae', 'workbuddy')]
            return await asyncio.gather(*[
                store.get_or_set(s.cache_key('models'), 60, factory(s.platform))
                for s in scopes])

        values = asyncio.run(run())
        self.assertEqual(sorted(values), ['payload-qoder', 'payload-trae',
                                         'payload-workbuddy'])
        self.assertEqual(sorted(calls), ['qoder', 'trae', 'workbuddy'],
                         '三个作用域各算一次，说明没有互相命中')

    def test_realm_scopes_do_not_share_entries(self) -> None:
        from server.qtgcore.cache import TTLCache
        store = TTLCache()
        calls: list[str] = []

        async def run() -> list:
            def factory(realm):
                async def build():
                    calls.append(realm)
                    return f'catalog-{realm}'
                return build

            return await asyncio.gather(*[
                store.get_or_set(
                    scope_service.Scope(platform='workbuddy', realm=r,
                                        config_generation='gen000000001').cache_key('catalog'),
                    60, factory(r))
                for r in ('cn', 'global')])

        values = asyncio.run(run())
        self.assertEqual(sorted(values), ['catalog-cn', 'catalog-global'])
        self.assertEqual(sorted(calls), ['cn', 'global'])

    def test_same_scope_is_cached_once(self) -> None:
        from server.qtgcore.cache import TTLCache
        store = TTLCache()
        calls: list[int] = []

        async def build():
            calls.append(1)
            return 'x'

        async def run() -> None:
            scope = scope_service.Scope(platform='qoder', realm='cn',
                                        config_generation='gen000000001')
            for _ in range(3):
                await store.get_or_set(scope.cache_key('models'), 60, build)

        asyncio.run(run())
        self.assertEqual(len(calls), 1, '同作用域必须命中缓存')

    def test_new_generation_does_not_reuse_old_entry(self) -> None:
        """配置换代后不得再读旧条目 —— 这是"改了不生效"的根因。"""
        from server.qtgcore.cache import TTLCache
        store = TTLCache()
        calls: list[str] = []

        async def run() -> list:
            def factory(gen):
                async def build():
                    calls.append(gen)
                    return f'data-{gen}'
                return build

            out = []
            for gen in ('gen000000001', 'gen000000002'):
                scope = scope_service.Scope(platform='all', realm='all',
                                            config_generation=gen)
                out.append(await store.get_or_set(scope.cache_key('aggregate'),
                                                  60, factory(gen)))
            return out

        values = asyncio.run(run())
        self.assertEqual(values, ['data-gen000000001', 'data-gen000000002'])
        self.assertEqual(len(calls), 2)

    def test_modelcatalog_realm_slots_are_independent(self) -> None:
        """既有实现里 `modelcatalog` 已按 realm 分键 —— 把它钉住。"""
        from server.services import modelcatalog
        saved = dict(modelcatalog._cache)
        try:
            modelcatalog._cache.clear()
            modelcatalog._cache['cn'] = {'at': 1.0, 'ttl': 300,
                                         'payload': {'models': [{'id': 'cn-a'}]}}
            modelcatalog._cache['global'] = {'at': 1.0, 'ttl': 300,
                                             'payload': {'models': [{'id': 'global-b'}]}}
            self.assertEqual(modelcatalog.cached_ids('cn'), {'cn-a'})
            self.assertEqual(modelcatalog.cached_ids('global'), {'global-b'})
            modelcatalog.invalidate('cn')
            self.assertIsNone(modelcatalog.cached_ids('cn'))
            self.assertEqual(modelcatalog.cached_ids('global'), {'global-b'},
                             '清一个版本不能连另一个一起清')
        finally:
            modelcatalog._cache.clear()
            modelcatalog._cache.update(saved)

    def test_aggregate_cache_key_has_no_platform_bleed(self) -> None:
        from server.qtgcore import service
        qoder = scope_service.Scope(platform='qoder', realm='all',
                                    config_generation='gen000000001')
        trae = scope_service.Scope(platform='trae', realm='all',
                                   config_generation='gen000000001')
        self.assertNotEqual(service._aggregate_cache_key(qoder),
                            service._aggregate_cache_key(trae))
        self.assertTrue(service._aggregate_cache_key(qoder).startswith('aggregate|'))


class AggregateCacheIntegrationTest(unittest.TestCase):
    """`build_aggregate` 真的按作用域取缓存（不是只在函数里拼了个 key）。"""

    class _Adapter:
        name = 'fake'
        base_url = 'http://127.0.0.1:1'

        async def accounts(self):
            return {'accounts': []}

        async def usage(self, days):
            return {}

        async def usage_analytics(self):
            return {}

        async def credits_flow(self, days):
            return {}

        async def scheduler(self):
            return {}

        async def overview(self):
            return {'accounts': []}

        async def settings(self):
            return {}

        async def health(self):
            return {'up': True, 'latency_ms': 1}

    def _rt(self):
        class _Settings:
            cache_overview_sec = 60
            qoder_base = 'http://127.0.0.1:1'
            trae_base = 'http://127.0.0.1:2'
        rt = mock.Mock()
        rt.settings = _Settings()
        rt.qoder = self._Adapter()
        rt.trae = self._Adapter()
        rt.wb = None
        return rt

    def setUp(self) -> None:
        from server.qtgcore.cache import cache
        cache.invalidate()
        self.addCleanup(cache.invalidate)

    def test_same_scope_hits_cache(self) -> None:
        from server.qtgcore import service
        rt = self._rt()
        scope = scope_service.Scope(platform='all', realm='all',
                                    config_generation='gen000000001')
        first = asyncio.run(service.build_aggregate(rt, scope=scope))
        second = asyncio.run(service.build_aggregate(rt, scope=scope))
        self.assertIs(first, second, '同作用域第二次必须命中缓存')

    def test_new_generation_rebuilds(self) -> None:
        from server.qtgcore import service
        rt = self._rt()
        first = asyncio.run(service.build_aggregate(
            rt, scope=scope_service.Scope(platform='all', realm='all',
                                          config_generation='gen000000001')))
        second = asyncio.run(service.build_aggregate(
            rt, scope=scope_service.Scope(platform='all', realm='all',
                                          config_generation='gen000000002')))
        self.assertIsNot(first, second, '换代后必须重新构建，不能复用旧条目')


# ────────────────────────────────────────────────────────────────────────
# 5. 缓存 key 清单（source 守卫：不许退回字面量 key）
# ────────────────────────────────────────────────────────────────────────
class CacheKeyInventoryTest(unittest.TestCase):
    def _src(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding='utf-8')

    def test_aggregate_uses_scoped_key(self) -> None:
        src = self._src('qtgcore/service.py')
        self.assertIn('_aggregate_cache_key(', src)
        self.assertNotIn('get_or_set("aggregate"', src)
        self.assertNotIn("get_or_set('aggregate'", src)

    def test_proxy_catalog_uses_scoped_key(self) -> None:
        src = self._src('qtgcore/routers/proxy.py')
        self.assertIn('scoped_key("proxy.catalog")', src)
        self.assertNotIn('get_or_set("proxy.catalog"', src)

    def test_panel_model_caches_are_platform_scoped(self) -> None:
        src = self._src('routers/qtg_panel.py')
        self.assertIn("scoped_key('qoder.models', fixed('qoder'", src)
        self.assertIn("scoped_key('trae.models', fixed('trae'", src)
        self.assertNotIn("get_or_set('qoder.models'", src)
        self.assertNotIn("get_or_set('trae.models'", src)


# ────────────────────────────────────────────────────────────────────────
# 6. 端到端：响应元数据（body + 响应头）
# ────────────────────────────────────────────────────────────────────────
class ResponseScopeMetadataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        directory = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE)
        config.DB_PATH = directory / 'u17.db'
        config.USERS_FILE = directory / 'users.json'
        security.save_users({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash(ADMIN_PW)}],
            'api_keys': [],
        })
        db._conn = None
        db.connect()
        from server.main import app
        cls.app = app
        cls.client = TestClient(app)
        response = cls.client.post('/api/login',
                                   json={'username': 'admin', 'password': ADMIN_PW},
                                   headers={'Origin': 'http://testserver'})
        assert response.status_code == 200, response.text
        cls.client.cookies.update(dict(response.cookies))

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def test_version_carries_scope_in_body_and_headers(self) -> None:
        r = self.client.get('/api/read-api/version')
        self.assertEqual(r.status_code, 200, r.text)
        scope = r.json()['scope']
        for field in ('platform', 'realm', 'source_authority', 'config_generation'):
            self.assertIn(field, scope, f'响应元数据缺 {field}')
        self.assertEqual(scope['platform'], 'all')
        self.assertEqual(scope['realm'], 'all')
        self.assertEqual(scope['source_authority'], 'local_db')
        self.assertTrue(scope['platform_honored'])
        self.assertEqual(r.headers.get('x-scope-platform'), 'all')
        self.assertEqual(r.headers.get('x-scope-authority'), 'local_db')
        self.assertEqual(r.headers.get('x-scope-config-generation'),
                         scope['config_generation'])

    def test_usage_endpoints_carry_scope(self) -> None:
        for path in ('/api/read-api/usage-totals', '/api/read-api/health'):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIn('scope', r.json(), f'{path} 缺作用域元数据')
        r = self.client.get('/api/read-api/usage-matrix', params={'since': '2020-01-01'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['scope']['realm'], 'all',
                         '不按 realm 过滤的端点必须显式声明 realm=all')

    def test_platform_header_is_degraded_in_metadata(self) -> None:
        r = self.client.get('/api/read-api/version',
                            headers={'X-Qtg-Platform': 'qoder'})
        self.assertEqual(r.status_code, 200, r.text)
        scope = r.json()['scope']
        self.assertEqual(scope['platform'], 'qoder')
        self.assertEqual(scope['platform_requested'], 'qoder')
        self.assertFalse(scope['platform_honored'])
        self.assertEqual(scope['degraded'], ['platform_not_routable'])
        self.assertEqual(r.headers.get('x-scope-degraded'), 'platform_not_routable')

    def test_query_platform_is_also_degraded(self) -> None:
        r = self.client.get('/api/read-api/version', params={'platform': 'trae'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['scope']['platform'], 'trae')
        self.assertFalse(r.json()['scope']['platform_honored'])

    def test_unknown_platform_is_rejected(self) -> None:
        r = self.client.get('/api/read-api/version',
                            headers={'X-Qtg-Platform': 'qoder2'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('invalid_platform', r.text)

    def test_config_generation_changes_after_bump(self) -> None:
        before = self.client.get('/api/read-api/version').json()['scope']['config_generation']
        scope_service.bump_generation()
        after = self.client.get('/api/read-api/version').json()['scope']['config_generation']
        self.assertNotEqual(before, after)


# ────────────────────────────────────────────────────────────────────────
# 7. 面板（/api/v1/*）的作用域接线
#
# 本机没有 qoder/trae 运行时，`/api/v1/*` 平时恒 503 —— 那意味着面板侧的作用域
# 接线（`_panel_scope` / `meta['scope']` / `attach`）**一行都没被执行过**。
# 这里用假 runtime 把 handler 真正跑起来，避免"接线写错了但测试全绿"。
# ────────────────────────────────────────────────────────────────────────
class PanelScopeWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        directory = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE)
        config.DB_PATH = directory / 'u17-panel.db'
        config.USERS_FILE = directory / 'users.json'
        security.save_users({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash(ADMIN_PW)}],
            'api_keys': [],
        })
        db._conn = None
        db.connect()
        from server.main import app
        cls.client = TestClient(app)
        response = cls.client.post('/api/login',
                                   json={'username': 'admin', 'password': ADMIN_PW},
                                   headers={'Origin': 'http://testserver'})
        assert response.status_code == 200, response.text
        cls.client.cookies.update(dict(response.cookies))

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    @staticmethod
    def _fake_rt():
        from types import SimpleNamespace

        class _Settings:
            read_requires_auth = True
            qoder_base = 'http://127.0.0.1:1'
            trae_base = 'http://127.0.0.1:2'
            data_dir = '/tmp/u17-fake'

        class _Adapter:
            async def settings(self):
                return {'api_keys': []}

            async def keys(self):
                return {}

        class _Audit:
            def tail(self, limit):
                return []

            def verify(self):
                return True

            def count(self):
                return 0

        return SimpleNamespace(settings=_Settings(), qoder=_Adapter(),
                               trae=_Adapter(), audit=_Audit())

    def _patch_rt(self):
        runtime = self._fake_rt()

        async def _rt():
            return runtime

        return mock.patch('server.routers.qtg_panel._rt_or_503', _rt)

    def test_settings_endpoint_exposes_scope_metadata(self) -> None:
        with self._patch_rt():
            response = self.client.get('/api/v1/settings')
        self.assertEqual(response.status_code, 200, response.text)
        scope = response.json()['qtg']['scope']
        self.assertEqual(scope['platform'], 'all')
        self.assertEqual(scope['realm'], 'all')
        self.assertEqual(scope['source_authority'], 'upstream')
        self.assertEqual(len(scope['config_generation']), 12)

    def test_audit_endpoint_exposes_scope_metadata(self) -> None:
        with self._patch_rt():
            response = self.client.get('/api/v1/audit')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('scope', response.json())

    def test_cache_invalidate_degrades_explicitly(self) -> None:
        with self._patch_rt():
            response = self.client.post('/api/v1/cache/invalidate',
                                        headers={'X-Qtg-Platform': 'qoder'})
        self.assertEqual(response.status_code, 200, response.text)
        scope = response.json()['scope']
        self.assertEqual(scope['platform'], 'qoder')
        self.assertFalse(scope['platform_honored'],
                         '面板不按平台取数，必须显式标注"平台未被采纳"')
        self.assertEqual(scope['degraded'], ['platform_not_routable'])


if __name__ == '__main__':
    unittest.main()
