"""Wire-level contracts for scoped compatibility, content preservation and refusal."""
import copy
import json
from unittest.mock import patch
from server.routers import gateway
from server.tests.test_request_identity import _HttpCase


PHRASE, REPLACEMENT = gateway._DEFAULT_DEFUSE_RULES[0]


class PromptWireContractTest(_HttpCase):
    def forward(self, messages, platform='workbuddy', model='fake-model:echo', **options):
        key = self.new_key('prompt-contract-' + platform, platforms=[platform])
        prefix = '/v1' if platform == 'workbuddy' else '/' + platform + '/v1'
        return self.client.post(prefix + '/chat/completions',
            headers={'Authorization': 'Bearer ' + key['key']},
            json=dict(model=model, messages=messages, max_tokens=32, **options))

    def echo(self, messages, **options):
        response = self.forward(messages, **options)
        self.assertEqual(response.status_code, 200, response.text)
        return json.loads(response.json()['choices'][0]['message']['content'])

    def test_user_assistant_and_tool_results_remain_exact(self):
        messages = [{'role': 'user', 'content': '原文 ' + PHRASE},
                    {'role': 'assistant', 'content': PHRASE, 'tool_calls': [
                        {'id': 'call_1', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}]},
                    {'role': 'tool', 'tool_call_id': 'call_1', 'content': PHRASE},
                    {'role': 'user', 'content': [{'type': 'text', 'text': PHRASE},
                        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aGVsbG8='}}]}]
        self.assertEqual(self.echo(messages)['messages'], messages)

    def test_only_workbuddy_system_and_developer_text_are_adapted(self):
        messages = [{'role': 'system', 'content': PHRASE},
                    {'role': 'developer', 'content': [{'type': 'text', 'text': PHRASE}]},
                    {'role': 'user', 'content': PHRASE}]
        expected = copy.deepcopy(messages)
        expected[0]['content'] = REPLACEMENT
        expected[1]['content'][0]['text'] = REPLACEMENT
        self.assertEqual(self.echo(messages)['messages'], expected)
        for platform in ('qoder', 'trae'):
            with self.subTest(platform=platform):
                self.assertEqual(self.echo(messages, platform=platform)['messages'], messages)

    def test_boolean_switch_disables_adapter(self):
        messages = [{'role': 'system', 'content': PHRASE}, {'role': 'user', 'content': 'hi'}]
        with patch.object(gateway.db, 'get_setting', return_value=False):
            self.assertEqual(self.echo(messages)['messages'], messages)

    def test_long_user_context_remains_exact(self):
        messages = [{'role': 'user', 'content': ('上下文 ' + PHRASE + '\n') * 2400}]
        self.assertEqual(self.echo(messages)['messages'], messages)

    def test_explicit_provider_refusal_keeps_status_and_details(self):
        response = self.forward([{'role': 'user', 'content': PHRASE}], model='fake-model:refusal')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'content_policy_rejected')
        self.assertEqual(response.json()['error']['message'], 'Explicit provider refusal')
        self.assertNotIn('choices', response.json())

    def test_empty_custom_pattern_cannot_expand_every_character(self):
        body = {'messages': [{'role': 'system', 'content': 'ordinary prompt'}]}
        before = copy.deepcopy(body)
        def settings(name):
            return [['', 'expanded']] if name == 'prompt_defuse_rules' else None
        with patch.object(gateway.db, 'get_setting', side_effect=settings):
            self.assertEqual(gateway.defuse_prompt_fingerprint(body), 0)
        self.assertEqual(body, before)
