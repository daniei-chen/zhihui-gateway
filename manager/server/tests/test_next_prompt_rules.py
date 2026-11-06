import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from server import promptcompat


class NoopCompatibilityRuleTest(unittest.TestCase):
    def setUp(self):
        self.rule = next(rule for rule in promptcompat._bundle['rules']
                         if rule['id'] == 'claude-branch-template')

    def apply(self, pairs, role='system'):
        body = {'messages': [{'role': role, 'content': self.rule['match'] + ': main'}]}
        result = promptcompat.apply(body, platform='workbuddy',
                                    get_setting={'prompt_defuse_rules': pairs}.get)
        return body, result

    def test_noop_hot_rule_cannot_shadow_signed_builtin(self):
        body, result = self.apply([[self.rule['match'], self.rule['match']]])
        self.assertIn(self.rule['replacement'], body['messages'][0]['content'])
        self.assertIn(self.rule['id'], result.rules)

    def test_noop_does_not_claim_priority_before_valid_override(self):
        body, result = self.apply([[self.rule['match'], self.rule['match']],
                                   [self.rule['match'], 'Reviewed branch description']])
        self.assertEqual(body['messages'][0]['content'], 'Reviewed branch description: main')
        self.assertEqual(len(result.rules), 1)
        self.assertTrue(result.rules[0].startswith('custom-'))

    def test_noop_filter_keeps_application_data_exact(self):
        phrase = self.rule['match']
        body = {'messages': [{'role': role, 'content': phrase} for role in ('user', 'assistant', 'tool')],
                'tools': [{'type': 'function', 'function': {'name': 'read', 'description': phrase,
                           'parameters': {'type': 'object', 'port': 11128}}}]}
        before = copy.deepcopy(body)
        promptcompat.apply(body, platform='workbuddy',
                           get_setting={'prompt_defuse_rules': [[phrase, phrase]]}.get)
        self.assertEqual(body, before)

    def test_hot_disable_still_preserves_system_prompt(self):
        phrase = self.rule['match']
        body = {'messages': [{'role': 'system', 'content': phrase}]}
        promptcompat.apply(body, platform='workbuddy', get_setting={
            'prompt_compat_enabled': False, 'prompt_defuse_rules': [[phrase, phrase]]}.get)
        self.assertEqual(body['messages'][0]['content'], phrase)

    def test_builtin_noop_rejected_at_load(self):
        bundle = copy.deepcopy(promptcompat._bundle)
        bundle['rules'][0]['replacement'] = bundle['rules'][0]['match']
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if path.name == 'workbuddy-compat.json':
                return json.dumps(bundle)
            return original_read(path, *args, **kwargs)
        name = 'isolated_noop_bundle_fixture'
        spec = importlib.util.spec_from_file_location(name, promptcompat.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {name: module}), patch.object(Path, 'read_text', read):
            with self.assertRaisesRegex(ValueError, 'invalid compatibility rules'):
                spec.loader.exec_module(module)
