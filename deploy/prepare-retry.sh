#!/bin/bash
# 带诊断与自动等待的 prepare 包装。
#
# ## 为什么需要它
#
# `prepare` 的写入者校验要求：**进程列表必须恰好只有主服务一个**。
#
#     rows = self.inventory.processes()
#     if [r['pid'] for r in rows] != [identity['pid']]:
#         raise Rejected('unknown or additional writer blocks first migration')
#
# 而系统里有一个独立的就绪看门狗（`zhihui-steward-watchdog.timer`），每约 80 秒
# 触发一次，每次运行约 18 秒，期间会跑一次 `TRUSTED-PREFLIGHT-OK`（也是一个
# python 进程）。
#
# **`prepare` 撞进那 18 秒窗口就会被拒。** 等它结束再试就好。
#
# 这正是 v1.2.45 / v1.2.49 / v1.2.53 三次发布都在 `prepare` 卡一次的原因 ——
# 当时入口把异常吞了，只能靠重试蒙过去，还被误判成「空壳事务目录」。
#
# ## 这个脚本做什么
#
# 1. 如果看门狗正在跑，先等它结束（这是最可能的原因，先避开它）；
# 2. 跑 prepare；
# 3. 失败时把真实错误打出来（入口现在会输出原因）；
# 4. 只在**确认是写入者冲突**时等待并重试，其它错误如实报告、绝不盲目重试。
#
# ## 刻意不做的事
#
# 不清理任何事务目录。早先那版会删「空壳目录」，但那是基于错误结论 —— 系统里
# 一直留着几个空壳目录，而之后的多次 prepare 都成功，证明它不挡路。删它没有
# 收益，还可能在判断失误时删掉真事务。
set -euo pipefail

MIG=/tmp/mig.sh
WATCHDOG=zhihui-steward-watchdog.service
CANDIDATE="${1:?usage: prepare-retry.sh <candidate-version> <transaction-name>}"
TRANSACTION="${2:?usage: prepare-retry.sh <candidate-version> <transaction-name>}"
MAX_ATTEMPTS=4

# 等看门狗这一轮结束，以及它拉起的预检进程退出。
#
# ## 为什么要查三种东西
#
# 看门狗的服务本身只跑约 3 秒，但它**内部**会拉起一个完整性预检进程跑约 15 秒，
# 合计约 18 秒。只查服务状态会漏掉后半段 —— 2026-11-14 实测：只查服务状态时，
# 脚本连续重试三次都撞在预检进程上，第四次才过。
#
# 所以要同时看：
#   · systemd 说这个服务在不在活动状态；
#   · 看门狗自己的进程还在不在（状态切换的间隙）；
#   · 它拉起的预检进程还在不在（这才是占满 18 秒窗口的那个）。
#
# 匹配预检进程时用变量拼出模式串，避免本脚本自己的命令行里出现完整字面量而匹配到
# 自身（`grep -v grep` 挡不住 `bash -c` 里的同一串）。
watchdog_running() {
    systemctl is-active --quiet "$WATCHDOG" 2>/dev/null && return 0
    local base='trusted_bootstrap'
    ps -eo args= 2>/dev/null | grep -F "$base.py --root" | grep -qv 'grep' && return 0
    local guard='steward_watchdog'
    ps -eo args= 2>/dev/null | grep -F "$guard.py" | grep -qv 'grep' && return 0
    return 1
}

wait_for_watchdog() {
    local waited=0
    local limit=90
    local was_busy=0
    while [ "$waited" -lt "$limit" ]; do
        if ! watchdog_running; then
            # 再确认一次：进程刚退出的瞬间 systemd 可能还没更新状态。
            sleep 1
            if ! watchdog_running; then
                [ "$was_busy" -eq 1 ] && echo "    看门狗与预检进程都已退出（ waited ${waited}s ）"
                return 0
            fi
        fi
        was_busy=1
        sleep 1
        waited=$((waited + 1))
    done
    echo "    等待超过 ${limit}s 仍在运行，继续尝试" >&2
    return 0
}

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    echo "[$attempt/$MAX_ATTEMPTS] 先确认看门狗不在运行"
    wait_for_watchdog

    echo "[$attempt/$MAX_ATTEMPTS] 执行 prepare：$CANDIDATE / $TRANSACTION"
    set +e
    OUTPUT=$("$MIG" prepare "$CANDIDATE" "$TRANSACTION" 2>&1)
    CODE=$?
    set -e
    echo "$OUTPUT"

    if [ "$CODE" -eq 0 ]; then
        echo "prepare 成功"
        exit 0
    fi

    # 只有明确是「多了一个写入者」才值得等待重试；其它原因重试只是浪费时间，
    # 还可能掩盖真问题。
    case "$OUTPUT" in
        *"unknown or additional writer blocks first migration"*)
            echo "    原因：有额外写入者占用（通常是看门狗那一轮），等它退出后重试" >&2
            ;;
        *"active migration must be recovered or explicitly archived"*)
            echo "    原因：该事务已存在未完成记录 —— 这是真事务，必须走恢复流程" >&2
            echo "    不要删目录。查看：$MIG status $TRANSACTION" >&2
            exit 1
            ;;
        *)
            echo "    原因不在已知的可重试范围，停止。完整输出见上。" >&2
            exit 1
            ;;
    esac
done

echo "重试 ${MAX_ATTEMPTS} 次仍失败。最后一次的输出见上。" >&2
exit 1
