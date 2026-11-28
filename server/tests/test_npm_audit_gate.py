"""依赖审计豁免闸门：未豁免的高危必须红，有效豁免放行，过期豁免必须红。"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import npm_audit_gate as gate  # noqa: E402


def report_with(package='left-pad', severity='high', advisory=1234):
    return {
        'auditReportVersion': 2,
        'vulnerabilities': {
            package: {
                'name': package,
                'severity': severity,
                'via': [{'source': advisory, 'name': package, 'title': f'{package} 通告',
                         'severity': severity, 'url': f'https://example.invalid/{advisory}'}],
            },
        },
        'metadata': {'vulnerabilities': {'info': 0, 'low': 0, 'moderate': 0,
                                         'high': 1 if severity == 'high' else 0,
                                         'critical': 1 if severity == 'critical' else 0,
                                         'total': 1}},
    }


class ExemptionDecisionTests(unittest.TestCase):
    TODAY = datetime.date(2026, 11, 28)

    def test_unexempted_high_vulnerability_fails(self):
        result = gate.evaluate(report_with(), {'schema': 1, 'exemptions': []}, self.TODAY)
        self.assertEqual(result['findings'], 1)
        self.assertTrue(result['failures'], '未豁免的 high 必须判失败')
        self.assertIn('left-pad', ' '.join(result['failures']))

    def test_exempted_and_unexpired_passes(self):
        exemptions = {'schema': 1, 'exemptions': [{
            'package': 'left-pad', 'advisory': '1234', 'severity': 'high',
            'expires_on': '2026-12-31', 'mitigation': '只在内网离线脚本里调用'}]}
        result = gate.evaluate(report_with(), exemptions, self.TODAY)
        self.assertEqual(result['failures'], [])
        self.assertTrue(result['covered'], '放行时必须有可读的说明')

    def test_expired_exemption_fails(self):
        exemptions = {'schema': 1, 'exemptions': [{
            'package': 'left-pad', 'advisory': '*', 'severity': 'high',
            'expires_on': '2026-11-27', 'mitigation': '曾经缓解过'}]}
        result = gate.evaluate(report_with(), exemptions, self.TODAY)
        self.assertTrue(result['failures'], '过期豁免必须判失败')
        self.assertIn('到期', ' '.join(result['failures']))

    def test_exemption_without_expiry_is_rejected(self):
        exemptions = {'schema': 1, 'exemptions': [{
            'package': 'left-pad', 'advisory': '*', 'severity': 'high',
            'mitigation': '没有到期日'}]}
        result = gate.evaluate(report_with(), exemptions, self.TODAY)
        self.assertTrue(result['failures'])
        self.assertIn('expires_on', ' '.join(result['failures']))

    def test_low_and_moderate_do_not_block(self):
        result = gate.evaluate(report_with(severity='moderate'),
                               {'schema': 1, 'exemptions': []}, self.TODAY)
        self.assertEqual(result['findings'], 0)
        self.assertEqual(result['failures'], [])


class ReportMustBeParseableTests(unittest.TestCase):
    """一次网络失败 / 半截文件不能被当成「没有漏洞」。"""

    def test_missing_metadata_is_not_an_empty_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'audit.json'
            path.write_text(json.dumps({'vulnerabilities': {}}), encoding='utf-8')
            with self.assertRaises(gate.AuditReportError):
                gate.load_report(path)

    def test_truncated_file_is_not_an_empty_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'audit.json'
            path.write_text('{"metadata": {"vulner', encoding='utf-8')
            with self.assertRaises(gate.AuditReportError):
                gate.load_report(path)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / 'audit.json'
            report.write_text(json.dumps(report_with()), encoding='utf-8')
            clean = root / 'ok.json'
            clean.write_text(json.dumps({'schema': 1, 'exemptions': [{
                'package': 'left-pad', 'advisory': '1234', 'severity': 'high',
                'expires_on': '2099-01-01', 'mitigation': '测试用'}]}), encoding='utf-8')
            empty = root / 'empty.json'
            empty.write_text(json.dumps({'schema': 1, 'exemptions': []}), encoding='utf-8')
            script = str(ROOT / 'tools' / 'npm_audit_gate.py')
            env = {'PYTHONIOENCODING': 'utf-8', 'PATH': ''}
            failing = subprocess.run([sys.executable, script, '--report', str(report),
                                      '--exemptions', str(empty), '--today', '2026-11-28'],
                                     capture_output=True, text=True, encoding='utf-8',
                                     errors='replace', env=env)
            self.assertEqual(failing.returncode, 1, failing.stdout + failing.stderr)
            passing = subprocess.run([sys.executable, script, '--report', str(report),
                                      '--exemptions', str(clean), '--today', '2026-11-28'],
                                     capture_output=True, text=True, encoding='utf-8',
                                     errors='replace', env=env)
            self.assertEqual(passing.returncode, 0, passing.stdout + passing.stderr)
            self.assertIn('已签豁免', passing.stdout)


def gate_fresh_exemptions() -> dict:
    """读仓库里真实的豁免清单，跟判定者看到的一样。"""
    return json.loads((ROOT / 'tools' / 'npm-audit-exemptions.json').read_text(encoding='utf-8'))


BASELINE = ROOT / 'web' / 'audit-baseline.json'


class CommittedBaselineTests(unittest.TestCase):
    """必检 job 里能看到的真实依赖审计，只能是这份随仓库走的快照。

    `manager` 与 `gateway` 都跑在 `unshare --net` 里（`quality.yml:47`、`:149`），
    没有 registry 可连，`npm audit` 在必检 job 里跑不起来。于是取「快照 + 刷新
    期限」：快照过期就红，逼人刷新，而不是让它悄悄变旧、看起来一直在守。
    """

    def test_baseline_is_readable_and_clean(self):
        report, generated = gate.load_baseline(BASELINE)
        self.assertIsInstance(generated, datetime.date)
        result = gate.evaluate(report, gate_fresh_exemptions(), datetime.date.today())
        self.assertEqual(result['failures'], [], '快照里出现了没有有效豁免的 high/critical')

    def test_baseline_must_be_refreshed_within_the_deadline(self):
        _, generated = gate.load_baseline(BASELINE)
        problem = gate.baseline_staleness(generated, datetime.date.today())
        self.assertEqual(problem, '', problem or '基线新鲜')

    def test_stale_baseline_is_red(self):
        today = datetime.date(2026, 11, 28)
        self.assertTrue(gate.baseline_staleness(datetime.date(2026, 10, 1), today))
        self.assertEqual(gate.baseline_staleness(datetime.date(2026, 11, 20), today), '')

    def test_future_baseline_date_is_red(self):
        today = datetime.date(2026, 11, 28)
        self.assertTrue(gate.baseline_staleness(datetime.date(2026, 12, 1), today))

    def test_baseline_without_report_is_not_an_empty_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'baseline.json'
            path.write_text(json.dumps({'schema': 1, 'generated_on': '2026-11-28'}),
                            encoding='utf-8')
            with self.assertRaises(gate.AuditReportError):
                gate.load_baseline(path)

    def test_cli_rejects_a_stale_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / 'baseline.json'
            document = json.loads(BASELINE.read_text(encoding='utf-8'))
            document['generated_on'] = '2026-01-01'
            baseline.write_text(json.dumps(document), encoding='utf-8')
            exemptions = root / 'exemptions.json'
            exemptions.write_text(json.dumps({'schema': 1, 'exemptions': []}), encoding='utf-8')
            script = str(ROOT / 'tools' / 'npm_audit_gate.py')
            proc = subprocess.run(
                [sys.executable, script, '--baseline', str(baseline),
                 '--exemptions', str(exemptions), '--today', '2026-11-28'],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                env={'PYTHONIOENCODING': 'utf-8', 'PATH': ''})
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn('没刷新', proc.stderr)


if __name__ == '__main__':
    unittest.main()
