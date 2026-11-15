"""v1.2.164：记录「上游的等待时间被丢掉」这个缺口。

## 为什么用文档而不是直接改

调查用了 8 次调用才把链路摸清：上层只能拿到异常、异常不带等待字段、
只有底层请求处才有响应头。**完整实现要改四处并逐处验证。**

在余量不足时改四处，风险高于留一份准确的记录。

**但「以后再说」必须有具体内容**，所以文档里写清了四步路径、安全边界和验证方式。

## 本测试守什么

**最重要的是第一条**：这份文档必须明确标记为「未实现」。

否则半年后有人读到它，会以为这个能力已经有了 —— 那比没有文档更糟。
"""
from __future__ import annotations

import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parents[2] / 'docs' /
       'KNOWN-GAP-UPSTREAM-RETRY-AFTER.md').read_text(encoding='utf-8')


class ItSaysItIsNotImplementedTests(unittest.TestCase):
    """文档必须自我声明状态，否则会被误读成已完成。"""

    def test_status_is_marked_unimplemented(self):
        self.assertIn('未实现', DOC)

    def test_it_does_not_claim_a_fix(self):
        for claim in ('已实现', '已修复', '已接入'):
            self.assertNotIn(claim, DOC,
                             '文档声称已做，但这轮确实没做：' + claim)


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


class ItGivesFourStepsTests(unittest.TestCase):
    def test_four_steps_are_listed(self):
        for marker in ('底层请求处', '异常类型', '错误文案函数', '最终响应'):
            self.assertIn(marker, DOC, '缺少步骤：' + marker)

    def test_it_warns_against_a_default_value(self):
        """没给等待时间时不能编一个数 —— 默认值就是假信息。"""
        self.assertIn('不要默认成某个数', DOC)


class ItKeepsTheSecurityBoundaryTests(unittest.TestCase):
    def test_redaction_stays(self):
        self.assertIn('脱敏', DOC)

    def test_only_the_number_crosses(self):
        self.assertIn('只透传', DOC)


class ItSaysHowToVerifyTests(unittest.TestCase):
    def test_it_lists_the_negative_case(self):
        """上游没给这个头时，对外响应里不该出现该字段。"""
        self.assertIn('不该出现该字段', DOC)

    def test_it_mentions_a_live_check(self):
        self.assertIn('真机', DOC)


class ItExplainsWhyItWasNotDoneTests(unittest.TestCase):
    def test_it_states_the_reason(self):
        self.assertIn('余量不足', DOC)

    def test_it_is_not_a_brush_off(self):
        self.assertIn('下一轮可以直接执行', DOC)


if __name__ == '__main__':
    unittest.main()
