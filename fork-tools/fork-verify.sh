#!/usr/bin/env bash
# fork-verify.sh —— 本地定制完整性自检（保守模式：只读，不改任何东西）
#
# 用途：升级后 / 构建后 / 怀疑配置漂移时跑一遍，逐项断言本地机制还在。
# 退出码：0 = 全部通过（可能有警告）；1 = 有致命缺失。
set -uo pipefail

REPO="${REPO:-/home/agentuser/wbfork/repo}"
MANAGER="${MANAGER:-/opt/workbuddy-manager}"
UPSTREAM="${UPSTREAM:-/opt/workbuddy2api}"
MANIFEST="$REPO/fork-manifest.json"

pass=0; fail=0; warn=0
ok()   { printf '  [OK]   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  [FAIL] %s\n' "$1"; fail=$((fail+1)); }
wrn()  { printf '  [WARN] %s\n' "$1"; warn=$((warn+1)); }

echo "== 1. 基础文件 =="
for f in "$REPO/CHANGELOG.md" "$REPO/README.md" "$MANIFEST"; do
  if [ -f "$f" ]; then ok "$(basename "$f")"; else bad "$f 缺失"; fi
done
if [ -f "$REPO/server/CHANGELOG.md" ]; then
  if cmp -s "$REPO/CHANGELOG.md" "$REPO/server/CHANGELOG.md"; then
    ok "CHANGELOG 根/server 副本一致"
  else
    wrn "CHANGELOG 根/server 副本不一致（build 时自动同步，仓库内应手工对齐）"
  fi
fi
if [ -f "$MANAGER/.version" ]; then
  v=$(cat "$MANAGER/.version")
  if echo "$v" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(\+local\.[0-9]{8}\.[0-9]+)?$'; then
    ok ".version 格式合法：$v"
  else
    bad ".version 格式异常：$v"
  fi
fi

echo "== 2. 本地 Python 模块（12 个） =="
for m in budget.py budgetowner.py ledger.py observability.py bodyguard.py operational.py \
         offsitebackup.py backup_application.py pooledhttp.py upstreamschema.py \
         upstream-schema.json routers/budgets.py; do
  if [ -f "$REPO/server/$m" ]; then ok "server/$m"; else bad "server/$m 缺失"; fi
done

echo "== 3. 本地 Go 定制（从 manifest 读取） =="
while read -r g; do
  [ -z "$g" ] && continue
  if [ -f "$UPSTREAM/$g" ]; then ok "$g"; else bad "$UPSTREAM/$g 缺失"; fi
done < <(python3 -c "
import json
d=json.load(open('$MANIFEST'))
for x in d.get('go_files_added',[]): print(x['path'])
")
echo "  修改过的文件（校验本地定制标记是否还在）："
while read -r g; do
  [ -z "$g" ] && continue
  n=$(grep -c "本地定制" "$UPSTREAM/$g" 2>/dev/null | head -1); n=${n:-0}
  if [ "$n" -gt 0 ]; then ok "$g（$n 处标记）"; else bad "$UPSTREAM/$g 本地定制标记丢失"; fi
done < <(python3 -c "
import json
d=json.load(open('$MANIFEST'))
for x in d.get('go_files_modified',[]): print(x['path'])
")

echo "== 4. 前端 vendor 安全补丁 =="
if [ -f "$REPO/web/vendor/braces/PATCH_PROVENANCE.json" ]; then
  if python3 "$MANAGER/fork-tools/fork-verify-vendor.py" "$REPO/web/vendor/braces" >/dev/null 2>&1; then
    ok "vendor/braces 哈希全部匹配"
  else
    bad "vendor/braces 哈希漂移"
  fi
  if grep -q '"braces": *"file:\./vendor/braces"' "$REPO/web/package.json"; then
    ok "package.json overrides 指向 vendor/braces"
  else
    bad "overrides 丢失：npm 会装回上游版本，ReDoS 补丁失效"
  fi
else
  bad "vendor/braces/PATCH_PROVENANCE.json 缺失"
fi

echo "== 5. 部署机制 =="
for f in "$MANAGER/deploy/source_preflight.py" "$MANAGER/deploy/managed-release.json" \
         "$MANAGER/release-trust.pem" "$MANAGER/local-overrides/sitecustomize.py"; do
  if [ -f "$f" ]; then ok "$(basename "$f")"; else bad "$f 缺失"; fi
done
n=$(ls /etc/systemd/system/workbuddy-web.service.d/*.conf 2>/dev/null | wc -l)
if [ "$n" -ge 7 ]; then ok "systemd drop-in $n 个"; else wrn "systemd drop-in 仅 $n 个（期望 7）"; fi

echo "== 6. 签名与发布树 =="
if [ -L "$MANAGER/releases/current" ]; then
  cur=$(readlink -f "$MANAGER/releases/current")
  ok "current -> $(basename "$cur")"
  if [ -f "$cur/deploy/managed-release.json" ]; then ok "托管清单存在"; else bad "托管清单缺失"; fi
  if [ -f "$cur/CHANGELOG.md" ]; then ok "发布树含根目录 CHANGELOG.md"; else wrn "发布树缺 CHANGELOG.md"; fi
  if [ -f "$cur/server/CHANGELOG.md" ]; then ok "发布树含 server/CHANGELOG.md"; else bad "发布树缺 server/CHANGELOG.md"; fi
else
  bad "releases/current 软链缺失"
fi

echo "== 7. 离线备份 =="
if [ -f /root/backups/workbuddy-recovery/recovery-public.pem ]; then
  if [ -f /root/backups/workbuddy-recovery/offsite.json ]; then ok "离线备份已配置"; else wrn "公钥在但 offsite.json 缺失"; fi
else
  wrn "离线备份未配置（缺 recovery-public.pem，需用户提供）"
fi
latest=/root/backups/workbuddy-daily/latest.json
if [ -f "$latest" ]; then
  age=$(( ( $(date +%s) - $(stat -c %Y "$latest") ) / 3600 ))
  if [ "$age" -le 26 ]; then ok "最近备份 ${age}h 前"; else wrn "最近备份 ${age}h 前（>26h 触发 backup_stale）"; fi
fi

echo
echo "== 结果：通过 $pass / 失败 $fail / 警告 $warn =="
[ "$fail" -eq 0 ] && exit 0 || exit 1
