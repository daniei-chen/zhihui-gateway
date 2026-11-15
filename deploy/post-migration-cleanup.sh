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
# ## 关于 timer：它会自愈，这一步是兜底
#
# 实测（2026-11-15）：block() 停掉它之后，**systemd 会自己在重新评估时把它拉起来**
# —— 全仓没有任何代码启动它，我的命令也没有，但它确实自己变回了 active。
#
# 所以「对账停跑」这个说法并不成立，脚本里这一步只是**幂等兜底**：
# 不依赖自愈发生的时机，也不依赖下一次 daemon-reload。
#
# 另外 timer 是 `OnCalendar` + `Persistent=yes`，错过的时间点会补跑，
# 但 migration 期间本来就会跑一次同一个 oneshot service，所以补跑不构成额外重活。
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

# 验证门禁真的解除了：**看文件，不看 systemd 的状态缓存**。
#
# 2026-11-15 实测过两件事，都很反直觉：
#
#   1. marker 存在时 `systemctl start` **返回 0**，而服务根本没起来
#      （`ActiveState=inactive`）。**退出码是骗人的** —— 任何只看退出码的
#      收尾脚本都会认为一切正常，而服务其实一直没启动。
#
#   2. `ConditionResult` 是**上一次启动尝试**的结果缓存，删掉 marker 之后
#      **不会自动刷新**。上一版拿它当判据，于是刚清完文件就立即读，
#      读到旧值 `no`，每次部署都误报「仍被条件挡住」。
#
# 所以判据只能是文件本身：marker 不在了，门禁就没了。
# 至于 unit 实际能不能起，那是真的调用 `start` 之后看 `ActiveState` 的事，
# **不该由收尾脚本替它下结论** —— 下错结论比不下更糟。
for unit in workbuddy-web-helper.service workbuddy-web.service; do
    if [ -e "$STATE/$unit.start-blocked" ]; then
        say "$unit 门禁仍在（marker 未清除），需人工确认"
    else
        say "$unit 门禁已移除（marker 不存在）"
    fi
done

# ── 3. 清掉同版本的陈旧空事务 ────────────────────────────────────────
#
# ## 为什么会有垃圾
#
# `prepare` 常被乐观锁拒（「多了一个写入者」），而**被拒的尝试也会留下一个事务目录**。
# 2026-11-15 清点：26 个事务里 11 个是这种情况 ——
# 9 个空 phase（被拒）、2 个 prepared（成功但没 run）。
#
# 每个约 1 MB，单看不值一提，但**没有任何东西清理它们**，会随每次发布累积。
# 完整事务是 57 MB，那部分由人工按保留策略处理，不在这个脚本里做。
#
# ## 安全边界（三条，缺一不可）
#
# 1. **只动同一个版本号的**（`v12155a` 只清 `v12155*`），绝不跨版本扫
# 2. **只动空 phase 或 prepared 的**，complete 的一律保留（那是审计记录）
# 3. **绝不动本次事务**
#
# 版本号解析失败就整段跳过 —— 表达式会退化成 `*`，那等于清掉所有事务。
# **这一步宁可不动，也不能动错。**

version_tag=$(printf '%s' "$TXN" | sed -n 's/^\(v[0-9]\{1,\}\)[a-z].*/\1/p')
if [ -z "$version_tag" ]; then
    say "跳过陈旧事务清理（无法从 $TXN 解析版本号）"
else
    stale=0
    for d in "$STATE"/transactions/"${version_tag}"*/; do
        [ -d "$d" ] || continue
        name=$(basename "$d")
        [ "$name" = "$TXN" ] && continue
        phase=$(grep -oE '"phase":"[a-z-]+"' "$d/active.json" 2>/dev/null | head -1 | cut -d'"' -f4)
        case "$phase" in
            ""|prepared)
                if rm -rf "$d" 2>/dev/null; then
                    say "已清掉陈旧空事务：$name（phase=${phase:-空}）"
                    stale=$((stale + 1))
                else
                    say "清掉失败：$name（保留）"
                fi
                ;;
            *) : ;;   # complete 等，保留
        esac
    done
    [ "$stale" -eq 0 ] && say "无同版本陈旧空事务（$version_tag）"
fi

# ── 4. 恢复每日预算对账 timer ────────────────────────────────────────

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

# ── 5. 汇总当前状态 ──────────────────────────────────────────────────

say "主服务：$(systemctl is-active workbuddy-web.service 2>/dev/null || true)"
# `is-active` 对 inactive 的单元**输出 'inactive' 同时返回非 0**。
# 上一版写成 `$(...) || echo unknown`，于是两种情况都触发：
# 先打印 'inactive'，再追加一个 'unknown'，日志变成两行。
# 这里先把输出收进变量，空才回落。
helper_state=$(systemctl is-active workbuddy-web-helper.service 2>/dev/null || true)
say "helper：${helper_state:-unknown}（按需实例，inactive 正常）"
say "收尾完成"

exit 0
