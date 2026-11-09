"""Real loopback HTTP: public key CRUD -> models -> generation -> revoke.

The ASGI entry uses TestClient; outbound HTTP is not mocked. The isolated runner's
fake provider records how many generations actually reached the wire.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import budget, budgetowner, config, db, security, tokensvc
from server.routers import gateway


class LiveHttpPlatformFlowTest(unittest.TestCase):
    def setUp(self):
        base = os.environ['OPENAI_BASE_URL'].rstrip('/')
        self.base = base.removesuffix('/v1')
        self.assertEqual(urlsplit(self.base).hostname, '127.0.0.1',
                         'Integration tests must use the isolated loopback provider')
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_db = config.DB_PATH
        self.original_users = config.USERS_FILE
        budgetowner.close()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = self.root / 'live.db'
        config.USERS_FILE = self.root / 'users.json'
        security.save_users({'secret': 'S' * 64, 'users': [], 'api_keys': []})
        self.base_patch = patch.object(config, 'WB2API_BASE', self.base)
        self.base_patch.start()
        self.key_patch = patch.object(config, 'WB2API_KEY', 'isolated-upstream-test')
        self.key_patch.start()
        secret = self.root / 'upstream-key'
        secret.write_text('isolated-upstream-test', encoding='utf-8')
        self.spec_patch = patch.object(gateway, '_THREEWAY_SPEC', {
            'qoder': (self.base, str(secret)), 'trae': (self.base, str(secret))})
        self.spec_patch.start()
        db.connect()
        from server.main import app
        self.client = TestClient(app)
        token = tokensvc.create_token('live-http-test', 'admin', None, 'test')
        self.admin = {'Authorization': 'Bearer ' + token['token']}

    def tearDown(self):
        self.client.close()
        budgetowner.close()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = self.original_db
        config.USERS_FILE = self.original_users
        self.spec_patch.stop()
        self.key_patch.stop()
        self.base_patch.stop()
        self.tmp.cleanup()

    def provider_stats(self):
        with urlopen(self.base + '/__stats', timeout=3) as response:
            return json.load(response)

    def test_three_platform_key_lifecycle_reaches_real_http_and_settles_usage(self):
        before = self.provider_stats()
        for platform in ('workbuddy', 'qoder', 'trae'):
            with self.subTest(platform=platform):
                created = self.client.post('/api/keys', headers=self.admin,
                    json={'name': 'live-' + platform, 'platforms': [platform]})
                self.assertEqual(created.status_code, 200, created.text)
                key = created.json()
                headers = {'Authorization': 'Bearer ' + key['key']}
                prefix = '/v1' if platform == 'workbuddy' else f'/{platform}/v1'
                models = self.client.get(prefix + '/models', headers=headers)
                self.assertEqual(models.status_code, 200, models.text)
                self.assertTrue(models.json()['data'])
                for stream in (False, True):
                    response = self.client.post(prefix + '/chat/completions', headers=headers,
                        json={'model': 'fake-model', 'messages': [{'role': 'user', 'content': 'hello'}],
                              'max_tokens': 32, 'stream': stream})
                    self.assertEqual(response.status_code, 200, response.text)
                    if stream:
                        self.assertIn('data: [DONE]', response.text)
                    else:
                        self.assertTrue(response.json()['choices'])
                rows = db.query('SELECT actual_platform, terminal_state, usage_complete '
                                'FROM request_logs WHERE key_id=? AND status=200 '
                                "AND endpoint LIKE '%/chat/completions'", (key['id'],))
                self.assertEqual(len(rows), 2)
                discovery = db.query('SELECT prompt_tokens, completion_tokens FROM request_logs '
                                     "WHERE key_id=? AND endpoint LIKE '%/models'", (key['id'],))
                self.assertEqual(len(discovery), 1)
                self.assertEqual(tuple(discovery[0]), (0, 0))
                self.assertEqual({row['actual_platform'] for row in rows}, {platform})
                self.assertTrue(all(row['terminal_state'] == 'completed' for row in rows))
                usage = db.query_one('SELECT used_tokens FROM api_keys WHERE id=?', (key['id'],))
                self.assertGreater(usage['used_tokens'], 0)
                if platform != 'workbuddy':
                    # Exercise the public platform middleware and the same
                    # Manager keys used above, not an independent QTG ledger.
                    for scope in ({'platform': platform}, {}):
                        stats_headers = self.admin if scope else {**self.admin, 'X-QTG-Platform': platform}
                        stats = self.client.get('/api/stats/by-key', headers=stats_headers,
                                                params={'days': 1, 'realm': 'cn', **scope})
                        self.assertEqual(stats.status_code, 200, stats.text)
                        current = next(row for row in stats.json() if row['key_id'] == key['id'])
                        self.assertEqual(current['platform'], platform)
                        self.assertEqual(current['requests'], 2, 'Model discovery is not inference usage')
                        expected = db.query_one(
                            'SELECT SUM(prompt_tokens+completion_tokens) t FROM request_logs '
                            'WHERE key_id=? AND actual_platform=? AND usage_complete=1',
                            (key['id'], platform))['t']
                        self.assertEqual(current['total_tokens'], expected)
                        self.assertEqual(current['source'], 'manager_retained_request_logs')
                sent = self.provider_stats()['total']
                removed = self.client.delete(f"/api/keys/{key['id']}", headers=self.admin)
                self.assertEqual(removed.status_code, 200, removed.text)
                denied = self.client.get(prefix + '/models', headers=headers)
                self.assertEqual(denied.status_code, 401, denied.text)
                self.assertEqual(self.provider_stats()['total'], sent,
                                 'Revoked keys must be rejected before upstream I/O')
                if platform != 'workbuddy':
                    stats = self.client.get('/api/stats/by-key', headers=self.admin,
                                            params={'platform': platform, 'days': 1, 'realm': 'cn'})
                    self.assertEqual(stats.status_code, 200, stats.text)
                    self.assertNotIn(key['id'], [row['key_id'] for row in stats.json()])
        after = self.provider_stats()
        self.assertEqual(after['by_path'].get('/v1/chat/completions', 0) -
                         before['by_path'].get('/v1/chat/completions', 0), 6,
                         'Every accepted generation must reach the wire exactly once')

    def test_accepted_generation_without_response_is_not_replayed_or_refunded(self):
        key = self.client.post('/api/keys', headers=self.admin,
            json={'name': 'ambiguous-transport', 'platforms': ['workbuddy'], 'quota': 10000}).json()
        before = self.provider_stats()['total']
        with patch.dict(os.environ, {'WB_SAFE_RETRY_MAX': '3'}):
            response = self.client.post('/v1/chat/completions',
                headers={'Authorization': 'Bearer ' + key['key']},
                json={'model': 'fake-model:accepted_disconnect',
                      'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 32})
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.provider_stats()['total'] - before, 1,
                         'An accepted request may be billed even when no response arrives')
        row = db.query_one('SELECT usage_complete, terminal_state FROM request_logs '
                           'WHERE key_id=? ORDER BY id DESC LIMIT 1', (key['id'],))
        self.assertFalse(row['usage_complete'])
        self.assertEqual(row['terminal_state'], 'upstream_error')
        self.assertTrue(budget.unresolved(key['id']), 'Unknown spend must remain reserved for reconciliation')
