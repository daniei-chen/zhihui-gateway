#!/usr/bin/env bash
# ops/install.sh —— 把告警体系（脚本 + 邮件素材）装到系统位置
#
# 这个目录是「网关运维层」的源头：改这里 → 跑本脚本 → 生效。
# 系统位置（运行时）：
#   /usr/local/bin/wb-gateway-alert.py     告警巡检脚本（5 分钟一次）
#   /var/www/mail-assets/                  邮件图片（nginx 免认证直出）
#   /etc/wb-gateway-alert.json             渠道配置（含 SMTP 授权码，600，不入库）
#
# 用法：
#   sudo ops/install.sh            # 安装/更新
#   sudo ops/install.sh --check    # 只检查差异，不写入
set -uo pipefail
cd "$(dirname "$0")"

SRC_SCRIPT="wb-gateway-alert.py"
DST_SCRIPT="/usr/local/bin/wb-gateway-alert.py"
SRC_ASSETS="mail-assets"
DST_ASSETS="/var/www/mail-assets"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

say() { printf '%s\n' "$*"; }

say "== 1. 告警脚本 =="
if [ ! -f "$SRC_SCRIPT" ]; then say "  [FAIL] 缺 $SRC_SCRIPT"; exit 1; fi
if cmp -s "$SRC_SCRIPT" "$DST_SCRIPT"; then
  say "  [OK] 已是最新"
else
  if [ "$CHECK" -eq 1 ]; then
    say "  [DIFF] 与系统版本不同"
  else
    install -m 755 "$SRC_SCRIPT" "$DST_SCRIPT" && say "  [OK] 已安装 $DST_SCRIPT"
  fi
fi

say "== 2. 邮件素材 =="
mkdir -p "$DST_ASSETS"
for f in "$SRC_ASSETS"/*; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  if cmp -s "$f" "$DST_ASSETS/$name"; then
    say "  [OK] $name"
  else
    if [ "$CHECK" -eq 1 ]; then
      say "  [DIFF] $name"
    else
      install -m 644 "$f" "$DST_ASSETS/$name" && say "  [OK] 已安装 $name"
    fi
  fi
done
chown -R www-data:www-data "$DST_ASSETS" 2>/dev/null || true
chmod 775 "$DST_ASSETS" 2>/dev/null || true

say "== 3. 渠道配置 =="
CFG=/etc/wb-gateway-alert.json
if [ -f "$CFG" ]; then
  say "  [OK] $CFG 存在（含授权码，不入库）"
  python3 - "$CFG" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
e = c.get('email', {})
print(f"     email.enabled={e.get('enabled')} host={e.get('smtp_host')} to={e.get('to')}")
PY
else
  say "  [WARN] $CFG 不存在 —— 从模板创建后需手工填 SMTP 授权码"
  if [ "$CHECK" -eq 0 ] && [ -f wb-gateway-alert.example.json ]; then
    install -m 600 wb-gateway-alert.example.json "$CFG"
  fi
fi

say "== 4. nginx 路由 =="
if grep -q '/mail-assets/' /etc/nginx/sites-available/dsh.kaogong.art 2>/dev/null; then
  say "  [OK] /mail-assets/ 已配置（auth_request off）"
else
  say "  [WARN] nginx 缺 /mail-assets/ 路由，邮件图片会 401"
  say "         加：location ^~ /mail-assets/ { auth_request off; root /var/www; expires 7d; }"
fi

say "== 5. 定时器 =="
systemctl is-active --quiet wb-gateway-alert.timer \
  && say "  [OK] wb-gateway-alert.timer active" \
  || say "  [WARN] wb-gateway-alert.timer 未运行"

say
[ "$CHECK" -eq 1 ] && say "（check 模式，未写入任何文件）" || say "完成。"
