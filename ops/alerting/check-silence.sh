#!/usr/bin/env bash
# K46 告警链路只读检查（候选，未部署）。
# 只读：不修改 timer/配置，不触发告警。
# 用法：bash check-silence.sh [--json]
set -uo pipefail
JSON=0
[ "${1:-}" = "--json" ] && JSON=1
FAIL=0
declare -a ROWS
row() { ROWS+=("$1|$2|$3"); [ "$2" = "FAIL" ] && FAIL=$((FAIL+1)); }

# 1. 告警相关 timer 最近执行
for unit in wb-gateway-alert.timer healthcheck.timer trae-limits.timer; do
  if command -v systemctl >/dev/null 2>&1; then
    line=$(systemctl list-timers --all --no-pager 2>/dev/null | grep -F "$unit" | head -1)
    if [ -n "$line" ]; then
      last=$(printf '%s' "$line" | sed -E 's/^.{0,40}([A-Z][a-z]{2} [0-9-]+ [0-9:]+).*/\1/')
      row "$unit" "OK" "last=$last"
    else
      row "$unit" "WARN" "not found / not scheduled"
    fi
  else
    row "$unit" "SKIP" "systemctl 不可用"
  fi
done

# 2. notify 配置是否启用（只读键名，不显示值）
CFG=/opt/notify/config.json
if [ -r "$CFG" ]; then
  if grep -q '"enabled"[[:space:]]*:[[:space:]]*true' "$CFG" 2>/dev/null; then
    row "notify_enabled" "OK" "true"
  else
    row "notify_enabled" "WARN" "false（独立告警链仍需覆盖）"
  fi
else
  row "notify_enabled" "SKIP" "无权限读 $CFG"
fi

# 3. 最近一条告警日志时间（存在性即可）
for log in /var/log/zhihui-alert.log /var/log/wb-gateway-alert.log /var/log/healthcheck.log; do
  if [ -r "$log" ]; then
    row "log:$log" "OK" "mtime=$(date -r "$log" -Is 2>/dev/null || echo '?')"
  fi
done

# 4. 备份新鲜度
LATEST=/root/backups/workbuddy-daily/latest.json
if [ -r "$LATEST" ]; then
  mtime=$(date -r "$LATEST" +%s 2>/dev/null || echo 0)
  now=$(date +%s)
  age_h=$(( (now - mtime) / 3600 ))
  if [ "$age_h" -gt 36 ]; then row "backup_age" "FAIL" "${age_h}h > 36h"
  else row "backup_age" "OK" "${age_h}h"; fi
else
  row "backup_age" "SKIP" "无权限读 latest.json"
fi

if [ "$JSON" = "1" ]; then
  printf '{"checks":['
  first=1
  for r in "${ROWS[@]}"; do
    IFS='|' read -r id st detail <<<"$r"
    [ "$first" = "1" ] || printf ','
    first=0
    printf '{"id":"%s","status":"%s","detail":"%s"}' "$id" "$st" "$detail"
  done
  printf '],"fail":%d}\n' "$FAIL"
else
  printf '%-28s %-6s %s\n' "CHECK" "STATUS" "DETAIL"
  for r in "${ROWS[@]}"; do
    IFS='|' read -r id st detail <<<"$r"
    printf '%-28s %-6s %s\n' "$id" "$st" "$detail"
  done
  echo; echo "FAIL=$FAIL"
fi
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
