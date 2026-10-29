#!/usr/bin/env bash
# K11 回退前只读预检（候选，未部署）。
# 只读：不写文件、不 reload、不重启、不改软链。
# 用法：bash preflight.sh [--json]
# 在服务器上跑（需要能读 /var/www 软链与监听端口）；本地 Git Bash 只能跑部分项。
set -uo pipefail

JSON=0
[ "${1:-}" = "--json" ] && JSON=1
FAIL=0
declare -a ROWS

row() { # id status detail
  ROWS+=("$1|$2|$3")
  [ "$2" = "FAIL" ] && FAIL=$((FAIL+1))
}

# 1. 当前 release 与软链
LINK=/var/www/zhihui-console-current
if [ -L "$LINK" ]; then
  TGT=$(readlink -f "$LINK" 2>/dev/null || true)
  if [ -n "$TGT" ] && [ -d "$TGT" ]; then row "frontend_link" "OK" "$TGT"
  else row "frontend_link" "FAIL" "软链目标不存在: $TGT"; fi
else
  row "frontend_link" "WARN" "$LINK 不是软链或不存在"
fi

# 2. Manager release
if [ -d /opt/workbuddy-manager/releases/current ]; then
  row "manager_release" "OK" "$(readlink -f /opt/workbuddy-manager/releases/current 2>/dev/null || echo current)"
else
  row "manager_release" "WARN" "未找到 /opt/workbuddy-manager/releases/current"
fi

# 3. 关键端口监听
for port in 7864 8795 8799 7863 8080 8081; do
  if command -v ss >/dev/null 2>&1; then
    if ss -lnt 2>/dev/null | grep -q ":$port "; then row "port_$port" "OK" "listening"
    else row "port_$port" "FAIL" "not listening"; fi
  else
    row "port_$port" "SKIP" "ss 不可用"
  fi
done

# 4. Nginx 配置语法（不 reload）
if command -v nginx >/dev/null 2>&1; then
  if nginx -t >/dev/null 2>&1; then row "nginx_config" "OK" "syntax ok"
  else row "nginx_config" "FAIL" "nginx -t failed"; fi
else
  row "nginx_config" "SKIP" "nginx 不在 PATH"
fi

# 5. 生效配置是否仍含安全探针（回退目标必须也含）
SITE=/etc/nginx/sites-enabled/gateway.example.com
if [ -r "$SITE" ]; then
  if grep -q 'auth_request /__zh_session' "$SITE"; then row "auth_probe" "OK" "auth_request present"
  else row "auth_probe" "FAIL" "回退目标缺少 auth_request（会重开匿名管理面）"; fi
else
  row "auth_probe" "SKIP" "无法读取 $SITE"
fi

# 6. 备份新鲜度（只读 latest.json）
LATEST=/root/backups/workbuddy-daily/latest.json
if [ -r "$LATEST" ]; then
  if command -v python3 >/dev/null 2>&1; then
    OFF=$(python3 -c "import json,sys;d=json.load(open('$LATEST'));print(d.get('offsite'))" 2>/dev/null || echo unknown)
    row "backup_offsite" "WARN" "offsite=$OFF（true 才算有异地副本）"
  else
    row "backup_offsite" "SKIP" "python3 不可用"
  fi
else
  row "backup_offsite" "SKIP" "无权限读 $LATEST"
fi

# 输出
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
  printf '%-18s %-6s %s\n' "CHECK" "STATUS" "DETAIL"
  for r in "${ROWS[@]}"; do
    IFS='|' read -r id st detail <<<"$r"
    printf '%-18s %-6s %s\n' "$id" "$st" "$detail"
  done
  echo
  echo "FAIL=$FAIL"
fi

[ "$FAIL" -eq 0 ] && exit 0 || exit 1
