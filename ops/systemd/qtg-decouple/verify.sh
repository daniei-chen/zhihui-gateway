#!/usr/bin/env bash
# K13 verify: assert engine is decoupled and survives a qtg restart.
set -uo pipefail

UNIT_NAME="${UNIT_NAME:-qoder2-engine.service}"
QT_UNIT_NAME="${QT_UNIT_NAME:-qtg.service}"
HEALTH_URL_ENGINE="${HEALTH_URL_ENGINE:-}"
HEALTH_URL_QTG="${HEALTH_URL_QTG:-}"
CHECK_HEALTH="${CHECK_HEALTH:-1}"
fail=0
checks=()
record() { # name ok detail
  local name="$1" ok="$2" detail="$3"
  checks+=("{\"check\":\"${name}\",\"ok\":${ok},\"detail\":\"${detail}\"}")
  [ "$ok" = "true" ] || fail=1
  printf '%-32s %-5s %s\n' "$name" "$([ "$ok" = "true" ] && echo PASS || echo FAIL)" "$detail"
}
[ "$(id -u)" = "0" ] || { printf 'must run as root\n' >&2; exit 1; }

partof="$(systemctl show -p PartOf --value "$UNIT_NAME" 2>/dev/null || true)"
if [ -z "$partof" ]; then
  record partof_empty true "'${partof}'"
else
  if [[ "$partof" != *"$QT_UNIT_NAME"* ]]; then
    record partof_not_qtg true "'${partof}'"
  else
    record partof_empty false "'${partof}' 仍包含 ${QT_UNIT_NAME}"
  fi
fi

eng_active="$(systemctl is-active "$UNIT_NAME" 2>/dev/null || true)"
record engine_active "$([ "$eng_active" = active ] && echo true || echo false)" "is-active=${eng_active}"

eng_pid_before="$(systemctl show -p MainPID --value "$UNIT_NAME" 2>/dev/null || true)"
qtg_pid_before="$(systemctl show -p MainPID --value "$QT_UNIT_NAME" 2>/dev/null || true)"
record engine_pid_before "$([ -n "$eng_pid_before" ] && [ "$eng_pid_before" != "0" ] && echo true || echo false)" "MainPID=${eng_pid_before}"

if systemctl restart "$QT_UNIT_NAME"; then
  record qtg_restart true "restarted ${QT_UNIT_NAME}"
else
  record qtg_restart false "systemctl restart ${QT_UNIT_NAME} failed"
fi
sleep 2
eng_pid_after="$(systemctl show -p MainPID --value "$UNIT_NAME" 2>/dev/null || true)"
qtg_pid_after="$(systemctl show -p MainPID --value "$QT_UNIT_NAME" 2>/dev/null || true)"
record engine_pid_unchanged "$([ "$eng_pid_before" = "$eng_pid_after" ] && echo true || echo false)" "before=${eng_pid_before} after=${eng_pid_after}"
record qtg_pid_changed "$([ "$qtg_pid_before" != "$qtg_pid_after" ] && echo true || echo false)" "before=${qtg_pid_before} after=${qtg_pid_after}"
record engine_active_after "$([ "$(systemctl is-active "$UNIT_NAME" 2>/dev/null || true)" = active ] && echo true || echo false)" "is-active=$(systemctl is-active "$UNIT_NAME" 2>/dev/null || true)"

if [ "$CHECK_HEALTH" = "1" ]; then
  for pair in "engine:${HEALTH_URL_ENGINE}" "qtg:${HEALTH_URL_QTG}"; do
    label="${pair%%:*}"; url="${pair#*:}"
    if [ -z "$url" ]; then
      record "health_${label}" false "未提供 HEALTH_URL_${label^^}（CHECK_HEALTH=0 可跳过，但不得声称已验证健康）"
    else
      code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null || true)"
      record "health_${label}" "$([ "$code" = "200" ] && echo true || echo false)" "url=${url} status=${code}"
    fi
  done
else
  record health_skipped true "CHECK_HEALTH=0（VM 模拟 unit 无 HTTP 端点；真实环境必须提供 HEALTH_URL_*）"
fi

printf '{"ok":%s,"unit":"%s","qt_unit":"%s","checks":[%s]}\n' \
  "$([ "$fail" = "0" ] && echo true || echo false)" "$UNIT_NAME" "$QT_UNIT_NAME" "$(IFS=,; echo "${checks[*]}")"
exit "$fail"
