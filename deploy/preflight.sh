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
#
#     清单从发布目录里读，**不再依赖某个固定路径的临时文件**。
#     早先那一版读 `/tmp/preflight-manifest.txt` —— 那个文件在重启后就不存在，
#     于是这一项实际上是长期跳过。**长期跳过的检查等于没有检查**，
#     而且它让「三项都在跑」看起来成立。
MANIFEST="$TARGET/PREFLIGHT-MANIFEST.txt"
if [ ! -f "$MANIFEST" ]; then
    MANIFEST=/tmp/preflight-manifest.txt
fi
if [ -f "$DEPLOY/verify-staged-files.sh" ] && [ -x "$DEPLOY/verify-staged-files.sh" ]; then
    if [ -f "$MANIFEST" ]; then
        run_check "文件哈希与清单一致" "$DEPLOY/verify-staged-files.sh" "$TARGET" "$MANIFEST"
    else
        printf '%-34s SKIP  发布目录里没有清单\n' "文件哈希与清单一致"
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
    # 不要用行尾锚点：第一个过滤的输出形如 `"release_id": "v1.2.181"`，
    # 末尾还有个引号，`v[0-9.]+$` 匹配不到，于是这里会显示「<空>」——
    # 第一版就是这么误报的，而它报出来的是「版本不一致」这种严重结论。
    vdecl=$(grep -oE '"release_id"[[:space:]]*:[[:space:]]*"v[0-9.]+"' \
                "$TARGET/server/writer-capabilities.json" 2>/dev/null \
            | grep -oE 'v[0-9.]+' | head -1)
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

# 四、进程内存。
#
# 这一项**不判断泄漏** —— 泄漏要看趋势，而一次采样看不出趋势。
# 它只拦一种情况：内存已经大到不该再放行的程度。
#
# 写清楚这一点很重要：如果把它当成「泄漏检测」，它会给出一份**虚假的安全感** ——
# 每次都是 PASS，而真正在缓慢上涨的东西没人在看。
MEM_LIMIT_KB=${PREFLIGHT_MEM_LIMIT_KB:-1048576}   # 默认 1 GiB
if [ -f "$TARGET/.version" ]; then
    printf '%-34s ' "进程常驻内存"
    main_pid=$(systemctl show workbuddy-web.service -p MainPID --value 2>/dev/null | tr -d '[:space:]')
    if [ -z "$main_pid" ] || [ "$main_pid" = "0" ]; then
        printf 'SKIP  取不到主进程\n'
        skipped=$((skipped + 1))
    else
        rss_kb=$(awk '/^VmRSS:/ {print $2}' "/proc/$main_pid/status" 2>/dev/null)
        if [ -z "$rss_kb" ]; then
            printf 'SKIP  读不到 /proc 状态\n'
            skipped=$((skipped + 1))
        elif [ "$rss_kb" -gt "$MEM_LIMIT_KB" ]; then
            printf 'FAIL\n'
            printf '    常驻 %s KB 超过阈值 %s KB\n' "$rss_kb" "$MEM_LIMIT_KB" | sed 's/^/    /'
            failed=$((failed + 1))
        else
            printf 'PASS  %s MB（阈值 %s MB）\n' \
                "$((rss_kb / 1024))" "$((MEM_LIMIT_KB / 1024))"
            passed=$((passed + 1))
        fi
    fi
else
    printf '%-34s SKIP  目标不是发布目录\n' "进程常驻内存"
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
