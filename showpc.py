import hashlib, json
from pathlib import Path
ROOT = Path(r"D:\AI\kimi\2\zhihui-gateway")
data = json.loads((ROOT / "docs/protected-customizations.json").read_text(encoding="utf-8"))
for rel, expected in data.items():
    raw = (ROOT / rel).read_bytes()
    print("%s  expect=%s" % (rel, expected[:16]))
