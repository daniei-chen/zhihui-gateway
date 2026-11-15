"""v1.2.194：一个看起来像 bug、实际不该改的归类。

## 由来

`11102`（上游说「模型不存在」）被归类成 `no_healthy_account`，看起来是错的。
查代码之后发现它依据 2026-10-28 的现场取证：上游把**内容拒绝**伪装成这个错误，
短窗口、涉及 31/32 个账号、同账号同时出现在成功侧与被拒侧。

**改成 4xx 会让客户端认为「这个请求永远不该重试」**，而一分钟后通常就成功了。

## 这份文档防的是什么

**下一次有人（或某个模型）把它当成 bug 修掉。**
修了之后重试逻辑失效，而失效的表现是「偶发失败变多」——
**没人会把它和这次「修复」联系起来。**
"""
from __future__ import annotations

import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parents[2] / 'docs' /
       'WHY-11102-IS-NO-HEALTHY-ACCOUNT.md').read_text(encoding='utf-8')


class ItSaysDoNotFixThisTests(unittest.TestCase):
    def test_conclusion_is_up_front(self):
        self.assertIn('不要「修」它', DOC)

    def test_it_shows_the_confusing_record(self):
        self.assertIn('no_healthy_account', DOC)
        self.assertIn('service info not found', DOC)


class ItRecordsTheEvidenceTests(unittest.TestCase):
    def test_the_date(self):
        self.assertIn('2026-10-28', DOC)

    def test_the_account_span(self):
        self.assertIn('31/32', DOC)

    def test_it_rules_out_the_two_alternative_explanations(self):
        self.assertIn('账号缺这个模型', DOC)
        self.assertIn('模型映射没生效', DOC)


class ItExplainsWhatTheCodeMeansTests(unittest.TestCase):
    """这个 code 表达的不是字面的账号问题，而是「上游现在不接」。"""

    def test_it_says_replay_is_safe(self):
        self.assertIn('重放是安全的', DOC)

    def test_it_lists_no_generation_no_usage_no_billing(self):
        self.assertIn('没有生成', DOC)
        self.assertIn('没有计费', DOC)


class ItExplainsWhy503NotFourHundredTests(unittest.TestCase):
    def test_four_xx_would_signal_never_retry(self):
        self.assertIn('永远不该重试', DOC)

    def test_it_says_the_request_is_correct(self):
        self.assertIn('请求格式是对的', DOC)


class ItGivesTheBoundaryTests(unittest.TestCase):
    """依赖那段取证 —— 上游行为变了这条判断就错了。"""

    def test_it_names_the_failure_mode(self):
        self.assertIn('集中在少数账号', DOC)

    def test_it_contrasts_short_window(self):
        self.assertIn('短窗口', DOC)


class ItSaysWhyASeparateDocTests(unittest.TestCase):
    def test_the_two_places_are_far_apart(self):
        self.assertIn('相隔很远', DOC)

    def test_it_names_the_silent_consequence(self):
        self.assertIn('偶发失败变多', DOC)


if __name__ == '__main__':
    unittest.main()
