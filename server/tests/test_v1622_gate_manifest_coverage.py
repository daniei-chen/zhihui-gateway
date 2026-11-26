"""门禁必须真的加载磁盘上的测试文件。

## 为什么需要

2026-11-26 发现：`server/tests/` 下有 499 个测试文件，`tools/ci_regression.py`
的 `MODULES` 只列了 93 个。加载方式是 `loadTestsFromName(name)` —— **不在清单里
的文件永远不会执行**，而写下它们的人会以为「我加了测试」。

那次是因为改 `/api/stats/by-key` 才发现的：验证「合计与日序列对不对得上」的那个
文件正好在清单之外，断言从来没跑过。

## 本测试守什么

不要求立刻把 406 个存量全部纳入 —— 那是一次改 400 多个模块的改动，失败原因无法
逐个定位，属于规范里说的「无法审阅的规模」。这里守的是**不许再新增**：

  * 磁盘上有、清单里没有、又不在存量白名单里 → 失败，并指出是哪个文件；
  * 白名单只许减少：某个文件被纳入清单后，从白名单里删掉它是**进步**，
    而把它加回白名单是**退步**（`KNOWN_UNCOUNTED` 里不应出现已在清单的文件）。
"""
from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = ROOT / 'server' / 'tests'
GATE = ROOT / 'tools' / 'ci_regression.py'

#: 2026-11-26 的存量：这些测试文件还没进门禁，需要逐个收敛。
#: **只许删，不许加。** 每纳入一个就从这里划掉一个。
KNOWN_UNCOUNTED = frozenset({
    'server.tests.test_audit_20261029_security',
    'server.tests.test_budget_batch_bounds',
    'server.tests.test_command_palette',
    'server.tests.test_doubaowork_platform',
    'server.tests.test_issue39_estimate_effort',
    'server.tests.test_issue69_cache_tokens',
    'server.tests.test_platform_integration_guards',
    'server.tests.test_platform_policy',
    'server.tests.test_preauth_body',
    'server.tests.test_qoder_checkin_evidence',
    'server.tests.test_refresh_all_tokens',
    'server.tests.test_refresh_plan',
    'server.tests.test_section_nav',
    'server.tests.test_settings_contract',
    'server.tests.test_speed_boundaries',
    'server.tests.test_steward',
    'server.tests.test_unknown_archive',
    'server.tests.test_v12100_trae_native',
    'server.tests.test_v12137_snapshot_gauge_always_emitted',
    'server.tests.test_v12156_stale_transactions',
    'server.tests.test_v12160_context_metric',
    'server.tests.test_v1276_platform_timeout',
    'server.tests.test_v1279_route_authorization',
    'server.tests.test_web_polling_visibility',
    'server.tests.test_workbench_registration',
})


def _modules_on_disk() -> set[str]:
    return {
        f'server.tests.{path.stem}'
        for path in TESTS_DIR.glob('test_*.py')
        if path.is_file()
    }


def _modules_in_gate() -> set[str]:
    text = GATE.read_text(encoding='utf-8')
    start = text.index('MODULES = (')
    end = text.index('\n)', start)
    return set(re.findall(r'"(server\.tests\.[^"]+)"', text[start:end]))


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


if __name__ == '__main__':
    unittest.main()
