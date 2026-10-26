#!/usr/bin/env bash
# 采集当前 qtg 的真实 API 响应作为渲染测试 fixture（只读）
set -euo pipefail
BASE="${QTG_BASE:-http://127.0.0.1:8795}"
OUT="${QTG_FIXTURES:-/tmp/qtg-fixtures}"
mkdir -p "$OUT"
for ep in overview accounts usage keys models "requests?days=3" tasks ops settings logs alerts audit; do
  name="${ep%%\?*}"
  curl -s --compressed -m 30 "$BASE/api/v1/$ep" -o "$OUT/$name.json"
  printf '  %-12s %s bytes\n' "$name" "$(stat -c%s "$OUT/$name.json")"
done
echo "fixtures -> $OUT"
