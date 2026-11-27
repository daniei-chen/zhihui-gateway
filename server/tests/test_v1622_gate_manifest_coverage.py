"""门禁必须真的加载磁盘上的测试文件。

## 为什么需要

2026-11-26 发现：`server/tests/` 下有 499 个测试文件，`tools/ci_regression.py`
的 `MODULES` 只列了 93 个。加载方式是 `loadTestsFromName(name)` —— **不在清单里
的文件永远不会执行**，而写下它们的人会以为「我加了测试」。

那次是因为改 `/api/stats/by-key` 才发现的：验证「合计与日序列对不对得上」的那个
文件正好在清单之外，断言从来没跑过。

## 本测试守什么

2026-11-27 已分批核对并纳入全部存量，原白名单已清空。这里守的是：

  * 磁盘上有、清单里没有、又不在存量白名单里 → 失败，并指出是哪个文件；
  * 顶层函数测试必须进入 pytest loader，不能被 unittest 零用例静默通过；
  * 清单不能重复，已纳入的文件不能重新成为遗漏例外。
"""
from __future__ import annotations

from pathlib import Path
import ast
import unittest

ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = ROOT / 'server' / 'tests'
GATE = ROOT / 'tools' / 'ci_regression.py'

#: 2026-11-27 全部存量已纳入；保留空集合以守住原来的单向收敛合同。
KNOWN_UNCOUNTED = frozenset()


def _modules_on_disk() -> set[str]:
    return {
        f'server.tests.{path.stem}'
        for path in TESTS_DIR.glob('test_*.py')
        if path.is_file()
    }


def _modules_in_gate() -> set[str]:
    text = GATE.read_text(encoding='utf-8')
    modules = set()
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id in ('MODULES', 'PYTEST_MODULES') for target in node.targets):
            values = ast.literal_eval(node.value)
            assert not modules.intersection(values), 'a test module must not run in both loaders'
            assert len(values) == len(set(values)), 'duplicate gate module'
            modules.update(values)
    return modules


class GateManifestCoverageTests(unittest.TestCase):
    def test_disk_scan_finds_the_modules(self):
        self.assertGreater(len(_modules_on_disk()), 400)
        self.assertGreater(len(_modules_in_gate()), 50)

    def test_no_new_test_module_is_left_out_of_the_gate(self):
        uncounted = _modules_on_disk() - _modules_in_gate()
        new_ones = sorted(uncounted - KNOWN_UNCOUNTED)
        self.assertEqual(
            new_ones, [],
            '这些测试文件不在 tools/ci_regression.py 的 MODULES 里，永远不会被执行：\n  '
            + '\n  '.join(new_ones),
        )

    def test_the_known_list_only_shrinks(self):
        counted = _modules_in_gate()
        stale = sorted(KNOWN_UNCOUNTED & counted)
        self.assertEqual(
            stale, [],
            '这些文件已经进清单了，请从 KNOWN_UNCOUNTED 里删掉：\n  ' + '\n  '.join(stale),
        )

    def test_known_list_entries_actually_exist(self):
        missing = sorted(name for name in KNOWN_UNCOUNTED if not (TESTS_DIR / f'{name.split(".")[-1]}.py').is_file())
        self.assertEqual(missing, [], '白名单里有已经不存在的文件，删掉它们：\n  ' + '\n  '.join(missing))

    def test_function_tests_are_assigned_to_the_pytest_loader(self):
        from tools.ci_regression import MODULES, PYTEST_MODULES
        misplaced = []
        for name in _modules_in_gate():
            path = TESTS_DIR / (name.rsplit('.', 1)[-1] + '.py')
            functions = [node for node in ast.parse(path.read_text(encoding='utf-8')).body
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and node.name.startswith('test_')]
            if functions and name in MODULES:
                misplaced.append(name)
            if name in PYTEST_MODULES:
                self.assertTrue(functions, f'{name}: pytest module must expose test functions')
        self.assertEqual(misplaced, [], 'unittest does not execute top-level pytest functions')


if __name__ == '__main__':
    unittest.main()
