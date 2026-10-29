"""U19：settings/panel 形状、端点身份矩阵与写端点 CSRF 契约。

审计指出的三件事，这里逐条变成断言：

  1. **形状**：`settings` 是 key/value 表，`/api/settings/panel` 却用
     `SELECT * ... LIMIT 1` 取一行 —— 响应变成 `{'key':…, 'value':…}`，而且取到
     哪一行由扫描顺序决定（可能正好是 `github_token`，于是任何登录用户都能读到
     凭据明文）。现在必须是 key→value 形状 + 白名单。
  2. **身份矩阵**：`/api/settings/*`、`/api/v1/*`（QTG 控制面）、`/api/system/*`
     每条路由「方法 / 所需身份 / 是否写库 / 是否可能返回秘密 / CSRF」都钉在这里：
     匿名 401、viewer 按各自闸门 403 或放行、admin 通过。
  3. **CSRF**：Cookie 会话的写端点必须同源；跨站 Origin 被拒、浏览器语义下缺
     Origin 被拒；GET 不得有副作用（用表快照对比）。

所有请求都走隔离运行器里的 TestClient，**不碰生产**。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security  # noqa: E402

ADMIN_PW = 'AdminPw123!u19'
VIEWER_PW = 'ViewerPw123!u19'
ORIGIN = 'http://testserver'
#: 被测模块：只有这三个文件里的端点归本任务负责。
MODULES = ('server.routers.settings', 'server.routers.qtg_panel',
           'server.routers.system')
WRITE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
#: 直接调用同源校验的闸门（它们内部已调用 `check_same_origin`）。
CSRF_GATES = frozenset({'require_admin', 'require_session_admin',
                        'require_keys_writer'})


def _collect_routes(app):
    """摊平 app 里的 APIRoute，并取每条路由的依赖名（与 test_api_tokens 同一套取法）。"""
    from fastapi.routing import APIRoute
    rows = []
    for route in app.routes:
        inner = getattr(route, 'original_router', None)
        if inner is not None:
            candidates = [x for x in inner.routes if isinstance(x, APIRoute)]
        else:
            candidates = [route] if isinstance(route, APIRoute) else []
        for candidate in candidates:
            if not candidate.path.startswith('/api'):
                continue
            names: list[str] = []

            def walk(dep, _names=names) -> None:
                if dep is None:
                    return
                call = getattr(dep, 'call', None)
                if call is not None:
                    _names.append(getattr(call, '__name__', str(call)))
                for sub in getattr(dep, 'dependencies', []) or []:
                    walk(sub)

            walk(getattr(candidate, 'dependant', None))
            rows.append((candidate, frozenset(names)))
    return rows


class _EndpointCase(unittest.TestCase):
    """共享夹具：临时 DB + 临时 users.json + 三个身份的客户端。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        directory = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE)
        config.DB_PATH = directory / 'u19.db'
        config.USERS_FILE = directory / 'users.json'
        security.save_users({
            'secret': 'S' * 64,
            'users': [
                {'username': 'admin', 'role': 'admin',
                 'pwd_hash': security.make_hash(ADMIN_PW)},
                {'username': 'viewer', 'role': 'viewer',
                 'pwd_hash': security.make_hash(VIEWER_PW)},
            ],
            'api_keys': [],
        })
        db._conn = None
        db.connect()
        from server.main import app
        cls.app = app
        cls.routes = _collect_routes(app)

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

    def client_for(self, who: str) -> TestClient:
        client = TestClient(self.app)
        if who == 'anonymous':
            return client
        password = ADMIN_PW if who == 'admin' else VIEWER_PW
        response = client.post('/api/login',
                               json={'username': who, 'password': password},
                               headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 200, response.text)
        client.cookies.update(dict(response.cookies))
        return client

    def route_for(self, method: str, path: str):
        for route, names in self.routes:
            if route.path == path and method in (route.methods or set()):
                return route, names
        raise AssertionError(f'找不到路由 {method} {path} —— 端点改名了？这条守卫会空转')

    def qtg_unavailable(self):
        """把 QTG 控制面固定成 503：本机没有 qoder/trae 运行时。

        为什么打桩而不是让它真去连：真实调用会尝试解析 `qoder2` / `trae2` 这些
        主机，测试里既慢又不确定。身份与 CSRF 都在**依赖**里判定（早于 handler），
        所以打桩不影响这两项结论。
        """
        from fastapi import HTTPException

        async def _unavailable():
            raise HTTPException(status_code=503,
                                detail='qoder/trae 控制面不可用（测试桩）')

        return mock.patch('server.routers.qtg_panel._rt_or_503', _unavailable)

    def reset_settings(self) -> None:
        db.execute('DELETE FROM settings')


