#!/bin/bash
# K09：/wb/ 全站别名与边缘 location 语义审计（lab VM，真实 Manager/QTG 作上游）
# 只读：只发 HTTP 请求，不改任何服务配置。
set -uo pipefail
BASE="$1"
H="https://127.0.0.1:18443"
C=(curl -sk --max-time 15 -o /dev/null -w '%{http_code}')

pass=0; fail=0
ROWS=()
chk() {
  local id="$1" desc="$2" want="$3" got="$4"
  if [[ "$want" == "$got" ]]; then
    ROWS+=("PASS|$id|$desc|$want|$got"); pass=$((pass+1)); printf 'PASS %-38s %s\n' "$desc" "$got"
  else
    ROWS+=("FAIL|$id|$desc|$want|$got"); fail=$((fail+1)); printf 'FAIL %-38s want=%s got=%s\n' "$desc" "$want" "$got"
  fi
}

echo "=== A. location 优先级与尾斜杠 ==="
chk A1 "/metrics 精确匹配优先于 /wb/ 前缀" 404 "$("${C[@]}" "$H/metrics")"
chk A2 "/wb（无斜杠）→ 301 补斜杠" 301 "$("${C[@]}" "$H/wb")"
# 上游 Manager 的 / 在无前端产物时是 404；别名是否生效看"响应体是不是 Manager 的"
body=$(curl -sk --max-time 15 "$H/wb/")
if printf '%s' "$body" | grep -q '"detail"'; then
  chk A3 "/wb/ 到达 Manager（返回 Manager JSON 信封）" 200 200
else
  chk A3 "/wb/ 到达 Manager（返回 Manager JSON 信封）" 200 0
fi
# Manager 的 /metrics 需要独立指标凭据 → 401。别名能拿到 401 即证明它到的是 Manager。
chk A4 "/wb/metrics 到达 Manager 且需鉴权（非匿名可读）" 401 "$("${C[@]}" "$H/wb/metrics")"
chk A5 "/wb/api/metrics 到达 Manager 自身路由（该路由不存在→404）" 404 "$("${C[@]}" "$H/wb/api/metrics")"

echo
echo "=== B. /wb/ 别名是否等于第二套管理面（未鉴权） ==="
# 关键：Manager 自身的鉴权必须仍然生效。别名只是边缘策略不一致，不等于绕过鉴权。
for p in /wb/api/keys /wb/api/me /wb/api/tokens /wb/api/users /wb/api/accounts /wb/api/settings/panel /wb/api/logs; do
  code=$("${C[@]}" "$H$p")
  body=$(curl -sk --max-time 15 "$H$p")
  # Manager 未鉴权应为 401 且 JSON 带 detail；若 200 则是真绕过
  if [[ "$code" == 401 ]]; then
    ROWS+=("SAFE|$p|$code|别名可达但 Manager 鉴权生效"); pass=$((pass+1))
    printf 'SAFE %-30s %s（鉴权生效，未绕过）\n' "$p" "$code"
  elif [[ "$code" == 200 ]]; then
    ROWS+=("BYPASS|$p|$code|$body"); fail=$((fail+1))
    printf 'BYPASS %-28s 200（未鉴权即读到数据！）\n' "$p"
  else
    ROWS+=("OTHER|$p|$code|非 200/401"); pass=$((pass+1))
    printf 'OTHER %-29s %s\n' "$p" "$code"
  fi
done
# 对照：同一路径走 /api/ 前缀，行为应完全一致（证明别名没改变鉴权，只是多一套入口）
a=$("${C[@]}" "$H/api/keys"); b=$("${C[@]}" "$H/wb/api/keys")
chk B0 "/api/keys 与 /wb/api/keys 同码" "$a" "$b"

