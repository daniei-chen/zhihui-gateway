"""D-41：写操作的同源锚点不能由请求自己的 `Host` 头决定（DNS rebinding）。

## 修前实测（本文件第 5 角就是它的反面）

`expected_origin()` 以前用 `Host` 拼「本站期望来源」：

    POST /api/login   Host: evil.test:8000   Origin: http://evil.test:8000   -> 200

DNS rebinding 下浏览器访问 `http://evil.test:8000`，`Host` 与 `Origin` 天然相等，
于是**任意域名**都能通过同源校验（审计已实测能建立会话、创建 token）。

## 现在的判定顺序（与 `server/security.py` 的 D-41 注释同一份口径）

1. `WB_PUBLIC_ORIGIN`：部署方声明的公网来源，唯一权威；设了它 `Host` 完全不参与。
2. `WB_CORS_ORIGINS`：跨源白名单，外加「不可能被攻击者控制」的同源 Host。
3. 都没配：只采信**可信反代**（peer 落在 `WB_TRUSTED_PROXY_CIDRS`）送来的
   `X-Forwarded-Host`；非可信 peer 的 XFH 是普通请求头，忽略。
4. 最后才是 IP 字面量 / `localhost` / 单标签内网名这类「不可能被 rebinding」的 Host。
5. 逃生门 `WB_ALLOW_UNCONFIGURED_ORIGIN=1`：退回修前的 Host 锚点（跨站仍拦），
   启动日志醒目告警。默认关。

没有锚点时拒绝的机器可读原因：body `detail == 'origin_unconfigured'`，响应头
`X-Manager-Csrf-Reason` 同值 —— 前端靠它把「通用 403」换成页面横幅。

## 七角矩阵（大队长口径，逐条可证）

| # | 形态 | 期望 |
| --- | --- | --- |
| 1 | 回环 peer、无配置、Host=Origin=127.0.0.1 | 200 |
| 2 | IP 字面量、无配置 | 200 |
| 3 | 单标签内网名（`http://nas/`）、无配置 | 200 |
| 4 | 已配 `WB_PUBLIC_ORIGIN` 且匹配 | 200 |
| 5 | 点分域名直连、无配置、POST | 403 + `origin_unconfigured` |
| 6 | 第 5 角 + 逃生门 | 200，启动日志有醒目 warn |
| 7 | Origin 与 Host 真不一致（跨站） | 403（回归不能松） |

## 为什么用 `httpx.ASGITransport` 而不是 `TestClient`

「是不是可信反代」由 `request.client.host`（TCP 对端）决定。`TestClient` 的对端
固定是 `testclient`，说不出这件事；`ASGITransport(client=...)` 可以逐条指定。
"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from server import config, security
from server.routers import auth, portal, system
from server.services import platformauth

LOOPBACK = ('127.0.0.1', 5000)
LAN = ('10.0.0.9', 5000)


def _app(with_probe: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(auth.router)

    if with_probe:
        @app.api_route('/api/probe/write', methods=['POST', 'GET'])
        def probe(request: Request):
            security.check_same_origin(request)
            return {'ok': True}

    return app


def _request(host: str, origin: str, *, peer=LAN, method: str = 'POST',
             extra: dict | None = None) -> Request:
    headers = [(b'host', host.encode()), (b'origin', origin.encode())]
    headers += [(k.encode(), v.encode()) for k, v in (extra or {}).items()]
    scope = {
        'type': 'http', 'method': method, 'http_version': '1.1',
        'scheme': 'http', 'path': '/api/portal/x', 'raw_path': b'/api/portal/x',
        'query_string': b'', 'root_path': '',
        'headers': headers,
        'client': peer, 'server': ('testserver', 80),
    }
    return Request(scope)


class _AnchorCase(unittest.TestCase):
    """每个用例都从「没有配置」的干净状态开始，避免相互串味。"""

    def setUp(self) -> None:
        self._patch(config, 'PUBLIC_ORIGIN', '')
        self._patch(config, 'CORS_ORIGINS', [])
        self._patch(config, 'TRUST_PROXY', '1')
        self._patch(config, 'TRUSTED_PROXY_CIDRS', ['127.0.0.0/8', '::1/128'])
        self._patch(config, 'ALLOW_UNCONFIGURED_ORIGIN', False)
        security._unanchored_warned.clear()
        patcher = mock.patch.object(auth, '_complete_login',
                                    return_value=JSONResponse({'ok': True}))
        self.login = patcher.start()
        self.addCleanup(patcher.stop)
        self.app = _app()

    def _patch(self, target, name, value) -> None:
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, path: str = '/api/login', *, peer=LAN, host: str = 'testserver',
              origin: str | None = None, extra: dict | None = None,
              cookie: bool = False, method: str = 'post', app: FastAPI | None = None):
        headers = {'host': host}
        if origin is not None:
            headers['origin'] = origin
        if cookie:
            headers['cookie'] = f'{config.COOKIE_NAME}=synthetic'
        headers.update(extra or {})
        return asyncio.run(self._send(app or self.app, method, path, peer, headers))

    async def _send(self, app: FastAPI, method: str, path: str, peer, headers):
        transport = httpx.ASGITransport(app=app, client=peer)
        async with httpx.AsyncClient(transport=transport,
                                     base_url='http://testserver') as client:
            call = getattr(client, method)
            if method in ('get', 'head'):
                return await call(path, headers=headers)
            return await call(path, json={'username': 'u', 'password': 'p'},
                              headers=headers)


class SevenAngleMatrix(_AnchorCase):
    """大队长口径的七角矩阵：每条都要能独立证明。"""

    def test_angle_1_loopback_peer_without_config_passes(self) -> None:
        response = self._call(peer=LOOPBACK, host='127.0.0.1:7864',
                              origin='http://127.0.0.1:7864')
        self.assertEqual(response.status_code, 200, response.text)
        self.login.assert_called_once()

    def test_angle_2_ip_literal_without_config_passes(self) -> None:
        response = self._call(peer=LAN, host='192.168.1.5:7864',
                              origin='http://192.168.1.5:7864')
        self.assertEqual(response.status_code, 200, response.text)

    def test_angle_3_single_label_host_without_config_passes(self) -> None:
        response = self._call(peer=LAN, host='nas', origin='http://nas')
        self.assertEqual(response.status_code, 200, response.text)

    def test_angle_4_configured_public_origin_passes(self) -> None:
        self._patch(config, 'PUBLIC_ORIGIN', 'https://panel.example.com')
        response = self._call(peer=LAN, host='panel.example.com',
                              origin='https://panel.example.com')
        self.assertEqual(response.status_code, 200, response.text)

    def test_angle_5_dotted_domain_direct_is_403_with_machine_reason(self) -> None:
        response = self._call(peer=LAN, host='evil.test:8000',
                              origin='http://evil.test:8000')
        self.assertEqual(response.status_code, 403,
                         'Host 与 Origin 相同就放行 —— 同源锚点又回到了请求头手里')
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'origin_unconfigured')
        self.assertEqual(response.json().get('detail'), 'origin_unconfigured',
                         '403 body 没带机器可读原因，前端只能弹通用错误')
        self.login.assert_not_called()

    def test_angle_6_escape_hatch_restores_the_old_anchor(self) -> None:
        self._patch(config, 'ALLOW_UNCONFIGURED_ORIGIN', True)
        response = self._call(peer=LAN, host='panel.example.com',
                              origin='http://panel.example.com')
        self.assertEqual(response.status_code, 200, response.text)

    def test_angle_6_escape_hatch_warns_loudly_at_startup(self) -> None:
        self._patch(config, 'ALLOW_UNCONFIGURED_ORIGIN', True)
        with self.assertLogs('workbuddy.config', level='WARNING') as captured:
            config._warn_origin_anchoring()
        text = '\n'.join(captured.output)
        self.assertIn('WB_ALLOW_UNCONFIGURED_ORIGIN', text)
        self.assertIn('DNS rebinding', text)

    def test_angle_6_escape_hatch_does_not_relax_cross_site(self) -> None:
        """逃生门只放宽「点分域名无配置」的锚点 —— 不是「不比 Origin」。"""
        self._patch(config, 'ALLOW_UNCONFIGURED_ORIGIN', True)
        response = self._call(peer=LAN, host='panel.example.com',
                              origin='https://evil.example')
        self.assertEqual(response.status_code, 403,
                         '开了逃生门就放行跨站 —— 那不是旧行为，是真 CSRF')
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'csrf_origin_mismatch')

    def test_angle_7_cross_site_origin_is_still_rejected(self) -> None:
        response = self._call(peer=LAN, host='testserver',
                              origin='https://evil.example')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'csrf_origin_mismatch')


class ReboundHostIsRejected(_AnchorCase):
    """`Host == Origin == 攻击者域名` 必须失败 —— 这是修前被实测通过的那条。"""

    def test_loopback_proxy_does_not_launder_an_attacker_domain(self) -> None:
        """本机反代 + X-Forwarded-Proto 也不能把 evil.test 变成「本站」。"""
        response = self._call(peer=LOOPBACK, host='evil.test',
                              origin='https://evil.test',
                              extra={'x-forwarded-proto': 'https'})
        self.assertEqual(response.status_code, 403)
        self.login.assert_not_called()

    def test_session_cookie_write_on_a_rebound_host_is_rejected(self) -> None:
        """真正会被 CSRF 利用的入口：带会话 Cookie 的写请求。"""
        response = self._call('/api/probe/write', cookie=True,
                              host='evil.test:8000',
                              origin='http://evil.test:8000')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json().get('detail'), 'origin_unconfigured')

    def test_the_refusal_is_explained_once_in_the_log(self) -> None:
        """没有锚点不是「配错了」而是「没配」，要能在日志里看到该设什么。"""
        with self.assertLogs('workbuddy.security', level='WARNING') as captured:
            self._call(host='evil.test', origin='http://evil.test')
        self.assertTrue(any('WB_PUBLIC_ORIGIN' in line for line in captured.output),
                        '告警里没说要设哪个变量：%s' % captured.output)

    def test_safe_methods_are_untouched(self) -> None:
        """GET 不参与同源校验 —— 读页面不能被这条修复打坏。"""
        response = self._call('/api/probe/write', method='get',
                              host='evil.test', origin='http://evil.test')
        self.assertEqual(response.status_code, 200)


class ConfiguredOriginIsAuthoritative(_AnchorCase):
    """配了 `WB_PUBLIC_ORIGIN` 就只认它，Host 完全退出判定。"""

    def test_public_origin_passes_and_everything_else_fails(self) -> None:
        self._patch(config, 'PUBLIC_ORIGIN', 'https://panel.example.com')
        allowed = self._call(host='panel.example.com',
                             origin='https://panel.example.com')
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.login.assert_called_once()

        for host, origin, extra in (
                ('evil.test', 'https://evil.test', {}),
                ('127.0.0.1:7864', 'http://127.0.0.1:7864', {}),
                ('panel.example.com', 'https://evil.test',
                 {'x-forwarded-host': 'evil.test'})):
            with self.subTest(host=host, origin=origin, extra=extra):
                response = self._call(host=host, origin=origin, extra=extra)
                self.assertEqual(response.status_code, 403,
                                 'PUBLIC_ORIGIN 设了还认别的来源')

    def test_trailing_slash_and_explicit_port_are_normalized(self) -> None:
        self._patch(config, 'PUBLIC_ORIGIN', 'https://panel.example.com:8443/')
        response = self._call(host='panel.example.com:8443',
                              origin='https://panel.example.com:8443')
        self.assertEqual(response.status_code, 200, response.text)

    def test_malformed_public_origin_fails_loudly(self) -> None:
        """写错配置不能静默放行/静默全拒：请求期抛错，启动期同样会抛。"""
        self._patch(config, 'PUBLIC_ORIGIN', 'panel.example.com')
        with self.assertRaises(RuntimeError):
            self._call(host='panel.example.com', origin='http://panel.example.com')

    def test_cors_allowlist_anchors_writes(self) -> None:
        self._patch(config, 'CORS_ORIGINS', ['https://spa.example.com'])
        allowed = self._call(host='127.0.0.1:7864',
                             origin='https://spa.example.com')
        self.assertEqual(allowed.status_code, 200, allowed.text)
        evil = self._call(host='evil.test', origin='https://evil.test')
        self.assertEqual(evil.status_code, 403)


class TrustedProxyOnly(_AnchorCase):
    """`X-Forwarded-Host` 只在 peer 属于 `WB_TRUSTED_PROXY_CIDRS` 时算数。"""

    def test_forwarded_host_from_a_trusted_proxy_is_honored(self) -> None:
        response = self._call(peer=LOOPBACK, host='127.0.0.1:7864',
                              origin='https://panel.example.com',
                              extra={'x-forwarded-host': 'panel.example.com',
                                     'x-forwarded-proto': 'https'})
        self.assertEqual(response.status_code, 200, response.text)

    def test_forwarded_host_from_an_untrusted_peer_is_ignored(self) -> None:
        response = self._call(peer=LAN, host='testserver',
                              origin='http://evil.test',
                              extra={'x-forwarded-host': 'evil.test',
                                     'x-forwarded-proto': 'http'})
        self.assertEqual(response.status_code, 403,
                         '非可信 peer 的 X-Forwarded-Host 被采信了')

    def test_forwarded_proto_from_an_untrusted_peer_is_ignored(self) -> None:
        """与 `client_ip` 同一口径：scheme 也不能由非可信 peer 声明。"""
        response = self._call(peer=LAN, host='10.0.0.9:7864',
                              origin='https://10.0.0.9:7864',
                              extra={'x-forwarded-proto': 'https'})
        self.assertEqual(response.status_code, 403,
                         '非可信 peer 的 X-Forwarded-Proto 被采信了')

    def test_trust_proxy_off_disables_forwarded_host(self) -> None:
        self._patch(config, 'TRUST_PROXY', False)
        response = self._call(peer=LOOPBACK, host='127.0.0.1:7864',
                              origin='https://panel.example.com',
                              extra={'x-forwarded-host': 'panel.example.com',
                                     'x-forwarded-proto': 'https'})
        self.assertEqual(response.status_code, 403)


class OriginStatusField(_AnchorCase):
    """管理端状态字段（`GET /api/system/origin-status`，管理页横幅/安全页状态栏）。

    **不在 `/api/me` 里**：那是每次页面加载都拉的身份响应，
    `test_authorization_generation.test_internal_owner_binding_is_not_exposed_by_session_identity`
    用逐字相等钉住它的形状（`{'username','role'}`）—— 往里加字段就是在扩 API
    表面，而 D-41 要的只是「运维看得见锚点状态」。
    """

    def test_states_cover_every_anchor_shape(self) -> None:
        cases = (
            ('loopback-only', True,
             _request('192.168.1.5:7864', 'http://192.168.1.5:7864', peer=LAN)),
            ('unconfigured', False,
             _request('panel.example.com', 'http://panel.example.com', peer=LAN)),
            ('proxy', True,
             _request('127.0.0.1:7864', 'https://panel.example.com', peer=LOOPBACK,
                      extra={'x-forwarded-host': 'panel.example.com',
                             'x-forwarded-proto': 'https'})),
        )
        for expected_state, expected_allowed, request in cases:
            with self.subTest(expected=expected_state):
                status = security.origin_status(request)
                self.assertEqual(status['state'], expected_state)
                self.assertEqual(status['writes_allowed'], expected_allowed)

    def test_configured_and_relaxed_states(self) -> None:
        self._patch(config, 'PUBLIC_ORIGIN', 'https://panel.example.com')
        self.assertEqual(
            security.origin_status(_request('panel.example.com',
                                            'https://panel.example.com'))['state'],
            'configured')
        self._patch(config, 'PUBLIC_ORIGIN', '')
        self._patch(config, 'ALLOW_UNCONFIGURED_ORIGIN', True)
        status = security.origin_status(_request('panel.example.com',
                                                 'http://panel.example.com'))
        self.assertEqual(status['state'], 'relaxed')
        self.assertTrue(status['writes_allowed'])

    def test_unconfigured_reports_writes_blocked(self) -> None:
        status = security.origin_status(_request('panel.example.com',
                                                 'http://panel.example.com'))
        self.assertEqual(status['state'], 'unconfigured')
        self.assertFalse(status['writes_allowed'])
        self.assertEqual(status['host'], 'panel.example.com')

    def test_public_origin_check_endpoint_is_anonymous(self) -> None:
        """登录页要能在**填表之前**读到状态：这个 GET 不做鉴权。"""
        response = self._call('/api/origin-check', method='get', peer=LAN,
                              host='panel.example.com')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['state'], 'unconfigured')
        self.assertFalse(response.json()['writes_allowed'])

    def _panel_app(self, user: dict | None = None) -> FastAPI:
        """只替换会话身份，**不**替换 `require_panel_reader` —— 让真正的角色闸门跑到。"""
        app = FastAPI()
        app.include_router(system.router)
        app.dependency_overrides[security.current_user] = lambda: (
            user or {'username': 'fixture', 'role': 'admin'})
        self.addCleanup(app.dependency_overrides.clear)
        return app

    def test_the_management_status_endpoint_carries_the_state(self) -> None:
        """管理端状态端点：消费方（横幅/状态栏）拿到的是同一份字段。"""
        response = self._call('/api/system/origin-status', method='get', peer=LAN,
                              host='panel.example.com', app=self._panel_app())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['state'], 'unconfigured')
        self.assertFalse(body['writes_allowed'])
        self.assertEqual(body['host'], 'panel.example.com')

    def test_a_viewer_session_reads_the_field_too(self) -> None:
        """人类只读角色（account_admin / viewer）也要看得到横幅 —— 否则等于没写。"""
        app = self._panel_app({'username': 'watcher', 'role': 'account_admin'})
        response = self._call('/api/system/origin-status', method='get', peer=LAN,
                              host='panel.example.com', app=app)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['state'], 'unconfigured')

    def test_an_api_token_cannot_read_the_field(self) -> None:
        """带 scope 的机器令牌不是「面板读者」：锚点状态里有运维拓扑（Host）。"""
        app = self._panel_app({'username': 'token', 'role': 'admin', 'scope': 'keys'})
        response = self._call('/api/system/origin-status', method='get', peer=LAN,
                              host='panel.example.com', app=app)
        self.assertEqual(response.status_code, 403, response.text)

    def test_the_management_status_endpoint_requires_a_session(self) -> None:
        """不是匿名端点：锚点形态只给已登录的面板用户（host 是运维拓扑）。"""
        app = FastAPI()
        app.include_router(system.router)
        response = self._call('/api/system/origin-status', method='get', peer=LAN,
                              host='panel.example.com', app=app)
        self.assertEqual(response.status_code, 401, response.text)

    def test_api_me_keeps_its_identity_only_shape(self) -> None:
        """回归钉子：`/api/me` 的 body 只能是身份两个字段。

        这条与 `test_authorization_generation` 的那条同向，但在这里守。把
        `origin_check` 塞回 `/api/me`（变异 M9）→ 两个文件各红一条。
        """
        app = FastAPI()
        app.include_router(auth.router)
        app.dependency_overrides[security.current_user] = lambda: {
            'username': 'fixture', 'role': 'admin'}
        self.addCleanup(app.dependency_overrides.clear)
        response = self._call('/api/me', method='get', peer=LAN,
                              host='panel.example.com', app=app)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'username': 'fixture', 'role': 'admin'})


class SideGatesUseTheSameAnchors(_AnchorCase):
    """portal / platformauth 的旁路判定必须和主入口同源。"""

    def test_portal_write_gate_rejects_the_rebound_host(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            portal.same_origin(_request('evil.test:8000', 'http://evil.test:8000'))
        self.assertEqual(caught.exception.status_code, 403)

    def test_portal_write_gate_accepts_a_configured_origin(self) -> None:
        self._patch(config, 'PUBLIC_ORIGIN', 'https://panel.example.com')
        portal.same_origin(_request('panel.example.com', 'https://panel.example.com'))

    def test_platform_poll_gate_rejects_the_rebound_host(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            platformauth.check_poll_origin(
                _request('evil.test', 'https://evil.test'))
        self.assertEqual(caught.exception.status_code, 403)

    def test_platform_poll_gate_accepts_a_configured_origin(self) -> None:
        self._patch(config, 'PUBLIC_ORIGIN', 'https://panel.example.com')
        platformauth.check_poll_origin(
            _request('panel.example.com', 'https://panel.example.com'))


class DeploymentTrustModel(unittest.TestCase):
    """信任判定只能有一份：应用里的 `iputil.client_ip` / `WB_TRUSTED_PROXY_CIDRS`。

    uvicorn 的 `proxy_headers`（默认开、`forwarded_allow_ips=127.0.0.1`）会按
    `X-Forwarded-For` 把 `scope['client']` 改写成浏览器地址。仓库附带的 nginx
    配置（deploy/README.md）**会**传 XFF，于是「对端是不是本机反代」永远判错：
    来源锚点拿不到 X-Forwarded-Host（写请求 403），`/api/system/internal-session-verify`
    的「仅本机」也会拒掉真正的 nginx。启动器必须显式关掉这个改写。

    关掉之后的连带责任：任何按 `request.client.host` 判「仅本机」的端点都会退化成
    「凡经 nginx 的请求都算本机」，所以 `internal-session-verify` 改成两层判据
    （对端 + `client_ip()`），见 `InternalVerifyStaysLoopbackOnly`。
    """

    ROOT = Path(__file__).resolve().parents[2]

    def test_launchers_disable_uvicorn_proxy_header_rewriting(self) -> None:
        for rel in ('Dockerfile', 'deploy/workbuddy-web.service', 'deploy/install.sh'):
            with self.subTest(path=rel):
                text = (self.ROOT / rel).read_text(encoding='utf-8')
                self.assertIn('--no-proxy-headers', text,
                              f'{rel} 的 uvicorn 启动命令没有 --no-proxy-headers：'
                              '反代信任会被 uvicorn 改写 scope["client"] 破坏')
        serve = (self.ROOT / 'server' / 'serve.py').read_text(encoding='utf-8')
        self.assertEqual(serve.count('proxy_headers=False'), 2,
                         'server/serve.py 的两个 uvicorn.Config 都要关掉代理头改写')

    def test_compose_forwards_the_origin_settings(self) -> None:
        import yaml
        dc = yaml.safe_load((self.ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        env = dc['services']['workbuddy-manager']['environment']
        for key in ('WB_PUBLIC_ORIGIN', 'WB_ALLOW_UNCONFIGURED_ORIGIN'):
            with self.subTest(key=key):
                self.assertIn(key, env, f'compose 没把 {key} 透传进来 —— 写进 .env 也没用')
                self.assertIn('${' + key, str(env[key]), f'{key} 没从 .env 取值')

    def test_nginx_internal_location_clears_client_ip_headers(self) -> None:
        """`auth_request` 子请求要清空来源头：否则「仅本机」会把真正的 nginx 判成外部。"""
        text = (self.ROOT / 'deploy' / 'README.md').read_text(encoding='utf-8')
        self.assertIn('location = /__zh_session', text, 'README 没给出 internal location 的写法')
        block = text.split('location = /__zh_session', 1)[1][:600]
        for header in ('X-Real-IP', 'X-Forwarded-For'):
            with self.subTest(header=header):
                self.assertIn(f'{header} "";', block,
                              f'internal location 示例没把 {header} 清空 —— '
                              '转发它等于把子请求判成外部来源')
                self.assertNotIn(f'{header} $remote_addr', block,
                                 f'internal location 把 {header} 转发成了浏览器地址')

    def test_escape_hatch_schema_matches_the_runtime_parser(self) -> None:
        """`_env_bool` 只认 1/true/yes/on；`t` / `y` 必须在启动期被拒，而不是静默 false。"""
        from server import configcontract
        spec = configcontract.load_schema()['settings']['WB_ALLOW_UNCONFIGURED_ORIGIN']
        self.assertEqual(spec.get('boolean_dialect'), 'manager',
                         'schema 没对齐运行期方言：t/y 会通过校验却被当成 false')
        for bad in ('t', 'y', 'f', 'n'):
            with self.subTest(value=bad):
                with self.assertRaises(configcontract.ConfigurationError):
                    configcontract.validate_environment(
                        {'WB_ALLOW_UNCONFIGURED_ORIGIN': bad})
        parsed = configcontract.validate_environment({'WB_ALLOW_UNCONFIGURED_ORIGIN': 'on'})
        self.assertEqual(parsed['settings']['WB_ALLOW_UNCONFIGURED_ORIGIN']['value'], True)


class InternalVerifyStaysLoopbackOnly(unittest.TestCase):
    """关掉 uvicorn 的改写之后，`/api/system/internal-session-verify` 仍是「仅本机」。

    `--no-proxy-headers`（见 `DeploymentTrustModel`）让 nginx 转发进来的每个请求
    TCP 对端都是 127.0.0.1，于是只比对 `request.client.host` 的守卫对公网也成立：
    持有效会话的外部客户端能拿到自己的 qtg 身份断言。判据必须加上
    `iputil.client_ip()` 解出的真实来源（与 `WB_TRUSTED_PROXY_CIDRS` 同一份）。
    """

    PATH = '/api/system/internal-session-verify'

    def setUp(self) -> None:
        self.app = FastAPI()
        self.app.include_router(system.router)
        self.app.dependency_overrides[security.current_user] = lambda: {
            'id': 1, 'username': 'alice', 'role': 'viewer'}
        self.addCleanup(self.app.dependency_overrides.clear)
        for name, value in (('TRUST_PROXY', '1'),
                            ('TRUSTED_PROXY_CIDRS', ['127.0.0.0/8', '::1/128']),
                            ('TRUSTED_PROXY_HOPS', 1)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        secret = mock.patch.object(system.edgeidentity, '_secret', lambda: 'k' * 48)
        secret.start()
        self.addCleanup(secret.stop)
        system._forwarded_refusal_warned.clear()
        self.addCleanup(system._forwarded_refusal_warned.clear)

    def _call(self, peer=LOOPBACK, extra: dict | None = None):
        app = self.app

        async def send():
            transport = httpx.ASGITransport(app=app, client=peer)
            async with httpx.AsyncClient(transport=transport,
                                         base_url='http://testserver') as client:
                return await client.get(self.PATH, headers=dict(extra or {}))

        return asyncio.run(send())

    def test_direct_remote_caller_is_refused(self) -> None:
        response = self._call(peer=LAN)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn('本机', response.text)

    def test_nginx_proxied_external_request_is_refused(self) -> None:
        """回归核心：对端是 127.0.0.1（nginx），但真实来源是公网地址。"""
        response = self._call(extra={'x-real-ip': '203.0.113.9'})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertNotIn('x-qtg-identity', {k.lower() for k in response.headers})

    def test_forwarded_for_from_a_trusted_proxy_is_checked_too(self) -> None:
        response = self._call(extra={'x-forwarded-for': '203.0.113.9'})
        self.assertEqual(response.status_code, 403, response.text)

    def test_forged_header_from_an_untrusted_peer_does_not_help(self) -> None:
        response = self._call(peer=LAN, extra={'x-real-ip': '127.0.0.1'})
        self.assertEqual(response.status_code, 403, response.text)

    def test_auth_request_subrequest_still_gets_the_assertion(self) -> None:
        """nginx 的 internal 子请求没有来源头：放行，且照旧签发断言。"""
        response = self._call()
        self.assertEqual(response.status_code, 204, response.text)
        self.assertTrue(response.headers.get('x-qtg-identity', '').startswith('v1.'))

    def test_the_upgrade_trap_is_logged_once(self) -> None:
        """反代把来源头带进子请求时，日志要说明原因 —— 否则整站 403 无从查起。"""
        with self.assertLogs('workbuddy.system', level='WARNING') as logs:
            self._call(extra={'x-real-ip': '203.0.113.9'})
            self._call(extra={'x-real-ip': '203.0.113.9'})
        self.assertEqual(len(logs.records), 1, '同一次故障只告警一次，不要刷屏')
        message = logs.records[0].getMessage()
        self.assertIn('auth_request', message)
        self.assertIn('X-Real-IP', message)

    def test_whole_loopback_range_counts_as_local(self) -> None:
        """127.0.0.2 是回环（以前只认 127.0.0.1 / ::1）。"""
        response = self._call(peer=('127.0.0.2', 5000))
        self.assertEqual(response.status_code, 204, response.text)


if __name__ == '__main__':
    unittest.main()
