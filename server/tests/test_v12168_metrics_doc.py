"""v1.2.168：给 67 个指标写一份运维清单。

## 为什么

`/metrics` 有 67 个指标名、116 个序列，**没有任何一处说明**。
新来的人只能看到一堆名字，然后靠猜判断哪个该报警。

## 这份文档唯一的价值

**不是列名字，是解释「读数为 0 时该怎么理解」。**

同一个 `0` 在不同指标上含义完全相反：

| 指标 | 0 的含义 |
| --- | --- |
| `errors_5xx_24h` | **健康**（没有错误） |
| `retry_after_forwarded` | **还没触发**（功能正常，上游没限流） |
| `archive_worker_alive` | **异常**（线程没在跑） |
| `audit_chain_intact` | **断裂**（而 `-1` 才是「没查过」） |

**把这三类混在一起看，就会在正常时报警、在异常时放过。**

## 本测试守什么

文档最容易烂掉的方式是「指标全了但说反了」。这里钉住几处最容易写错的语义，
以及「哪些为 0 不该报警」那一节 —— 那是最容易被后来者推翻又改错的部分。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parents[2] / 'docs' / 'METRICS.md').read_text(encoding='utf-8')
SCALE = re.search(r'规模：\*\*(\d+) 个指标名 / (\d+) 个序列\*\*', DOC)


class ItStatesTheScaleTests(unittest.TestCase):
    def test_scale_is_recorded(self):
        self.assertIsNotNone(SCALE, '没记录指标规模')

    def test_numbers_are_plausible(self):
        """指标数会随版本变，但跌回个位数说明有人删了整段。"""
        names = int(SCALE.group(1))
        series = int(SCALE.group(2))
        self.assertGreater(names, 50, '指标数异常偏少')
        self.assertGreater(series, names, '序列数不可能少于指标名数')


class TheZeroSemanticsAreSpelledOutTests(unittest.TestCase):
    """这份文档存在的唯一理由。"""

    def test_it_opens_with_the_three_kinds_of_zero(self):
        self.assertIn('同一个 `0` 在不同指标上的含义完全相反', DOC)

    def test_healthy_zero_is_marked(self):
        self.assertIn('**健康**', DOC)

    def test_not_triggered_zero_is_marked(self):
        self.assertIn('还没遇到上游限流', DOC)

    def test_abnormal_zero_is_marked(self):
        self.assertIn('**异常**', DOC)


class TheTrapCasesAreRightTests(unittest.TestCase):
    """几处最容易写反的语义。"""

    def test_snapshot_age_minus_one_is_expected(self):
        self.assertIn('`-1` 是预期的', DOC)

    def test_audit_chain_three_states(self):
        self.assertIn('1 完好 / 0 断裂 / -1 从未检查', DOC)

    def test_audit_chain_minus_one_is_not_a_fault(self):
        idx = DOC.find('`-1` 是「还没查过」')
        self.assertGreater(idx, 0, '审计链的 -1 与 0 没有被区分开')

    def test_thin_samples_warn_against_quantiles(self):
        self.assertIn('少于 20 时不要引用分位值', DOC)


class TheAlertListIsUsefulTests(unittest.TestCase):
    def test_it_lists_a_do_alert_section(self):
        self.assertIn('建议告警的几条', DOC)

    def test_it_lists_a_do_not_alert_section(self):
        self.assertIn('这些不要告警', DOC)

    def test_the_not_alert_section_covers_the_new_counter(self):
        """新加的计数器为 0 是目标状态，不能进告警。"""
        idx = DOC.find('这些不要告警')
        window = DOC[idx:idx + 400]
        self.assertIn('retry_after_forwarded', window)


class ItSaysHowToKeepItCurrentTests(unittest.TestCase):
    def test_maintenance_note_exists(self):
        self.assertIn('新增指标时请同时更新本文件', DOC)


if __name__ == '__main__':
    unittest.main()
