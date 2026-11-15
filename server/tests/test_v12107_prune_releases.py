"""v1.2.107：按数量上限清理历史 release 目录。

## 为什么现在做

2026-11-15 实测 `releases/` 下有 **146 个目录、4.9 GB**，根分区到 **94%**。
其中绝大多数是几个月前的一次性候选版本，永远不会再被启动 —— 而每次部署
还会再留一个新目录。

这一版加了清理工具，**并且已经用它回收了 4,640 MB**（94% → 86%）。

## 两处曾经写错、且都会造成真实破坏的地方

### 一、忘了排序

第一版只做 `>> "$SORTED"` 追加，读回来时按 glob 的字母序处理。

于是 `v0.0.10+stable` 排在 `v1.2.105` 前面，被当成「最新」保留 ——
**而真正的新版本全进了删除清单**。

dry-run 打印的保留清单是 `v0.0.x` 那批，才发现的。

### 二、版本号按字符串比较

`v1.2.100` 的字符串排在 `v1.2.99` **之前**，直接比较会删掉最新版。

所以先把版本补零成 `A.BBB.CCC` 再比。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 排序真的发生 | 旧版本被当最新保留，新版本被删 |
| 版本号补零比较 | `1.2.100` 被误判为旧 |
| 带 `+` 后缀的名字被正确解析 | 候选目录被当成最大版本 |
| 默认不动手 | 一次误跑删掉全部历史 |
| current 永不入选 | 删掉正在运行的版本 |
| 在用目录永不入选 | 删掉活跃进程的工作目录 |
| 删除前有自检 | 上面两条的兜底 |
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "prune-releases.sh"


class _Source:
    text = SCRIPT.read_text(encoding="utf-8") if SCRIPT.is_file() else ""


class TheSortActuallyHappensTests(unittest.TestCase):
    def test_sort_is_invoked(self):
        """第一版就是漏了这一行，导致旧版本被当最新保留。"""
        self.assertIn('sort -r "$SORTED"', _Source.text)

    def test_sort_happens_before_the_keep_loop(self):
        text = _Source.text
        sort_at = text.index('sort -r "$SORTED"')
        keep_at = text.index("KEPT_N=0")
        self.assertLess(sort_at, keep_at, '排序必须在挑选保留项之前')


class VersionComparisonIsNumericTests(unittest.TestCase):
    def test_regex_accepts_a_build_suffix(self):
        """`v0.0.10+stable.20261107.2` 这类名字必须解析出版本号，
        否则它会被当成「不认识」而拿到极端排序值。"""
        # 直接找那一行，别用嵌套转义去拆 —— 第一次就是这么写错的。
        line = next((ln for ln in _Source.text.splitlines()
                     if '=~' in ln and 'BASH_REMATCH' not in ln
                     and ln.strip().startswith('if [[')), None)
        self.assertIsNotNone(line, '没找到版本解析那一行')
        self.assertIn(r'(\+.*)?', line,
                      '正则要吃掉 + 后面的构建标记')
        self.assertIn(r'([0-9]+)', line, '必须提取数字分组才能补零比较')

    def test_versions_are_zero_padded_for_lexical_compare(self):
        self.assertIn("%03d.%03d.%03d", _Source.text,
                      '必须补零，否则 1.2.100 会排在 1.2.99 前面')

    def test_unrecognised_names_sink(self):
        """不认识的目录名不能拿到最大值 —— 那会把它顶成「最新」。"""
        m = re.search(r"else\s*\n\s*printf '([0-9.]+)'", _Source.text)
        self.assertIsNotNone(m, '没找到兜底返回值')
        self.assertEqual(m.group(1), "000.000.000",
                         '兜底必须是下沉，不是顶置')


class CurrentIsNeverACandidateTests(unittest.TestCase):
    def test_current_symlink_is_read(self):
        self.assertIn('readlink -f "$ROOT/current"', _Source.text)

    def test_current_is_pinned(self):
        self.assertIn('PIN   current', _Source.text,
                      'current 要单独标记，不能进候选')

    def test_current_is_checked_again_before_deleting(self):
        """删之前再查一次 —— 删目录不可逆，多查一遍值得。"""
        tail = _Source.text[_Source.text.index('开始删除'):]
        self.assertIn('CURRENT', tail)
        self.assertIn('跳过（是 current）', tail)


class InUseDirectoriesArePinnedTests(unittest.TestCase):
    def test_busy_scan_happens_once(self):
        """每个目录各扫一遍 /proc 是 O(N×M)，实测两分钟跑不完。"""
        self.assertIn('collect_busy', _Source.text)
        self.assertIn('is_busy', _Source.text)

    def test_busy_list_is_built_from_proc(self):
        self.assertIn('/proc/[0-9]*', _Source.text)
        self.assertIn('readlink "$pid/cwd"', _Source.text)

    def test_busy_directories_are_pinned(self):
        self.assertIn('PIN   in-use', _Source.text)


class DefaultIsDryRunTests(unittest.TestCase):
    def test_apply_defaults_to_zero(self):
        # 用 findall 而不是 assertRegex：后者的 ^ 默认只锚在整段文本开头，
        # 不加 MULTILINE 就永远匹配不到第 60 行那个赋值。
        self.assertIn('APPLY=0', _Source.text.splitlines(),
                      'APPLY 必须默认 0，否则一次误跑就删光了')
        assigns = [ln for ln in _Source.text.splitlines()
                   if ln.strip().startswith('APPLY=')]
        self.assertEqual(assigns[0].strip(), 'APPLY=0',
                         '第一条 APPLY 赋值必须是 0')


    def test_dry_run_exits_before_deleting(self):
        text = _Source.text
        dry = text.index('dry-run：未删除任何东西')
        first_rm = text.index('rm -rf --')
        self.assertLess(dry, first_rm, 'dry-run 的退出必须早于任何删除')

    def test_apply_requires_an_explicit_flag(self):
        self.assertIn('--apply) APPLY=1', _Source.text)


class SelfCheckTests(unittest.TestCase):
    def test_drop_list_is_audited_against_pins(self):
        self.assertIn('自检失败', _Source.text)

    def test_self_check_failure_aborts_everything(self):
        self.assertIn('自检未通过，拒绝执行任何删除', _Source.text)

    def test_keep_count_is_validated(self):
        self.assertIn('--keep 必须是正整数', _Source.text)
        self.assertIn('--keep 至少为 1', _Source.text)


class TheTwoMistakesAreRecordedTests(unittest.TestCase):
    def test_header_names_both_failure_modes(self):
        head = _Source.text[:3000]
        self.assertIn('忘了排序', head)
        self.assertIn('字符串序', head)

    def test_header_says_what_the_damage_would_have_been(self):
        head = _Source.text[:3000]
        self.assertIn('新版本全进了删除清单', head.replace('**', ''))


if __name__ == '__main__':
    unittest.main()
