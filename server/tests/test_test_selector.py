"""`select_tests.py` 自身的守卫。

为什么要测这个脚本：它是「按改动挑测试」的入口，一旦它挑错了，开发者的本地
门禁就是**部分失效**的 —— 而且失效得没有声音。已经发生过一次：守卫清单里
写着 `test_writer_capabilities.py`，那个文件早就没了，`glob` 自然跳过，
清单看起来 5 条、实际只跑 4 条，几个月没人发现。

这里的断言只覆盖「会静默失效」的那几处，不测挑选规则的细枝末节。
"""
from __future__ import annotations

import unittest
from unittest import mock

from server.tests import select_tests


class GuardListTest(unittest.TestCase):
    def test_missing_guards_is_empty_for_this_checkout(self) -> None:
        # 清单里不该再有指向不存在文件的条目。真要加"理想守卫"，先在同一个提交里
        # 把文件建出来 —— 否则就等于没有。
        self.assertEqual(select_tests.missing_guards(), [])

    def test_missing_guards_detects_a_name_without_file(self) -> None:
        with mock.patch.object(select_tests, 'ALWAYS',
                               ('test_release_consistency.py', 'test_nope.py')):
            self.assertEqual(select_tests.missing_guards(), ['test_nope.py'])

    def test_every_existing_guard_is_selected_without_changes(self) -> None:
        picked = {path.name for path in select_tests.select(set())}
        for name in select_tests.ALWAYS:
            if (select_tests.HERE / name).is_file():
                self.assertIn(name, picked, name)

    def test_selection_has_no_duplicates(self) -> None:
        # 直接改到的测试文件同时也可能命中"文件名含模块名"规则；重复出现会让
        # pytest 把同一个文件跑两遍。
        target = select_tests.HERE / 'test_changelog.py'
        picked = select_tests.select({'changelog'}, [target])
        names = [path.name for path in picked]
        self.assertEqual(len(names), len(set(names)), names)


if __name__ == '__main__':
    unittest.main()