# ────────────────────────────────────────────────────────────────────────
# 1. 端点矩阵
# ────────────────────────────────────────────────────────────────────────
#: (method, path, 所需身份, admin 探测方式, body)
#:
#: 身份取值与 `server/security.py` 的依赖一一对应：
#:   current_user / admin / session_admin / admin_or_read_scope
#:
#: admin 探测方式：
#:   ok            —— 真发一次，断言不是 401/403
#:   service_503   —— QTG 控制面在本机没有运行时，认证过后必然 503（仍证明鉴权通过）
#:   loopback_403  —— 端点只允许本机调用，TestClient 的 peer 不是回环 → 403
#:   skip          —— **不执行**（会重启上游 / 启动更新 / 清状态），只做依赖声明核对
MATRIX = (
    # ── /api/settings/* 与同文件里的 /api/users、/api/audit-logs ──────────
    ('GET', '/api/settings/upstream', 'current_user', 'ok', None),
    ('POST', '/api/settings/upstream', 'session_admin', 'skip', None),
    ('GET', '/api/upstream/reload-state', 'current_user', 'ok', None),
    ('POST', '/api/settings/upstash/test', 'session_admin', 'ok', {'url': ''}),
    ('POST', '/api/settings/upstash/reload', 'session_admin', 'skip', None),
    ('GET', '/api/settings/model-map', 'current_user', 'ok', None),
    ('POST', '/api/settings/model-map', 'admin', 'ok', {}),
    ('GET', '/api/settings/github-token', 'admin', 'ok', None),
    ('PUT', '/api/settings/github-token', 'admin', 'ok', {'token': ''}),
    ('GET', '/api/settings/github-mirror', 'admin', 'ok', None),
    ('PUT', '/api/settings/github-mirror', 'admin', 'ok', {'mirror': ''}),
    ('GET', '/api/settings/panel', 'current_user', 'ok', None),
    ('GET', '/api/users', 'current_user', 'ok', None),
    ('POST', '/api/users', 'session_admin', 'skip', None),
    ('PATCH', '/api/users/{username}', 'session_admin', 'skip', None),
    ('DELETE', '/api/users/{username}', 'session_admin', 'skip', None),
    ('GET', '/api/audit-logs', 'admin_or_read_scope', 'ok', None),
    # ── QTG 控制面（qtg_panel.py，前缀 /api/v1）──────────────────────────
    ('GET', '/api/v1/overview', 'current_user', 'service_503', None),
    ('GET', '/api/v1/accounts', 'current_user', 'skip', None),
    ('GET', '/api/v1/usage', 'current_user', 'skip', None),
    ('GET', '/api/v1/tasks', 'current_user', 'skip', None),
    ('GET', '/api/v1/keys', 'current_user', 'skip', None),
    ('GET', '/api/v1/models', 'current_user', 'skip', None),
    ('GET', '/api/v1/logs', 'current_user', 'skip', None),
    ('GET', '/api/v1/alerts', 'current_user', 'skip', None),
    ('GET', '/api/v1/requests', 'current_user', 'skip', None),
    ('GET', '/api/v1/ops', 'current_user', 'skip', None),
    ('GET', '/api/v1/settings', 'current_user', 'skip', None),
    ('GET', '/api/v1/audit', 'current_user', 'skip', None),
    ('POST', '/api/v1/cache/invalidate', 'current_user', 'service_503', None),
    # ── /api/system/* ────────────────────────────────────────────────────
    ('GET', '/api/system/configuration', 'admin', 'ok', None),
    # U16 新增的配置四源对照（只读、仅管理员）。登记在这里是为了让
    # `test_matrix_covers_every_route_in_scope` 继续有效：将来任何任务在
    # 这三个模块里加端点，都必须同时在这里补一行身份契约。
    ('GET', '/api/system/config', 'admin', 'ok', None),
    ('GET', '/api/system/drain', 'admin', 'ok', None),
    ('POST', '/api/system/drain', 'session_admin', 'skip', None),
    ('GET', '/api/system/update-status', 'current_user', 'ok', None),
    ('POST', '/api/system/update', 'session_admin', 'skip', None),
    ('GET', '/api/system/update-package', 'current_user', 'ok', None),
    ('POST', '/api/system/update-package', 'session_admin', 'skip', None),
    ('DELETE', '/api/system/update-package', 'session_admin', 'ok', None),
    ('DELETE', '/api/system/update-status', 'session_admin', 'ok', None),
    ('GET', '/api/system/check-update', 'current_user', 'ok', None),
    ('GET', '/api/system/versions', 'current_user', 'ok', None),
    ('GET', '/api/system/changelog', 'current_user', 'ok', None),
    ('GET', '/api/system/internal-session-verify', 'current_user', 'loopback_403', None),
)

