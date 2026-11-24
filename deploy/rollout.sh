#!/bin/bash
# 一次完整的滚动发布：prepare → run → 收尾。
#
# ## 为什么需要它
#
# 这三步以前是分开做的，而且**第三步经常被忘掉**：
#
#   1. `prepare-retry.sh <ver> <txn>`   准备（首次常被乐观锁拒，需要重试）
#   2. `migration_entry.py run <txn>`   真正切换
#   3. `post-migration-cleanup.sh <txn>` 收尾（清门禁、恢复定时器）
#
# 第 3 步漏掉的后果是静默的：门禁文件留着，对应单元下次**连启动都不会尝试**
# （`ConditionPathExists=!` 不满足时 systemd 直接 skip，日志只有一句 skipped）。
#
# **一个必须靠人记住的步骤，等于没有这个步骤。** 这个脚本把它变成一条命令。
#
# ## 为什么不用 `set -e`
#
# 三步的失败处理不一样：prepare 被拒要提示重试，run 失败要保留事务供排查，
# 收尾失败**不该**让一次已经成功的发布变成失败。
# 统一 `set -e` 会把这三种情况压成同一个「退出 1」。
#
# ## 用法
#
#   rollout.sh <version> <transaction-id>
#
# 例：rollout.sh v1.2.152 v12152a-release-20261115
#
# 事务名建议带字母后缀（a/b/c），因为同一个版本重试时不能复用已完成的事务。

set -uo pipefail

VERSION=${1:-}
TXN=${2:-}
RELEASES=/opt/workbuddy-manager/releases
STATE=/opt/zhihui-release/writer-state
GUARD=/opt/zhihui-release/writer-guard
CACHE=/opt/zhihui-release/python-cache
PYTHON=/usr/bin/python3

say() { printf '[rollout] %s\n' "$*"; }
die() { printf '[rollout] 失败：%s\n' "$*" >&2; exit 1; }

[ -n "$VERSION" ] || die "用法：rollout.sh <version> <transaction-id>"
[ -n "$TXN" ] || die "用法：rollout.sh <version> <transaction-id>"

ROOT="$RELEASES/$VERSION"
[ -d "$ROOT" ] || die "版本目录不存在：$ROOT"
[ -x "$ROOT/deploy/prepare-retry.sh" ] || die "缺 prepare-retry.sh"
[ -x "$ROOT/deploy/post-migration-cleanup.sh" ] || die "缺 post-migration-cleanup.sh"
[ -x "$ROOT/deploy/install-resource-budget.sh" ] || die "缺 install-resource-budget.sh"
[ -f "$STATE/transactions/$TXN/active.json" ] || true   # 首次运行时还不存在，正常

JOURNAL="$STATE/transactions/$TXN/active.json"

# ── 等事务进入指定阶段 ────────────────────────────────────────────────
#
# `run` 是长任务（实测 20 秒到 2 分钟，取决于滚动重启用多久）。
# 不能只看命令退出码：那要等到最后，期间什么都看不到。
#
# 判据用 journal 里的 phase，而不是猜时间。
wait_phase() {
    local want=$1 limit=${2:-360} waited=0
    while [ "$waited" -lt "$limit" ]; do
        if grep -q "\"phase\":\"$want\"" "$JOURNAL" 2>/dev/null; then
            return 0
        fi
        # failure 非 null 说明事务已经失败，不必等满超时。
        if grep -qE '"failure":"[^n]' "$JOURNAL" 2>/dev/null; then
            say "事务报告失败，停止等待"
            return 1
        fi
        sleep 3
        waited=$((waited + 3))
    done
    say "等待 $want 超时（${limit}s）"
    return 2
}

# ── 1. prepare ────────────────────────────────────────────────────────
#
# **这里需要两层重试，不是一层。**
#
# `prepare-retry.sh` 内部会重试 4 次，但实测下来**一整轮 4 次全部被拒是常态**
# （2026-11-15 连续几轮都是这样）：拒绝原因是「多了一个写入者」，
# 而那个写入者是看门狗的一轮巡视 —— 它并不会因为你在 4 次内重试就消失。
#
# **过去的做法是「人工再跑一次」，第二次通常就过了。** 这个脚本第一版只调一次，
# 自举验证时正是这样失败的：prepare 4 连拒 → 直接中止。
#
# 所以外层再套几轮，每轮之间留出让看门狗跑完的间隔。
prepare_with_outer_retry() {
    local attempt=1 max=4
    while [ "$attempt" -le "$max" ]; do
        say "prepare 第 $attempt/$max 轮"
        if "$ROOT/deploy/prepare-retry.sh" "$VERSION" "$TXN"; then
            return 0
        fi
        if [ "$attempt" -eq "$max" ]; then
            return 1
        fi
        say "第 $attempt 轮未成功，等 15 秒让写入者退出后重试"
        sleep 15
        attempt=$((attempt + 1))
    done
    return 1
}

