"""v1.2.169：给 37 份文档写一份按场景组织的索引。

## 为什么

`docs/` 里有 37 份文档，**没有索引**。按字母排的目录等于没有目录 ——
读者得先知道文件名才能找到它。

所以这份索引按「你现在要做什么」组织：新来的人、出事的人、要发布的人、
关心安全的人、管容量的人、跟上游打交道的人。

## 最容易出问题的一条

索引指向的文档必须**真的存在**。文档改名或删除后索引不更新，读者会点进 404 ——
**而这比没有索引更糟**：他会以为自己记错了，而不是文档过时了。

所以测试里最重要的一条是**逐个校验链接目标存在**。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / 'docs'
INDEX = DOCS / 'README.md'
TEXT = INDEX.read_text(encoding='utf-8')


def _linked_targets() -> list[str]:
    """索引里所有 `[label](file.md)` 形式的相对链接。"""
    return re.findall(r'\]\(([A-Za-z0-9._-]+\.md)\)', TEXT)


class EveryLinkPointsAtARealFileTests(unittest.TestCase):
    """**最重要的一条。** 失效链接比没有索引更误导人。"""

    def test_links_exist(self):
        targets = _linked_targets()
        self.assertGreater(len(targets), 25, '索引里的链接太少，可能整段没写')
        missing = [t for t in targets if not (DOCS / t).is_file()]
        self.assertEqual(missing, [], '索引指向了不存在的文档：%s' % missing)

    def test_no_duplicate_links(self):
        targets = _linked_targets()
        dupes = {t for t in targets if targets.count(t) > 1}
        self.assertEqual(dupes, set(), '同一份文档被列了多次：%s' % dupes)


class ItIsOrganisedByTaskTests(unittest.TestCase):
    """按场景组织，不是按字母。"""

    def test_it_has_task_oriented_sections(self):
        for heading in ('我是新来的', '线上出事了', '要发布一个新版本',
                        '安全和凭据', '数据、容量、账务', '跟上游打交道'):
            self.assertIn(heading, TEXT, '缺少场景小节：' + heading)

    def test_it_says_why_not_alphabetical(self):
        self.assertIn('按字母排的目录等于没有目录', TEXT)

    def test_it_tells_where_to_start_when_something_breaks(self):
        idx = TEXT.find('线上出事了')
        window = TEXT[idx:idx + 400]
        self.assertIn('先看这个', window)


class ItPointsAtTheTwoEasyToMissOnesTests(unittest.TestCase):
    """这两条能省掉一整轮重复劳动。"""

    def test_upstream_capabilities_is_flagged(self):
        self.assertIn('提改进前先看，别重复造', TEXT)

    def test_upstream_local_changes_is_flagged(self):
        self.assertIn('重装前的必读', TEXT)

    def test_metrics_zero_semantics_is_flagged(self):
        self.assertIn('同一个 `0` 在不同指标上含义相反', TEXT)


class ItSeparatesRecordsFromGuidesTests(unittest.TestCase):
    """审计记录是留档，不是操作手册 —— 混淆它们会让新来的人照着过期结论做。"""

    def test_audit_section_is_marked_as_records(self):
        idx = TEXT.find('审计记录（按日期，留档用）')
        self.assertGreater(idx, 0)
        window = TEXT[idx:idx + 200]
        self.assertIn('不是操作手册', window)


class ItSaysHowToMaintainItTests(unittest.TestCase):
    def test_maintenance_note_exists(self):
        self.assertIn('新增文档时请加进对应小节', TEXT)


if __name__ == '__main__':
    unittest.main()