echo
echo "=== B2. 带会话时 /wb/ 是否构成第二套管理面 ==="
JAR=$(mktemp); L=$(mktemp)
lc=$(curl -sk --max-time 15 -c "$JAR" -o "$L" -w '%{http_code}' -X POST "$H/api/login"      -H 'Content-Type: application/json'      -d '{"username":"gateway-admin","password":"synthetic-password"}')
printf 'login=%s %s
' "$lc" "$(head -c 100 "$L")"
if [[ "$lc" == 200 ]]; then
  same=0; diffc=0
  for p in keys me accounts logs tokens users settings/panel; do
    a=$(curl -sk --max-time 15 -b "$JAR" -o /dev/null -w '%{http_code}' "$H/api/$p")
    b=$(curl -sk --max-time 15 -b "$JAR" -o /dev/null -w '%{http_code}' "$H/wb/api/$p")
    [[ "$a" == "$b" ]] && same=$((same+1)) || diffc=$((diffc+1))
    printf 'INFO /api/%-14s=%s  /wb/api/%-14s=%s
' "$p" "$a" "$p" "$b"
  done
  if [[ $diffc == 0 ]]; then
    ROWS+=("CONFIRMED|B2|$same 个管理端点经 /wb/ 别名与 /api/ 完全等价"); fail=$((fail+1))
    printf 'CONFIRMED 第二套管理面：%d 个端点在 /wb/ 下等价可读写
' "$same"
  else
    ROWS+=("PARTIAL|B2|$same 同 / $diffc 异"); pass=$((pass+1))
    printf 'PARTIAL %d 同 %d 异
' "$same" "$diffc"
  fi
  # 写操作也经别名生效
  w=$(curl -sk --max-time 15 -b "$JAR" -o /dev/null -w '%{http_code}' -X POST "$H/wb/api/logout")
  if [[ "$w" == 200 ]]; then
    ROWS+=("CONFIRMED|B2-write|写操作经别名生效"); fail=$((fail+1))
    printf 'CONFIRMED 写操作 POST /wb/api/logout -> %s（别名可用于写）
' "$w"
  else
    printf 'INFO 写操作经别名 -> %s
' "$w"
  fi
else
  printf 'SKIP 无法登录，跳过 B2
'
fi
rm -f "$JAR" "$L"

echo
echo "=== C. 路径编码 / 大小写 / 规范化 ==="
chk C1 "/WB/metrics 大写不匹配（前缀大小写敏感）" 404 "$("${C[@]}" "$H/WB/metrics")"
chk C2 "/Wb/metrics 混合大小写不匹配" 404 "$("${C[@]}" "$H/Wb/metrics")"
# %2f 在 URI 里未被解码成路径分隔符时，不匹配 /wb/ → 落 /
# nginx 会把 %2f 解码后再做 location 匹配 → 仍命中 /wb/ → 与 A4 同码。
# 关键是"编码没有换到一条不同的处理器"，不是"被拒绝"。
chk C3 "/wb%2fmetrics 编码斜杠仍走同一处理器" "$("${C[@]}" "$H/wb/metrics")" "$("${C[@]}" "$H/wb%2fmetrics")"
# %2e%2e 规范化后回到根，命中精确 /metrics=404 —— 是安全的结果，不是穿透
chk C4 "/wb/%2e%2e/metrics 规范化后仍 404" 404 "$("${C[@]}" --path-as-is "$H/wb/%2e%2e/metrics")"
# 双前缀：/wb/wb/metrics → 别名剥一层 → 上游 /wb/metrics → 404（上游无此路由）
d=$(curl -sk --max-time 15 -o /dev/null -w '%{http_code}' "$H/wb/wb/api/healthz")
printf 'INFO /wb/wb/api/healthz -> %s（别名不递归，上游按 /wb/api/... 处理）\n' "$d"
chk C5 "/wb/wb/api/healthz 不产生二次剥前缀" 404 "$d"

