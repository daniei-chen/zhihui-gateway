"""K06: platform selection cannot expand a key's server-owned authority."""
import asyncio
import itertools
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from server import config, db, keysvc, migrations, platformpolicy, tokensvc


class PlatformSelectionTest(unittest.TestCase):
    def test_path_header_model_agree_or_reject(self):
        for p in platformpolicy.PLATFORMS:
            self.assertEqual(platformpolicy.select(f'/{p}/v1/models', p, f'{p}/model'), p)
            self.assertEqual(platformpolicy.select('/v1/models', '', f'{p}/model'), p)
            self.assertEqual(platformpolicy.select('/v1/models', p), p)
            for other in platformpolicy.PLATFORMS:
                if p != other:
                    with self.assertRaises(platformpolicy.PlatformConflict):
                        platformpolicy.select(f'/{p}/v1/models', other)
                    with self.assertRaises(platformpolicy.PlatformConflict):
                        platformpolicy.select(f'/{p}/v1/models', '', f'{other}/model')
        self.assertEqual(platformpolicy.select('/v1/models'), 'workbuddy')
        with self.assertRaises(platformpolicy.PlatformConflict):
            platformpolicy.select('/v1/models', 'unknown')

    def test_existing_database_expands_without_resetting_keys(self):
        conn = sqlite3.connect(':memory:')
        try:
            conn.execute('CREATE TABLE api_keys(id INTEGER PRIMARY KEY, name TEXT)')
            conn.execute("INSERT INTO api_keys VALUES(7, 'old')")
            migrations._key_platforms(conn)
            migrations._key_platforms(conn)
            row = conn.execute('SELECT id, name, platforms FROM api_keys').fetchone()
            self.assertEqual(row[:2], (7, 'old'))
            self.assertEqual(json.loads(row[2]), list(platformpolicy.PLATFORMS))
        finally:
            conn.close()


class PlatformKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = config.DB_PATH
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = Path(self.tmp.name) / 'platforms.db'
        db.connect()
        from server.main import app
        self.client = TestClient(app)
        token = tokensvc.create_token('platform-test', 'admin', None, 'test')
        self.admin = {'Authorization': 'Bearer ' + token['token']}

    def tearDown(self):
        self.client.close()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_all_allow_deny_combinations(self):
        for bits in itertools.product((False, True), repeat=3):
            allowed = [p for p, use in zip(platformpolicy.PLATFORMS, bits) if use]
            key = keysvc.create_key('matrix', platforms=allowed)
            for p in platformpolicy.PLATFORMS:
                with self.subTest(allowed=allowed, requested=p):
                    reason = keysvc.validate(key, '127.0.0.1', None,
                                             is_model_list=True, platform=p)
                    if p in allowed:
                        self.assertIsNone(reason)
                    else:
                        self.assertEqual(reason.code, 'platform_not_allowed')
                        self.assertEqual(reason.status, 403)

    def test_legacy_policy_intersection_cannot_expand(self):
        key = keysvc.create_key('legacy', platforms=['qoder', 'trae'])
        key['legacy_platforms'] = ['qoder']
        self.assertIsNone(keysvc.validate(key, '127.0.0.1', None,
                                         is_model_list=True, platform='qoder'))
        self.assertEqual(keysvc.validate(key, '127.0.0.1', None,
                                        is_model_list=True, platform='trae').status, 403)

    def test_crud_persists_platforms_and_rejects_unknown_or_null_patch(self):
        r = self.client.post('/api/keys', headers=self.admin,
                             json={'name': 'qoder-only', 'platforms': ['qoder']})
        self.assertEqual(r.status_code, 200, r.text)
        key_id = r.json()['id']
        self.assertEqual(r.json()['platforms'], ['qoder'])
        r = self.client.patch(f'/api/keys/{key_id}', headers=self.admin,
                              json={'platforms': ['trae']})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(keysvc.resolve(self.client.post('/api/keys', headers=self.admin,
            json={'name': 'lookup', 'platforms': []}).json()['key'])['platforms'], [])
        for invalid in (['unknown'], None, 'qoder'):
            r = self.client.patch(f'/api/keys/{key_id}', headers=self.admin,
                                  json={'platforms': invalid})
            self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(keysvc.list_keys()[-1]['platforms'], ['trae'])

    def test_header_model_and_path_cannot_bypass_key_authority(self):
        key = keysvc.create_key('workbuddy-only', platforms=['workbuddy'])
        h = {'Authorization': 'Bearer ' + key['key']}
        for path, body, hint in (
                ('/qoder/v1/models', None, ''),
                ('/v1/models', None, 'trae'),
                ('/v1/chat/completions', {'model': 'qoder/Auto'}, '')):
            headers = dict(h)
            if hint:
                headers['X-QTG-Platform'] = hint
            r = self.client.get(path, headers=headers) if body is None else self.client.post(
                path, json=body, headers=headers)
            self.assertEqual(r.status_code, 403, r.text)
            self.assertEqual(r.json()['error']['code'], 'platform_not_allowed')
        r = self.client.get('/qoder/v1/models', headers={**h, 'X-QTG-Platform': 'trae'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['error']['code'], 'platform_conflict')

    def test_corrupt_stored_policy_denies_inference_without_breaking_admin_list(self):
        key = keysvc.create_key('broken')
        db.execute("UPDATE api_keys SET platforms='invalid-json' WHERE id=?", (key['id'],))
        parsed = keysvc.resolve(key['key'])
        self.assertFalse(parsed['platform_policy_valid'])
        self.assertEqual(keysvc.validate(parsed, '127.0.0.1', None,
                                        is_model_list=True).code, 'invalid_key_policy')
        self.assertTrue(keysvc.list_keys())


class PlatformContextLifetimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_platforms_are_isolated_and_context_is_reset(self):
        both = asyncio.Event()
        values = []

        async def app(scope, receive, send):
            before = platformpolicy.current_platform.get()
            values.append(before)
            if len(values) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
            self.assertEqual(platformpolicy.current_platform.get(), before)
            self.assertEqual(scope['path'], '/v1/models')

        async def receive():
            return {'type': 'http.request', 'body': b''}

        async def send(message):
            pass

        wrapped = platformpolicy.PlatformRoutingMiddleware(app)
        await asyncio.gather(*(wrapped({'type': 'http', 'path': f'/{p}/v1/models',
                                       'headers': []}, receive, send)
                               for p in ('qoder', 'trae')))
        self.assertCountEqual(values, ['qoder', 'trae'])
        self.assertEqual(platformpolicy.current_platform.get(), '')
