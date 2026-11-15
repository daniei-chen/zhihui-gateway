"""v1.2.156：清掉同版本的陈旧空事务。

## 垃圾从哪来

`prepare` 常被乐观锁拒（「多了一个写入者」），而**被拒的尝试也会留下一个事务目录**。

2026-11-15 清点：26 个事务里 **11 个是这种情况** —— 9 个空 phase（被拒）、
2 个 prepared（成功但没 run）。每个约 1 MB，单看不值一提，
但**没有任何东西清理它们**，会随每次发布累积。

## 安全边界，缺一不可

| 边界 | 破坏后的后果 |
| --- | --- |
| 只动同一个版本号 | 跨版本扫会删掉别的版本的记录 |
| 只动空 phase / prepared | 删掉 complete 的事务等于删审计记录 |
| 绝不动本次事务 | 把正在收尾的自己删了 |
| **版本号解析失败就整段跳过** | **表达式退化成正则 `*`，会清掉所有事务** |

最后一条是这里最危险的地方：`"${version_tag}"*` 在 version_tag 为空时
变成 `*`，匹配整个 transactions 目录。

## 本测试守什么

前面四条边界各一条断言；另外**单独验证版本号解析正则的实际行为**
（用 Python 复刻同一模式，比对真实事务名），而不是只找字符串。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'post-migration-cleanup.sh'
SOURCE = SCRIPT.read_text(encoding='utf-8')


class ItClearsStaleTransactionsTests(unittest.TestCase):
    def test_it_has_the_section(self):
        self.assertIn('陈旧空事务', SOURCE)

    def test_it_deletes_them(self):
        self.assertIn('rm -rf "$d"', SOURCE)


class ItRefusesToCrossVersionsTests(unittest.TestCase):
    def test_it_scopes_by_version_prefix(self):
        self.assertIn('"${version_tag}"*/', SOURCE,
                      '没有按版本号收窄范围')

    def test_it_skips_when_parsing_fails(self):
        """**最重要的一条**：version_tag 为空时表达式变成 `*`，会清掉所有事务。"""
        self.assertIn('[ -z "$version_tag" ]', SOURCE,
                      '没检查解析结果，空值会扫全表')
        idx = SOURCE.find('[ -z "$version_tag" ]')
        window = SOURCE[idx:idx + 300]
        self.assertIn('跳过', window, '解析失败时没有跳过')


class ItNeverTouchesCompletedOrItsOwnTests(unittest.TestCase):
    def test_it_skips_its_own_transaction(self):
        self.assertIn('[ "$name" = "$TXN" ] && continue', SOURCE,
                      '会把自己删掉')

    def test_it_only_acts_on_empty_or_prepared(self):
        self.assertIn('""|prepared)', SOURCE,
                      '没把范围限制在空 phase / prepared')
        # complete 必须落到保留分支
        idx = SOURCE.find('"")|prepared)') if '"")|prepared)' in SOURCE else SOURCE.find('""|prepared)')
        self.assertGreater(idx, 0)
        window = SOURCE[idx:idx + 700]
        self.assertIn('保留', window, 'complete 的事务没有明确的保留分支')


class TheVersionPatternWorksTests(unittest.TestCase):
    """用 Python 复刻脚本里的正则，拿真实事务名验证它。"""

    #: 与脚本里 sed 的 ``s/^\(v[0-9]\{1,\}\)[a-z].*/\1/p`` 等价
    PATTERN = re.compile(r'^(v[0-9]{1,})[a-z]')

    def test_it_extracts_the_version_from_real_names(self):
        for name, want in (('v12155a-release-20261115', 'v12155'),
                           ('v12149b-release-20261115', 'v12149'),
                           ('v12140a-release-20261115', 'v12140')):
            with self.subTest(name=name):
                m = self.PATTERN.match(name)
                self.assertIsNotNone(m, '解析不出：' + name)
                self.assertEqual(m.group(1), want)

    def test_it_returns_nothing_for_a_foreign_name(self):
        """格式不认识时必须解析不出来 —— 那样脚本会跳过整段。"""
        for name in ('weird-format', '', 'release-20261115', 'v-abc'):
            with self.subTest(name=name):
                self.assertIsNone(self.PATTERN.match(name),
                                  '不该解析出东西：%r' % name)

    def test_same_version_names_share_the_prefix(self):
        """同一版本的不同尝试必须共享前缀，否则清理不到它们。"""
        a = self.PATTERN.match('v12155a-release-20261115').group(1)
        b = self.PATTERN.match('v12155b-release-20261115').group(1)
        self.assertEqual(a, b)

    def test_different_versions_do_not_share_the_prefix(self):
        a = self.PATTERN.match('v12155a-release-20261115').group(1)
        b = self.PATTERN.match('v12154a-release-20261115').group(1)
        self.assertNotEqual(a, b, '不同版本共享前缀会导致跨版本清理')


class TheReasonIsWrittenDownTests(unittest.TestCase):
    def test_it_records_the_measured_counts(self):
        self.assertIn('26 个事务', SOURCE, '没写出清点结果')
        self.assertIn('11 个', SOURCE)
        self.assertIn('57 MB', SOURCE, '没写出完整事务有多大，读者不知道这笔账')

    def test_it_says_why_this_is_dangerous(self):
        self.assertIn('宁可不动', SOURCE,
                      '没写出「宁可不动也不能动错」这个取舍')


if __name__ == '__main__':
    unittest.main()
