import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
prov = json.loads((root/'PATCH_PROVENANCE.json').read_text(encoding='utf-8'))
bad = 0
for rel, want in prov['files'].items():
    p = root/rel
    if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != want:
        print(f"hash mismatch: {rel}"); bad += 1
sys.exit(1 if bad else 0)