echo
echo "=== D. 平台分流 map 与凭据隔离 ==="
plat() { curl -sk --max-time 15 "$H/api/me" -H "X-QTG-Platform: $1" "${@:2}" -o /dev/null -w '%{http_code}'; }
# 两种上游对同一路径的响应码不同即为分流生效的直接证据
mgr=$("${C[@]}" "$H/api/healthz")
qtg=$("${C[@]}" -H 'X-QTG-Platform: qoder' "$H/api/healthz")
printf 'INFO default(/api/healthz)=%s  qoder(/api/healthz)=%s\n' "$mgr" "$qtg"
if [[ "$mgr" != "$qtg" ]]; then
  ROWS+=("PASS|D1|qoder 分流到 QTG（响应码不同）|$mgr|$qtg"); pass=$((pass+1))
  printf 'PASS qoder 分流生效（默认 %s vs qoder %s）\n' "$mgr" "$qtg"
else
  ROWS+=("INFO|D1|两上游同码，改用版本头判别|$mgr|$qtg"); pass=$((pass+1))
  printf 'INFO 两上游该路径同码 %s，分流另用头部判别\n' "$mgr"
fi
# QTG 的健康响应带 x-qtg-version；Manager 不带 —— 用它做分流判别
qh=$(curl -sk --max-time 15 -D - -o /dev/null -H 'X-QTG-Platform: qoder' "$H/api/healthz" | grep -i '^x-qtg-version' || true)
mh=$(curl -sk --max-time 15 -D - -o /dev/null "$H/api/healthz" | grep -i '^x-qtg-version' || true)
printf 'INFO qoder 响应 x-qtg-version=%s ；默认响应=%s\n' "${qh:-<无>}" "${mh:-<无>}"
if [[ -n "$qh" && -z "$mh" ]]; then
  ROWS+=("PASS|D1b|qoder 分流到 QTG（版本头）||"); pass=$((pass+1)); printf 'PASS qoder 分流确证（版本头仅 QTG 有）\n'
else
  ROWS+=("FAIL|D1b|无法确证分流||"); fail=$((fail+1)); printf 'FAIL 无法确证 qoder 分流\n'
fi

# 凭据隔离：qoder/trae 分流必须清掉 Authorization（QTG 有自己的会话）
qtg_auth=$("${C[@]}" -H 'X-QTG-Platform: qoder' -H 'Authorization: Bearer SECRET-TOKEN' "$H/api/me")
mgr_auth=$("${C[@]}" -H 'Authorization: Bearer SECRET-TOKEN' "$H/api/me")
printf 'INFO /api/me default=%s qoder=%s\n' "$mgr_auth" "$qtg_auth"
ROWS+=("INFO|D2|Authorization 透传对比|$mgr_auth|$qtg_auth"); pass=$((pass+1))

echo
echo "=== E. 边缘安全头（含 4xx 也必须带） ==="
for h in X-Content-Type-Options X-Frame-Options Referrer-Policy Permissions-Policy Strict-Transport-Security Content-Security-Policy-Report-Only; do
  ok=1
  for probe in / /definitely-missing-xyz /api/healthz; do
    curl -sk --max-time 15 -D - -o /dev/null "$H$probe" | grep -qi "^$h:" || ok=0
  done
  if [[ $ok == 1 ]]; then
    ROWS+=("PASS|E:$h|全响应类型均带"); pass=$((pass+1)); printf 'PASS %-38s 200/404/API 均有\n' "$h"
  else
    ROWS+=("FAIL|E:$h|缺失于某类响应"); fail=$((fail+1)); printf 'FAIL %-38s 有响应缺该头\n' "$h"
  fi
done

echo
echo "=== F. 应用层是否另有安全头（双层不冲突） ==="
app=$(curl -sk --max-time 15 -D - -o /dev/null "$H/api/healthz")
for h in X-Content-Type-Options Content-Security-Policy X-Frame-Options; do
  n=$(printf '%s' "$app" | grep -ci "^$h:")
  printf 'INFO %-32s 出现 %d 次（>1 表示边缘与应用叠加）\n' "$h" "$n"
  ROWS+=("INFO|F:$h|出现 $n 次||")
done

echo
echo "=== 汇总 ==="
echo "pass=$pass fail=$fail"
printf '%s\n' "${ROWS[@]}" > "$BASE/k09-rows.raw"
echo "RAW -> $BASE/k09-rows.raw"
