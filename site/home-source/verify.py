"""Check public asset integrity and local-only packaging without a browser."""
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit, unquote
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
errors = []


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.anchors = []
        self.assets = set()
        self.buttons = set()
        self.panels = set()

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        identity = attrs.get("id")
        if identity:
            if identity in self.ids:
                errors.append("Duplicate id: " + identity)
            self.ids.add(identity)
        if "data-console" in attrs:
            self.buttons.add(attrs["data-console"])
        if "data-panel" in attrs:
            self.panels.add(attrs["data-panel"])
        if tag == "a" and attrs.get("href", "").startswith("#"):
            self.anchors.append(attrs["href"][1:])
        if tag in {"img", "script", "source", "iframe"} and attrs.get("src"):
            self.assets.add(attrs["src"])
        if tag == "source" and attrs.get("srcset"):
            self.assets.add(attrs["srcset"])
        if tag == "link" and attrs.get("href"):
            self.assets.add(attrs["href"])
        if tag == "img" and "alt" not in attrs:
            errors.append("Image without alt text")


html = (PUBLIC / "index.html").read_text(encoding="utf-8")
page = Page()
page.feed(html)
for anchor in page.anchors:
    if anchor and anchor not in page.ids:
        errors.append("Missing anchor: " + anchor)
for asset in page.assets:
    url = urlsplit(asset)
    if url.scheme or url.netloc:
        errors.append("External asset: " + asset)
        continue
    target = (PUBLIC / unquote(url.path).lstrip("/")).resolve()
    if not target.is_relative_to(PUBLIC.resolve()) or not target.is_file():
        errors.append("Missing or invalid asset: " + asset)
if page.buttons or page.panels or 'console' in page.ids:
    errors.append("Removed duplicate workspace has returned")
if html.count('class="console-capture"') != 1:
    errors.append("The original hero preview must remain unique")
if re.findall(r'<script[^>]+src="([^"]+)"', html) != ["/scripts/upgrade.js", "/scripts/motion.js", "/scripts/ride.js", "/scripts/sculpture.js"]:
    errors.append("Unexpected script ownership")
for scene in ['task-workspace.html', 'velo-coast.html']:
    child = Page()
    child.feed((PUBLIC / 'animations' / scene).read_text(encoding='utf-8'))
    for asset in child.assets:
        if urlsplit(asset).scheme or not (PUBLIC / asset.lstrip('/')).is_file():
            errors.append('Missing or external scene asset: ' + asset)
for asset in ['scripts/sculpture-viewer.js', 'models/thinker.glb', 'models/thinker-poster.webp',
              'models/thinker-manifest.json', 'models/ATTRIBUTION.txt', 'vendor/draco/draco_wasm_wrapper.js',
              'vendor/draco/draco_decoder.wasm', 'vendor/draco/draco_decoder.js', 'vendor/THREE-LICENSE.txt']:
    if not (PUBLIC / asset).is_file():
        errors.append('Missing sculpture asset: ' + asset)
login = Page()
login.feed((PUBLIC / 'login/index.html').read_text(encoding='utf-8'))
for asset in login.assets:
    if urlsplit(asset).scheme or not (PUBLIC / asset.lstrip('/')).is_file():
        errors.append('Missing or external login asset: ' + asset)
if not (PUBLIC / 'scripts/gateway-viewer.js').is_file():
    errors.append('Missing local gateway renderer')
for token in ["console-side", "zh23GuideNext", "zh3Inspector", "movingSpark", "intelligence-field"]:
    if token in html:
        errors.append("Obsolete UI remains: " + token)

source = ROOT / "source-snapshot" / "site"
provenance = json.loads((ROOT / "source-snapshot" / "manifest.json").read_text(encoding="utf-8"))
for item in provenance["files"]:
    path = source / item["file"]
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        errors.append("Snapshot hash mismatch: " + item["file"])
previews = json.loads((source / "preview" / "manifest.json").read_text(encoding="utf-8"))
for preview in previews["previews"]:
    path = PUBLIC / "preview" / preview["image"]
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != preview["sha256"]:
        errors.append("Preview differs from public synthetic fixture: " + preview["image"])
for path in PUBLIC.rglob("*"):
    if path.is_file() and path.suffix in {".html", ".css", ".js", ".json"}:
        text = path.read_text(encoding="utf-8")
        if "live-private" in text or "workbench-reference" in text:
            errors.append("Private reference in public output: " + path.name)
js = (PUBLIC / "scripts" / "upgrade.js").read_text(encoding="utf-8")
if re.search(r"\b(?:fetch|XMLHttpRequest|WebSocket|sendBeacon)\s*\(", js):
    errors.append("Unexpected networking in homepage controller")
base_css = (PUBLIC / "styles" / "source.css").read_text(encoding="utf-8")
if re.search(r"(?<![-\w])animation(?:-[\w]+)?\s*:", base_css):
    errors.append("Legacy animation declarations remain")
if errors:
    raise SystemExit("\n".join(errors))
print(f"Static checks passed: {len(page.assets)} assets, {len(page.ids)} unique ids, one hero preview, no duplicate workspace, {len(previews['previews'])} fixture hashes, {len(provenance['files'])} snapshot hashes.")
