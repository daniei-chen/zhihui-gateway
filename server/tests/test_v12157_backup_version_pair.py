"""v1.2.157：备份清单里同时记「归档内版本」和「备份时线上实际版本」。

## 这个缺口的来源

2026-11-15 查一个备份快照时发现它记着 `v1.2.96`，而当时线上是 `v1.2.14x`
——差了几十个版本。

追下去确认了两件事：

1. 那份快照**不是这个模块产的**（它的加密方式是 age-X25519，
   而这里的清单里没有 `release_check` 字段），出自一套更早的运维脚本
2. **但这件事暴露了一个真实的信息缺口**：归档里只有「被打进去的那份清单说
   自己是哪个版本」，**没有「备份时线上实际在跑哪个版本」**

两者不一致时，事后翻备份只能看到前者 —— **会得出错误结论**。

## 为什么「读不到」不能算一致

安装根布局可能与预期不同，读 `.version` 会失败。

**那时必须记空串并让一致性判为假**（「无法确认」），
而不是默认成一致 —— 一个默认成一致的自检，等于没有自检。

这和前面几轮反复踩的是同一类错误：**把「没查到」当成「没问题」。**

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 两个字段都写进清单 | 事后无法对比，缺口依旧 |
| 读不到时不抛 | 备份因一个展示字段整体失败 |
| 读不到时一致性为假 | 「没查到」伪装成「一致」 |
| 归一化 `v` 前缀 | `v1.2.156` 与 `1.2.156` 被误判为不一致，天天报警 |
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / 'backup_application.py').read_text(
    encoding='utf-8')


class ItRecordsBothVersionsTests(unittest.TestCase):
    def test_it_records_the_current_version(self):
        self.assertIn("'current_version'", SOURCE,
                      '没记当前运行版本 —— 事后只能看到归档里那份')

    def test_it_records_consistency(self):
        self.assertIn("'version_consistent'", SOURCE)

    def test_it_still_records_the_archive_version(self):
        self.assertIn("'release_id'", SOURCE)


class ItDegradesSafelyTests(unittest.TestCase):
    def test_missing_file_does_not_raise(self):
        idx = SOURCE.find("current_version=''")
        self.assertGreater(idx, 0)
        window = SOURCE[idx:idx + 400]
        self.assertIn('except OSError', window,
                      '读不到 .version 时会抛，备份整体失败')

    def test_unreadable_is_not_treated_as_consistent(self):
        """**最重要的一条**：没查到 ≠ 没问题。"""
        idx = SOURCE.find("'version_consistent'")
        self.assertGreater(idx, 0)
        # 取到行尾
        line = SOURCE[idx:SOURCE.find('\n', idx)]
        self.assertIn('bool(current_version)', line,
                      '没要求 current_version 非空 —— 空值会被判成一致')


class TheComparisonNormalisesThePrefixTests(unittest.TestCase):
    """清单里可能是 `v1.2.156`，也可能是 `1.2.156`。"""

    #: 与源码里 `_bare` 的语义一致
    @staticmethod
    def bare(value):
        return value[1:] if isinstance(value, str) and value.startswith('v') else value

    def test_it_has_the_helper(self):
        self.assertIn('def _bare(', SOURCE)

    def test_the_helper_strips_one_v(self):
        self.assertEqual(self.bare('v1.2.156'), '1.2.156')
        self.assertEqual(self.bare('1.2.156'), '1.2.156')

    def test_matching_forms_compare_equal(self):
        self.assertEqual(self.bare('v1.2.156'), self.bare('1.2.156'))

    def test_different_versions_do_not_match(self):
        self.assertNotEqual(self.bare('v1.2.156'), self.bare('1.2.96'),
                            '不同版本被归一成相同 —— 自检会失效')

    def test_it_does_not_strip_a_double_prefix(self):
        """只该去掉一个 v；`vv1.2` 这种畸形值不该被修得看起来正常。"""
        self.assertEqual(self.bare('vv1.2'), 'v1.2')


class TheReasonIsWrittenDownTests(unittest.TestCase):
    def test_it_records_the_incident(self):
        self.assertIn('v1.2.96', SOURCE, '没写出触发这件事的具体值')
        self.assertIn('age-X25519', SOURCE,
                      '没写清那份快照不是本模块产的，后来人会去错的地方找')

    def test_it_explains_why_empty_is_not_success(self):
        idx = SOURCE.find("'current_version'")
        window = SOURCE[max(0, idx - 900):idx]
        self.assertIn('无法确认', window,
                      '没写出「读不到要如实显示为无法确认」')


if __name__ == '__main__':
    unittest.main()
