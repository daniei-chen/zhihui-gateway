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

        writer = json.loads(
            (ROOT / 'server/writer-capabilities.json').read_text(encoding='utf-8')
        )
        self.assertEqual(writer['release_id'], f'v{version}')
        # `version` 是 **writer 协议版本**（整数 1），不是产品版本 —— 产品版本只在
        # `release_id`。这两者曾被写成同一个值（`"version": "1.5.27"`），而启动校验
        # （`ops/writer-guard/state_guard.py`）与发布前置检查
        # （`deploy/preflight-gate.py`）都要求它必须是整数 1：照仓库内容发布，
        # 服务会以「unsupported writer descriptor」被拒启动。
        # 所以这里显式钉住类型与取值，而不是只查 release_id。
        self.assertIsInstance(writer['version'], int,
                              'writer 描述的 version 必须是整数协议版本，不是产品版本字符串')
        self.assertEqual(writer['version'], 1)
        self.assertEqual(writer['schema'], 'zhihui.writer-release')


if __name__ == '__main__':
    unittest.main()
