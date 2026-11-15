#!/bin/bash
# 迁移收尾：把 migration 停掉的东西恢复回来。
#
# ## 为什么需要这个脚本
#
# 每次 `migration_entry.py run` 都会做两件「停掉」的事，但**没有对应的恢复**：
#
# 1. `ServiceGate.block()` 往
#    `/opt/zhihui-release/writer-state/<unit>.start-blocked` 写一个 marker。
#    对应的 drop-in 是 `ConditionPathExists=!<marker>` ——
#    **marker 在，unit 会被 systemd 静默 skip**（不是 failure，`Restart=` 不生效，
#    日志里只有一句 skipped，没有任何告警）。
#
# 2. `block()` 还会 `systemctl stop wb-budget-autoreconcile.timer`。
#    **没有任何代码把它启动回来。** 它 `enabled`，所以下次重启会恢复 ——
#    但在那之前，每日预算对账就只剩「每次部署时跑一次」。
#
# 这两件事以前靠人记着做。**记不住就是静默失效**：
# helper 起不来没人知道，对账停跑也没人知道。
#
# ## 为什么可以安全地清 marker
#
# marker 的语义是「迁移进行中，别让旧实现起来」。脚本只在事务
# `phase=complete` 之后才动手，那时新代码已经在跑，门禁已无必要。
# **对未完成的事务绝不清理** —— 那才是它该拦的时候。
#
# ## 为什么恢复 timer 不会引发重任务
#
# timer 是 `OnCalendar` + `Persistent=yes`：错过的时间点会补跑。
# 但 migration 期间本来就会手动跑一次同一个 oneshot service，
# 所以补跑不会造成额外的重活（实测：恢复后 service 没有立刻触发）。
#
# ## 用法
#
#   post-migration-cleanup.sh <transaction-id>
#
# 退出码恒为 0：收尾失败不该让一次已经成功的部署变成失败。
# 每一处跳过和恢复都会打一行日志，方便事后核对。

set -uo pipefail

TXN=${1:-}
STATE=/opt/zhihui-release/writer-state
BUDGET_TIMER=wb-budget-autoreconcile.timer

say() { printf '[cleanup] %s\n' "$*"; }

# ── 1. 确认事务真的完成了 ────────────────────────────────────────────
#
# 没给事务名、或事务还没 complete，就什么都不做。
# **这条判断是整个脚本的安全边界**：在上面两种情况下清 marker
# 等于在迁移进行中打开门禁。

if [ -z "$TXN" ]; then
    say "未指定事务，跳过（只在部署收尾时调用）"
    exit 0
fi

JOURNAL="$STATE/transactions/$TXN/active.json"
if [ ! -f "$JOURNAL" ]; then
    say "找不到事务日志 $JOURNAL，跳过"
    exit 0
fi

if ! grep -q '"phase":"complete"' "$JOURNAL" 2>/dev/null; then
    say "事务 $TXN 尚未 complete，跳过（此时门禁应当保持）"
    exit 0
fi

say "事务 $TXN 已 complete，开始收尾"

# ── 2. 清残留的启动门禁 marker ───────────────────────────────────────

restored=0
for marker in "$STATE"/*.start-blocked; do
    [ -e "$marker" ] || continue
    unit=$(basename "$marker" .start-blocked)
    if rm -f "$marker" 2>/dev/null; then
        say "已清除残留门禁：$unit（marker 曾存在，说明 release 未走到）"
        restored=$((restored + 1))
    else
        say "清除失败：$unit（保留，需人工确认）"
    fi
done
[ "$restored" -eq 0 ] && say "无残留门禁"

# daemon-reload 让 systemd 重新评估 ConditionPathExists。
# 不清缓存的话，marker 删了 unit 仍可能维持 skipped。
systemctl daemon-reload 2>/dev/null || say "daemon-reload 失败（不阻断）"

# ── 3. 恢复每日预算对账 timer ────────────────────────────────────────

if systemctl is-active --quiet "$BUDGET_TIMER" 2>/dev/null; then
    say "$BUDGET_TIMER 已在运行"
else
    if systemctl start "$BUDGET_TIMER" 2>/dev/null; then
        # 报一下下次触发时间 —— 这是「它真的排上了」的直接证据，
        # 比只写「已启动」有用。
        next=$(systemctl show "$BUDGET_TIMER" -p NextElapseUSecRealtime --value 2>/dev/null)
        say "已恢复 $BUDGET_TIMER，下次触发：${next:-未知}"
    else
        say "$BUDGET_TIMER 启动失败（不阻断部署，需人工确认）"
    fi
fi

# ── 4. 汇总当前状态 ──────────────────────────────────────────────────

say "主服务：$(systemctl is-active workbuddy-web.service 2>/dev/null || echo unknown)"
say "helper：$(systemctl is-active workbuddy-web-helper.service 2>/dev/null || echo unknown)（按需实例，inactive 正常）"
say "收尾完成"

exit 0
