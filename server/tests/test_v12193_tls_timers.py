"""v1.2.193：TLS 与定时任务审计。

## 两个值得记住的点

**一、TLS 结论是握手测出来的，不是看配置得出的。**
配置写了禁用、和实际握手被拒，是两件事。

**二、机器上还有别的服务。**
每小时跑的健康检查查的是 `kaogong-api`，**不是这个网关** ——
很容易把它的绿灯当成「网关也正常」。
"""
from __future__ import annotations

import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parents[2] / 'docs' /
       'TLS-AND-TIMERS-2026-11-15.md').read_text(encoding='utf-8')


class ItRecordsTheProtocolResultsTests(unittest.TestCase):
    def test_old_versions_rejected(self):
        self.assertIn('TLS 1.0', DOC)
        self.assertIn('拒绝', DOC)

    def test_modern_versions_accepted(self):
        self.assertIn('TLS 1.2', DOC)
        self.assertIn('TLS 1.3', DOC)

    def test_it_says_this_was_measured_not_read(self):
        """配置写禁用和握手被拒是两件事。"""
        self.assertIn('不是看配置文件得出的', DOC)


class ItRecordsTheCertificateTests(unittest.TestCase):
    def test_expiry_is_recorded(self):
        self.assertIn('2026-12-15', DOC)

    def test_issuer(self):
        self.assertIn("Let's Encrypt", DOC)


class ItRecordsAutomaticRenewalTests(unittest.TestCase):
    def test_timer_exists(self):
        self.assertIn('certbot.timer', DOC)

    def test_the_unverified_part_is_stated(self):
        """续期成功之后还要重载证书 —— 这一环没验证过。"""
        self.assertIn('没有验证', DOC)

    def test_it_gives_a_verification_method(self):
        self.assertIn('比对证书文件', DOC)


class ItWarnsAboutOtherServicesTests(unittest.TestCase):
    """最容易犯的错：把别的服务的绿灯当成自己的。"""

    def test_it_names_the_other_app(self):
        self.assertIn('kaogong-api', DOC)

    def test_it_says_it_does_not_cover_this_gateway(self):
        self.assertIn('没有一项覆盖智汇网关', DOC)

    def test_it_explains_the_confusion(self):
        self.assertIn('当成「我的网关也正常」', DOC)


class ItSaysWhatWasNotConfirmedTests(unittest.TestCase):
    def test_reload_hook_remains_unverified(self):
        self.assertIn('而不是假装已经验证过', DOC)


if __name__ == '__main__':
    unittest.main()
