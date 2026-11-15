"""v1.2.162：把「上游有一批未提交改动」这件事写下来。

## 为什么要写

2026-11-15 查上游仓库时发现 `/opt/workbuddy2api` 有 **9 个文件、
247 行新增**的改动没有提交。

用时间线证明它们是**生效的**（容器构建 11-12 晚于源文件修改 11-10），
所以不是「改了没部署」。风险在另一边：

- 重装机器 → 丢
- 回滚版本 → 丢
- 从仓库重建容器 → 丢

**而丢掉之后不报错**，表现是「某些请求开始被上游拦截」或「退避行为变了」——
很难联想到是少了一段代码。

## 顺带纠正的一件事

审计中曾建议「给慢账号加延迟感知路由」。上游 2026-09-29 就实现了
（`pool: slow-degrade slow-lane accounts via TTFB streak`），
描述与本机量到的「同类账号首字延迟差 4~5 倍」完全吻合。

**所以那条建议是重复劳动。** 两层职责必须分清：

| 层 | 负责 |
| --- | --- |
| 上游 | 账号池、慢账号降权、看门狗、连接与退避 |
| 网关 | 多上游路由、计费、审计链、密钥权限、可观测性 |

## 本测试守什么

文档最容易烂掉的方式是「内容还是旧的」。这里只钉住**最容易失真的几个数字与事实**：
文件数、行数、四个修复的名字，以及「职责边界」那张表的存在。

**不断言行文** —— 措辞可以改，事实不能变。
"""
from __future__ import annotations

import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parents[2] / 'docs' / 'UPSTREAM-LOCAL-CHANGES.md').read_text(
    encoding='utf-8')


class ItRecordsTheScaleTests(unittest.TestCase):
    def test_file_count_and_lines(self):
        """规模数字变了就说明又有人在上面改了东西，文档需要复核。"""
        self.assertIn('9 个文件', DOC)
        self.assertIn('247 行', DOC)

    def test_it_names_the_target_directory(self):
        self.assertIn('/opt/workbuddy2api', DOC)

    def test_it_says_it_is_not_this_repo(self):
        """必须区分清楚：那是上游服务，不是网关仓库。"""
        self.assertIn('不是', DOC)
        self.assertIn('WorkBuddy-API', DOC)


class ItProvesTheChangesAreLiveTests(unittest.TestCase):
    """「改了没生效」和「生效了没提交」是两个完全不同的问题。"""

    def test_it_records_the_timeline(self):
        self.assertIn('2026-11-10', DOC)
        self.assertIn('2026-11-12', DOC)

    def test_it_explains_the_inference(self):
        self.assertIn('容器在源文件修改之后构建', DOC)

    def test_it_names_the_failure_shape(self):
        """丢了之后不报错，这条最容易被低估。

        注意措辞：文档写的是「表现不是报错，是……」，不是「不报错」。
        第一版断言按后者写，红了一次 —— 是我把句子记成了另一个样子，
        不是文档写错。断言行文时容易这样，所以只钉语义关键词。
        """
        self.assertIn('不是报错', DOC)


class ItNamesAllFourFixesTests(unittest.TestCase):
    def test_egress_backoff(self):
        self.assertIn('egressBackoff', DOC)

    def test_queue_wait(self):
        self.assertIn('ParseQueueWait', DOC)

    def test_tool_description_sanitising(self):
        self.assertIn('sanitizeTools', DOC)

    def test_429_reset_time(self):
        self.assertIn('429', DOC)

    def test_the_tool_name_is_not_rewritten(self):
        """改 name 会让工具调用对不上，这个取舍必须写下来。"""
        self.assertIn('不动 `name`', DOC)


class ItGivesAnActionableChecklistTests(unittest.TestCase):
    def test_it_tells_you_to_export_the_diff(self):
        self.assertIn('git diff', DOC)

    def test_it_mentions_untracked_files(self):
        """`??` 开头的未跟踪项同样会丢，只导 diff 不够。"""
        self.assertIn('??', DOC)

    def test_it_tells_you_to_check_build_time(self):
        self.assertIn('构建时间', DOC)


class ItDrawsTheBoundaryTests(unittest.TestCase):
    """这条表是为了防止下一个人再提一个已经存在的建议。"""

    def test_boundary_table_exists(self):
        self.assertIn('慢账号降权', DOC)

    def test_it_records_the_prior_misjudgement(self):
        self.assertIn('重复劳动', DOC)

    def test_it_dates_the_upstream_implementation(self):
        self.assertIn('2026-09-29', DOC)


class ItLeaksNothingTests(unittest.TestCase):
    """仓库是公开的，文档不能带凭据或内网细节。"""

    def test_no_ssh_alias(self):
        self.assertNotIn('github-backup', DOC)

    def test_no_private_key_material(self):
        for marker in ('BEGIN OPENSSH', 'BEGIN RSA', 'ssh-rsa AAAA'):
            self.assertNotIn(marker, DOC)


if __name__ == '__main__':
    unittest.main()
