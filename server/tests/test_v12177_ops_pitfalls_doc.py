"""v1.2.177：把这两天踩过的运维坑固化成文档。

## 为什么要有这份

两天里有十次「我以为是 A，实际是 B」，**其中三次是同一类错误的重复**：

- `| Out-Null` 让退出码说谎（报「推送成功」而远端没变）
- 注释里引用代码让字符串匹配失效（两天里踩了三次）
- 列举式清单静默失败（新脚本缺执行位，而发布完全不报错）

**不写下来，下一轮还会踩。** 而这些坑的共同点是：**它们都不报错，只是让某个东西悄悄地不对。**

## 每条必须写三件事

**症状 → 原因 → 正确做法**。

只写「不要做 X」没有用 —— 下一个人不知道为什么要避免它，
就会在看起来更合理的时候再做一次。

## 本测试守什么

钉住那三条重复踩过的必须在文档里、且每条都有「正确做法」，
以及「为什么这些坑危险」的共性判断 —— 那是这份文档的核心论点。
"""
from __future__ import annotations

import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parents[2] / 'docs' / 'OPS-PITFALLS.md').read_text(encoding='utf-8')


class TheRepeatedOnesAreRecordedTests(unittest.TestCase):
    """这三条是同一个错误踩了不止一次，最需要固化。"""

    def test_pipeline_swallows_exit_code(self):
        self.assertIn('Out-Null', DOC)
        self.assertIn('$LASTEXITCODE', DOC)

    def test_comment_quoting_breaks_string_match(self):
        self.assertIn('注释里引用代码', DOC)

    def test_hand_written_list_fails_silently(self):
        self.assertIn('列举式清单会静默失败', DOC)


class TheOtherRealIncidentsAreRecordedTests(unittest.TestCase):
    def test_invented_commit_id(self):
        self.assertIn('不要编造提交标识', DOC)

    def test_building_with_old_tool(self):
        self.assertIn('用旧工具造新工具', DOC)

    def test_error_response_downloaded_as_file(self):
        self.assertIn('rate limit exceeded', DOC)

    def test_rm_rf_while_inside(self):
        self.assertIn('Unable to read current working directory', DOC)

    def test_same_name_multiple_resources(self):
        self.assertIn('同名资源可能不止一个', DOC)

    def test_zero_has_three_meanings(self):
        self.assertIn('`0` 有三种含义', DOC)


class EachEntryHasASymptomCauseFixTests(unittest.TestCase):
    """只写「不要做 X」没有用 —— 必须解释为什么。"""

    def test_most_entries_have_symptoms(self):
        self.assertGreaterEqual(DOC.count('**症状**'), 8,
                                '有太多条目没写症状')

    def test_most_entries_explain_the_cause(self):
        self.assertGreaterEqual(DOC.count('**原因**'), 7,
                                '有太多条目没解释原因')

    def test_most_entries_give_the_correct_approach(self):
        self.assertGreaterEqual(DOC.count('**正确做法**'), 7,
                                '有太多条目没给正确做法')


class ItStatesWhyTheseAreDangerousTests(unittest.TestCase):
    """共性判断：这些坑的共同点是不报错。"""

    def test_it_names_the_common_property(self):
        self.assertIn('它们都不报错', DOC)

    def test_it_distinguishes_from_generic_advice(self):
        self.assertIn('不是通用建议', DOC)


class TheTwoNewEntriesTests(unittest.TestCase):
    """第十一条是同一类错误里的第四次；第十二条是一次险些误判。"""

    def test_validation_and_commit_must_be_separate_calls(self):
        """一次调用里既跑测试又提交就是错的 —— 测试失败不会中断提交。

        这条犯了三次，每次靠强推收拾，而强推会改写远端历史。
        """
        self.assertIn('十一次', DOC.replace('十一、', '十一次'))
        self.assertIn('一次工具调用里只要既跑测试又提交，它就是错的', DOC)

    def test_it_explains_the_cost_of_force_push(self):
        self.assertIn('强推会改写远端历史', DOC)

    def test_config_limit_is_not_a_user_limit(self):
        self.assertIn('16 MiB', DOC)
        self.assertIn('离上限很近', DOC)

    def test_both_new_entries_have_all_three_parts(self):
        """症状/原因/正确做法是这份文档的硬要求。"""
        self.assertGreaterEqual(DOC.count('**症状**'), 10)
        self.assertGreaterEqual(DOC.count('**原因**'), 9)


class ItSaysHowToMaintainItTests(unittest.TestCase):
    def test_maintenance_requires_three_parts(self):
        self.assertIn('症状 → 原因 → 正确做法', DOC)

    def test_it_explains_why_bare_prohibitions_fail(self):
        self.assertIn('只写「不要做 X」没有用', DOC)


if __name__ == '__main__':
    unittest.main()
