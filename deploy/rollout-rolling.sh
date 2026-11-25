#!/usr/bin/env bash
# 智汇网关零停机滚动发布：切换发布指针之后要做的事，按正确顺序执行并自证。
#
# 顺序是有讲究的，颠倒就白做：
#   1) 先让中继跑上新代码（它含启动空窗重试）—— 此时网关还在服务旧代码，影响面为零
#   2) 再重启网关 —— 那 6 秒空窗里中继会等它回来
# 若先重启网关再重启中继，中断依旧会发生，只是多了一跳。
#
# 用法：
#   ./rollout-rolling.sh --check                 # 只体检，不动任何东西
#   ./rollout-rolling.sh 1.5.59                  # 常态发布（中继代码没变）
#   ./rollout-rolling.sh 1.5.59 --relay-changed  # 中继代码也变了，需要一并重启
#
# 退出码：0 成功；1 前置条件不满足；2 重启后仍有 5xx（已上线但没做到零中断）。
set -uo pipefail

# 站点配置是 0600 root，重启服务也要 root。所以整个脚本按 root 跑，
# 不做"只读模式降级"—— 半套检查比没有检查更容易误导。
if [ "$(id -u)" -ne 0 ]; then
    printf '需要 root（要读 0600 的 nginx 配置、要重启服务）：请用 sudo 运行\n' >&2
    exit 1
fi

SITE=/etc/nginx/sites-enabled/zhihui.kaogong.art
SNIPPET=/etc/nginx/snippets/zh-relay-inference.location.conf
MAPFILE=/etc/nginx/conf.d/zhihui-relay-upstream.conf
RELEASES=/opt/workbuddy-manager/releases
ACCESS_LOG=/var/log/nginx/access.log
RELAY=zhihui-rolling-relay
GATEWAY=workbuddy-web
#: 脚本自身所在目录 —— 配置校验工具与它同目录，不能靠调用者的 cwd 找。
SELF_DIR=$(cd "$(dirname "$0")" && pwd)

step() { printf '\n== %s\n' "$*"; }
die() { printf '!! %s\n' "$*" >&2; exit 1; }

check_prereqs() {
    step '体检'
    systemctl cat "$RELAY.service" >/dev/null 2>&1 || die "中继服务不存在：$RELAY"
    grep -q 'zhihui_relay_upstream' "$MAPFILE" 2>/dev/null \
        || die "反代没有定义 \$zhihui_relay_upstream（$MAPFILE）"
    [ -f "$SNIPPET" ] || die "中继片段未安装：$SNIPPET"
    grep -q 'zh-relay-inference' "$SITE" || die "站点配置没有接中继：$SITE"
    systemctl is-active --quiet "$RELAY" || die "中继没在运行：$RELAY"
    printf '   ok 中继在跑、map 已定义、片段已安装、站点已接线\n'
}

current_release() {
    local target
    target=$(readlink -f "$RELEASES/current" 2>/dev/null) || return 1
    basename "$target"
}

restart_relay() {
    step "重启中继（加载含空窗重试的新代码）"
    printf '   注意：中继重启约 1-2 秒，期间经中继的推理请求会失败。\n'
    printf '   这是它唯一会中断的地方，而中继代码本身很少变。\n'
    systemctl restart "$RELAY" || die "重启中继失败"
    local pid
    for _ in $(seq 1 20); do
        sleep 0.5
        pid=$(systemctl show "$RELAY" -p MainPID --value 2>/dev/null)
        if [ -n "$pid" ] && [ "$pid" != 0 ] \
            && curl -sf -o /dev/null --max-time 2 http://127.0.0.1:7867/v1/models 2>/dev/null; then
            printf '   中继已就绪（pid %s）\n' "$pid"
            return 0
        fi
        # 401 也算活着：它证明中继在转发，只是我们没带令牌
        if curl -s -o /dev/null --max-time 2 -w '%{http_code}' \
            http://127.0.0.1:7867/v1/models 2>/dev/null | grep -q '^401$'; then
            printf '   中继已就绪（pid %s）\n' "$pid"
            return 0
        fi
    done
    die '中继重启后 10 秒仍未就绪'
}

verify_runtime_config() {
    step '用这一版的代码校验当前配置'
    # 2026-11-23 23:25 真实踩过：drop-in 先加了 WB_TRAFFIC_TAP=1，而后端代码的
    # schema 还不认识它，于是每次启动都被拒、每 3 秒重启一次，直到
    # StartLimitBurst=10 才停 —— 约 8 分钟完全不可用。
    #
    # 这件事**在重启前就能查出来**：校验读的是「当前环境」与「这一版的 schema」，
    # 两者在重启前都是已知的。原流程只是没人去比。
    local vdir
    vdir=$(readlink -f "$RELEASES/current" 2>/dev/null) || vdir=''
    [ -n "$vdir" ] || die '无法解析发布指针，不敢继续'
    [ -x "$SELF_DIR/verify-runtime-config.py" ] \
        || die "缺少 $SELF_DIR/verify-runtime-config.py"
    "$SELF_DIR/verify-runtime-config.py" "$vdir" "$GATEWAY" \
        || die '这一版的配置校验不通过 —— 重启会陷入崩溃重启循环，已中止（服务仍在跑旧代码）'
}

