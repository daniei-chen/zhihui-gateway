#!/usr/bin/env bash
# 校验发布完整性：签名 → 逐文件 sha256 → 权限位。任何不一致即非零退出。
# 用法：sudo ./scripts/verify-release.sh [目标目录，默认 /opt/qtg]
set -euo pipefail
DST="${1:-/opt/qtg}"
PUB="${QTG_SIGN_PUB:-$DST/secrets/release-signing.pub}"
MAN="${QTG_MANIFEST:-$DST/deploy/managed-release.json}"
SIG="${QTG_MANIFEST_SIG:-$DST/deploy/managed-release.sig}"

for f in "$PUB" "$MAN" "$SIG"; do
  [ -s "$f" ] || { echo "缺少 $f（先跑 scripts/sign-release.sh）" >&2; exit 2; }
done

echo "== 1/2 验签 =="
openssl dgst -sha256 -verify "$PUB" -signature "$SIG" "$MAN"

echo "== 2/2 逐文件校验 =="
python3 - "$DST" "$MAN" <<'PY'
import hashlib, json, os, stat, sys
from pathlib import Path
dst = Path(sys.argv[1]); man = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
bad = []
for item in man["files"]:
    p = dst / item["path"]
    if not p.is_file():
        bad.append(f'缺失 {item["path"]}'); continue
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    if h != item["sha256"]:
        bad.append(f'内容被改 {item["path"]}'); continue
    if oct(stat.S_IMODE(p.stat().st_mode)) != item["mode"]:
        bad.append(f'权限被改 {item["path"]}: {oct(stat.S_IMODE(p.stat().st_mode))} != {item["mode"]}')
if bad:
    print(f'✗ {len(bad)} 处不一致：')
    for b in bad[:20]:
        print('   ', b)
    sys.exit(1)
print(f'✓ {len(man["files"])} 个文件全部匹配（sha256 + mode）')
PY
echo "发布完整性：OK"