EXPECTED_GATE = {
    'current_user': {'current_user'},
    'admin': {'require_admin'},
    'session_admin': {'require_session_admin'},
    'admin_or_read_scope': {'require_admin_or_read_scope'},
}


class EndpointMatrixTest(_EndpointCase):
    def test_matrix_covers_every_route_in_scope(self) -> None:
        """矩阵必须覆盖三个模块里的**全部**路由 —— 新增端点不能悄悄溜过去。"""
        listed = {(method, path) for method, path, *_ in MATRIX}
        actual = set()
        for route, _names in self.routes:
            if route.endpoint.__module__ not in MODULES:
                continue
            for method in (route.methods or set()):
                if method in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
                    actual.add((method, route.path))
        self.assertEqual(actual - listed, set(),
                         f'这些端点没进身份矩阵：{sorted(actual - listed)}')
        self.assertEqual(listed - actual, set(),
                         f'矩阵里有已不存在的端点：{sorted(listed - actual)}')

    def test_declared_identity_matches_the_dependency(self) -> None:
        """矩阵里写的身份必须真的由对应依赖把关（读代码与读测试的结论一致）。"""
        for method, path, identity, _probe, _body in MATRIX:
            with self.subTest(endpoint=f'{method} {path}'):
                _route, names = self.route_for(method, path)
                self.assertTrue(EXPECTED_GATE[identity] & names,
                                f'{method} {path} 声明为 {identity}，实际依赖 {sorted(names)}')

    def test_anonymous_is_rejected_everywhere(self) -> None:
        client = self.client_for('anonymous')
        for method, path, _identity, _probe, body in MATRIX:
            with self.subTest(endpoint=f'{method} {path}'):
                response = client.request(method, path, json=body)
                self.assertEqual(response.status_code, 401,
                                 f'匿名访问 {method} {path} 得到 {response.status_code}')

    def test_viewer_is_rejected_where_admin_is_required(self) -> None:
        client = self.client_for('viewer')
        for method, path, identity, _probe, body in MATRIX:
            if identity == 'current_user':
                continue
            with self.subTest(endpoint=f'{method} {path}'):
                response = client.request(method, path, json=body)
                self.assertEqual(response.status_code, 403,
                                 f'viewer 访问 {method} {path} 得到 {response.status_code}')

    def test_viewer_is_allowed_on_read_scope_endpoints(self) -> None:
        """`current_user` 端点：viewer 必须放行（既有能力不削减）。"""
        client = self.client_for('viewer')
        with self.qtg_unavailable():
            for method, path, identity, probe, body in MATRIX:
                if identity != 'current_user' or probe in {'skip', 'loopback_403'}:
                    continue
                with self.subTest(endpoint=f'{method} {path}'):
                    response = client.request(method, path, json=body)
                    self.assertNotIn(response.status_code, (401, 403),
                                     f'viewer 被挡在 {method} {path}：{response.text[:200]}')

    def test_admin_passes_every_probed_endpoint(self) -> None:
        client = self.client_for('admin')
        with mock.patch('server.services.updater.check_updates',
                        lambda force=False: {'cached': True, 'manager': {},
                                             'upstream': {}}), self.qtg_unavailable():
            for method, path, _identity, probe, body in MATRIX:
                if probe == 'skip':
                    continue
                with self.subTest(endpoint=f'{method} {path}', probe=probe):
                    response = client.request(method, path, json=body)
                    if probe == 'ok':
                        self.assertNotIn(response.status_code, (401, 403),
                                         f'admin 被挡在 {method} {path}：'
                                         f'{response.text[:200]}')
                    elif probe == 'service_503':
                        self.assertEqual(response.status_code, 503,
                                         f'{method} {path} 期望 503（控制面不可用），'
                                         f'实际 {response.status_code}')
                    elif probe == 'loopback_403':
                        self.assertEqual(response.status_code, 403)
                        self.assertIn('本机', response.text)


