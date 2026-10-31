"""Synthetic-only session routing and unmodified cache-usage regressions."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from starlette.requests import Request

from server import observability
from server.routers import anthropic, gateway


def request(body, headers=None):
    encoded = json.dumps(body).encode()

    async def receive():
        return {'type': 'http.request', 'body': encoded, 'more_body': False}

    return Request({'type': 'http', 'method': 'POST', 'path': '/v1/messages',
                    'query_string': b'', 'headers': [
                        (k.encode(), v.encode()) for k, v in (headers or {}).items()
                    ]}, receive)


class SessionTranslationTests(unittest.TestCase):
    def body(self):
        return {'model': 'cn:hy3', 'max_tokens': 10,
                'messages': [{'role': 'user', 'content': 'synthetic-first-turn'}]}

    def test_explicit_conversation_and_cache_fields_survive_translation(self):
        for fields in ({'conversation_id': 'fixture-conversation'},
                       {'conversationId': 'fixture-camel'},
                       {'prompt_cache_key': 'fixture-cache'},
                       {'metadata': {'conversation_id': 'fixture-meta'}},
                       {'metadata': {'conversationId': 'fixture-meta-camel'}}):
            with self.subTest(fields=fields):
                body = {**self.body(), **fields}
                original = copy.deepcopy(body)
                payload = anthropic.to_openai_request(body)
                for name, value in fields.items():
                    self.assertEqual(payload.get(name), value)
                self.assertEqual(body, original)

    def test_user_identity_is_not_reused_as_conversation_identity(self):
        payload = anthropic.to_openai_request({**self.body(), 'metadata': {
            'user_id': 'fixture-user', 'private_field': 'not-forwarded'}})
        self.assertEqual(payload.get('user'), 'fixture-user')
        self.assertNotIn('metadata', payload)
        self.assertNotIn('conversation_id', payload)

    def test_only_nonempty_string_session_fields_are_forwarded(self):
        for value in (None, '', '  ', [], {}, True, 12):
            with self.subTest(value=value):
                payload = anthropic.to_openai_request({**self.body(),
                    'conversation_id': value, 'prompt_cache_key': value,
                    'metadata': {'conversationId': value}})
                self.assertNotIn('conversation_id', payload)
                self.assertNotIn('prompt_cache_key', payload)
                self.assertNotIn('metadata', payload)

    def test_explicit_cache_key_is_not_overridden_by_text_fallback(self):
        payload = {**self.body(), 'prompt_cache_key': 'fixture-cache'}
        gateway._inject_session_key(payload, request({}))
        self.assertNotIn('conversation_id', payload)

    def test_session_digest_matches_upstream_precedence(self):
        body = {'metadata': {'conversation_id': 'meta'},
                'conversation_id': 'top', 'prompt_cache_key': 'cache'}
        self.assertEqual(gateway._session_key_digest(body),
                         hashlib.sha256(b'meta').hexdigest()[:16])
        self.assertIsNone(gateway._session_key_digest({}))
        self.assertIsNone(gateway._session_key_digest({'conversation_id': {}}))

    def test_identical_client_sessions_keep_distinct_key_upstream_namespaces(self):
        namespaces = set()
        for key_id, upstream_id in ((1, 1), (2, 1), (1, 2)):
            state = observability.RequestAudit('fixture', '/v1/messages', 'POST', 'anthropic', 0,
                                               key_id=key_id)
            token = observability.current.set(state)
            try:
                with patch.object(gateway.upstreamsvc, 'forward_api_key', return_value=''):
                    headers = gateway._upstream_headers({'id': upstream_id})
                namespaces.add(headers['X-WB-Tenant'])
            finally:
                observability.current.reset(token)
        self.assertEqual(len(namespaces), 3)


class SessionForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def forward(self, *, stream=False, headers=None, fields=None, error=False):
        captured = []
        recorded = []
        usage = {'prompt_tokens': 100, 'completion_tokens': 5,
                 'prompt_cache_hit_tokens': 20, 'prompt_cache_miss_tokens': 80}
        data = {'choices': [{'message': {'role': 'assistant', 'content': 'fixture'},
                             'finish_reason': 'stop'}], 'usage': usage}

        class Response:
            status_code = 200
            headers = {}

            def json(self):
                return data

            async def aiter_bytes(self):
                if error:
                    yield b'data: {"error":{"message":"synthetic failure"}}\n\n'
                else:
                    yield ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode()

            async def aclose(self):
                pass

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kwargs):
                captured.append(json.loads(kwargs['content']))
                return Response()

            def build_request(self, method, url, **kwargs):
                captured.append(json.loads(kwargs['content']))
                return object()

            async def send(self, req, **kwargs):
                return Response()

            async def aclose(self):
                pass

        body = {'model': 'cn:hy3', 'max_tokens': 10, 'stream': stream,
                'messages': [{'role': 'user', 'content': 'synthetic-first-turn'}],
                **(fields or {})}
        with ExitStack() as stack:
            stack.enter_context(patch.object(anthropic, '_authorize',
                return_value=({'id': 1}, '127.0.0.1', None)))
            stack.enter_context(patch.object(gateway, '_map_model', return_value='cn:hy3'))
            stack.enter_context(patch.object(gateway, 'clamp_max_tokens'))
            stack.enter_context(patch.object(gateway, '_upstream_for',
                return_value=({'base_url': 'http://fixture.invalid'}, None)))
            stack.enter_context(patch.object(gateway, '_stamp_route'))
            stack.enter_context(patch.object(gateway, '_prepare_forward',
                side_effect=lambda key, payload: json.dumps(payload).encode()))
            stack.enter_context(patch.object(gateway, '_forwarding_identity', return_value='fixture'))
            stack.enter_context(patch.object(gateway, '_upstream_headers', return_value={}))
            stack.enter_context(patch.object(gateway, '_record',
                side_effect=lambda *args, **kwargs: recorded.append(kwargs)))
            stack.enter_context(patch.object(anthropic.config, 'http_client',
                return_value=Client()))
            response = await anthropic.messages(request(body, headers))
            self.assertEqual(response.status_code, 200)
            if stream:
                async for _ in response.body_iterator:
                    pass
        return captured[0], recorded, usage

    async def test_both_forwarding_branches_inject_and_record_session_header(self):
        for stream in (False, True):
            for header in gateway._SESSION_HEADERS:
                with self.subTest(stream=stream, header=header):
                    payload, recorded, usage = await self.forward(stream=stream,
                        headers={header: 'fixture-session'})
                    self.assertEqual(payload['conversation_id'], 'fixture-session')
                    self.assertEqual(recorded[-1].get('session_key'),
                        hashlib.sha256(b'fixture-session').hexdigest()[:16])
                    self.assertEqual(recorded[-1].get('usage'), usage)

    async def test_explicit_body_identity_wins_over_header(self):
        payload, recorded, _ = await self.forward(headers={'x-session-id': 'header'},
            fields={'metadata': {'conversation_id': 'body'}})
        self.assertNotIn('conversation_id', payload)
        self.assertEqual(payload['metadata']['conversation_id'], 'body')
        self.assertEqual(recorded[-1]['session_key'], hashlib.sha256(b'body').hexdigest()[:16])

    async def test_no_session_header_uses_existing_chat_fallback(self):
        payload, recorded, _ = await self.forward()
        self.assertTrue(payload['conversation_id'].startswith('wb-'))
        self.assertEqual(recorded[-1]['session_key'], gateway._session_key_digest(payload))

    async def test_fallback_stays_stable_as_history_grows_and_system_changes(self):
        first, _, _ = await self.forward(fields={'system': 'synthetic-time-one'})
        later, _, _ = await self.forward(fields={'system': 'synthetic-time-two', 'messages': [
            {'role': 'user', 'content': 'synthetic-first-turn'},
            {'role': 'assistant', 'content': 'fixture-response'},
            {'role': 'user', 'content': 'synthetic-next-turn'},
        ]})
        other, _, _ = await self.forward(fields={'messages': [
            {'role': 'user', 'content': 'synthetic-other-conversation'}]})
        self.assertEqual(first['conversation_id'], later['conversation_id'])
        self.assertNotEqual(first['conversation_id'], other['conversation_id'])
        self.assertNotIn('synthetic-first-turn', first['conversation_id'])

    async def test_cache_key_survives_both_branches_without_derived_override(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                payload, recorded, _ = await self.forward(stream=stream,
                    fields={'prompt_cache_key': 'fixture-cache'}, headers={'x-session-id': 'header'})
                self.assertNotIn('conversation_id', payload)
                self.assertEqual(payload['prompt_cache_key'], 'fixture-cache')
                self.assertEqual(recorded[-1]['session_key'],
                                 hashlib.sha256(b'fixture-cache').hexdigest()[:16])

    async def test_stream_error_keeps_session_for_diagnosis(self):
        _, recorded, _ = await self.forward(stream=True, error=True,
            headers={'x-session-id': 'fixture-error'})
        self.assertEqual(recorded[-1]['session_key'],
                         hashlib.sha256(b'fixture-error').hexdigest()[:16])

    async def test_denied_request_does_not_start_upstream_or_derive_session(self):
        with patch.object(anthropic, '_authorize', return_value=(None, '', anthropic._err('denied', 401))), \
             patch.object(gateway, '_map_model', return_value='cn:hy3'), \
             patch.object(gateway, 'clamp_max_tokens'), \
             patch.object(gateway, '_inject_session_key') as inject, \
             patch.object(anthropic.config, 'http_client') as client:
            response = await anthropic.messages(request({'model': 'cn:hy3', 'max_tokens': 10}))
        self.assertEqual(response.status_code, 401)
        inject.assert_not_called()
        client.assert_not_called()
