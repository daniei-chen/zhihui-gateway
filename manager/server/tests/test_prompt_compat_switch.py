"""提示词对冲开关必须**立即**生效（2026-10-28 的失败实验留下的护栏）。

## 由来

`defuse_prompt_fingerprint()` 对每条 workbuddy 请求都要读两个设置项
（`prompt_compat_enabled` + `prompt_defuse_rules`）。曾经给它加过 60 秒缓存以减少
热路径查库，结果既有测试 `test_prompt_integrity...test_boolean_switch_disables_adapter`
直接失败：**关掉开关后最多 60 秒还在改写提示词**。

那是一次真实的取舍判断：`prompt_compat_enabled=false` 是「停用这个适配器」的
安全开关，让它有 60 秒延迟是不可接受的；而两次 `get_setting` 只是本地 SQLite
点查（微秒级）。结论是**不缓存**。

本文件把这个决定钉住：任何人再想加缓存，会先撞上这里的断言。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import db  # noqa: E402
from server.routers import gateway  # noqa: E402

from ._db_fixture import isolate_database  # noqa: E402

TRIGGER = ('Tokens prefixed with @ are paths the user explicitly referenced. '
           'Relative paths resolve from the workspace root')


def _body() -> dict:
    return {'messages': [{'role': 'system', 'content': 'head ' + TRIGGER + ' tail'}]}


def _rewritten(body: dict) -> bool:
    return 'prefixed with @' not in body['messages'][0]['content']


class PromptCompatSwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        isolate_database(self)

    def test_default_rules_rewrite_known_fingerprint(self) -> None:
        body = _body()
        self.assertEqual(gateway.defuse_prompt_fingerprint(body), 1)
        self.assertTrue(_rewritten(body))

    def test_switch_off_takes_effect_immediately(self) -> None:
        """核心护栏：关掉开关后**下一条请求**就不得再改写（不许有缓存延迟）。"""
        self.assertEqual(gateway.defuse_prompt_fingerprint(_body()), 1)
        db.set_setting('prompt_compat_enabled', False)
        body = _body()
        self.assertEqual(gateway.defuse_prompt_fingerprint(body), 0)
        self.assertFalse(_rewritten(body))

    def test_switch_back_on_takes_effect_immediately(self) -> None:
        db.set_setting('prompt_compat_enabled', False)
        self.assertEqual(gateway.defuse_prompt_fingerprint(_body()), 0)
        db.set_setting('prompt_compat_enabled', True)
        self.assertEqual(gateway.defuse_prompt_fingerprint(_body()), 1)

    def test_extra_rule_from_settings_applies_immediately(self) -> None:
        db.set_setting('prompt_defuse_rules', [['SECRET-TRIGGER', 'REWRITTEN']])
        body = {'messages': [{'role': 'system', 'content': 'a SECRET-TRIGGER b'}]}
        self.assertEqual(gateway.defuse_prompt_fingerprint(body), 1)
        self.assertEqual(body['messages'][0]['content'], 'a REWRITTEN b')

    def test_invalid_rules_are_dropped(self) -> None:
        db.set_setting('prompt_defuse_rules', [
            ['OK', 'ok'], ['只有一项'], ['', '空白模式'],
            ['x' * 5000, 'too-long'], [123, 'not-a-string'], 'not-a-pair',
        ])
        body = {'messages': [{'role': 'system', 'content': 'OK'}]}
        self.assertEqual(gateway.defuse_prompt_fingerprint(body), 1)
        self.assertEqual(body['messages'][0]['content'], 'ok')

    def test_user_and_tool_content_is_immutable(self) -> None:
        body = {'messages': [
            {'role': 'user', 'content': TRIGGER},
            {'role': 'tool', 'content': TRIGGER},
        ]}
        self.assertEqual(gateway.defuse_prompt_fingerprint(body), 0)
        self.assertEqual(body['messages'][0]['content'], TRIGGER)
        self.assertEqual(body['messages'][1]['content'], TRIGGER)


if __name__ == '__main__':
    unittest.main()
