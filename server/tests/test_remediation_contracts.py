import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
from unittest import mock

import httpx

from server import config, db, providerevents
from server.routers import gateway
from server.tests.test_rejection_retry_integration import RejectionRetryIntegrationTest


class RemediationContracts(RejectionRetryIntegrationTest):
    def test_all_protocols_share_system_compatibility_and_preserve_user_text(self):
        pattern = 'You are a coding agent running in the Codex CLI'
        replacement = 'You are a coding agent, running in the Codex CLI'
        db.set_setting('prompt_defuse_rules', [[pattern, replacement]])
        routes = (
            ('chat/completions', {'messages': [{'role': 'system', 'content': pattern}, {'role': 'user', 'content': pattern}], 'max_tokens': 16}),
            ('responses', {'instructions': pattern, 'input': pattern, 'max_output_tokens': 16}),
            ('messages', {'system': pattern, 'messages': [{'role': 'user', 'content': pattern}], 'max_tokens': 16}),
        )
        for route, request in routes:
            with self.subTest(route=route):
                captured = []

                def provider(sent):
                    captured.append(json.loads(sent.content))
                    return httpx.Response(200, json={'id': 'test', 'model': 'm', 'choices': [
                        {'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'credit': 0.01}})

                with mock.patch.object(gateway.config, 'http_client', lambda *args, **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(provider))):
                    result = self.c.post('/v1/' + route, headers={'Authorization': 'Bearer ' + self.key}, json=dict(request, model='m'))
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual([message['content'] for message in captured[0]['messages'] if message['role'] == 'system'], [replacement])
                self.assertEqual([message['content'] for message in captured[0]['messages'] if message['role'] == 'user'], [pattern])

    def test_unsupported_responses_semantics_fail_before_provider(self):
        cases = ({'previous_response_id': 'resp_previous'}, {'background': True}, {'parallel_tool_calls': False},
                 {'tools': [{'type': 'function', 'name': 'read', 'parameters': {'type': 'object'}, 'strict': True}]})
        for extra in cases:
            with self.subTest(extra=extra), mock.patch.object(gateway.config, 'http_client', side_effect=AssertionError('no spend')):
                result = self.c.post('/v1/responses', headers={'Authorization': 'Bearer ' + self.key},
                                     json=dict(model='m', input='question', **extra))
                self.assertEqual(result.status_code, 400, result.text)
                self.assertTrue(result.json()['error']['code'].startswith('unsupported_'))

    def test_durable_provider_identity_never_collides_across_dispatches(self):
        value = {'event': 'workbuddy_provider_attempt_v2', 'request_id': 'execution-1', 'dispatch_id': 'dispatch-1',
                 'rotation': 1, 'provider_attempt': 1, 'phase': 'sending', 'ts': int(time.time()),
                 'account_uid': 'synthetic-account', 'model': 'm', 'realm': 'cn', 'path': '/v2/chat/completions', 'status': 0}
        self.assertTrue(providerevents.record(value))
        self.assertTrue(providerevents.record(value))
        second = dict(value, dispatch_id='dispatch-2')
        self.assertTrue(providerevents.record(second))
        report = providerevents.report('execution-1')
        self.assertEqual(report['attempt_count'], 2)
        self.assertEqual(report['attempt_source'], 'provider_attempt_events_v2')
        self.assertIn('legacy_attempt_events', report)
        conflict = dict(value, account_uid='different-account')
        self.assertFalse(providerevents.record(conflict))
        self.assertEqual(db.query_one('SELECT COUNT(*) count FROM provider_evidence_conflicts_v2')['count'], 1)

    def test_collector_imports_only_complete_atomic_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / 'data/provider-events-v2'
            events.mkdir(parents=True)
            value = {'event': 'workbuddy_provider_attempt_v2', 'request_id': 'atomic-execution', 'dispatch_id': 'atomic-dispatch',
                     'rotation': 1, 'provider_attempt': 1, 'phase': 'sending', 'ts': int(time.time()),
                     'account_uid': 'synthetic', 'model': 'm', 'realm': 'cn', 'path': '/v2/chat/completions', 'status': 0}
            (events / '.provider-incomplete.tmp').write_bytes(b'{')
            path = events / 'atomic-dispatch-01-01-sending.json'
            path.write_text(json.dumps(value))
            with mock.patch.object(gateway.config, 'UPSTREAM_DIR', root):
                self.assertEqual(providerevents.collect(), 1)
                self.assertEqual(providerevents.collect(), 0)
                self.assertEqual(providerevents.report('atomic-execution')['attempt_count'], 1)

    def test_provider_evidence_health_is_unknown_until_a_collection_sample(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(config, 'UPSTREAM_DIR', Path(temporary)), \
                mock.patch.dict(providerevents._last_collection,
                                {'at': None, 'error': None}, clear=True):
            before = providerevents.health()
            self.assertFalse(before['ok'])
            self.assertFalse(before['fresh'])
            self.assertIsNone(before['collection_age_seconds'])
            providerevents.collect()
            after = providerevents.health()
            self.assertTrue(after['fresh'])
            self.assertEqual(after['collection']['error'], None)
            providerevents._last_collection['at'] = int(time.time()) - providerevents.COLLECTION_STALE_SECONDS - 1
            stale = providerevents.health()
            self.assertFalse(stale['fresh'])
            self.assertFalse(stale['ok'])


    def test_recent_human_session_required_without_sliding_extension(self):
        from fastapi import HTTPException
        from server import security
        for stamp in (None, int(time.time()) - 901, int(time.time()) + 10):
            with self.subTest(stamp=stamp), self.assertRaises(HTTPException):
                security.require_recent_session_admin(None, {'_session_auth_at': stamp})
        user = {'_session_auth_at': int(time.time())}
        self.assertIs(security.require_recent_session_admin(None, user), user)

    def test_main_financial_connection_is_full_synchronous(self):
        self.assertEqual(db.query_one('PRAGMA synchronous')[0], 2)

    def test_full_wal_commits_survive_process_crash_without_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'synthetic.db'
            program = """import os,sqlite3,sys
connection=sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA synchronous=FULL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute('CREATE TABLE receipts(id INTEGER PRIMARY KEY,amount INTEGER)')
connection.executemany('INSERT INTO receipts VALUES(?,?)',[(number,number*7)for number in range(100)])
connection.commit()
os._exit(23)
"""
            result = subprocess.run([sys.executable, '-B', '-c', program, str(database)], timeout=15)
            self.assertEqual(result.returncode, 23)
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*),SUM(amount) FROM receipts').fetchone(), (100, 34650))
                self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_budget_resolution_rejects_admin_token(self):
        from server import tokensvc
        token = tokensvc.create_token('financial-token-refusal', 'admin')['token']
        response = self.c.post('/api/budgets/synthetic-request/resolve', headers={'Authorization': 'Bearer ' + token},
                               json={'tokens': 0, 'credit': 0, 'reason': 'synthetic valid reason', 'evidence': 'synthetic valid evidence'})
        self.assertEqual(response.status_code, 403, response.text)
