#!/bin/bash
# 发布前检查：一次跑完所有能自动化的校验。
#
# ## 为什么需要
#
# 仓库里现在有三个检查脚本，分散在三处：
#
#   deploy/verify-staged-files.sh    组装目录里的文件是否与期望哈希一致
#   deploy/check-route-coverage.sh   前端调用的路径后端是否都注册了
#   deploy/build-release.sh          从提交组装发布（内含三道校验）
#
# **要记住三个名字、三个参数顺序、三个退出码含义。** 记不住就会跳过其中一个 ——
# 而跳过的那一个恰好可能是这次会失败的那个。
#
# 这个入口把它们串起来：**跑一次，全部有结果，任何一项失败就不继续。**
#
# ## 它不做什么
#
# - **不组装发布**（那是 build-release.sh 的事，它要参数）
# - **不部署**（那是 rollout.sh 的事）
# - **不改任何东西**：所有检查都是只读的
#
# ## 用法
#
#     preflight.sh [目标目录]
#
# 目标目录省略时检查当前发布（`releases/current`）。
# 退出码：0 全部通过；1 有检查失败；2 用法或环境错误。
set -uo pipefail

TARGET=${1:-/opt/workbuddy-manager/releases/current}

if [ ! -d "$TARGET" ]; then
    echo "FAIL: 目录不存在: $TARGET" >&2
    exit 2
fi

DEPLOY="$TARGET/deploy"
passed=0
failed=0
skipped=0

run_check() {
    local name=$1; shift
    printf '%-34s ' "$name"
    local out rc
    out=$("$@" 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ]; then
        # 只回显最后一行，避免把整段输出刷进发布日志
        printf 'PASS  %s\n' "$(printf '%s' "$out" | tail -1)"
        passed=$((passed + 1))
    elif [ "$rc" -eq 2 ]; then
        # 退出码 2 语义是「没法检查」，与「检查失败」必须分开
        printf 'SKIP  %s\n' "$(printf '%s' "$out" | tail -1)"
        skipped=$((skipped + 1))
    else
        printf 'FAIL\n'
        printf '%s\n' "$out" | tail -6 | sed 's/^/    /'
        failed=$((failed + 1))
    fi
}

echo "发布前检查: $TARGET"
echo ""

# 一、发布目录自身的完整性。
#     verify-staged-files.sh 需要一份哈希清单；没有清单时它退 2，这里记为跳过而不是失败。
if [ -f "$DEPLOY/verify-staged-files.sh" ] && [ -x "$DEPLOY/verify-staged-files.sh" ]; then
    if [ -f /tmp/preflight-manifest.txt ]; then
        run_check "文件哈希与清单一致" "$DEPLOY/verify-staged-files.sh" "$TARGET" /tmp/preflight-manifest.txt
    else
        printf '%-34s SKIP  没有清单（/tmp/preflight-manifest.txt）\n' "文件哈希与清单一致"
        skipped=$((skipped + 1))
    fi
else
    printf '%-34s SKIP  脚本缺失或不可执行\n' "文件哈希与清单一致"
    skipped=$((skipped + 1))
fi

# 二、前后端契约。
#     这一项需要前端产物；产物不存在时它自己退 2。
if [ -x "$DEPLOY/check-route-coverage.sh" ]; then
    run_check "前端路径后端全覆盖" "$DEPLOY/check-route-coverage.sh" "$TARGET/web/out" "$TARGET/server"
else
    printf '%-34s SKIP  脚本缺失或不可执行\n' "前端路径后端全覆盖"
    skipped=$((skipped + 1))
fi

# 三、版本一致性：版本文件与能力声明必须指同一个版本。
if [ -f "$TARGET/.version" ] && [ -f "$TARGET/server/writer-capabilities.json" ]; then
    printf '%-34s ' "版本号与能力声明一致"
    vfile=$(tr -d '[:space:]' < "$TARGET/.version")
    vdecl=$(grep -oE '"release_id"[[:space:]]*:[[:space:]]*"v[0-9.]+"' \
                "$TARGET/server/writer-capabilities.json" 2>/dev/null \
            | grep -oE 'v[0-9.]+$' | tr -d '"')
    if [ "$vfile" = "$vdecl" ]; then
        printf 'PASS  %s\n' "$vfile"
        passed=$((passed + 1))
    else
        printf 'FAIL\n'
        printf '    版本文件 %s 与能力声明 %s 不一致\n' "${vfile:-<空>}" "${vdecl:-<空>}" | sed 's/^/    /'
        failed=$((failed + 1))
    fi
else
    printf '%-34s SKIP  版本文件或能力声明缺失\n' "版本号与能力声明一致"
    skipped=$((skipped + 1))
fi

echo ""
echo "PREFLIGHT passed=$passed failed=$failed skipped=$skipped"

# 跳过不等于通过：如果一项都没跑成，那是环境问题，不该报成功。
if [ "$failed" -gt 0 ]; then
    exit 1
fi
if [ "$passed" -eq 0 ]; then
    echo "FAIL: 一项检查都没能执行，环境不对" >&2
    exit 2
fi
exit 0
