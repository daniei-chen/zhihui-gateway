"""Release metadata must describe one product version everywhere users see it."""
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ReleaseConsistencyTests(unittest.TestCase):
    def test_product_version_is_consistent_across_release_surfaces(self):
        version = (ROOT / 'VERSION').read_text(encoding='ascii').strip()
        self.assertRegex(version, r'^\d+\.\d+\.\d+$')

        product = json.loads((ROOT / 'server/product-release.json').read_text(encoding='utf-8'))
        self.assertEqual(product['version'], version)
        self.assertEqual(product['release_channel'], 'github-actions')

        frontend = (ROOT / 'web/lib/product.ts').read_text(encoding='utf-8')
        self.assertRegex(
            frontend,
            rf"PRODUCT_VERSION\s*=\s*['\"]{re.escape(version)}['\"]",
        )

        changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
        server_changelog = (ROOT / 'server/CHANGELOG.md').read_text(encoding='utf-8')
        self.assertEqual(changelog, server_changelog)
        self.assertRegex(changelog, rf'(?m)^## \[{re.escape(version)}\]')

    def test_provenance_cannot_remain_an_old_candidate(self):
        version = (ROOT / 'VERSION').read_text(encoding='ascii').strip()
        provenance = json.loads(
            (ROOT / 'server/release-provenance.json').read_text(encoding='utf-8')
        )
        self.assertEqual(provenance['version'], version)
        self.assertEqual(provenance['release_id'], f'v{version}')
        self.assertFalse(provenance['candidate_only'])
        self.assertEqual(provenance['source_kind'], 'github-actions-release')


if __name__ == '__main__':
    unittest.main()
