import json
from pathlib import Path
from unittest import mock

import httpx

from server import config, db
from server.tests.test_rejection_retry_integration import REJECT_BODY, RejectionRetryIntegrationTest


class ProtocolReplayTest(RejectionRetryIntegrationTest):
    def test_retries_share_one_budget_across_dispatches(self):
        db.set_setting('upstream_rejection_retry', 4)
        budgets = []

        def respond(request):
            remaining = int(request.headers['x-wb-attempt-budget'])
            budgets.append(remaining)
            return httpx.Response(503, json=REJECT_BODY, headers={'x-wb-provider-attempts': str(min(3, remaining))})

        with mock.patch.object(config, 'http_client',
                side_effect=lambda *args, **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond))):
            result = self.c.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + self.key},
                                 json={'model': 'm', 'messages': [{'role': 'user', 'content': 'fixture'}], 'max_tokens': 16})
        self.assertEqual(result.status_code, 503, result.text)
        self.assertEqual(budgets, [8, 5, 2])

    def test_missing_attempt_receipt_prevents_extra_rejection_replay(self):
        calls = []

        def respond(request):
            calls.append(1)
            return httpx.Response(503, json=REJECT_BODY)

        with mock.patch.object(config, 'http_client',
                side_effect=lambda *args, **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond))):
            result = self.c.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + self.key},
                                 json={'model': 'm', 'messages': [{'role': 'user', 'content': 'fixture'}], 'max_tokens': 16})
        self.assertEqual(result.status_code, 503, result.text)
        self.assertEqual(len(calls), 1)

    def test_versioned_synthetic_protocol_matrix_has_no_unexpected_dispatch(self):
        fixture = json.loads(Path(__file__).with_name('fixtures').joinpath('protocol-replay-v007.json').read_text(encoding='utf-8'))
        self.assertEqual(fixture['schema'], 1)
        self.assertIsNone(fixture['client_versions']['codex'])
        for case in fixture['cases']:
            calls = []

            def respond(request):
                if request.method != 'POST':
                    return httpx.Response(200, json={'data': []})
                calls.append(json.loads(request.content))
                return httpx.Response(200, headers={'x-wb-provider-attempts': '1'}, json={
                    'id': 'synthetic-fixture', 'model': 'm', 'choices': [
                        {'index': 0, 'message': {'role': 'assistant', 'content': 'fixture ok'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})

            with self.subTest(case=case['id']), mock.patch.object(config, 'http_client',
                    side_effect=lambda *args, **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond))):
                response = self.c.post(case['path'], headers={'Authorization': 'Bearer ' + self.key},
                                       json=dict(case['body'], model='m'))
                self.assertEqual(response.status_code, case['status'], response.text)
                self.assertEqual(len(calls), 1 if case['status'] == 200 else 0)
