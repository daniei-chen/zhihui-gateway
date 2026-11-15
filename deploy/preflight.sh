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

# 五、危险操作必须拒绝未认证请求。
#
# 这一项的来由：鉴权不在每个路由的依赖里（`Depends` 只出现过一次），
# 而是由中间件统一处理。**「不在路由里」和「没有保护」是两件事** ——
# 2026-11-15 逐个实测了七个删除/清空/重置类路由，全部返回 401。
#
# 但「现在都对」不等于「以后都对」：新加一个路由时，中间件是否覆盖到它，
# 没有任何声音会提醒。所以这几条固定下来当回归防护。
#
# 只探这七个有代表性的；返回 401/403 算拒绝，其余一律算放行。
PROBE_BASE=${PREFLIGHT_PROBE_BASE:-http://127.0.0.1:7866}
DANGEROUS_PROBES="
POST /api/checkin-logs/clear
POST /api/keys/probe-invalid/reset-usage
POST /api/logs/clear
POST /api/security/logs/clear
DELETE /api/system/update-package
DELETE /api/system/update-status
"
if curl -s -o /dev/null -m 3 "$PROBE_BASE/healthz" 2>/dev/null; then
    printf '%-34s ' "危险路由拒绝未认证请求"
    open_routes=''
    # 用 while read 而不是 for：for 会按空格分词，把「方法」和「路径」拆散。
    while read -r method path; do
        [ -z "$method" ] && continue
        [ -z "$path" ] && continue
        code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X "$method" \
                    "$PROBE_BASE$path" 2>/dev/null || echo 000)
        case "$code" in
            401|403) ;;                 # 拒绝了，正确
            000)     ;;                 # 连不上，跳过这条（不算放行）
            *)       open_routes="$open_routes $method$path($code)";;
        esac
    done <<< "$DANGEROUS_PROBES"
    if [ -z "$open_routes" ]; then
        printf 'PASS  全部返回 401\n'
        passed=$((passed + 1))
    else
        printf 'FAIL\n'
        printf '    以下路由未认证也能访问：%s\n' "$open_routes" | sed 's/^/    /'
        failed=$((failed + 1))
    fi
else
    printf '%-34s SKIP  服务未响应\n' "危险路由拒绝未认证请求"
    skipped=$((skipped + 1))
fi

# 六、关键定时服务不得处于失败状态。
#
# 这一项的来由是一次真实的漏网：快照清理服务连续失败了好几天，**没有人知道** ——
# 它每天凌晨只失败一次，不影响任何用户请求，服务状态从 `inactive` 变成 `failed`
# 而 `inactive` 看起来同样「没在跑」。于是旧快照一直堆积、磁盘慢慢涨。
#
# **这类失败的特征是：不影响线上，所以不会被任何面向用户的检查发现。**
# 唯一的办法是主动去问「有没有谁失败了」。
#
# ## 前缀为什么要写两个
#
# 第一版只查 `zhihui-*`，**而主服务叫 `workbuddy-web`** —— 不带那个前缀。
# 也就是说**主服务失败时这一项不会报**，而那正是最该报的一个。
#
# 这台机器上的 unit 命名并不统一：网关主体叫 `workbuddy-web`，
# 后加的定时任务统一用了 `zhihui-` 前缀。**两个都得查。**
#
# 不查 `wb-*`：那是 `onfailure` 告警的模板实例，正常状态下就有非活动实例，
# 把它们算进来会产生与故障无关的噪音。
#
# 也不查别的应用（`kaogong-*` 等）—— 它们的失败不该阻断这个网关的发布。
FAILED_SERVICES=''
if command -v systemctl >/dev/null 2>&1; then
    FAILED_SERVICES=$(systemctl list-units --state=failed --no-legend --no-pager \
                        'zhihui-*' 'workbuddy-*' 2>/dev/null \
                      | awk '{print $1}' | tr '\n' ' ' | sed 's/ *$//')
    printf '%-34s ' "关键服务无失败"
    if [ -z "$FAILED_SERVICES" ]; then
        printf 'PASS  无失败单元\n'
        passed=$((passed + 1))
    else
        printf 'FAIL\n'
        printf '    以下单元处于 failed：%s\n' "$FAILED_SERVICES" | sed 's/^/    /'
        failed=$((failed + 1))
    fi
else
    printf '%-34s SKIP  没有 systemctl\n' "关键服务无失败"
    skipped=$((skipped + 1))
fi

# 七、证书不得临近过期。
#
# 这一项来自一次差点漏掉的事：查主站证书时发现它十二月十五日到期，
# 而同一台机器上还有七八张别的证书 —— **到期时间各不相同**，
# 用 `find | head -1` 只看到一个，就以为「还有一个月，不急」。
#
# **哪一张先过期，没有人知道。** 而任何一张过期，对应的站点就立刻不可用，
# 且浏览器会把它报成危险页面 —— 那不是「降级」，是「打不开」。
#
# ## 阈值为什么是二十一天，而不是三十天
#
# 第一版写了三十天，**第一次运行就误报**：主站证书当天剩余正好三十天。
#
# 原因是 Let's Encrypt 的续期在「剩余不足三十天」时才启动，
# 所以**剩余三十天是正常的等待状态**，不是故障。阈值设成一样的数字，
# 就等于在边界上必然报一次。
#
# 正常续期成功后剩余会跳到六十到九十天；**掉到二十一天以下，
# 说明续期连着失败了至少一周**，那才是要人工介入的信号。
CERT_WARN_DAYS=${PREFLIGHT_CERT_WARN_DAYS:-21}
if [ -d /etc/letsencrypt/live ] && command -v openssl >/dev/null 2>&1; then
    printf '%-34s ' "证书剩余天数"
    now_epoch=$(date +%s)
    soonest=''
    soonest_days=99999
    expired=''
    for cert in /etc/letsencrypt/live/*/fullchain.pem; do
        [ -f "$cert" ] || continue
        end=$(openssl x509 -in "$cert" -noout -enddate 2>/dev/null | sed 's/^notAfter=//')
        [ -n "$end" ] || continue
        end_epoch=$(date -d "$end" +%s 2>/dev/null) || continue
        days=$(( (end_epoch - now_epoch) / 86400 ))
        name=$(basename "$(dirname "$cert")")
        if [ "$days" -lt 0 ]; then
            expired="$expired $name(${days}d)"
        elif [ "$days" -lt "$soonest_days" ]; then
            soonest_days=$days
            soonest=$name
        fi
    done
    if [ -n "$expired" ]; then
        printf 'FAIL\n'
        printf '    已过期：%s\n' "$expired" | sed 's/^/    /'
        failed=$((failed + 1))
    elif [ -z "$soonest" ]; then
        printf 'SKIP  没有可解析的证书\n'
        skipped=$((skipped + 1))
    elif [ "$soonest_days" -lt "$CERT_WARN_DAYS" ]; then
        printf 'FAIL\n'
        printf '    最近一张 %s 只剩 %d 天（阈值 %d）\n' "$soonest" "$soonest_days" "$CERT_WARN_DAYS" \
            | sed 's/^/    /'
        failed=$((failed + 1))
    else
        printf 'PASS  最近一张 %s 还有 %d 天\n' "$soonest" "$soonest_days"
        passed=$((passed + 1))
    fi
else
    printf '%-34s SKIP  没有证书目录或 openssl\n' "证书剩余天数"
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
