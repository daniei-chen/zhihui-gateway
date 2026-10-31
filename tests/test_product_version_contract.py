import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('product_check', Path(__file__).resolve().parents[1] / 'tools/check_product.py')
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


class ProductVersionContractTests(unittest.TestCase):
    def test_exact_product_and_deployment_batch_preserve_own_history(self):
        notes = '## [0.0.3] - now\n- useful note\n## [0.0.2] - before\n'
        for marker in ('v0.0.3', 'v0.0.3+steward.20261031.1'):
            subject.version_contract('0.0.3', marker, notes)

    def test_drift_malformed_suffix_and_inherited_history_are_not_accepted(self):
        notes = '## [0.0.3]\n## [0.0.2]\n'
        for marker in ('v0.0.2', 'v0.0.3+', 'v0.0.3+../outside', 'v0.0.3-rc1', 'v0.0.3+UPPER'):
            with self.subTest(marker=marker), self.assertRaises(AssertionError):
                subject.version_contract('0.0.3', marker, notes)
        for invalid in ('## [0.0.2]\n## [0.0.3]', '## [0.0.3]\n## [0.0.3]',
                        '## [0.0.3]\n## [v10.7.22]', '## [0.0.3]\n## [0.0.1]', ''):
            with self.subTest(history=invalid), self.assertRaises(AssertionError):
                subject.version_contract('0.0.3', 'v0.0.3', invalid)


if __name__ == '__main__':
    unittest.main()
