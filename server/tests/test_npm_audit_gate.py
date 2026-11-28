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


if __name__ == '__main__':
    unittest.main()