# ────────────────────────────────────────────────────────────────────────
# 2. /api/settings/panel 的形状与秘密
# ────────────────────────────────────────────────────────────────────────
class SettingsPanelShapeTest(_EndpointCase):
    def setUp(self) -> None:
        self.reset_settings()

    def _panel(self, who: str = 'viewer'):
        response = self.client_for(who).get('/api/settings/panel')
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_key_value_table_is_not_read_as_a_single_row(self) -> None:
        """核心回归：旧实现返回 `{'key':…, 'value':…}`（第一行的两列）。"""
        db.set_setting('model_map', {'alias': 'real-model'})
        body = self._panel().json()
        self.assertEqual(body, {'model_map': {'alias': 'real-model'}})
        self.assertNotIn('key', body, '响应又变成了"单行配置"的形状')
        self.assertNotIn('value', body, '响应又变成了"单行配置"的形状')

    def test_secret_rows_are_never_returned(self) -> None:
        """只有敏感键时必须是 `{}`（"没配置"），而不是把凭据当配置发出去。"""
        db.set_setting('github_token', 'ghp_CANARY_SECRET_123')
        db.set_setting('pg_sync_config', {'password': 'pg-CANARY-PW'})
        db.set_setting('update_proxy', 'http://user:proxy-CANARY@proxy.example:8080')
        db.set_setting('security', {'ip_allowlist': 'CANARY-NET'})
        response = self._panel()
        self.assertEqual(response.json(), {})
        for canary in ('CANARY', 'ghp_', 'proxy.example'):
            self.assertNotIn(canary, response.text,
                             f'设置面板泄露了敏感内容：{canary}')

    def test_whitelist_keeps_non_secret_settings(self) -> None:
        db.set_setting('model_map', {'a': 'b'})
        db.set_setting('global_proxy_enabled', False)
        db.set_setting('github_token', 'ghp_CANARY')
        body = self._panel().json()
        self.assertEqual(sorted(body), ['global_proxy_enabled', 'model_map'])
        self.assertIs(body['global_proxy_enabled'], False)

    def test_empty_table_returns_empty_dict(self) -> None:
        """QTG 读通道依赖这个契约（空表 = 没配置，不是"读不到"）。"""
        self.assertEqual(self._panel().json(), {})

    def test_values_are_parsed_json(self) -> None:
        """值是 JSON 文本，出口必须是解析后的结构（与 db.get_setting 同口径）。"""
        db.set_setting('task_claim_schedule', {'hours': [9, 21], 'enabled': True})
        self.assertEqual(self._panel().json()['task_claim_schedule'],
                         {'hours': [9, 21], 'enabled': True})

    def test_panel_is_readable_by_viewer_but_carries_no_credentials(self) -> None:
        db.set_setting('github_token', 'ghp_CANARY')
        db.set_setting('model_map', {'a': 'b'})
        viewer_body = self._panel('viewer').json()
        admin_body = self._panel('admin').json()
        self.assertEqual(viewer_body, admin_body,
                         '面板数据不该因为身份不同而不同（敏感键对谁都不发）')


