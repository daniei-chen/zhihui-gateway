"""skip 下限判据：整模块一条都没跑必须红，白名单例外要签字。

为什么这个文件存在：`unittest.TestResult.wasSuccessful()` 对 skip 返回 True，
所以「一个模块的用例全被 skip 掉」在门禁看来和「全跑通」一模一样。2026-11-28
之前两条赛道（unittest / pytest）还是两套标准——pytest 要 `passed > 0`，
unittest 只看 `wasSuccessful()`。这里把判据的四种形态各钉一条用例：

* 整模块全 skip → 红
* 一条用例都没加载出来（import 失败 / 清单名字写错）→ 红，且报错要说清是哪种
* 单条 skip → 绿（那是用例自己的事，不该连坐整个模块）
* 白名单里写了 reason → 放行，并打印一行可读的签字说明

另外两条守「计数来自真实执行」：`_CountingResult` 必须真的看到用例进过
`startTest` / `addSkip`，否则「tests 数对了」可能来自一个根本没跑的路径。
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ci_regression as gate  # noqa: E402


class _SkipEverything(unittest.TestCase):
    """夹具：两条用例，一条真跑、一条 skip。"""

    def test_passes(self):
        self.assertTrue(True)

    @unittest.skip('夹具：故意 skip')
    def test_skipped(self):
        self.fail('被 skip 的用例不该执行到断言')


class _AllSkipped(unittest.TestCase):
    @unittest.skip('夹具：整模块全 skip')
    def test_only(self):
        self.fail('整模块全 skip，这条不该跑')


class ModuleAttributionTests(unittest.TestCase):
    MODULES = ('server.tests.test_alpha', 'server.tests.test_alpha_extra')

    def test_dotted_id_maps_to_registered_module(self):
        self.assertEqual(
            gate._module_of('server.tests.test_alpha.Class.test_x', self.MODULES),
            'server.tests.test_alpha')

    def test_longer_module_name_wins(self):
        self.assertEqual(
            gate._module_of('server.tests.test_alpha_extra.Class.test_x', self.MODULES),
            'server.tests.test_alpha_extra')

    def test_unknown_id_falls_back_to_its_own_module(self):
        self.assertEqual(
            gate._module_of('server.tests.test_something_else.test_x', self.MODULES),
            'server.tests.test_something_else')


class SkipFloorTests(unittest.TestCase):
    MODULES = ('server.tests.test_alpha',)

    def _floor(self, per_module, allowlist):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'skip_allowlist.json'
            path.write_text(json.dumps(allowlist), encoding='utf-8')
            with mock.patch.object(gate, 'SKIP_ALLOWLIST', path):
                return gate._skip_floor(per_module, self.MODULES, track='unittest')

    def test_fully_skipped_module_is_red(self):
        violations = self._floor({'server.tests.test_alpha': {'tests': 3, 'skipped': 3}}, {})
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]['module'], 'server.tests.test_alpha')

    def test_zero_loaded_cases_is_red(self):
        violations = self._floor({}, {})
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]['tests'], 0)

    def test_single_skip_does_not_condemn_the_module(self):
        self.assertEqual(self._floor({'server.tests.test_alpha': {'tests': 5, 'skipped': 1}}, []),
                         [])

    def test_allowlisted_module_is_waived_with_a_visible_line(self):
        allowlist = {'server.tests.test_alpha': {'reason': '仅 Linux 可用', 'platform': 'gateway'}}
        stdout = io.StringIO()
        with mock.patch('sys.stdout', stdout):
            violations = self._floor({'server.tests.test_alpha': {'tests': 2, 'skipped': 2}},
                                     allowlist)
        self.assertEqual(violations, [])
        self.assertIn('已签名放行', stdout.getvalue())
        self.assertIn('仅 Linux 可用', stdout.getvalue())

    def test_allowlist_entry_without_reason_does_not_count(self):
        violations = self._floor({'server.tests.test_alpha': {'tests': 2, 'skipped': 2}},
                                 {'server.tests.test_alpha': {'platform': 'gateway'}})
        self.assertEqual(len(violations), 1)

    def test_allowlist_must_be_valid_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'skip_allowlist.json'
            path.write_text('{', encoding='utf-8')
            with mock.patch.object(gate, 'SKIP_ALLOWLIST', path):
                with self.assertRaises(SystemExit):
                    gate._skip_allowlist()


class CountingComesFromRealExecutionTests(unittest.TestCase):
    """计数必须来自真实执行，不能来自一个「全都跳过但数字漂亮」的路径。"""

    def _run(self, case):
        result = unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0,
            resultclass=lambda *a, **k: gate._CountingResult(
                *a, modules=('server.tests.test_ci_skip_floor',), **k))
        return result.run(unittest.TestSuite([case]))

    def test_counts_come_from_executed_cases(self):
        result = self._run(unittest.defaultTestLoader.loadTestsFromTestCase(_SkipEverything))
        row = result.per_module['server.tests.test_ci_skip_floor']
        self.assertEqual(row, {'tests': 2, 'skipped': 1})
        self.assertTrue(result.wasSuccessful())

    def test_was_successful_is_not_enough_by_itself(self):
        result = self._run(unittest.defaultTestLoader.loadTestsFromTestCase(_AllSkipped))
        self.assertTrue(result.wasSuccessful(), 'wasSuccessful() 对全 skip 返回 True')
        row = result.per_module['server.tests.test_ci_skip_floor']
        self.assertEqual(row, {'tests': 1, 'skipped': 1})
        self.assertLessEqual(row['tests'] - row['skipped'], 0)


if __name__ == '__main__':
    unittest.main()
