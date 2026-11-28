"""D-41：写操作的同源锚点不能由请求自己的 `Host` 头决定（DNS rebinding）。

## 修前实测（本文件第 1、2 条就是它的反面）

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

## 为什么用 `httpx.ASGITransport` 而不是 `TestClient`

「是不是可信反代」由 `request.client.host`（TCP 对端）决定。`TestClient` 的对端
固定是 `testclient`，说不出这件事；`ASGITransport(client=...)` 可以逐条指定。

## 覆盖的另一件事

`routers/portal.py` 的 `same_origin` 与 `services/platformauth.py` 的
`check_poll_origin` 也曾经各自复制了一份「等于 `expected_origin`」的判定。
D-41 把三处统一到 `security.origin_allowed()`，这里同时钉住那两个入口 ——
否则只堵住主入口，旁路仍然认攻击者的域名。
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from server import config, security
from server.routers import auth, portal
from server.services import platformauth

LOOPBACK = ('127.0.0.1', 5000)
LAN = ('10.0.0.9', 5000)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth.router)

    @app.api_route('/api/probe/write', methods=['POST', 'GET'])
    def probe(request: Request):
        security.check_same_origin(request)
        return {'ok': True}

    return app


def _request(host: str, origin: str, *, peer=LAN, method: str = 'POST') -> Request:
    scope = {
        'type': 'http', 'method': method, 'http_version': '1.1',
        'scheme': 'http', 'path': '/api/portal/x', 'raw_path': b'/api/portal/x',
        'query_string': b'', 'root_path': '',
        'headers': [(b'host', host.encode()), (b'origin', origin.encode())],
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
              cookie: bool = False, method: str = 'post'):
        headers = {'host': host}
        if origin is not None:
            headers['origin'] = origin
        if cookie:
            headers['cookie'] = f'{config.COOKIE_NAME}=synthetic'
        headers.update(extra or {})
        return asyncio.run(self._send(method, path, peer, headers))

    async def _send(self, method: str, path: str, peer, headers):
        transport = httpx.ASGITransport(app=self.app, client=peer)
        async with httpx.AsyncClient(transport=transport,
                                     base_url='http://testserver') as client:
            call = getattr(client, method)
            if method in ('get', 'head'):
                return await call(path, headers=headers)
            return await call(path, json={'username': 'u', 'password': 'p'},
                              headers=headers)


class ReboundHostIsRejected(_AnchorCase):
    """`Host == Origin == 攻击者域名` 必须失败 —— 这是修前被实测通过的那条。"""

    def test_attacker_domain_as_host_and_origin_is_rejected(self) -> None:
        response = self._call(host='evil.test:8000', origin='http://evil.test:8000')
        self.assertEqual(response.status_code, 403,
                         'Host 与 Origin 相同就放行 —— 同源锚点又回到了请求头手里')
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'csrf_origin_unconfigured')
        self.login.assert_not_called()

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
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'csrf_origin_unconfigured')

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


class DefaultDeploymentStillWorks(_AnchorCase):
    """没有配置时的正常形态：IP / localhost / 内网名单标签，`Origin == Host` 就放行。"""

    def test_ip_localhost_and_single_label_hosts_pass(self) -> None:
        cases = (
            ('192.168.1.5:7864', 'http://192.168.1.5:7864'),
            ('localhost:7864', 'http://localhost:7864'),
            ('testserver', 'http://testserver'),
            ('[::1]:7864', 'http://[::1]:7864'),
        )
        for host, origin in cases:
            with self.subTest(host=host):
                response = self._call(host=host, origin=origin)
                self.assertEqual(response.status_code, 200,
                                 f'{host} 的同源表单提交被误伤：{response.text}')
        self.assertEqual(self.login.call_count, len(cases))

    def test_domain_direct_without_config_is_rejected_with_a_reason(self) -> None:
        """唯一被收紧的形态：域名直连且没有任何服务端声明。"""
        response = self._call(host='panel.example.com',
                              origin='http://panel.example.com')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers.get('x-manager-csrf-reason'),
                         'csrf_origin_unconfigured')


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


if __name__ == '__main__':
    unittest.main()
