#!/bin/bash
# 重新采集 managed-release.json 的文件哈希并签名。
#   sudo ./sign-release.sh --dry-run   只比对，不写入
#   sudo ./sign-release.sh             写入并签名
set -euo pipefail
ROOT=/opt/workbuddy-manager
KEY=${WB_SIGNING_KEY:-$ROOT/deploy/release-signing.key}
# 2026-10-23：签名私钥已迁离线（迁移前 sha256 51c7ee70…，496? 见 SIGNING.md）
if [[ ! -f "$KEY" ]]; then
  echo "ABORT: 签名私钥不在本机。" >&2
  echo "  用法: WB_SIGNING_KEY=/root/.tmp-sign.key sudo -E ./sign-release.sh" >&2
  echo "  签完立刻 rm 掉临时私钥。完整流程见 /opt/workbuddy-manager/SIGNING.md" >&2
  exit 2
fi
JSON=$ROOT/deploy/managed-release.json
SIG=$ROOT/deploy/managed-release.sig
DRY=${1:-}
cd "$ROOT"
TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT

: > "$TMPD/state.tsv"
bad=0
while IFS= read -r rel; do
  if [[ -L "$rel" ]]; then printf 'SYMLINK %s\n' "$rel" >&2; bad=1; continue; fi
  if [[ ! -f "$rel" ]]; then printf 'MISSING %s\n' "$rel" >&2; bad=1; continue; fi
  sha=$(sha256sum -- "$rel" | cut -d' ' -f1)
  mode=$(printf '%d' "0$(stat -c '%a' -- "$rel")")
  printf '%s\t%s\t%s\n' "$rel" "$sha" "$mode" >> "$TMPD/state.tsv"
done < <(jq -r '.files | keys[]' "$JSON")
[[ $bad -eq 0 ]] || { echo "ABORT: 清单引用的文件缺失或为软链"; exit 1; }

count=$(wc -l < "$TMPD/state.tsv")
jq -R -s 'split("\n") | map(select(length>0)) |
  map(split("\t") | {key: .[0], value: {candidate_sha256: .[1], mode: (.[2]|tonumber)}}) |
  from_entries' "$TMPD/state.tsv" > "$TMPD/files.json"
jq --slurpfile f "$TMPD/files.json" '.files = $f[0] | .at = (now|todate)' "$JSON" > "$TMPD/new.json"

changed=$(jq -r --slurpfile f "$TMPD/files.json" '
  [.files | to_entries[] | select($f[0][.key].candidate_sha256 != .value.candidate_sha256 or $f[0][.key].mode != .value.mode) | .key] | length' "$JSON")

echo "files=$count changed=$changed"
if [[ "$changed" -gt 0 ]]; then
  jq -r --slurpfile f "$TMPD/files.json" '
    .files | to_entries[] | select($f[0][.key].candidate_sha256 != .value.candidate_sha256) | .key' "$JSON" | head -20
fi
if [[ "$DRY" == "--dry-run" ]]; then echo "dry-run 结束，未写入"; exit 0; fi

cp "$TMPD/new.json" "$JSON"
openssl dgst -sha256 -sign "$KEY" -out "$SIG" "$JSON"
echo "已签名 $count 个文件"
