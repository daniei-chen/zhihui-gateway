#!/bin/bash
# 迁移收尾：清门禁、恢复对账定时器，并以退出码报告真实结果。
#
# 只允许在事务 `phase=complete` 后执行。命令返回 0 表示每一项都已复核；
# 任一门禁未清除、systemd 未重载、对账 timer 未激活或主服务未运行都返回非零。
# 此时新版本通常已经开始服务，调用方不得自动回退代码，必须保留现场并人工检查。

set -uo pipefail

TXN=${1:-}
STATE=/opt/zhihui-release/writer-state
BUDGET_SERVICE=wb-budget-autoreconcile.service
BUDGET_TIMER=wb-budget-autoreconcile.timer
UNITS=(workbuddy-web-helper.service workbuddy-web.service)
FAILED=0

say() { printf '[cleanup] %s\n' "$*"; }
fail() { say "失败：$*"; FAILED=1; }

if [ -z "$TXN" ]; then
    say "用法：post-migration-cleanup.sh <transaction-id>"
    exit 2
fi

JOURNAL="$STATE/transactions/$TXN/active.json"
if [ ! -f "$JOURNAL" ]; then
    fail "找不到事务日志 $JOURNAL"
    exit 1
fi
if ! grep -q '"phase":"complete"' "$JOURNAL" 2>/dev/null; then
    fail "事务 $TXN 尚未 complete；门禁必须保持"
    exit 1
fi

say "事务 $TXN 已 complete，开始收尾"

for unit in "${UNITS[@]}"; do
    marker="$STATE/$unit.start-blocked"
    [ -e "$marker" ] || continue
    if rm -f "$marker"; then
        say "已清除残留门禁：$unit"
    else
        fail "无法清除 $unit 启动门禁"
    fi
done

if systemctl daemon-reload; then
    say "systemd 单元已重新加载"
else
    fail "systemd daemon-reload 失败"
fi

for unit in "${UNITS[@]}"; do
    if [ -e "$STATE/$unit.start-blocked" ]; then
        fail "$unit 启动门禁仍存在"
    else
        say "$unit 启停门禁已解除"
    fi
done

# 清掉上一次迁移留下的 failed 状态；不清定时器，只清 alarms。
systemctl reset-failed "$BUDGET_SERVICE" 2>/dev/null || true
if systemctl is-failed --quiet "$BUDGET_SERVICE"; then
    fail "$BUDGET_SERVICE 仍处于 failed"
fi

if ! systemctl is-active --quiet "$BUDGET_TIMER"; then
    if ! systemctl start "$BUDGET_TIMER"; then
        fail "无法启动 $BUDGET_TIMER"
    fi
fi
if systemctl is-active --quiet "$BUDGET_TIMER"; then
    next=$(systemctl show "$BUDGET_TIMER" -p NextElapseUSecRealtime --value 2>/dev/null)
    say "$BUDGET_TIMER 已激活，下次触发：${next:-未知}"
else
    fail "$BUDGET_TIMER 未激活"
fi

if ! systemctl is-active --quiet workbuddy-web.service; then
    fail "workbuddy-web.service 未运行"
fi

helper_state=$(systemctl is-active workbuddy-web-helper.service 2>/dev/null || true)
say "主服务：$(systemctl is-active workbuddy-web.service 2>/dev/null || true)"
say "helper：${helper_state:-unknown}（按需实例，inactive 正常）"

if [ "$FAILED" -ne 0 ]; then
    say "收尾未完成；已切换版本保持服务，禁止盲目回滚，需按上述失败项处理"
    exit 1
fi
say "收尾完成且全部恢复项已复核"
exit 0
