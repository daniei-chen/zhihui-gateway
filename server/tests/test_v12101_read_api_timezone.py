"""v1.2.101：口径声明里的时区字段是有歧义的。

## 问题

`/api/read-api/version` 的注释写着：`timezone` 是**口径声明，避免调用方猜**。

但它返回的是 `time.strftime('%Z')` —— 在生产（`Asia/Shanghai`）上是 **`CST`**。

**`CST` 是一个三义缩写：**

| 解释 | 偏移 |
| --- | --- |
| China Standard Time | UTC+8 |
| Central Standard Time（北美） | UTC-6 |
| Cuba Standard Time | UTC-5 |

**一个需要猜的缩写，做不到「避免调用方猜」。**

后果不是崩溃，是**统计口径偏 14 小时**——而这类问题最难排查，正是这段注释自己
说要避免的那种。

## 修法

保留 `timezone` 不动（已发布的契约），**另加两个无歧义字段**：

| 字段 | 形如 | 用途 |
| --- | --- | --- |
| `utc_offset` | `+08:00` | 算「现在几点」 |
| `tz_name` | `Asia/Shanghai` | 算「历史上某天的边界」（有夏令时的时区必须靠名字） |

两个都要，因为用途不同：偏移量能算当下，但算不了历史日期边界。
"""
from __future__ import annotations

import re
import time
import unittest
from unittest import mock

from server.routers import read_api


class UtcOffsetTests(unittest.TestCase):
    def test_format_is_signed_hh_mm(self):
        value = read_api._utc_offset()
        self.assertRegex(value, r'^[+-]\d{2}:\d{2}$', value)

    def test_positive_offset_for_east_of_greenwich(self):
        real = time.localtime

        def fake(*_a, **_k):
            return type('T', (), {'tm_gmtoff': 8 * 3600})()

        with mock.patch.object(time, 'localtime', fake):
            self.assertEqual(read_api._utc_offset(), '+08:00')

    def test_negative_offset_for_west_of_greenwich(self):
        def fake(*_a, **_k):
            return type('T', (), {'tm_gmtoff': -6 * 3600})()

        with mock.patch.object(time, 'localtime', fake):
            self.assertEqual(read_api._utc_offset(), '-06:00')

    def test_half_hour_offset_is_preserved(self):
        r"""印度 +05:30、尼泊尔 +05:45 这类偏移不能被截成整点。"""
        def fake(*_a, **_k):
            return type('T', (), {'tm_gmtoff': 5 * 3600 + 30 * 60})()

        with mock.patch.object(time, 'localtime', fake):
            self.assertEqual(read_api._utc_offset(), '+05:30')

    def test_zero_offset(self):
        def fake(*_a, **_k):
            return type('T', (), {'tm_gmtoff': 0})()

        with mock.patch.object(time, 'localtime', fake):
            self.assertEqual(read_api._utc_offset(), '+00:00')

    def test_uses_gmtoff_not_the_static_timezone_attribute(self):
        r"""`time.timezone` 不含夏令时；用它在有 DST 的时区上会差一小时。

        **不能只查源码字符串** —— `time.timezone` 出现在文档里，正是为了解释
        「为什么不用它」。这里查的是实际行为。
        """

        def fake(*_a, **_k):
            return type('T', (), {'tm_gmtoff': 2 * 3600})()

        with mock.patch.object(time, 'localtime', fake):
            self.assertEqual(read_api._utc_offset(), '+02:00')
        self.assertIn('tm_gmtoff', read_api._utc_offset.__doc__ or '',
                      '文档要写明为什么用 gmtoff')


class TimezoneNameTests(unittest.TestCase):
    def test_returns_a_loadable_zone_name_or_none(self):
        value = read_api._tz_name()
        if value is None:
            self.skipTest('本机没有可用的 IANA 时区名')
        from zoneinfo import ZoneInfo
        ZoneInfo(value)      # 名字必须真的能加载

    def test_rejects_an_invalid_name(self):
        with mock.patch.dict('os.environ', {'TZ': 'Not/AZone'}):
            self.assertIsNone(read_api._tz_name())

    def test_falls_back_to_etc_localtime_symlink(self):
        """生产上 TZ 通常是空的，名字要从 /etc/localtime 的链接推出来。"""
        import os
        import sys
        from pathlib import Path
        if sys.platform != 'linux':
            self.skipTest('/etc/localtime 只在 Linux 上有意义')
        with mock.patch.dict(os.environ, {'TZ': ''}, clear=False):
            os.environ.pop('TZ', None)
            real_resolve = Path.resolve

            def fake_resolve(self, *a, **k):
                if str(self) == '/etc/localtime':
                    return Path('/usr/share/zoneinfo/Asia/Shanghai')
                return real_resolve(self, *a, **k)

            with mock.patch.object(Path, 'resolve', fake_resolve):
                self.assertEqual(read_api._tz_name(), 'Asia/Shanghai')


class TheContractCarriesBothTests(unittest.TestCase):
    def test_version_payload_declares_the_new_fields(self):
        import inspect
        source = inspect.getsource(read_api.read_api_version)
        self.assertIn("'utc_offset'", source)
        self.assertIn("'tz_name'", source)

    def test_timezone_field_is_kept_for_compatibility(self):
        import inspect
        source = inspect.getsource(read_api.read_api_version)
        self.assertIn("'timezone'", source,
                      '已发布字段不能删，只能加')

    def test_the_ambiguity_is_documented(self):
        import inspect
        source = inspect.getsource(read_api.read_api_version)
        self.assertIn('CST', source)
        self.assertIn('歧义', source,
                      '要写明为什么加这两个字段，否则后来人会当成冗余删除')

    def test_module_docstring_is_honest_about_it(self):
        """顶部说明原本写「避免调用方猜」，那个说法本身要修正。"""
        import inspect
        source = inspect.getsource(read_api)
        self.assertIn('CST', source)


if __name__ == '__main__':
    unittest.main()
