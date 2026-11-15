"""v1.2.188：备份现状，以及一个错误判断的纠正。

## 这份文档存在的理由

此前几轮我反复说过「日常备份只保留最新一份，没有 N 天策略」。

**这句话是错的。** 我没查过实际的备份脚本，只是看到某个目录里只有一个文件。

**一个错误的判断会让人去改一个本来正确的策略** —— 而改备份的风险比不动它大得多。
"""
from __future__ import annotations

import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parents[2] / 'docs' /
       'BACKUP-RESTORE-STATUS-2026-11-15.md').read_text(encoding='utf-8')


class ItCorrectsTheWrongClaimTests(unittest.TestCase):
    """纠正必须写在最前面，不能藏在中间。"""

    def test_correction_is_in_the_first_section(self):
        idx = DOC.find('这句话是错的')
        self.assertGreater(idx, 0)
        # 前一千字之内
        self.assertLess(idx, 1200, '纠正没有放在显著位置')

    def test_it_says_what_was_wrong(self):
        self.assertIn('只保留最新一份', DOC)

    def test_it_explains_why_a_wrong_claim_is_dangerous(self):
        self.assertIn('正确的备份策略', DOC)


class ItDescribesTheThreeLayersTests(unittest.TestCase):
    def test_daily_package(self):
        self.assertIn('workbuddy-daily', DOC)
        self.assertIn('05:30', DOC)

    def test_mirror(self):
        self.assertIn('workbuddy-mirror', DOC)
        self.assertIn('7 份', DOC)

    def test_retention_rules(self):
        self.assertIn('最近 3 份', DOC)
        self.assertIn('latest.json', DOC)

    def test_latest_is_never_deleted(self):
        """这条最关键：任何清空都不能让系统失去最后一个可用恢复点。"""
        self.assertIn('永不删除', DOC)


class ItShowsTheBackupsAreSelfVerifiedTests(unittest.TestCase):
    """「打包成功」不等于「备份可用」。"""

    def test_restore_check(self):
        self.assertIn('restore_check', DOC)

    def test_signature_verified(self):
        self.assertIn('signature_verified', DOC)

    def test_it_explains_why_verification_matters(self):
        self.assertIn('你以为有备份', DOC)


class ItNamesTheRealGapTests(unittest.TestCase):
    def test_offsite_is_false(self):
        self.assertIn('offsite', DOC)
        self.assertIn('false', DOC)

    def test_both_copies_on_same_disk(self):
        self.assertIn('同一块盘', DOC)

    def test_it_gives_the_cost(self):
        self.assertIn('300 MB', DOC)


class ItRecordsTheFailureCaseTests(unittest.TestCase):
    def test_hash_mismatch_incident(self):
        self.assertIn('hash mismatch', DOC)

    def test_it_notes_nobody_was_notified(self):
        """失败是响的，但没有人被通知到。"""
        self.assertIn('没有人被通知到', DOC)


if __name__ == '__main__':
    unittest.main()
