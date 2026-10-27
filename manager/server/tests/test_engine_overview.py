"""U3 阶段 2：Manager 读引擎侧数据（`/api/engine/overview`）的回归。

## 为什么这些断言值得钉

这条通道的三个失败模式都不会让服务报错，只会让面板「看起来正常但数字不对」：

1. **未配置却显示零账号**：`available=false` 被吞掉、静默返回空列表 → 运维会去
   查引擎，而真正原因是 Manager 没配 `WB_QTG_ADAPTER_BASE`；
2. **部分端点挂了却装作完整**：某个字段空着，前端画一半，没有任何提示；
3. **契约漂移静默通过**：QTG 改了字段形状，Manager 照旧解析，字段错位但类型对。

所以这里钉住的是：**每种降级都必须可分辨**，且读不到不是 5xx。

另外钉住「不写业务账本」——这是观测通道，调它不应改变 `request_logs`。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, main, qtgadapter, security  # noqa: E402

from ._db_fixture import isolate_database  # noqa: E402

BASE = 'http://qtg.internal.test'


def _contract(version: int = 1) -> dict:
    return {
        'contract_version': version,
        'service': 'qtg',
        'capabilities': {'accounts': {'available': True}},
        'auth': 'internal_service_token_or_admin',
        'read_only': True,
        'data_source': 'engine_aggregate',
    }


def _bodies() -> dict[str, dict]:
    return {
        'contract': _contract(),
        'health': {'contract_version': 1, 'overall': 'ok', 'upstreams': []},
        'accounts': {'contract_version': 1, 'platforms': {'qoder': [], 'trae': [],
                                                         'workbuddy': []},
                     'totals': {'qoder': 0, 'trae': 0, 'workbuddy': 0}},
        'models': {'contract_version': 1, 'models': {'qoder': [], 'trae': [],
                                                     'workbuddy': []},
                   'totals': {'qoder': 0, 'trae': 0, 'workbuddy': 0}},
        'usage': {'contract_version': 1, 'days': 14,
                  'platforms': {'qoder': {'total': {}, 'points': []}}},
    }


class _AdapterCase(unittest.TestCase):
    """把 config 指到临时令牌 + 可注入的 transport。"""

    def setUp(self) -> None:
        self.root = isolate_database(self)
        self.token_file = Path(self.root) / 'internal_service_token'
        self.token_file.write_text('test-internal-token', encoding='utf-8')
        self._patches = [
            mock.patch.object(config, 'QTG_ADAPTER_BASE', BASE),
            mock.patch.object(config, 'QTG_ADAPTER_TOKEN_FILE', str(self.token_file)),
            mock.patch.object(config, 'QTG_ADAPTER_TIMEOUT_S', 2),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    # ---- helpers ----
    def _login(self) -> None:
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('TestPw123!')}], 'api_keys': []})
        r = self.client.post('/api/login',
                             json={'username': 'admin', 'password': 'TestPw123!'},
                             headers={'Origin': 'http://testserver'})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.cookies.update(dict(r.cookies))

    def _serve(self, handler) -> None:
        """把 config.http_client 换成 MockTransport 客户端（走真实 fetch 逻辑）。"""
        def factory(*args, **kwargs):
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                timeout=httpx.Timeout(2),
            )
        patcher = mock.patch.object(config, 'http_client', factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self) -> dict:
        self._login()
        r = self.client.get('/api/engine/overview')
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class AuthTest(_AdapterCase):
    def test_requires_auth(self) -> None:
        self.assertEqual(self.client.get('/api/engine/overview').status_code, 401)


class DegradationTest(_AdapterCase):
    """三种「读不到」必须各自可分辨，且都不是 5xx。"""

    def test_not_configured_is_explicit(self) -> None:
        with mock.patch.object(config, 'QTG_ADAPTER_BASE', ''):
            body = self._get()
        self.assertFalse(body['available'])
        self.assertEqual(body['reason'], 'adapter_not_configured')
        # 关键：不能给出一个"看起来正常"的空 data
        self.assertEqual(body['data'], {})

    def test_missing_token_file_is_explicit(self) -> None:
        self.token_file.unlink()
        body = self._get()
        self.assertFalse(body['available'])
        self.assertEqual(body['reason'], 'token_file_missing')
        self.assertEqual(body['data'], {})

    def test_empty_token_file_is_explicit(self) -> None:
        self.token_file.write_text('   \n', encoding='utf-8')
        body = self._get()
        self.assertEqual(body['reason'], 'token_file_empty')

    def test_all_endpoints_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout('boom', request=request)
        self._serve(handler)
        body = self._get()
        self.assertFalse(body['available'])
        self.assertEqual(body['reason'], 'all_endpoints_failed')
        self.assertEqual(len(body['degraded']), 5, body['degraded'])
        self.assertTrue(all(d['reason'] == 'timeout' for d in body['degraded']))

    def test_partial_failure_is_visible_not_silent(self) -> None:
        bodies = _bodies()

        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit('/', 1)[-1]
            if name == 'models':
                return httpx.Response(500, json={'detail': 'broken'})
            return httpx.Response(200, json=bodies[name])
        self._serve(handler)
        body = self._get()
        self.assertTrue(body['available'], '其余端点可用时不应整体不可用')
        self.assertNotIn('models', body['data'])
        self.assertEqual([d['endpoint'] for d in body['degraded']], ['models'])
        self.assertEqual(body['degraded'][0]['reason'], 'http_500')


class SuccessTest(_AdapterCase):
    def test_full_payload(self) -> None:
        bodies = _bodies()
        seen_headers: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.append(dict(request.headers))
            name = request.url.path.rsplit('/', 1)[-1]
            return httpx.Response(200, json=bodies[name])
        self._serve(handler)

        body = self._get()
        self.assertTrue(body['available'], body)
        self.assertIsNone(body['reason'])
        self.assertEqual(body['contract_version'], 1)
        self.assertEqual(body['source'], 'qtg_adapter')
        self.assertEqual(set(body['data']), set(qtgadapter.ENDPOINTS))
        self.assertEqual(body['degraded'], [])
        # 口径必须随响应给出，避免调用方把它和 /api/stats 相加
        self.assertEqual(body['ledger_authority'], '/api/stats/*')
        # 每个请求都带内部令牌，且不外泄到别的头
        self.assertTrue(seen_headers)
        for headers in seen_headers:
            self.assertEqual(headers.get('x-qtg-internal-auth'), 'test-internal-token')

    def test_usage_days_is_forwarded(self) -> None:
        bodies = _bodies()
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit('/', 1)[-1]
            if name == 'usage':
                seen.append(request.url.params.get('days', ''))
            return httpx.Response(200, json=bodies[name])
        self._serve(handler)
        self._login()
        r = self.client.get('/api/engine/overview', params={'days': 7})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, ['7'])

    def test_contract_version_mismatch_is_flagged(self) -> None:
        bodies = _bodies()
        bodies['contract'] = _contract(version=99)

        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit('/', 1)[-1]
            return httpx.Response(200, json=bodies[name])
        self._serve(handler)
        body = self._get()
        self.assertTrue(body['available'], '版本不符不应阻断读取')
        self.assertEqual(body['contract_version'], 99)
        reasons = [d['reason'] for d in body['degraded']]
        self.assertIn('contract_version_mismatch', reasons)
        flag = next(d for d in body['degraded']
                    if d['reason'] == 'contract_version_mismatch')
        self.assertEqual(flag['expected'], qtgadapter.EXPECTED_CONTRACT_VERSION)
        self.assertEqual(flag['actual'], 99)


class ModelsUpstreamErrorTest(_AdapterCase):
    """模型目录的上游失败必须透成 degraded。

    QTG 的 models 端点在上游取不到时仍然返回 200 + 空数组，只是多一个 `errors`
    字段。如果 Manager 不把它透出来，调用方看到的就是「引擎上一个模型都没有」——
    这正是"静默空"最典型的形态，比 500 更难查。
    """

    def test_models_errors_surface_as_degraded(self) -> None:
        bodies = _bodies()
        bodies['models'] = {
            'contract_version': 1,
            'models': {'qoder': [], 'trae': [], 'workbuddy': []},
            'totals': {'qoder': 0, 'trae': 0, 'workbuddy': 0},
            'errors': ['qoder 模型接口失败：timeout'],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit('/', 1)[-1]
            return httpx.Response(200, json=bodies[name])

        self._serve(handler)
        body = self._get()
        self.assertTrue(body['available'], '端点自报错误不应让整体不可用')
        pairs = [(d.get('endpoint'), d.get('reason')) for d in body['degraded']]
        self.assertIn(('models', 'upstream_error'), pairs)
        detail = next(d for d in body['degraded']
                      if d.get('reason') == 'upstream_error')
        self.assertIn('qoder', detail['detail'])

    def test_healthy_models_produce_no_degraded(self) -> None:
        bodies = _bodies()
        bodies['models'] = {
            'contract_version': 1,
            'models': {'qoder': ['Auto'], 'trae': [], 'workbuddy': []},
            'totals': {'qoder': 1, 'trae': 0, 'workbuddy': 0},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit('/', 1)[-1]
            return httpx.Response(200, json=bodies[name])

        self._serve(handler)
        body = self._get()
        self.assertEqual(body['degraded'], [])
        self.assertEqual(body['data']['models']['models']['qoder'], ['Auto'])


class NoSideEffectTest(_AdapterCase):
    def test_reads_do_not_write_ledger(self) -> None:
        bodies = _bodies()

        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit('/', 1)[-1]
            return httpx.Response(200, json=bodies[name])
        self._serve(handler)
        self._login()
        before = db.query('SELECT COUNT(*) AS n FROM request_logs')[0]['n']
        self.assertEqual(self.client.get('/api/engine/overview').status_code, 200)
        after = db.query('SELECT COUNT(*) AS n FROM request_logs')[0]['n']
        self.assertEqual(after, before, '引擎数据读取通道写了业务账本')


if __name__ == '__main__':
    unittest.main()