# ────────────────────────────────────────────────────────────────────────
# 3. 写端点的 CSRF / Origin
# ────────────────────────────────────────────────────────────────────────
class WriteEndpointCsrfTest(_EndpointCase):
    def test_identity_entry_point_enforces_same_origin(self) -> None:
        """同源校验挂在**身份入口** `_current_user`。

        为什么是那里：管理面所有已认证请求（`current_user` 依赖）与 `__qtg`
        兼容层（`main._qtg_platform_route` 直接调本函数）共用它，一处挂上等于
        全覆盖。这里同时断言「函数体里真的调了」，避免只定义不调用。
        """
        source = (Path(__file__).resolve().parents[1] / 'security.py'
                  ).read_text(encoding='utf-8')
        body = source.split('def _current_user(', 1)[1].split('\ndef ', 1)[0]
        self.assertIn('check_same_origin(request)', body,
                      '身份入口没有调用同源校验 —— 写端点就失去 CSRF 保护')

    def test_every_write_endpoint_in_scope_is_gated(self) -> None:
        """三个模块里的写端点必须都过同源校验（身份入口或显式依赖）。"""
        missing = []
        for route, names in self.routes:
            if route.endpoint.__module__ not in MODULES:
                continue
            if not (set(route.methods or ()) & WRITE_METHODS):
                continue
            # `current_user` 是身份入口（内部已调 check_same_origin）；
            # 三个 admin 闸门都依赖它，`require_same_origin` 是显式声明。
            gated = (bool(CSRF_GATES & names) or 'current_user' in names
                     or 'require_same_origin' in names)
            if not gated:
                missing.append(f'{sorted(route.methods)} {route.path}')
        self.assertEqual(missing, [], f'这些写端点没有同源校验：{missing}')

    def test_cookie_write_without_origin_is_rejected(self) -> None:
        """浏览器语义（带 Sec-Fetch-*）但缺 Origin/Referer → 403。"""
        client = self.client_for('admin')
        response = client.put('/api/settings/github-mirror', json={'mirror': ''},
                              headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'csrf_origin_missing')

    def test_cross_site_origin_is_rejected(self) -> None:
        client = self.client_for('admin')
        response = client.put('/api/settings/github-mirror', json={'mirror': ''},
                              headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'csrf_origin_mismatch')

    def test_lookalike_host_origin_is_rejected(self) -> None:
        """子串匹配的经典绕过：`testserver.evil.example` 必须过不去。"""
        client = self.client_for('admin')
        response = client.put('/api/settings/github-mirror', json={'mirror': ''},
                              headers={'Origin': 'http://testserver.evil.example'})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'csrf_origin_mismatch')

    def test_origin_with_credentials_is_rejected(self) -> None:
        """`http://testserver@evil.example` 形态（userinfo 绕过）必须被拒。"""
        client = self.client_for('admin')
        response = client.put('/api/settings/github-mirror', json={'mirror': ''},
                              headers={'Origin': 'http://testserver@evil.example'})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'csrf_origin_malformed')

    def test_multiple_origin_headers_are_rejected(self) -> None:
        client = self.client_for('admin')
        response = client.put(
            '/api/settings/github-mirror', json={'mirror': ''},
            headers=[('origin', ORIGIN), ('origin', 'https://evil.example')])
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'csrf_multiple_origin')

    def test_same_origin_write_is_allowed(self) -> None:
        client = self.client_for('admin')
        response = client.put('/api/settings/github-mirror', json={'mirror': ''},
                              headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 200, response.text)

    def test_referer_is_accepted_as_fallback(self) -> None:
        client = self.client_for('admin')
        response = client.put('/api/settings/github-mirror', json={'mirror': ''},
                              headers={'Referer': ORIGIN + '/settings'})
        self.assertEqual(response.status_code, 200, response.text)

    def test_non_browser_client_without_browser_headers_is_allowed(self) -> None:
        """无 Origin/Referer/Sec-Fetch 的机器调用不受 CSRF 影响（无 CSRF 语义）。

        浏览器对非安全方法一定会带 Origin；一个 Fetch 头都没有的请求不可能来自
        浏览器。这条**有意**不拒 —— 否则所有"带会话 Cookie 的脚本/CI 调用"都会
        被打断，而安全上并没有多出任何东西（详见 security.check_same_origin）。
        """
        client = self.client_for('admin')
        response = client.put('/api/settings/github-mirror', json={'mirror': ''})
        self.assertEqual(response.status_code, 200, response.text)

    def test_read_only_token_write_still_blocked_before_csrf(self) -> None:
        """Bearer 令牌没有 Cookie → 不做 CSRF 判定，但仍由鉴权挡住写操作。"""
        client = TestClient(self.app)
        response = client.put('/api/settings/github-mirror', json={'mirror': ''},
                              headers={'Authorization': 'Bearer wbt_not_a_real_token'})
        self.assertEqual(response.status_code, 401, response.text)

    def test_current_user_write_endpoint_checks_origin_before_the_handler(self) -> None:
        """/api/v1/cache/invalidate 只要求 current_user，但同源校验必须在
        真正执行前发生（否则跨站页面能借会话把缓存打空）。"""
        client = self.client_for('viewer')
        response = client.post('/api/v1/cache/invalidate',
                               headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'csrf_origin_mismatch')

    def test_platform_routed_compat_write_is_also_origin_checked(self) -> None:
        """`__qtg` 兼容层（`X-Qtg-Platform` 路由）同样被同源校验覆盖。

        `qtgcore/routers/compat*.py` 的 handler 签名里**没有**任何 manager 依赖，
        它们靠 `main._qtg_platform_route` 调 `security._current_user` 鉴权 ——
        同源校验挂在这个身份入口上，所以这条路径一并受保护（这也是把校验放在
        `_current_user` 而不是各 admin 闸门里的原因）。
        """
        client = self.client_for('admin')
        path = '/api/accounts/u19-probe-account.txt'
        platform = {'X-Qtg-Platform': 'qoder'}
        cross = client.delete(path, headers={**platform,
                                             'Origin': 'https://evil.example'})
        self.assertIn(cross.status_code, (401, 403),
                      f'跨站请求打到了兼容层写端点：{cross.status_code} {cross.text[:200]}')
        same = client.delete(path, headers={**platform, 'Origin': ORIGIN})
        self.assertNotIn(same.status_code, (401, 403),
                         f'同源请求被误拦：{same.status_code} {same.text[:200]}')


