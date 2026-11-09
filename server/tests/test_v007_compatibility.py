import copy
import unittest

from server import promptcompat


class SignedCompatibilityTest(unittest.TestCase):
    def test_claude_variants_are_scoped_and_idempotent(self):
        for apostrophe in ("'", '\u2019'):
            for cli in ('CLI', 'CLI tool'):
                for transform in (str, str.lower, str.upper):
                    phrase = transform(f"You are Claude Code, Anthropic{apostrophe}s official {cli} for Claude.")
                    with self.subTest(phrase=phrase):
                        messages = [{'role': role, 'content': '  ' + phrase + '\n\u4fdd\u7559\u4e0b\u6587'}
                                    for role in ('system', 'developer', 'user', 'assistant', 'tool')]
                        body = {'messages': messages, 'tools': [{'function': {'description': phrase}}]}
                        original = copy.deepcopy(body)
                        changed = promptcompat.apply(body, platform='workbuddy', get_setting=lambda key: None)
                        self.assertIn('claude-cli-identity-variants', changed.rules)
                        for index in (0, 1):
                            self.assertIn('Your role is Claude Code', messages[index]['content'])
                            self.assertTrue(messages[index]['content'].startswith('  '))
                            self.assertTrue(messages[index]['content'].endswith('\n\u4fdd\u7559\u4e0b\u6587'))
                        self.assertEqual(messages[2:], original['messages'][2:])
                        self.assertEqual(body['tools'], original['tools'])
                        snapshot = copy.deepcopy(body)
                        self.assertEqual(promptcompat.apply(body, platform='workbuddy', get_setting=lambda key: None).changes, 0)
                        self.assertEqual(body, snapshot)

    def test_disable_and_non_workbuddy_preserve_input(self):
        phrase = "You are Claude Code, Anthropic's official CLI tool for Claude"
        for platform, settings in (('workbuddy', {'prompt_compat_enabled': False}), ('qoder', {})):
            body = {'messages': [{'role': 'system', 'content': phrase}]}
            original = copy.deepcopy(body)
            promptcompat.apply(body, platform=platform, get_setting=settings.get)
            self.assertEqual(body, original)

    def test_legacy_hot_rule_cannot_hide_new_signed_variant(self):
        original = "You are Claude Code, Anthropic's official CLI for Claude"
        legacy = "You are Claude Code, Anthropic's official CLI tool for Claude"
        body = {'messages': [{'role': 'developer', 'content': original}]}
        settings = {'prompt_defuse_rules': [[original, legacy]]}
        result = promptcompat.apply(body, platform='workbuddy', get_setting=settings.get)
        self.assertIn('claude-cli-identity-variants', result.rules)
        self.assertTrue(body['messages'][0]['content'].startswith('Your role is Claude Code'))
