"""Generate only the selected portable contract; retain Manager extensions."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('qoder_contract_generator', ROOT / 'tools/build_config_contracts.py')
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


class QoderConfigGenerationTests(unittest.TestCase):
    def test_inventory_audits_the_serving_merged_qtg_tree(self):
        spec = importlib.util.spec_from_file_location('qoder_contract_inventory', ROOT / 'tools/audit_config_inventory.py')
        inventory = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inventory)
        self.assertEqual(inventory.QTG_DIR, ROOT / 'manager/server/qtgcore')
        result = inventory.audit('qtg')
        self.assertTrue(result['ok'], result)
        self.assertGreater(result['code_reads'], 0)
        self.assertNotIn('QTG_QODER2_TIMEZONE', result['missing_in_schema'])
        self.assertNotIn('QTG_QODER2_TIMEZONE', result['dead_in_schema'])

    def test_selected_qtg_generation_is_reproducible_and_does_not_touch_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'manager/server/qtgcore'
            folder.mkdir(parents=True)
            (root / 'tools').mkdir()
            (root / 'tools/config_contract_core.py').write_bytes((ROOT / 'tools/config_contract_core.py').read_bytes())
            source = ROOT / 'manager/server/qtgcore'
            for name in ('config.py',):
                (folder / name).write_bytes((source / name).read_bytes())
            with patch.object(GENERATOR, 'ROOT', root):
                count = GENERATOR.generate('qtg', folder)
            import json
            schema = json.loads((folder / 'config-schema.json').read_bytes())
            self.assertEqual(schema['settings']['QTG_QODER2_TIMEZONE']['format'], 'iana_timezone')
            self.assertEqual(count, len(schema['settings']))
            self.assertEqual((folder / 'configcontract.py').read_bytes(), (source / 'configcontract.py').read_bytes())

    def test_cli_requires_explicit_service_and_refuses_extended_manager(self):
        target = ROOT / 'manager/server/configcontract.py'
        schema = ROOT / 'manager/server/config-schema.json'
        before = (target.read_bytes(), schema.read_bytes())
        for args in ([], ['--service', 'manager']):
            result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tools/build_config_contracts.py'), *args],
                                    capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((target.read_bytes(), schema.read_bytes()), before)
        with self.assertRaisesRegex(ValueError, 'extended Manager'):
            GENERATOR.generate('manager', target.parent)
        self.assertEqual((target.read_bytes(), schema.read_bytes()), before)


if __name__ == '__main__':
    unittest.main()