# ────────────────────────────────────────────────────────────────────────
# 4. GET 不得有副作用
# ────────────────────────────────────────────────────────────────────────
SAFE_GETS = (
    '/api/settings/upstream',
    '/api/settings/model-map',
    '/api/settings/panel',
    '/api/settings/github-token',
    '/api/settings/github-mirror',
    '/api/upstream/reload-state',
    '/api/users',
    '/api/audit-logs',
    '/api/system/configuration',
    '/api/system/drain',
    '/api/system/update-status',
    '/api/system/update-package',
    '/api/system/changelog',
)


class GetEndpointsHaveNoSideEffectsTest(_EndpointCase):
    def _snapshot(self) -> dict:
        """只快照**业务状态**（settings / 日志 / 审计 / 用户文件）。

        `request_outcomes` 不计入：那是 `observability` 中间件对**每个**请求
        都会写的一行（包含 GET），属于可观测性副作用，不是端点行为。
        """
        settings = db.query('SELECT key, value FROM settings ORDER BY key')
        return {
            'settings': sorted((str(row['key']), str(row['value'])) for row in settings),
            'request_logs': db.query_one('SELECT count(*) n FROM request_logs')['n'],
            'audit_logs': db.query_one('SELECT count(*) n FROM audit_logs')['n'],
            'users_file': Path(config.USERS_FILE).read_text(encoding='utf-8'),
        }

    def test_read_endpoints_do_not_change_state(self) -> None:
        self.reset_settings()
        db.set_setting('model_map', {'x': 'y'})
        db.set_setting('github_token', 'ghp_CANARY')
        client = self.client_for('admin')
        before = self._snapshot()
        with self.qtg_unavailable():
            for path in SAFE_GETS:
                response = client.get(path)
                self.assertNotIn(response.status_code, (401, 403),
                                 f'{path} 对 admin 不可用：{response.text[:200]}')
        after = self._snapshot()
        self.assertEqual(before, after,
                         'GET 端点改变了业务状态 —— 读接口不能有副作用')

    def test_repeated_reads_are_stable(self) -> None:
        """同一个 GET 连读两次结果必须一致（幂等）。"""
        self.reset_settings()
        db.set_setting('model_map', {'x': 'y'})
        client = self.client_for('admin')
        with self.qtg_unavailable():
            for path in SAFE_GETS:
                with self.subTest(path=path):
                    first = client.get(path)
                    second = client.get(path)
                    self.assertEqual(first.status_code, second.status_code, path)
                    self.assertEqual(first.content, second.content, path)


if __name__ == '__main__':
    unittest.main()
