"""v1.2.164 记录缺口，v1.2.165 把它实现了 —— 这份测试跟着改。

## 为什么当时用文档而不是直接改

调查用了 8 次调用才把链路摸清：上层只能拿到异常、异常不带等待字段、
只有底层请求处才有响应头。余量不足时改四处，风险高于留一份准确的记录。

## 为什么现在改这个测试

v1.2.165 按那份记录把缺口补上了（`_upstream_retry_after` + 错误分支转发）。

**文档也必须同步改。** 否则会出现最糟的一种状态：代码已经修好，
文档还写着「未实现」，而下一个人照着文档再去「修」一遍 —— 或者更糟，
以为能力不存在而绕路实现。

所以这里断言的方向整个翻过来：**必须标记为已实现，且必须写明落点。**
"""
from __future__ import annotations

import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parents[2] / 'docs' /
       'KNOWN-GAP-UPSTREAM-RETRY-AFTER.md').read_text(encoding='utf-8')


class ItSaysItIsImplementedTests(unittest.TestCase):
    """文档必须自我声明状态，且必须与代码一致。"""

    def test_status_is_marked_implemented(self):
        self.assertIn('已实现', DOC)

    def test_it_no_longer_says_unimplemented(self):
        """代码已修，文档若还写「未实现」会让人重复劳动。"""
        self.assertNotIn('状态：**未实现**', DOC)

    def test_it_names_the_landing_point(self):
        """实现必须落在具体符号上，不能只写「已支持」。"""
        self.assertIn('_upstream_retry_after', DOC)


class ItRecordsTheEvidenceTests(unittest.TestCase):
    def test_it_names_the_upstream_business_code(self):
        self.assertIn('6020', DOC)

    def test_it_lists_the_field_family(self):
        for field in ('estimated_wait', 'retry_after', 'wait_seconds'):
            self.assertIn(field, DOC)

    def test_it_records_the_field_cap(self):
        self.assertIn('7200', DOC)

    def test_it_says_where_the_value_stops(self):
        """上层只有异常，异常没有这个字段 —— 这是整条链断开的地方。"""
        self.assertIn('异常', DOC)


class ItRecordsWhereEachStepLandedTests(unittest.TestCase):
    def test_reading_and_parsing_are_in_one_place(self):
        for marker in ('读上游响应头', '解析与可信度判断', '转发'):
            self.assertIn(marker, DOC, '缺少步骤：' + marker)

    def test_it_warns_against_a_default_value(self):
        """没给等待时间时不能编一个数 —— 默认值就是假信息。"""
        self.assertIn('而不是写 0', DOC)


class ItKeepsTheSecurityBoundaryTests(unittest.TestCase):
    def test_redaction_stays(self):
        self.assertIn('脱敏', DOC)

    def test_only_the_number_crosses(self):
        self.assertIn('只透传', DOC)


class ItSaysHowToVerifyTests(unittest.TestCase):
    def test_it_lists_the_negative_case(self):
        """上游没给这个头时，对外响应里不该出现该字段。"""
        self.assertIn('拿不到就不写字段', DOC)

    def test_it_mentions_a_live_check(self):
        self.assertIn('真机', DOC)


class ItKeepsTheHistoryTests(unittest.TestCase):
    """修完之后，当初为什么没一次做完仍然值得留着。"""

    def test_it_records_the_earlier_deferral(self):
        self.assertIn('余量不足', DOC)

    def test_it_records_the_negative_boundary_case(self):
        self.assertIn('7200', DOC)


if __name__ == '__main__':
    unittest.main()
