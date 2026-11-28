"""A missing release check must fail the command and its recorded summary."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml

from tools import ci_regression


class RequiredGateTests(unittest.TestCase):
    def run_gate(self, flags, frontend, static, historical=None, modules=()):
        cwd = Path.cwd()
        backend = unittest.TestResult()
        backend.testsRun = 1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            try:
                with (mock.patch.object(ci_regression, 'ROOT', root),
                      mock.patch.object(ci_regression, 'MODULES', modules),
                      mock.patch.object(ci_regression, 'frontend_suite', return_value=frontend),
                      mock.patch.object(ci_regression, 'static_suite', return_value=static),
                      mock.patch.object(ci_regression, 'pytest_suite',
                                        return_value={'ok': True, 'required': True} if historical is None else historical),
                      mock.patch.object(unittest, 'TextTestRunner') as runner,
                      mock.patch.object(sys, 'path', list(sys.path)),
                      mock.patch.dict(os.environ, {'ZH_CI_NET_ISOLATED': '0'}),
                      mock.patch('server.budgetowner.close'),
                      mock.patch('server.db._close_connection_locked'),
                      contextlib.redirect_stderr(io.StringIO())):
                    runner.return_value.run.return_value = backend
                    status = ci_regression.main(flags)
                    summary = json.loads((root / 'ci-validation-summary.json').read_text())
                return status, summary
            finally:
                os.chdir(cwd)

    def test_missing_required_frontend_fails_and_records_reason(self):
        status, summary = self.run_gate(
            ['--require-frontend'], {'skipped': True, 'reason': 'npm missing'}, {'ok': True})
        self.assertEqual(status, 1)
        self.assertFalse(summary['frontend']['ok'])
        self.assertEqual(summary['frontend']['reason'], 'npm missing')

    def test_missing_required_static_fails_and_records_reason(self):
        status, summary = self.run_gate(
            ['--require-static'], {'skipped': True, 'reason': 'separate frontend job'},
            {'skipped': True, 'reason': 'ruff not installed'})
        self.assertEqual(status, 1)
        self.assertFalse(summary['static']['ok'])
        self.assertTrue(summary['static']['required'])

    def test_local_optional_checks_remain_explicitly_skipped(self):
        status, summary = self.run_gate([], {'skipped': True}, {'skipped': True})
        self.assertEqual(status, 0)
        self.assertTrue(summary['frontend']['skipped'])
        self.assertFalse(summary['frontend']['required'])

    def test_required_checks_must_both_succeed(self):
        for frontend, static in (({'ok': False}, {'ok': True}),
                                 ({'ok': True}, {'ok': False}),
                                 ({}, {'ok': True})):
            with self.subTest(frontend=frontend, static=static):
                status, _ = self.run_gate(
                    ['--require-frontend', '--require-static'], frontend, static)
                self.assertEqual(status, 1)
        status, summary = self.run_gate(
            ['--require-frontend', '--require-static'], {'ok': True}, {'ok': True})
        self.assertEqual(status, 0)
        self.assertTrue(summary['static']['required'])

    def test_historical_pytest_is_required_even_for_local_runs(self):
        for result in ({'ok': False, 'required': True}, {'skipped': True}, {}):
            with self.subTest(result=result):
                status, summary = self.run_gate([], {'ok': True}, {'ok': True}, result)
                self.assertEqual(status, 1)
                self.assertEqual(summary['historical_pytest'], result)

    def test_a_named_module_with_no_unittest_cases_fails_discovery(self):
        name = 'server.tests.synthetic_provider'
        status, summary = self.run_gate([], {'ok': True}, {'ok': True}, modules=(name,))
        self.assertEqual(status, 1)
        self.assertEqual(summary['empty_modules'], [name])


ROOT = Path(__file__).resolve().parents[2]
QUALITY_WORKFLOW = ROOT / '.github' / 'workflows' / 'quality.yml'
REQUIRED_JOBS = ('manager', 'frontend', 'gateway')
ZERO_VERIFICATION = 'skip 计为通过 = 零验合并'


def _workflow_document() -> dict:
    document = yaml.safe_load(QUALITY_WORKFLOW.read_text(encoding='utf-8'))
    if not isinstance(document, dict):
        raise AssertionError(f'{QUALITY_WORKFLOW} 不是一个 mapping')
    return document


def _triggers(document: dict) -> dict:
    """PyYAML 按 YAML 1.1 把裸 `on:` 解析成 True 键，两种都认。"""
    triggers = document.get('on', document.get(True))
    if not isinstance(triggers, dict):
        raise AssertionError(f'quality.yml 的 on: 不是 mapping：{triggers!r}')
    return triggers


class RequiredWorkflowShapeTests(unittest.TestCase):
    """必检 job 的触发形状。任何「让它不跑」的写法都等于零验合并。

    `manager` / `frontend` / `gateway` 是 main 规则集 28284164 的三个 required
    context。PR 上只要它们因为路径过滤或 job 级 `if:` 而不被排队，GitHub 就报
    "skipped"，规则集不把 skipped 当作满足，PR 卡住；而如果哪天有人把「不跑」
    调成「通过」，那就是**零验合并** —— 这个文件守这两头。
    """

    def test_pull_request_trigger_carries_no_path_filter(self):
        pull_request = _triggers(_workflow_document()).get('pull_request')
        keys = set(pull_request) if isinstance(pull_request, dict) else set()
        forbidden = sorted(keys & {'paths', 'paths-ignore', 'branches', 'branches-ignore'})
        self.assertEqual(
            forbidden, [],
            f'quality.yml 的 pull_request 上出现 {forbidden}：改动不命中时三个必检 job 根本'
            f'不排队，PR 会停在 "skipped"（{ZERO_VERIFICATION}）')

    def test_required_jobs_exist_and_have_no_job_level_condition(self):
        jobs = _workflow_document().get('jobs')
        self.assertIsInstance(jobs, dict)
        for name in REQUIRED_JOBS:
            with self.subTest(job=name):
                self.assertIn(name, jobs, f'{name} 是 main 规则集的必检 job，不能改名或删掉')
                self.assertNotIn(
                    'if', jobs[name],
                    f'{name} 出现了 job 级 if: —— 条件不成立时 GitHub 报 "skipped"，'
                    f'规则集不认它为通过；若有人把 skipped 当成通过，就是零验合并')
                self.assertNotIn(
                    'continue-on-error', jobs[name],
                    f'{name} 不能 continue-on-error：失败被吞掉的必检等于没有必检'
                    f'（{ZERO_VERIFICATION}）')

    def test_required_jobs_are_the_only_required_contexts(self):
        """规则集的 required 就是这三项；workflow 里多出来的 job 不该被当成必检。"""
        jobs = set(_workflow_document().get('jobs') or {})
        self.assertTrue(set(REQUIRED_JOBS) <= jobs)
        expected_extras = jobs - set(REQUIRED_JOBS)
        self.assertEqual(
            expected_extras, set(),
            f'quality.yml 出现了 {sorted(expected_extras)}：新增 job 若不在规则集里，'
            f'它红了也不会挡合并——要么把它加进规则集，要么别把它当门禁')


if __name__ == '__main__':
    unittest.main()
