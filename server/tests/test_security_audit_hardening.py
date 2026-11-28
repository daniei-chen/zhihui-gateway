"""Synthetic regressions for read capabilities, login bounds and trace privacy.

No claim is redeemed, no provider is contacted, and captures use temporary files.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from server import config, security, traffic
from server.bodyguard import APIBodyGuard
from server.routers import portal, redpackets, traffic as traffic_routes


class RedPacketCapabilityTests(unittest.TestCase):
    def test_only_admin_can_read_claim_code_without_mutating_source(self):
        app = FastAPI()
        app.include_router(redpackets.router)
        original = {'id': 7, 'title': 'synthetic packet', 'code': 'synthetic-claim-code'}
        identities = [
            ({'username': 'viewer', 'role': 'viewer'}, 200, ''),
            ({'username': 'readonly', 'role': 'viewer', 'scope': 'readonly'}, 200, ''),
            ({'username': 'reader', 'role': 'account_admin'}, 200, ''),
            ({'username': 'read-service', 'role': 'viewer', 'scope': 'read'}, 403, None),
            ({'username': 'key-service', 'role': 'viewer', 'scope': 'keys'}, 403, None),
            ({'username': 'admin', 'role': 'admin'}, 200, original['code']),
            ({'username': 'admin-token', 'role': 'admin', 'scope': 'admin'}, 200, original['code']),
        ]
        with TestClient(app) as client, \
             mock.patch.object(redpackets.redpacket, 'list_packets', return_value=[original]), \
             mock.patch.object(redpackets.redpacket, 'packet_detail', return_value=original):
            for user, expected_status, expected_code in identities:
                with self.subTest(user=user), mock.patch.object(security, '_current_user', return_value=user):
                    listing = client.get('/api/red-packets')
                    detail = client.get('/api/red-packets/7')
                    self.assertEqual(listing.status_code, expected_status, listing.text)
                    self.assertEqual(detail.status_code, expected_status, detail.text)
                    if expected_status == 200:
                        self.assertEqual(listing.json()[0]['code'], expected_code)
                        self.assertEqual(detail.json()['code'], expected_code)
        self.assertEqual(original['code'], 'synthetic-claim-code')

    def test_missing_or_unknown_role_is_not_a_claim_capability(self):
        original = {'id': 7, 'code': 'synthetic-claim-code'}
        with mock.patch.object(redpackets.redpacket, 'list_packets', return_value=[original]), \
             mock.patch.object(redpackets.redpacket, 'packet_detail', return_value=original):
            for user in ({}, {'role': 'future-role'}):
                self.assertEqual(redpackets.list_packets(user)[0]['code'], '')
                self.assertEqual(redpackets.packet_detail(7, user)['code'], '')


class PortalBodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def _send_body(self, path, chunks, headers=()):
        from server import main
        entered, received, sent = [], [], []

        async def app(scope, receive, send):
            entered.append((await receive())['body'])
            await send({'type': 'http.response.start', 'status': 204, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        async def receive():
            index = len(received)
            received.append(index)
            return {'type': 'http.request', 'body': chunks[index], 'more_body': index + 1 < len(chunks)}

        async def send(message):
            sent.append(message)

        guard = APIBodyGuard(app, maximum=main.MAX_API_BODY_BYTES, path_limits=main.API_PATH_LIMITS)
        await guard({'type': 'http', 'method': 'POST', 'path': path, 'headers': list(headers)}, receive, send)
        self.assertEqual(guard.active, 0)
        return sent[0]['status'], entered, received

    async def test_declared_oversize_portal_login_never_reads_or_parses(self):
        for path in ('/api/login', '/api/portal/login', '/api/portal/login/'):
            with self.subTest(path=path):
                status, entered, received = await self._send_body(path, [], [(b'content-length', b'65537')])
                self.assertEqual(status, 413)
                self.assertEqual((entered, received), ([], []))

    async def test_chunked_oversize_portal_login_is_rejected_before_handler(self):
        status, entered, received = await self._send_body('/api/portal/login', [b'x' * 32768, b'x' * 32769])
        self.assertEqual(status, 413)
        self.assertEqual(entered, [])
        self.assertEqual(len(received), 2)

    async def test_small_portal_login_body_is_preserved(self):
        body = b'{"username":"synthetic","password":"unused"}'
        status, entered, _ = await self._send_body('/api/portal/login', [body], [(b'content-length', str(len(body)).encode())])
        self.assertEqual(status, 204)
        self.assertEqual(entered, [body])


class PortalFailureBoundsTests(unittest.TestCase):
    def setUp(self):
        self.clock = 1000.0
        for patch in (
            mock.patch.dict(portal._PORTAL_IP_FAILS, {}, clear=True),
            mock.patch.dict(portal._PORTAL_USER_FAILS, {}, clear=True),
            mock.patch.object(portal, '_portal_fail_prune_at', 0.0),
            mock.patch.object(portal, '_PORTAL_FAIL_MAX_ENTRIES', 3),
            mock.patch.object(portal.time, 'monotonic', side_effect=lambda: self.clock),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_ip_capacity_rejects_new_identity_without_evicting_blocked_one(self):
        for _ in range(portal._PORTAL_IP_MAX):
            portal._portal_ip_record('198.51.100.1')
        for ip in ('198.51.100.2', '198.51.100.3', '198.51.100.4'):
            portal._portal_ip_record(ip)
        self.assertEqual(len(portal._PORTAL_IP_FAILS), 3)
        self.assertTrue(portal._portal_ip_blocked('198.51.100.1'))
        self.assertTrue(portal._portal_ip_blocked('198.51.100.4'))
        self.assertFalse(portal._portal_ip_blocked('198.51.100.2'))

    def test_user_capacity_and_normalized_identity_are_bounded(self):
        for _ in range(portal._PORTAL_USER_MAX):
            portal._portal_user_record('198.51.100.1', ' Alice ')
        for name in ('bob', 'carol', 'dana', 'erin'):
            portal._portal_user_record('198.51.100.1', name)
        self.assertEqual(len(portal._PORTAL_USER_FAILS), 3)
        # 判定已归 SQLite 的 `blocked_until` 独占（审计 D-2）；这张表退回来源统计
        # 与容量边界，这里只校验它的边界行为：键归一化、容量上限、不互相顶替。
        self.assertIn(('198.51.100.1', 'alice'), portal._PORTAL_USER_FAILS)
        self.assertEqual(portal._PORTAL_USER_FAILS[('198.51.100.1', 'alice')][0],
                         portal._PORTAL_USER_MAX)
        # bob 在表里（第 2 个身份）但只失败过 1 次：额度按身份各自计算，不互相顶替。
        self.assertEqual(portal._PORTAL_USER_FAILS[('198.51.100.1', 'bob')][0], 1)
        # 表满之后的新身份被拒（容量边界），已存在的身份保留自己的额度。
        self.assertTrue(portal._portal_user_capacity_blocked('198.51.100.1', 'ERIN'))
        self.assertFalse(portal._portal_user_capacity_blocked('198.51.100.1', 'ALICE'))

    def test_expired_entries_are_removed_from_both_tables(self):
        for index in range(3):
            portal._portal_ip_record(f'198.51.100.{index}')
            portal._portal_user_record('198.51.100.1', f'user-{index}')
        self.clock += portal._PORTAL_IP_WINDOW + 1
        self.assertFalse(portal._portal_ip_blocked('203.0.113.1'))
        self.assertEqual(portal._PORTAL_IP_FAILS, {})
        self.assertEqual(portal._PORTAL_USER_FAILS, {})
        portal._portal_user_record('203.0.113.1', 'new-user')
        self.assertIn(('203.0.113.1', 'new-user'), portal._PORTAL_USER_FAILS)

    def test_success_only_clears_matching_source_and_identity(self):
        for name in ('alice', 'bob'):
            portal._portal_user_record('198.51.100.1', name)
        portal._portal_ip_record('198.51.100.1')
        portal._portal_ip_record('198.51.100.2')
        portal._portal_ip_clear('198.51.100.1')
        portal._portal_user_clear('198.51.100.1', ' ALICE ')
        self.assertEqual(set(portal._PORTAL_IP_FAILS), {'198.51.100.2'})
        self.assertEqual(set(portal._PORTAL_USER_FAILS), {('198.51.100.1', 'bob')})

    def test_full_user_table_rejects_before_database_or_password_work(self):
        for name in ('alice', 'bob', 'carol'):
            portal._portal_user_record('198.51.100.1', name)
        request = Request({'type': 'http', 'method': 'POST', 'path': '/api/portal/login',
            'headers': [(b'host', b'testserver'), (b'origin', b'http://testserver')],
            'scheme': 'http', 'server': ('testserver', 80), 'client': ('198.51.100.1', 1234)})
        with mock.patch.object(portal.store, 'transaction', side_effect=AssertionError('must not access storage')):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(portal.login(request, {'username': 'new-user', 'password': 'synthetic'}))
        self.assertEqual(caught.exception.status_code, 429)


class TrafficPrivacyAndSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='traffic-security-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.setting = True
        for patch in (
            mock.patch.object(config, 'DATA_DIR', self.root),
            mock.patch.object(config, 'TRAFFIC_TAP', True),
            mock.patch.dict(traffic._FLAG, {'value': None, 'expires': 0.0}, clear=True),
            mock.patch.object(traffic.db, 'get_setting', side_effect=lambda *_: self.setting),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _write_setting(self, sql, params):
        self.assertEqual(params[0], 'traffic.tap')
        self.setting = json.loads(params[1])

    def _payload(self):
        return {'request_id': 'synthetic-r1', 'ts': 1795708800, 'status': 200,
            'stream': True, 'client': {}, 'gateway': {}, 'diff': [],
            'model': {'requested': 'synthetic'}, 'key': {'name': 'synthetic@example.invalid'},
            'response': {'kind': 'stream', 'text': 'phone 13800138000',
                         'reasoning': 'synthetic@example.invalid',
                         'error': 'contact synthetic@example.invalid',
                         'metadata': {'authorization': 'synthetic-bearer-secret'}},
            'latency_ms': 1}

    def test_write_boundary_scrubs_stream_errors_metadata_and_index(self):
        payload = self._payload()
        traffic._write_payload(payload)
        captured = next((self.root / 'traffic').glob('*/*.json.gz'))
        with gzip.open(captured, 'rt', encoding='utf-8') as handle:
            raw = handle.read()
        index = (self.root / 'traffic/index.jsonl').read_text(encoding='utf-8')
        for forbidden in ('13800138000', 'synthetic@example.invalid', 'synthetic-bearer-secret'):
            self.assertNotIn(forbidden, raw)
            self.assertNotIn(forbidden, index)
        self.assertIn('<手机号>', raw)
        self.assertIn('<邮箱>', raw)
        self.assertIn('<已脱敏>', raw)
        self.assertEqual(payload['response']['text'], 'phone 13800138000')

    def test_stream_summary_is_scrubbed_before_enqueue(self):
        request = SimpleNamespace(client=None, url=SimpleNamespace(path='/v1/chat/completions'), state=None)
        trace = traffic.Trace('synthetic-stream', request, {'model': 'synthetic'})
        audit = SimpleNamespace(collected=lambda key: '13800138000' if key == 'text' else 'synthetic@example.invalid')
        with mock.patch.object(traffic, '_enqueue') as enqueue:
            traffic.response_stream(trace, status=200, audit=audit, error='synthetic@example.invalid')
        response = enqueue.call_args.args[0]['response']
        self.assertEqual(response['text'], '<手机号>')
        self.assertEqual(response['reasoning'], '<邮箱>')
        self.assertEqual(response['error'], '<邮箱>')

    def test_historical_read_and_export_are_redacted_without_rewriting_source(self):
        path = self.root / 'traffic/2026-11-27/1795708800-synthetic-r1.json.gz'
        path.parent.mkdir(parents=True)
        with gzip.open(path, 'wt') as handle:
            json.dump(self._payload(), handle)
        original = path.read_bytes()
        record = traffic.read_record('synthetic-r1')
        exported = traffic.render_html(self._payload())
        self.assertNotIn('13800138000', json.dumps(record))
        self.assertNotIn('synthetic@example.invalid', exported)
        self.assertNotIn('synthetic-bearer-secret', exported)
        self.assertEqual(path.read_bytes(), original)

    def test_enable_and_disable_supersede_a_warm_opposite_cache(self):
        with mock.patch.object(traffic_routes.db, 'execute', side_effect=self._write_setting):
            for desired in (False, True, False):
                self.setting = not desired
                traffic.invalidate_flag()
                self.assertIs(traffic.enabled(), not desired)
                result = traffic_routes.set_enabled({'enabled': desired}, {'role': 'admin'})
                self.assertIs(result['desired'], desired)
                self.assertIs(result['effective'], desired)
                self.assertIs(traffic.enabled(), desired)
                self.assertNotIn('blocked_by', result)
                self.assertEqual(result['effective_scope'], 'instance')
                self.assertEqual(result['propagation_seconds'], 5)

    def test_environment_block_is_distinct_from_cache_or_storage_failure(self):
        with mock.patch.object(traffic_routes.db, 'execute', side_effect=self._write_setting), \
             mock.patch.object(config, 'TRAFFIC_TAP', False):
            result = traffic_routes.set_enabled({'enabled': True}, {'role': 'admin'})
        self.assertIs(result['desired'], True)
        self.assertIs(result['effective'], False)
        self.assertEqual(result['blocked_by'], 'WB_TRAFFIC_TAP=0')
        self.assertEqual(result['blocked_reason'], 'environment_disabled')
        with mock.patch.object(traffic.db, 'get_setting', side_effect=RuntimeError('synthetic storage failure')):
            result = traffic.setting_status()
        self.assertIsNone(result['desired'])
        self.assertIs(result['effective'], False)
        self.assertEqual(result['blocked_by'], 'settings_unavailable')

    def test_status_refreshes_this_instance_after_another_writer(self):
        self.assertTrue(traffic.enabled())
        self.setting = False
        result = traffic_routes.status({'role': 'admin'})
        self.assertIs(result['setting'], False)
        self.assertIs(result['enabled'], False)
        self.assertIs(result['effective'], False)
        self.assertFalse(traffic.enabled())

    def test_completed_setting_write_supersedes_an_inflight_old_refresh(self):
        self.setting = False
        reading, written, release = threading.Event(), threading.Event(), threading.Event()
        first = True

        def read_setting(*_):
            nonlocal first
            snapshot = self.setting
            if first:
                first = False
                reading.set()
                if not release.wait(2):
                    raise AssertionError('synthetic cache read was not released')
            return snapshot

        def write_setting(sql, params):
            self._write_setting(sql, params)
            written.set()

        with mock.patch.object(traffic.db, 'get_setting', side_effect=read_setting), \
             mock.patch.object(traffic_routes.db, 'execute', side_effect=write_setting), \
             ThreadPoolExecutor(max_workers=2) as pool:
            old_read = pool.submit(traffic.enabled)
            try:
                self.assertTrue(reading.wait(2))
                change = pool.submit(traffic_routes.set_enabled, {'enabled': True}, {'role': 'admin'})
                self.assertTrue(written.wait(2))
            finally:
                release.set()
            self.assertFalse(old_read.result(timeout=2))
            self.assertTrue(change.result(timeout=2)['effective'])
            self.assertTrue(traffic.enabled())

    def test_non_boolean_switch_is_rejected_without_writing(self):
        for value in ('false', 'true', 0, 1, None, [], {}):
            with self.subTest(value=value), mock.patch.object(traffic_routes.db, 'execute') as write:
                with self.assertRaises(HTTPException) as caught:
                    traffic_routes.set_enabled({'enabled': value}, {'role': 'admin'})
                self.assertEqual(caught.exception.status_code, 400)
                write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
