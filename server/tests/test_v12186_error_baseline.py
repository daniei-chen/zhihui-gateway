"""v1.2.186：记下错误预算基线。

## 为什么要有基线

没有基线的话，**每一次 502 都会被当成新问题重新排查一遍** ——
而它可能只是这一周的正常水平。

## 两个成功率都要看

按全部请求算是 98.5%，按排除限流之后算是 99.0%。**差额全部是 429。**

**只看一个数会得出相反结论**：按全部算是「差一点到 99」，按排除算是「稳稳超过 99」。
"""
from __future__ import annotations

import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parents[2] / 'docs' /
       'ERROR-BUDGET-BASELINE-2026-11-15.md').read_text(encoding='utf-8')


class ItRecordsTheRealNumbersTests(unittest.TestCase):
    def test_total_and_success(self):
        self.assertIn('36 603', DOC)
        self.assertIn('36 043', DOC)

    def test_both_success_rates(self):
        """两个都得看，因为差额全部来自限流。"""
        self.assertIn('98.5%', DOC)
        self.assertIn('99.0%', DOC)

    def test_it_explains_the_gap(self):
        self.assertIn('差额全部是限流', DOC)


class ItSeparatesThrottlingFromFailureTests(unittest.TestCase):
    """看到 429 就去「修」，是把保护机制当成 bug。"""

    def test_429_is_not_a_fault(self):
        self.assertIn('主动限流', DOC)

    def test_400_is_a_client_problem(self):
        self.assertIn('客户端参数', DOC)

    def test_the_two_429_subtypes_are_distinguished(self):
        self.assertIn('key_concurrency_exceeded', DOC)
        self.assertIn('rate_limit_exceeded', DOC)

    def test_it_names_the_sixty_fold_difference(self):
        self.assertIn('60 倍', DOC)


class ItRecordsTheUncertainCaseCorrectlyTests(unittest.TestCase):
    """传输断了就是断了，不能编一个结果出来。"""

    def test_it_quotes_not_replayed(self):
        self.assertIn('Request was not replayed', DOC)

    def test_it_explains_why_not_replaying_matters(self):
        self.assertIn('重复计费', DOC)

    def test_it_prefers_an_honest_502(self):
        self.assertIn('宁可返回一个诚实的 502', DOC)


class ItSaysHowToUseTheNumbersTests(unittest.TestCase):
    def test_gives_a_threshold(self):
        self.assertIn('100 次', DOC)

    def test_says_when_to_update_it(self):
        self.assertIn('回头更新', DOC)


if __name__ == '__main__':
    unittest.main()
