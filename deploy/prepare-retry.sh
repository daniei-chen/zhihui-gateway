#!/bin/bash
# 带诊断与自清理的 prepare 包装。
#
# 为什么需要它：`prepare` 失败时入口只打印一句固定的
#   WRITER-MIGRATION-BLOCKED: inspect retained private journals; no automatic legacy fallback
# 真正的异常被那个 except 吞掉了。2026-11-14 的一次失败就因此查了三轮才定位：
# 闭环校验其实是好的，问题是一次失败留下了空的事务目录，重试时被当成
# 「保留的私有 journal」拒绝。
#
# 这个包装做三件事：
#   1. 失败时把可诊断的状态打出来（闭环、事务目录是否为空、是否有 active.json）；
#   2. 如果失败原因确实是「只有空目录、没有 active.json」，清掉重试一次；
#   3. 任何情况下都不删除有 active.json 的目录 —— 那是真事务，要走恢复流程。
set -euo pipefail

STATE=/opt/zhihui-release/writer-state
MIG=/tmp/mig.sh
CANDIDATE="${1:?usage: prepare-retry.sh <candidate-version> <transaction-name>}"
TRANSACTION="${2:?usage: prepare-retry.sh <candidate-version> <transaction-name>}"
DIR="$STATE/transactions/$TRANSACTION"

diagnose() {
    echo '--- 诊断 ---' >&2
    echo "候选版本: $CANDIDATE" >&2
    echo "事务名:   $TRANSACTION" >&2
    if [ -d "$DIR" ]; then
        echo "事务目录存在，内容：" >&2
        ls -la "$DIR" >&2 || true
    else
        echo "事务目录不存在（本次是全新事务）" >&2
    fi
    echo '闭环校验：' >&2
    if [ -x /tmp/run_verify.sh ]; then
        /tmp/run_verify.sh 2>/dev/null | head -4 >&2 || true
    else
        echo '  (/tmp/run_verify.sh 不存在，跳过)' >&2
    fi
}

# 空目录 = 上一次失败留下的壳：没有 active.json，也没有 initialized.json。
is_empty_shell() {
    [ -d "$DIR" ] || return 1
    [ -f "$DIR/active.json" ] && return 1
    # 除 . 与 .. 之外没有任何条目才算空壳
    [ -z "$(ls -A "$DIR" 2>/dev/null)" ]
}

echo "1/3 先跑一次 prepare"
if "$MIG" prepare "$CANDIDATE" "$TRANSACTION"; then
    echo "prepare 成功"
    exit 0
fi
echo "第一次 prepare 失败" >&2
diagnose

if is_empty_shell; then
    echo "2/3 事务目录是空壳（无 active.json、无内容），清掉后重试一次" >&2
    rm -rf "$DIR"
    echo "已删除空壳目录：$DIR" >&2
    if "$MIG" prepare "$CANDIDATE" "$TRANSACTION"; then
        echo "3/3 重试成功"
        exit 0
    fi
    echo "重试仍失败 —— 原因不是空壳目录，请按上面的诊断继续排查" >&2
    exit 1
fi

cat >&2 <<'EOF'
2/3 事务目录不是空壳（有 active.json 或有内容），**不自动清理**。
    那是一个真实事务，必须走恢复流程，不能当成残留删掉：
      /tmp/mig.sh status <transaction>
      /tmp/mig.sh run <transaction>      # 继续未完成的阶段
    只有在确认该事务已经彻底废弃后，才允许手工归档它的目录。
EOF
exit 1