restart_gateway() {
    local version="$1" before after ready=0
    # 参数可能带 `v` 前缀（`v1.5.92`）也可能不带（`1.5.92`）。
    #
    # `/api/release` 里两个字段的写法不一样：`version` 是 `1.5.92`，
    # `release_id` 是 `v1.5.92`。下面比对的是 `version`，所以必须先去掉
    # 前缀，否则 `v$version` 会拼出 `vv1.5.92`，永远匹配不上。
    #
    # 2026-11-25 发 v1.5.92 时就是这么误报的：服务已经正常返回
    # `{"version":"1.5.92",...}`，脚本却报「重启后 60 秒仍未返回 vv1.5.92」
    # 然后 die —— 发布本身是成功的，但后面的 5xx 统计没跑，退出码也没给，
    # 看起来就像一次失败的发版。调用方传哪种写法都该能用。
    version="${version#v}"
    before=$(wc -l < "$ACCESS_LOG" 2>/dev/null || echo 0)
    step "重启网关（空窗期间由中继重试兜住）"
    systemctl restart "$GATEWAY" || die "重启网关失败"

    for _ in $(seq 1 40); do
        sleep 1.5
        if curl -sf --max-time 3 http://127.0.0.1:7866/api/release 2>/dev/null \
            | grep -q "\"version\":\"$version\""; then
            ready=1
            break
        fi
    done
    [ "$ready" = 1 ] || die "重启后 60 秒仍未返回 v$version，请检查 $GATEWAY"

    after=$(wc -l < "$ACCESS_LOG" 2>/dev/null || echo 0)
    step "重启窗口内的 5xx（只算推理流量）"
    local failures
    # 只统计 `/v1/` 的 5xx。
    #
    # `/var/log/nginx/access.log` 是**整台机器的共享日志**，同一个文件里还有
    # 控制台页面、`/_next` 静态资源、`/dsh-login`（属于另一个站点）等等。
    # 2026-11-25 实测：这份日志里的 5xx 有 8 条属于 `/v1/`，另有 4 条不是 ——
    # 那 4 条跟推理无关，却会被算进「本次发版丢了几个请求」。
    #
    # 判据必须只覆盖本次发布真正影响的流量：推理请求。否则一次无关站点的
    # 5xx 就能把「零中断」判成失败，而**误报的判据比没有判据更糟** ——
    # 下次真出问题时它不再被相信。
    failures=$(awk -v s="$before" 'NR>s && $9 ~ /^5/ && $7 ~ /^\/v1\//' "$ACCESS_LOG" 2>/dev/null | wc -l)
    if [ "$failures" -eq 0 ]; then
        printf '   0 条 —— 零中断\n'
    else
        printf '   %s 条（下面这些）：\n' "$failures"
        awk -v s="$before" 'NR>s && $9 ~ /^5/ && $7 ~ /^\/v1\//' "$ACCESS_LOG" 2>/dev/null \
            | awk '{print "   ", $4, $9, $7}' | head -10
        printf '\n   说明：中继的重试只覆盖「连接阶段失败、累计 9 秒」。\n'
        printf '   若失败集中在重启第一秒之外，多半是中继没跑上新代码，\n'
        printf '   或这一段流量绕过了中继（站点里中继 location 缺失）。\n'
    fi

    step '页面与就绪探针'
    local url code
    for url in / /portal/ /keys/ /dashboard/ /members/; do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://zhihui.kaogong.art$url" 2>/dev/null)
        printf '   %-12s %s\n' "$url" "$code"
        [ "$code" = 200 ] || printf '   ^^ 这个页面不是 200\n'
    done
    printf '   readyz       %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:7866/readyz 2>/dev/null)"

    [ "$failures" -eq 0 ] || return 2
    return 0
}

main() {
    local version='' relay_changed=0 check_only=0 arg
    for arg in "$@"; do
        case "$arg" in
            --check) check_only=1 ;;
            --relay-changed) relay_changed=1 ;;
            -*) die "未知参数：$arg" ;;
            *) version="$arg" ;;
        esac
    done

    check_prereqs
    if [ "$check_only" = 1 ]; then
        step '发布指针'
        printf '   current -> %s\n' "$(current_release || echo '(无法解析)')"
        # --check 也跑配置校验：这是只读的，而且正是「体检」该报的东西。
        verify_runtime_config
        step '体检通过，未做任何改动'
        return 0
    fi

    [ -n "$version" ] || die '用法：rollout-rolling.sh <版本号> [--check|--relay-changed]'
    local pointing
    pointing=$(current_release || echo '')
    case "$pointing" in
        *"$version"*) printf '\n   ok 发布指针已指向 %s\n' "$pointing" ;;
        *) die "发布指针还是 $pointing，请先 release.py activate $version" ;;
    esac

    if [ "$relay_changed" = 1 ]; then
        restart_relay
    else
        step '跳过中继重启（中继代码未变）'
        printf '   中继代码有改动时请加 --relay-changed，否则它会继续跑旧代码，\n'
        printf '   而旧代码没有空窗重试 —— 那样这次发布仍会打断在途调用。\n'
    fi

    # 重启前先确认这一版能接受当前配置 —— 2026-11-23 那次 8 分钟崩溃重启
    # 就是这一步的缺失造成的。检查在重启之前，所以失败时服务仍在跑旧代码。
    verify_runtime_config

    restart_gateway "$version"
    local status=$?

    step '完成'
    [ "$status" -eq 0 ] && printf '   v%s 已上线，重启窗口零中断\n' "$version"
    return "$status"
}

main "$@"
