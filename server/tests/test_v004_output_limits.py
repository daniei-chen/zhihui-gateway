"""The budget decision and the actual outgoing output cap must agree."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from server import budget, budgetowner, config, db, keysvc


OK = {'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': 'm',
      'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                   'finish_reason': 'stop'}],
      'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}


class OutputLimitWireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zh-output-contract-')
        self.addCleanup(self.tmp.cleanup)
        self.old_conn = db._conn
        db._conn = None
        self.addCleanup(self.close_db)
        root = Path(self.tmp.name)
        for name, value in {'DATA_DIR': root, 'DB_PATH': root / 'test.db',
                            'USERS_FILE': root / 'users.json', 'STATIC_DIR': root / 'no-static'}.items():
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        config.USERS_FILE.write_text(json.dumps({'secret': 'synthetic-wire-only', 'users': [
            {'username': 'admin', 'role': 'admin', 'sv': 1, 'pwd_hash': 'synthetic'}]}), encoding='utf-8')
        db.connect()
        budget.initialize()
        from server.main import app
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.sent = []
        def respond(request):
            if request.method == 'POST':
                self.sent.append(json.loads(request.content))
                return httpx.Response(200, json=OK)
            return httpx.Response(200, json={'data': []})
        patch = mock.patch.object(config, 'http_client', lambda *a, **k: httpx.AsyncClient(
            transport=httpx.MockTransport(respond)))
        patch.start()
        self.addCleanup(patch.stop)

    def close_db(self):
        budgetowner.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = self.old_conn

    def post(self, path, fields, *, quota=10000):
        key = keysvc.create_key('wire-fixture', quota=quota)
        body = {'model': 'm', **fields}
        if 'responses' in path:
            body['input'] = 'hi'
        else:
            body['messages'] = [{'role': 'user', 'content': 'hi'}]
        headers = {'Authorization': 'Bearer ' + key['key'], 'anthropic-version': '2023-06-01'}
        return self.client.post(path, json=body, headers=headers), key

    def test_chat_conflicting_output_caps_are_rejected_before_reserving_or_sending(self):
        for a, b in ((20000, 30000), (30000, 20000), (10**12, 10**11)):
            with self.subTest(a=a, b=b):
                response, key = self.post('/v1/chat/completions', {'max_tokens': a, 'max_completion_tokens': b})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()['error']['code'], 'conflicting_output_limits')
                self.assertEqual(self.sent, [])
                self.assertEqual(db.query_one('SELECT count(*) FROM budget_reservations WHERE key_id=?',
                                             (key['id'],))[0], 0)

    def test_equal_aliases_are_both_clamped_to_the_same_wire_limit(self):
        response, key = self.post('/v1/chat/completions', {'max_tokens': 30000, 'max_completion_tokens': 30000})
        self.assertEqual(response.status_code, 200, response.text)
        effective = int(response.headers['x-manager-max-tokens-effective'])
        self.assertEqual(self.sent[0]['max_tokens'], effective)
        self.assertEqual(self.sent[0]['max_completion_tokens'], effective)
        self.assertLess(effective, 10000)
        self.assertEqual(db.query_one('SELECT reserved_tokens FROM budget_reservations WHERE key_id=?',
                                     (key['id'],))[0], 10000)

    def test_each_protocol_cap_is_the_actual_clamped_wire_value(self):
        cases = [('/v1/chat/completions', 'max_tokens'),
                 ('/v1/chat/completions', 'max_completion_tokens'),
                 ('/v1/messages', 'max_tokens'), ('/v1/responses', 'max_output_tokens')]
        for path, field in cases:
            with self.subTest(path=path, field=field):
                self.sent.clear()
                response, _ = self.post(path, {field: 30000})
                self.assertEqual(response.status_code, 200, response.text)
                wire_field = 'max_completion_tokens' if field == 'max_completion_tokens' else 'max_tokens'
                self.assertEqual(self.sent[0][wire_field], int(response.headers['x-manager-max-tokens-effective']))

    def test_unsupported_alias_on_other_protocols_is_not_silently_discarded(self):
        cases = [('/v1/messages', {'max_tokens': 20, 'max_completion_tokens': 10000}),
                 ('/v1/responses', {'max_output_tokens': 20, 'max_tokens': 10000}),
                 ('/v1/chat/completions', {'max_output_tokens': 10000})]
        for path, fields in cases:
            with self.subTest(path=path):
                response, _ = self.post(path, fields)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()['error']['code'], 'unsupported_output_limit')
                self.assertEqual(self.sent, [])

    def test_invalid_output_values_are_rejected_before_clamping(self):
        for path, field in (('/v1/chat/completions', 'max_tokens'),
                            ('/v1/chat/completions', 'max_completion_tokens'),
                            ('/v1/messages', 'max_tokens'), ('/v1/responses', 'max_output_tokens')):
            for invalid in (None, 0, -1, True, 1.5, '100'):
                with self.subTest(path=path, field=field, invalid=invalid):
                    response, _ = self.post(path, {field: invalid})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
