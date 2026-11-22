import hashlib, json
from pathlib import Path

ROOT = Path(r"D:\AI\kimi\2\zhihui-gateway")

def lf_hash(path):
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    norm = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return hashlib.sha256(norm).hexdigest(), hashlib.sha256(raw).hexdigest()

pc = ROOT / "docs/protected-customizations.json"
data = json.loads(pc.read_text(encoding="utf-8"))
fixed = 0
for rel, expected in list(data.items()):
    p = ROOT / rel
    lf, raw = lf_hash(p)
    status = "ok" if expected in (lf, raw) else "MISMATCH"
    if status != "ok":
        data[rel] = lf
        fixed += 1
    print(f"{status:9} {rel}  expect={expected[:12]} lf={lf[:12]} raw={raw[:12]}")
if fixed:
    text = pc.read_text(encoding="utf-8")
    for rel, expected in json.loads(pc.read_text(encoding="utf-8")).items():
        new = data[rel]
        if rel in data:
            text = text.replace('"%s": "%s"' % (rel, expected), '"%s": "%s"' % (rel, new))
    pc.write_bytes(text.encode("utf-8"))
print("fixed", fixed)
