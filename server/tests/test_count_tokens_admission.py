"""Counting is authenticated metadata work, not a second generation lease."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import budget, budgetowner, config, db, keysvc, observability


class CountTokensAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR, db._conn)
        root = Path(self.temporary.name)
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = root / 'fixture.db', root / 'users.json', root / 'no-static'
        config.USERS_FILE.write_text(json.dumps({'secret': 'synthetic-count-only', 'users': [
            {'username': 'admin', 'role': 'admin', 'sv': 1, 'pwd_hash': 'synthetic'}]}), encoding='utf-8')
        db._conn = None
        db.connect()
        budget.initialize()
        from server.main import app
        self.client = TestClient(app)
        self.key = keysvc.create_key(name='synthetic-count', quota=1000000)

    def tearDown(self):
        self.client.close()
        budgetowner.close()
        if db._conn is not None:
            db._conn.close()
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR, db._conn = self.previous
        self.temporary.cleanup()

    def count(self, **overrides):
        body = {'model': 'm', 'messages': [{'role': 'user', 'content': 'synthetic counting only'}], **overrides}
        return self.client.post('/v1/messages/count_tokens', json=body, headers={'x-api-key': self.key['key']})

    def test_count_with_model_never_borrows_a_generation_lease(self):
        with patch.object(observability, 'admit_key', return_value=False) as admit, \
                patch('server.budget.prepare') as prepare:
            response = self.count()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()['input_tokens'], 0)
        admit.assert_not_called()
        prepare.assert_not_called()
        self.assertEqual(db.query_one('SELECT count(*) n FROM budget_reservations')['n'], 0)

    def test_count_without_model_keeps_metadata_contract(self):
        with patch.object(observability, 'admit_key', return_value=False) as admit:
            response = self.count(model=None)
        self.assertEqual(response.status_code, 200, response.text)
        admit.assert_not_called()

    def test_generation_still_requires_a_key_slot(self):
        with patch.object(observability, 'admit_key', return_value=False) as admit:
            response = self.client.post('/v1/messages', json={'model': 'm', 'max_tokens': 10,
                'messages': [{'role': 'user', 'content': 'synthetic generation'}]},
                headers={'x-api-key': self.key['key']})
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()['error']['code'], 'key_concurrency_exceeded')
        admit.assert_called_once()

    def test_disabled_key_is_not_a_metadata_exemption(self):
        db.execute('UPDATE api_keys SET enabled=0 WHERE id=?', (self.key['id'],))
        response = self.count()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()['error']['code'], 'key_disabled')

    def test_model_allowlist_is_still_enforced(self):
        db.execute('UPDATE api_keys SET models=? WHERE id=?', (json.dumps(['allowed']), self.key['id']))
        response = self.count(model='denied')
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['error']['code'], 'model_not_allowed')

    def test_expired_key_is_still_rejected(self):
        db.execute('UPDATE api_keys SET expires_at=1 WHERE id=?', (self.key['id'],))
        response = self.count()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()['error']['code'], 'key_expired')

    def test_ip_allowlist_is_still_enforced(self):
        db.execute('UPDATE api_keys SET ip_allowlist=? WHERE id=?', (json.dumps(['192.0.2.9']), self.key['id']))
        response = self.count()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['error']['code'], 'ip_not_allowed')

    def test_platform_policy_is_still_enforced(self):
        db.execute('UPDATE api_keys SET platforms=? WHERE id=?', (json.dumps(['qoder']), self.key['id']))
        response = self.count()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()['error']['code'], 'platform_not_allowed')

    def test_metadata_still_obeys_rate_limit(self):
        with patch('server.routers.gateway._rate_limited', return_value=(True, 999)):
            response = self.count()
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()['error']['code'], 'rate_limit_exceeded')

    def test_real_held_generation_slot_does_not_block_counting(self):
        state = observability.RequestAudit('synthetic-held-generation', '/v1/messages',
                                           'POST', 'anthropic', 0)
        context = observability.current.set(state)
        try:
            key = keysvc.resolve(self.key['key'])
            with patch.dict('os.environ', {'WB_KEY_CONCURRENCY_QUOTA': '1'}):
                self.assertTrue(observability.admit_key(key))
                response = self.count()
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(state.key_slot, key['id'])
        finally:
            observability.release_key(state)
            observability.current.reset(context)


if __name__ == '__main__':
    unittest.main()
