"""Regression coverage for authenticated usage reads and bounded protocol inputs."""
import asyncio
import json
import time
import tracemalloc
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI

from server import db, keysvc, observability
from server.routers import gateway
from server.tests.authorization_test_support import isolated_data


class UsageBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(isolated_data())
        self.key = keysvc.create_key('audit-usage')
        self.app = FastAPI()
        self.app.include_router(gateway.router)
        self.headers = {'Authorization': 'Bearer ' + self.key['key']}

    async def get(self, path, *, headers=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(
                app=self.app, client=('203.0.113.7', 1234)), base_url='http://fixture') as client:
            return await client.get(path, headers=self.headers if headers is None else headers)

    async def test_usage_read_obeys_global_ip_policy_before_reading_statistics(self):
        db.set_setting('security', {'enabled': True, 'mode': 'blacklist'})
        db.execute("INSERT INTO ip_rules(kind,cidr,note,created_at) VALUES('deny',?,?,?)",
                   ('203.0.113.0/24', 'synthetic', int(time.time())))
        response = await self.get('/v1/usage')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'ip_blocked')
        self.assertNotIn('last_30d', response.json())
        self.assertEqual(db.query_one('SELECT reason FROM ip_access_logs ORDER BY id DESC')[0],
                         'ip_blocked')

    async def test_anonymous_usage_failures_are_audited(self):
        with mock.patch.object(gateway, '_auth_log_state', {}):
            self.assertEqual((await self.get('/v1/usage', headers={})).status_code, 401)
            self.assertEqual((await self.get('/v1/usage', headers={
                'Authorization': 'Bearer synthetic-invalid'})).status_code, 401)
        self.assertEqual([row['reason'] for row in db.query(
            'SELECT reason FROM ip_access_logs ORDER BY id')], ['missing_key', 'invalid_key'])

    async def test_fractional_credit_limit_is_never_reported_as_unlimited(self):
        db.execute('UPDATE api_keys SET quota_credit=?,used_credit=?,used_credit_units=? WHERE id=?',
                   (0.3, 0.2, 200000, self.key['id']))
        response = await self.get('/v1/key')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['credits'], {'used': 0.2, 'limit': 0.3, 'remaining': 0.1})

    async def test_credit_micro_unit_survives_self_service_read(self):
        db.execute('UPDATE api_keys SET quota_credit=?,used_credit=?,used_credit_units=? WHERE id=?',
                   (0.000002, 0.000001, 1, self.key['id']))
        self.assertEqual((await self.get('/v1/key')).json()['credits'],
                         {'used': 0.000001, 'limit': 0.000002, 'remaining': 0.000001})

    async def test_usage_totals_and_breakdown_share_two_aggregate_reads(self):
        today = time.strftime('%Y-%m-%d')
        for model, tokens in (('cn:MODEL', 50), ('model', 75)):
            db.execute('INSERT INTO usage_daily(day,key_id,model,realm,requests,prompt_tokens,'
                       'completion_tokens,cache_hit_tokens,cache_miss_tokens) VALUES(?,?,?,?,1,?,0,?,?)',
                       (today, self.key['id'], model, 'cn', tokens, tokens, 0))
        # Materialize the per-key quota row so authentication does not need its
        # compatibility fallback scan of usage_daily during this endpoint test.
        db.execute('INSERT INTO key_daily_usage(day,key_id,tokens,credit_units) VALUES(?,?,?,0)',
                   (today, self.key['id'], 125))
        selects = []
        conn = db.connect()
        conn.set_trace_callback(lambda sql: selects.append(sql))
        try:
            response = await self.get('/v1/usage')
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['today']['tokens'], 125)
        self.assertEqual(result['by_model']['model']['today']['tokens'], 125)
        queries = [sql for sql in selects if sql.lstrip().upper().startswith('SELECT')
                   and ('FROM usage_daily' in sql or 'FROM request_logs' in sql)]
        self.assertEqual(len(queries), 2,
                         'Avoid rescanning the same history for totals and per-model windows')


class NumericEvidenceTests(unittest.TestCase):
    def test_nonfinite_or_oversized_credit_is_unknown(self):
        for bad in (float('inf'), float('nan'), 'Infinity', '1e9999', 10**1000,
                    9_000_000_000_001):
            with self.subTest(value_type=type(bad).__name__):
                self.assertIsNone(gateway._usage_credit({'credit': bad}))

    def test_cache_token_counts_cannot_abort_settlement(self):
        for bad in (float('inf'), float('-inf'), float('nan'), 2**63, 1.5):
            with self.subTest(value=bad):
                self.assertEqual(gateway._usage_cache({'prompt_cache_hit_tokens': bad}),
                                 (None, None, None))
        self.assertEqual(gateway._usage_cache({'prompt_cache_hit_tokens': '7'}), (7, None, None))


class UnicodeMemoryTests(unittest.TestCase):
    def test_wide_json_does_not_allocate_a_second_node_graph(self):
        value = {'input': [0] * 100000}
        if tracemalloc.is_tracing():
            self.skipTest('Independent peak measurement requires an idle tracer')
        tracemalloc.start()
        try:
            self.assertIsNone(gateway._invalid_unicode_path(value))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 256 * 1024,
                        'Validation needs a depth stack, not an allocation for every JSON node')

    def test_unicode_validation_preserves_nested_keys_values_and_path_privacy(self):
        for value in ({'private prompt': ['valid', '\ud800']},
                      {'private\udfff': 'valid'}, {'nested': {'x': '\udfff'}}):
            path = gateway._invalid_unicode_path(value)
            self.assertIsNotNone(path)
            self.assertNotIn('private', path)
            self.assertLessEqual(len(path), 256)
        self.assertIsNone(gateway._invalid_unicode_path({'x': ['中文🙂', ''] * 10}))


class SSEMemoryTests(unittest.TestCase):
    def test_unterminated_empty_data_lines_have_a_finite_event_budget(self):
        audit = observability.StreamAudit(maximum=64)
        with self.assertRaises(ValueError):
            for _ in range(1000):
                audit.feed(b'data:\n')

    def test_long_completed_line_cannot_escape_event_limit(self):
        audit = observability.StreamAudit(maximum=64)
        with self.assertRaises(ValueError):
            audit.feed(b':' + b'x' * 10000 + b'\n')

    def test_coalesced_events_and_split_utf8_preserve_content(self):
        parts = [json.dumps({'choices': [{'delta': {'content': word}}]},
                            ensure_ascii=False) for word in ('中文', '🙂', 'tail')]
        wire = ''.join('data: ' + part + '\n\n' for part in parts).encode()
        audit = observability.StreamAudit()
        events = []
        for offset in range(0, len(wire), 7):
            events.extend(audit.feed(wire[offset:offset + 7]))
        events.extend(audit.finish())
        self.assertEqual([event['choices'][0]['delta']['content'] for event in events],
                         ['中文', '🙂', 'tail'])
