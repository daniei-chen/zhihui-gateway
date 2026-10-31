"""Real Manager admission/ledger under ASGI; provider I/O is synthetic only."""
import asyncio
import json
from unittest.mock import patch

import httpx

from server import dailyquota, db, keysvc, steward_ai, steward_config
from server.tests.test_audit_20261029_gateway_ledger import _Fixture, GOOD


class StewardMeteringTests(_Fixture):
    def test_two_round_deepseek_analysis_charges_ordinary_key_and_daily_limit_blocks_third(self):
        model = 'cn:deepseek-fixture'
        key = keysvc.create_key('synthetic-steward', models=[model], realm='cn', platforms=['workbuddy'],
                                ip_allowlist=['127.0.0.1/32'], daily_token_quota=60000, daily_credit_quota=0.02)
        calls = []
        def upstream(request):
            payload = json.loads(request.content)
            calls.append(payload)
            message = ({'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'fixture-call', 'type': 'function',
                        'function': {'name': 'recent_failure_classes', 'arguments': '{}'}}]}
                       if len(calls) == 1 else {'role': 'assistant', 'content': '已核实：本轮只有合成证据。'})
            return httpx.Response(200, json={**GOOD, 'model': model,
                'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if len(calls) == 1 else 'stop'}]})
        real_client = httpx.AsyncClient
        def gateway_client(*args, **kwargs):
            self.assertEqual(kwargs.get('trust_env'), False)
            self.assertEqual(kwargs.get('follow_redirects'), False)
            return real_client(*args, **kwargs, transport=httpx.ASGITransport(app=self.app, client=('127.0.0.1', 1234)))
        with patch('server.steward_ai.httpx.AsyncClient', side_effect=gateway_client), \
                patch('server.config.http_client', side_effect=lambda *a, **k: real_client(transport=httpx.MockTransport(upstream))), \
                patch.object(steward_config, 'model_key', return_value=key['key']):
            result = asyncio.run(steward_ai.answer('只读检查，不执行操作', {'observed_at': 123}, model=model))
            self.assertEqual(result['tools'], ['recent_failure_classes'])
            self.assertEqual(len(calls), 2)
            self.assertNotIn(key['key'], json.dumps(calls))
            daily = dailyquota.snapshot(key['id'], db.day_of())
            self.assertEqual(daily, {'tokens': 10, 'credit_units': 20000})
            logs = db.query('SELECT key_id,actual_platform,prompt_tokens,completion_tokens,credit FROM request_logs ORDER BY id')
            self.assertEqual(len(logs), 2)
            for row in logs:
                self.assertEqual((row['key_id'], row['actual_platform'], row['prompt_tokens'], row['completion_tokens']),
                                 (key['id'], 'workbuddy', 3, 2))
            self.assertAlmostEqual(sum(row['credit'] for row in logs), .02)
            with self.assertRaisesRegex(RuntimeError, 'model_gateway_rejected_429'):
                asyncio.run(steward_ai._request([{'role': 'user', 'content': 'hi'}], model))
            self.assertEqual(len(calls), 2, 'Daily budget rejection must precede provider I/O')
        self.assertFalse(steward_ai._answer_busy)

    def test_disabled_model_or_platform_scope_is_not_bypassed_by_internal_source_header(self):
        model = 'cn:deepseek-fixture'
        real_client = httpx.AsyncClient
        for kwargs in ({'models': ['cn:other']}, {'platforms': ['qoder']}, {'enabled': False}):
            fields = {'models': [model], 'realm': 'cn', 'platforms': ['workbuddy'], **kwargs}
            enabled = fields.pop('enabled', True)
            key = keysvc.create_key('synthetic-steward-boundary', **fields)
            if not enabled:
                keysvc.update_key(key['id'], {'enabled': False})
            with self.subTest(kwargs=kwargs), \
                    patch('server.steward_ai.httpx.AsyncClient', side_effect=lambda *a, **k:
                        real_client(*a, **k, transport=httpx.ASGITransport(app=self.app, client=('127.0.0.1', 1234)))) , \
                    patch.object(steward_config, 'model_key', return_value=key['key']), \
                    self.assertRaisesRegex(RuntimeError, 'model_gateway_rejected_'):
                asyncio.run(steward_ai._request([{'role': 'user', 'content': 'hi'}], model))
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM usage_daily')['n'], 0)
