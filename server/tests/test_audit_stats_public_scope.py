"""Retired-provider key usage stays readable without reviving its control plane."""
import json
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import config, db, platformpolicy, platformstats, tokensvc
from server.tests._db_fixture import isolate_database


class HistoricalKeyStatsRouteTest(unittest.TestCase):
    def setUp(self):
        root = isolate_database(self)
        users = root / 'users.json'
        users.write_text(json.dumps({'secret': 'S' * 64, 'api_keys': [],
                                     'users': [{'username': 'test', 'role': 'admin', 'sv': 0,
                                                'pwd_hash': 'synthetic-no-login'}]}),
                         encoding='utf-8')
        self.enterContext(patch.object(config, 'USERS_FILE', users))
        from server.main import app
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {}
        for scope in ('admin', 'read', 'keys'):
            token = tokensvc.create_token('stats-' + scope, scope, None, 'test')
            self.headers[scope] = {'Authorization': 'Bearer ' + token['token']}
        for key_id, platform in ((21, 'qoder'), (22, 'trae'), (23, 'workbuddy')):
            db.execute('INSERT INTO api_keys(id,name,key_hash,prefix,created_at,enabled) '
                       'VALUES(?,?,?,?,?,1)', (key_id, platform, str(key_id), 'synthetic', time.time()))
            for actual in (platform, 'workbuddy'):
                db.execute('INSERT INTO request_logs(ts,key_id,platform,actual_platform,protocol,'
                           'endpoint,realm,model,status,terminal_state,prompt_tokens,completion_tokens,'
                           'credit,usage_complete) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (int(time.time()), key_id, actual, actual, 'openai',
                            '/v1/chat/completions', 'cn', 'synthetic', 200, 'completed', 3, 2, 0, 1))

    def test_header_and_query_select_only_retained_qoder_evidence(self):
        self.assertFalse(platformpolicy.is_enabled('qoder'))
        for headers, params in (
            (self.headers['admin'], {'platform': 'qoder'}),
            ({**self.headers['admin'], 'X-QTG-Platform': 'qoder'}, {}),
            ({**self.headers['admin'], 'X-QTG-Platform': 'qoder'}, {'platform': 'trae'}),
        ):
            with self.subTest(headers=bool(headers.get('X-QTG-Platform')), query=params):
                response = self.client.get('/api/stats/by-key', headers=headers,
                    params={'days': 1, 'realm': 'cn', **params})
                self.assertEqual(response.status_code, 200, response.text)
                rows = response.json()
                self.assertEqual([row['key_id'] for row in rows], [21])
                self.assertEqual(rows[0]['platform'], 'qoder')
                self.assertEqual(rows[0]['source'], 'manager_retained_request_logs')
                self.assertEqual((rows[0]['requests'], rows[0]['total_tokens']), (1, 5))
        self.assertFalse(platformpolicy.is_enabled('qoder'))

    def test_auth_and_service_scope_guards_still_apply(self):
        for scope, status in ((None, 401), ('keys', 403), ('read', 200), ('admin', 200)):
            with self.subTest(scope=scope):
                response = self.client.get('/api/stats/by-key?platform=qoder',
                    headers=self.headers.get(scope, {}))
                self.assertEqual(response.status_code, status, response.text)

    def test_prefix_lookalikes_and_writes_do_not_reach_historical_reader(self):
        with patch.object(platformstats, 'current_key_usage',
                          side_effect=AssertionError('historical reader must not run')):
            for method, path in (('GET', '/api/stats/by-key-extra'),
                                 ('GET', '/api/stats/by-key/nested'),
                                 ('POST', '/api/stats/by-key')):
                with self.subTest(method=method, path=path):
                    response = self.client.request(method, path, headers=self.headers['admin'],
                                                   params={'platform': 'qoder'})
                    self.assertIn(response.status_code, (404, 405), response.text)


if __name__ == '__main__':
    unittest.main()
