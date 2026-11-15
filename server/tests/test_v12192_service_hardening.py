"""v1.2.192：服务加固现状。

## 最重要的一条

unit 文件里写着 `User=root`，**但进程实际是 uid 10001**。

只看配置文件会误判成「用 root 跑」。**必须看 `ps` 的实际身份。**

## 第二条

`CapabilityBoundingSet` 是全量，看起来像没限制 ——
但**这些能力只对 root 生效**，uid 10001 本来就不持有。
所以它是纵深防御层面的事，不是当前漏洞。
"""
from __future__ import annotations

import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parents[2] / 'docs' /
       'SERVICE-HARDENING-2026-11-15.md').read_text(encoding='utf-8')


class ItRecordsTheActualIdentityTests(unittest.TestCase):
    """配置写 root、实际跑 10001 —— 只看配置会误判。"""

    def test_it_states_the_real_uid(self):
        self.assertIn('10001', DOC)

    def test_it_warns_against_reading_only_the_unit_file(self):
        self.assertIn('只看 unit 文件会误判', DOC)

    def test_it_says_to_check_ps(self):
        self.assertIn('必须看 `ps` 的实际身份', DOC)


class ItListsWhatIsAlreadyHardenedTests(unittest.TestCase):
    def test_no_new_privileges(self):
        self.assertIn('NoNewPrivileges', DOC)

    def test_private_tmp(self):
        self.assertIn('PrivateTmp', DOC)

    def test_protect_system(self):
        self.assertIn('ProtectSystem', DOC)


class ItIsHonestAboutTheCapabilityGapTests(unittest.TestCase):
    """全量 capability 看起来吓人，但 uid 10001 拿不到。"""

    def test_it_says_capabilities_need_root(self):
        self.assertIn('只对 root 进程有效', DOC)

    def test_it_calls_it_defence_in_depth_not_a_hole(self):
        self.assertIn('不是当前存在的漏洞', DOC)


class ItSaysWhyItCannotChangeItTests(unittest.TestCase):
    """命令守卫不允许写 systemd unit —— 这是有意为之。"""

    def test_it_explains_the_fence(self):
        self.assertIn('不允许写 systemd unit', DOC)

    def test_it_explains_why_that_fence_exists(self):
        self.assertIn('一个配置错误就让服务起不来', DOC)


class ItGivesUsableConfigTests(unittest.TestCase):
    def test_provides_a_snippet(self):
        self.assertIn('CapabilityBoundingSet=', DOC)
        self.assertIn('RestrictAddressFamilies=', DOC)

    def test_warns_that_restart_interrupts(self):
        self.assertIn('会中断在飞请求', DOC)

    def test_prefers_rolling_through_a_release(self):
        self.assertIn('滚动切换', DOC)


class ItRanksTheRisksTests(unittest.TestCase):
    """最重要的是让读者知道先做哪一件。"""

    def test_it_compares_with_offsite(self):
        self.assertIn('异地副本', DOC)

    def test_it_says_do_offsite_first(self):
        self.assertIn('做异地备份', DOC)


if __name__ == '__main__':
    unittest.main()
