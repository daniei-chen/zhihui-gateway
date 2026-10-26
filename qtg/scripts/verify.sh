#!/usr/bin/env bash
# qtg 一键验收：服务 / 上游 / API / 鉴权 / 指标 / 审计 / 测试
# 用法：sudo ./scripts/verify.sh     或     QTG_BASE=http://127.0.0.1:8795 ./scripts/verify.sh
set -uo pipefail
BASE="${QTG_BASE:-http://127.0.0.1:8795}"
TOKEN_FILE="${QTG_TOKEN_FILE:-/opt/qtg/secrets/admin_token}"
pass=0; fail=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; fail=$((fail+1)); }
chk()  { if [ "$2" = "$3" ]; then ok "$1 ($3)"; else bad "$1 (期望 $3，实际 $2)"; fi; }

echo "===== 1. 服务 ====="
active=$(systemctl is-active qtg.service 2>/dev/null || echo unknown)
chk "qtg.service" "$active" "active"
m=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$BASE/healthz")
chk "GET /healthz" "$m" "200"

echo "===== 2. 上游就绪 ====="
ready=$(curl -s -m 25 "$BASE/readyz" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ok"))' 2>/dev/null)
chk "GET /readyz" "$ready" "True"

echo "===== 3. 真实数据 ====="
# 鉴权闸门（main.py `_auth_gate`）：/api/* 一律要求凭证，这里用管理令牌。
TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null || true)
ov=$(curl -s -m 30 --compressed -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/overview")
deg=$(printf '%s' "$ov" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("degraded"))' 2>/dev/null)
chk "overview.degraded" "$deg" "False"
stats=$(printf '%s' "$ov" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(",".join(s["value"] for s in d["stats"]))' 2>/dev/null)
if [ -n "$stats" ]; then ok "统计卡: $stats"; else bad "统计卡为空"; fi

echo "===== 4. 写操作鉴权（无凭证必须 401） ====="
for ep in checkin limits notify backup alerts/ack; do
  code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' "$BASE/api/v1/actions/$ep")
  chk "POST /actions/$ep 无凭证" "$code" "401"
done

echo "===== 4b. 无凭证读管理接口必须 401（2026-10-22 审计 P0 修复） ====="
for ep in v1/keys v1/accounts accounts keys security/config audit-logs; do
  code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$BASE/api/$ep")
  chk "GET /api/$ep 无凭证" "$code" "401"
done

echo "===== 4c. 静态兜底目录穿越必须拦截 ====="
trav=$(curl -s -m 8 --path-as-is -o /dev/null -w '%{http_code}' "$BASE/..%2f..%2f..%2fetc%2fpasswd")
if [ "$trav" = "403" ] || [ "$trav" = "404" ]; then ok "目录穿越已拦截 ($trav)"; else bad "目录穿越未拦截 ($trav)"; fi

echo "===== 5. 写操作鉴权（带凭证必须 200） ====="
if [ -r "$TOKEN_FILE" ]; then
  TOKEN=$(cat "$TOKEN_FILE")
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"enabled":true,"daily_token_limit":0,"daily_credit_limit":0,"reserve_credits":0}' \
    "$BASE/api/v1/actions/limits")
  chk "POST /actions/limits 带凭证" "$code" "200"
  echo "===== 6. 审计链 ====="
  integ=$(curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/audit?limit=5" | python3 -c 'import json,sys;print(json.load(sys.stdin)["integrity"]["ok"])' 2>/dev/null)
  chk "审计链完整性" "$integ" "True"
else
  echo "  (跳过：无权读取 $TOKEN_FILE)"
fi

echo "===== 7. 指标 ====="
n=$(curl -s -m 10 "$BASE/metrics" | grep -c '^qtg_')
if [ "$n" -gt 10 ]; then ok "qtg 指标行数 $n"; else bad "指标过少 ($n)"; fi

echo "===== 8. 安全头 ====="
hdr=$(curl -s -m 10 -o /dev/null -D - "$BASE/" | tr -d '\r')
for h in "x-content-type-options: nosniff" "x-frame-options: DENY" "content-security-policy:"; do
  if printf '%s' "$hdr" | grep -qi "$h"; then ok "响应头 $h"; else bad "缺少响应头 $h"; fi
done

echo "===== 9. 前端资源 ====="
for p in "/" "/assets/app.css" "/assets/js/app.js" "/assets/js/pages/overview.js" "/favicon.svg"; do
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$BASE$p")
  chk "GET $p" "$code" "200"
done

echo "===== 10. 测试套件 ====="
APP_DIR="${QTG_APP:-/opt/qtg/app}"
if [ -d "${QTG_SRC:-/opt/qtg}/tests" ]; then
  out=$(cd "${QTG_SRC:-/opt/qtg}" && PYTHONPATH="$APP_DIR" /opt/workbuddy-manager/venv/bin/python -m unittest discover -s tests 2>&1 | tail -3)
  if printf '%s' "$out" | grep -q '^OK'; then ok "后端 unittest: $(printf '%s' "$out" | grep -o 'Ran [0-9]* tests')"; else bad "后端测试失败: $out"; fi
else
  echo "  (跳过：未找到 tests 目录)"
fi

echo
echo "===== 结果：通过 $pass / 失败 $fail ====="
[ "$fail" -eq 0 ] || exit 1
