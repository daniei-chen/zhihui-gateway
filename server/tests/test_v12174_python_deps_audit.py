"""v1.2.174：补上 Python 后端依赖审计。

## 缺口从哪来

`docs/` 里已经有一份依赖审计文档，但**它审的是前端**（43 个 npm 依赖）。
后端跑在虚拟环境里的 34 个 Python 依赖，**从来没有对照过 CVE 清单**。

## 最值得记的一条

`h11` 停在 **0.16.0** —— 那正是请求走私问题的修复版本。

**不是「远在其后」，是「刚好够」。** 再往下掉一格就不安全了，
而「刚好够」这条状态不会自己提醒任何人。

## 这份审计的边界

它是**按已知 CVE 手工逐项核对**，不是全量扫描 —— 这台机器上没有装扫描工具。

所以文档里必须写清：**「没发现问题」不等于「没有问题」**。
本测试钉住这句话必须在，因为它是这份文档唯一诚实的部分。
"""
from __future__ import annotations

import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parents[2] / 'docs' /
       'DEPENDENCY-AUDIT-PYTHON-2026-11-15.md').read_text(encoding='utf-8')


class ItStatesItsScopeTests(unittest.TestCase):
    def test_says_how_many_dependencies(self):
        self.assertIn('34 个', DOC)

    def test_distinguishes_from_the_frontend_audit(self):
        """两份文档名字像，必须能分清。"""
        self.assertIn('Python', DOC)

    def test_records_the_absence_of_a_scanner(self):
        self.assertIn('未安装', DOC)


class ItCaughtTheH11BoundaryTests(unittest.TestCase):
    """`h11` 恰好停在修复版上 —— 这是这份审计最有价值的一条。"""

    def test_h11_version_is_recorded(self):
        self.assertIn('`h11`', DOC)
        self.assertIn('0.16.0', DOC)

    def test_it_says_just_barely_not_far_beyond(self):
        self.assertIn('刚好够', DOC)

    def test_it_warns_against_downgrade(self):
        self.assertIn('再往下掉一格就不安全', DOC)


class ItExplainsWhyNotUpgradingTests(unittest.TestCase):
    def test_lists_the_outdated_ones(self):
        self.assertIn('0.163.1', DOC)

    def test_gives_the_reason_for_not_upgrading(self):
        """「有新版」不是理由 —— 这条判断必须写下来，否则下次有人会随手升。"""
        self.assertIn('「有新版」不是升级的理由', DOC)

    def test_says_upgrade_one_at_a_time(self):
        self.assertIn('单独升那一个', DOC)


class ItAdmitsTheBlindSpotsTests(unittest.TestCase):
    """边界不是免责声明，是真实的盲区。"""

    def test_it_lists_what_was_not_done(self):
        self.assertIn('没有实际执行漏洞扫描', DOC)

    def test_it_says_absence_of_finding_is_not_absence_of_problem(self):
        self.assertIn('「没发现问题」不等于「没有问题」', DOC)


class ItSaysHowToKeepItCurrentTests(unittest.TestCase):
    def test_maintenance_note(self):
        self.assertIn('请回来更新第二张表', DOC)


if __name__ == '__main__':
    unittest.main()