# The helper is inactive before this point. Installing its finite resource
# ceiling cannot restart the serving primary.
say "步骤 0/4 安装 helper 资源上限"
"$ROOT/deploy/install-resource-budget.sh" || die "helper 资源上限安装失败"
say "helper 资源上限已复核"

say "步骤 1/4 prepare：$VERSION / $TXN"
if ! prepare_with_outer_retry; then
    # 到这一步说明外层也跑完了，是别的原因。
    # 不引用函数里的 $max —— 它是 local，在这里是空的。
    die "prepare 未成功（外层重试已用尽），事务保留：$TXN"
fi
say "prepare 成功"

# ── 2. run ────────────────────────────────────────────────────────────

say "步骤 2/4 run（滚动切换，可能 1–2 分钟）"
setsid "$PYTHON" -I -S -B -X pycache_prefix="$CACHE" \
    "$GUARD/migration_entry.py" run "$TXN" \
    > "/tmp/rollout-run-$TXN.log" 2>&1 < /dev/null &

run_pid=$!
say "run 已启动（pid=$run_pid），日志：/tmp/rollout-run-$TXN.log"

if wait_phase complete 360; then
    say "run 完成（phase=complete）"
else
    rc=$?
    # 进程还在就别杀它 —— 事务可能有补偿步骤要跑完。
    say "run 未在预期内完成（rc=$rc），进程仍在则保留，日志：/tmp/rollout-run-$TXN.log"
    die "发布中止，事务保留供排查：$TXN"
fi

# ── 3. 收尾 ───────────────────────────────────────────────────────────
#
# 事务已经 complete 时新版本正在服务，因此这里失败不自动回滚代码；
# 但命令必须非零，让上层停止宣称发布成功并进入人工检查。

say "步骤 3/4 收尾"
"$ROOT/deploy/post-migration-cleanup.sh" "$TXN"
cleanup_rc=$?
if [ "$cleanup_rc" -ne 0 ]; then
    die "版本已切换，但收尾未完成（rc=$cleanup_rc）；新流量保持，暂停发布并核对门禁/对账"
fi
say "收尾完成"

# ── 4. 核对：新代码是不是真的在跑 ──────────────────────────────────────
#
# 事务报 complete 只说明**迁移流程**走完了，不直接说明**进程加载了新代码**。
#
# 2026-11-15 验证过一次两者一致（`current` 指向新版本、进程 cwd 也在新版本、
# 启动时间与部署时间吻合），但那次是**手工查的**。
# 手工查过的东西不写下来，下次还得靠人想起。
#
# 这里把它变成每次自动核对。判据是进程的 cwd ——
# 那比 `current` 符号链接更直接：符号链接指对了，进程可能还在旧目录里跑。
say "步骤 4/4 核对运行时"
target=$(readlink -f "$RELEASES/current" 2>/dev/null || true)
expected_version=$(cat "$RELEASES/current/.version" 2>/dev/null || true)
[ -n "$target" ] && [ -n "$expected_version" ] || die "无法确定 current 发布身份"
say "current -> $target（$expected_version）"

main_pid=$(systemctl show workbuddy-web.service -p MainPID --value 2>/dev/null || true)
[ -n "$main_pid" ] && [ "$main_pid" != "0" ] || die "主服务没有运行中的 MainPID"
proc_cwd=$(readlink -f "/proc/$main_pid/cwd" 2>/dev/null || true)
say "主进程 $main_pid cwd = ${proc_cwd:-未知}"
[ "$proc_cwd" = "$target" ] || die "进程 cwd 与 current 不一致，可能仍在执行旧代码"

resolved_version=$(curl -fsS --max-time 5 http://127.0.0.1:7866/api/release \
    | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("release_id",""))')
[ "$resolved_version" = "$expected_version" ] \
    || die "运行时 release_id=$resolved_version，期望=$expected_version"
http_code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
    http://127.0.0.1:7866/v1/models || true)
case "$http_code" in 200|401|403) ;; *) die "真实推理入口不可用（/v1/models HTTP $http_code）" ;; esac
curl -fsS --max-time 5 http://127.0.0.1:7866/api/readyz >/dev/null \
    || die "候选版本 readiness 未通过"

say "发布结束：$expected_version"
say "主服务：$(systemctl is-active workbuddy-web.service 2>/dev/null || true)"

exit 0
