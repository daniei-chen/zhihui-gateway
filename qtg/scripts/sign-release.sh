#!/usr/bin/env bash
# 生成发布清单并签名（逐文件 sha256 + 权限位），供启动/部署前校验。
# 用法：sudo ./scripts/sign-release.sh [目标目录，默认 /opt/qtg]
set -euo pipefail
DST="${1:-/opt/qtg}"
KEY="${QTG_SIGN_KEY:-$DST/secrets/release-signing.key}"
PUB="${QTG_SIGN_PUB:-$DST/secrets/release-signing.pub}"
OUT="${QTG_MANIFEST:-$DST/deploy/managed-release.json}"
SIG="${QTG_MANIFEST_SIG:-$DST/deploy/managed-release.sig}"

# 1) 首次运行生成密钥（离线保存私钥！丢失则无法再签名）
if [ ! -s "$KEY" ]; then
  sudo openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$KEY" 2>/dev/null
  sudo chmod 400 "$KEY"
  sudo openssl pkey -in "$KEY" -pubout -out "$PUB"
  sudo chmod 444 "$PUB"
  echo "已生成发布签名密钥：$KEY（请离线备份，勿留服务器）"
fi

# 2) 清单：app/qtg + web（排除 __pycache__/.pyc）
sudo python3 - "$DST" "$OUT" <<'PY'
import hashlib, json, os, stat, sys
from pathlib import Path

dst = Path(sys.argv[1]); out = Path(sys.argv[2])
roots = [dst / "app" / "qtg", dst / "web"]
files = []
for root in roots:
    if not root.exists():
        continue
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(dst).as_posix()
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        mode = stat.S_IMODE(p.stat().st_mode)
        files.append({"path": rel, "sha256": h, "mode": oct(mode)})
manifest = {
    "product": "qtg",
    "version": open(dst / "app" / "qtg" / "__init__.py", encoding="utf-8").read().split('"')[1] if False else "1.0.0",
    "file_count": len(files),
    "files": files,
}
out.parent.mkdir(parents=True, exist_ok=True)
tmp = out.with_suffix(".json.tmp")
tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
os.replace(tmp, out)
os.chmod(out, 0o444)
print(f"清单：{len(files)} 个文件 -> {out}")
PY

# 3) 分离签名
sudo openssl dgst -sha256 -sign "$KEY" -out "$SIG" "$OUT"
sudo chmod 444 "$SIG"
echo "签名：$SIG"
sudo openssl dgst -sha256 -verify "$PUB" -signature "$SIG" "$OUT"
