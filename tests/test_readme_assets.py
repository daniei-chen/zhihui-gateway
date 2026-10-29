"""README visuals must stay portable, accessible and source-backed."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('render_readme_assets', ROOT / 'tools/render_readme_assets.py')
artwork = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artwork)


class ReadmeAssetsTests(unittest.TestCase):
    def test_generated_svgs_match_their_editable_source(self):
        for name, expected in artwork.assets().items():
            with self.subTest(name=name):
                self.assertEqual((artwork.OUT / name).read_bytes(), expected.encode('utf-8'))

    def test_svgs_have_accessible_static_content_and_no_external_dependencies(self):
        allowed = {'svg', 'title', 'desc', 'defs', 'linearGradient', 'radialGradient',
                   'stop', 'style', 'rect', 'circle', 'ellipse', 'path', 'g', 'text', 'animateMotion'}
        for name, source in artwork.assets().items():
            with self.subTest(name=name):
                svg = ET.fromstring(source)
                self.assertTrue(svg.find('{*}title').text)
                self.assertTrue(svg.find('{*}desc').text)
                self.assertIn('prefers-reduced-motion:reduce', source)
                self.assertNotIn('@import', source)
                for node in svg.iter():
                    self.assertIn(node.tag.rsplit('}', 1)[-1], allowed)
                    # A transparent gradient stop is intentional; hiding an
                    # actual content node would break static/reduced-motion use.
                    self.assertNotEqual(node.attrib.get('opacity'), '0')
                    for key, value in node.attrib.items():
                        self.assertFalse(key.lower().startswith('on'))
                        self.assertNotIn('href', key.lower())
                        self.assertNotIn('url(http', value)
                        self.assertNotIn('url(data:', value)

    def test_mobile_picture_fallbacks_and_link_labels_exist(self):
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        pictures = re.findall(r'<picture>(.*?)</picture>', readme, re.S)
        self.assertGreaterEqual(len(pictures), 5)
        for picture in pictures:
            self.assertRegex(picture, r'<source media="\(max-width: 640px\)" srcset="[^"]+"')
            self.assertRegex(picture, r'<img[^>]+alt="[^"]+"')
            for target in re.findall(r'(?:src|srcset)="([^"]+)"', picture):
                self.assertTrue((ROOT / target).is_file(), target)
        for image in re.findall(r'<img\b[^>]+>', readme):
            self.assertRegex(image, r'alt="[^"]+"')
        self.assertIn('合成数据', readme)
        self.assertNotIn('<script', readme.lower())
        self.assertNotIn('<style', readme.lower())

    def test_public_preview_manifest_hashes_match(self):
        manifest = json.loads((artwork.OUT / 'manifest.json').read_bytes())
        self.assertEqual((artwork.OUT / 'manifest.json').read_bytes(), artwork.public_manifest())
        self.assertEqual(manifest['schema'], 1)
        self.assertFalse(manifest['productionDataIncluded'])
        self.assertEqual(manifest['consoleBuildId'],
                         json.loads((ROOT / 'site/home/preview/manifest.json').read_bytes())['buildId'])
        for item in manifest['assets']:
            with self.subTest(path=item['path']):
                file = (ROOT / item['path']).resolve()
                self.assertTrue(file.is_relative_to(ROOT))
                self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), item['sha256'])
                if item.get('sourcePath'):
                    self.assertEqual(file.read_bytes(), (ROOT / item['sourcePath']).read_bytes())


if __name__ == '__main__':
    unittest.main()
