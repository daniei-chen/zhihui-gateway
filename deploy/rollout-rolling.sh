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
#   ./rollout-rolling.sh 1.5.59                  # 常态发布（默认蓝绿：候选实例先接管流量）
#   ./rollout-rolling.sh 1.5.59 --relay-changed  # 中继代码也变了，需要一并重启
#   ./rollout-rolling.sh 1.5.59 --no-blue-green  # 没有候选实例时，退回「重启 + 中继重试」
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
# 判据读网关自己记的请求终态，而不是 nginx 的共享 access.log。
# 2026-11-26 实测：那份日志里混着另一个站点（zhijing.kaogong.art）转发到境外
# 上游的 7 条 502，时间正好落在本次重启窗口里，被算成了「本次发版丢了 7 个
# 请求」；而真正进入网关的 8 条请求全部 200。误报的判据比没有判据更糟。
DB=/opt/workbuddy-manager/data/manager.db
RELAY=zhihui-rolling-relay
GATEWAY=workbuddy-web
#: 候选实例：同一份发布树、同一份数据，端口 7865、不跑后台作业
#: （WB_BACKGROUND_JOBS=0）。发布前先用它确认「这一版能起来」。
HELPER=workbuddy-web-helper
CANDIDATE_PORT=7865
#: 中继按这个文件决定把推理流量发给哪个后端实例；写它就能改流量去向。
#: 中继在每次请求时重读，改完立即生效，不需要重启它。
DATA_DIR=/opt/workbuddy-manager/data
ROUTE_FILE=/opt/workbuddy-manager/data/relay-route.json
#: 每次发布的结果追加一行到这里（JSONL）。发版中途断了、终端超时了，也能事后查。
#: 2026-11-26 就发生过：通过远程会话发版，命令 30 秒超时，脚本自己的统计输出全丢，
#: 只能事后翻请求终态表反推窗口数据 —— 结论没留痕，等于没有结论。
ROLLOUT_LOG=/opt/workbuddy-manager/data/rollout-log.jsonl
RECORDED=0
CHECK_ONLY=0
LOG_VERSION='unknown'
LOG_MODE='重启'
STARTED_AT=$(date '+%Y-%m-%dT%H:%M:%S%z')
#: 脚本自身所在目录 —— 配置校验工具与它同目录，不能靠调用者的 cwd 找。
SELF_DIR=$(cd "$(dirname "$0")" && pwd)

step() { printf '\n== %s\n' "$*"; }
die() { printf '!! %s\n' "$*" >&2; exit 1; }

#: 把这次发布的结果追一行进 ROLLOUT_LOG。
#:
#: 为什么落盘：发布窗口的结论（几条请求、几个 5xx）此前只活在脚本的 stdout 里。
#: 2026-11-26 09:45 那次走远程会话发版，命令 30 秒超时，stdout 全丢，只能事后
#: 翻 request_outcomes 反推 —— 而且「这次用的蓝绿还是重启」「脚本是正常结束还是
#: 中途退出」根本没法反推。巡检也就能看到「最近一次发版」到底成没成。
record_rollout() {
    local version="$1" mode="$2" requests="$3" failures="$4" status="$5" note="$6"
    [ "$RECORDED" = 1 ] && return 0
    RECORDED=1
    printf '{"started_at":"%s","ended_at":"%s","version":"%s","mode":"%s","requests":%s,"failures":%s,"exit":%s,"note":"%s"}\n' \
        "$STARTED_AT" "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$version" "$mode" \
        "${requests:-null}" "${failures:-null}" "$status" "$note" \
        >> "$ROLLOUT_LOG" 2>/dev/null \
        || printf '   注意：发布记录没写进 %s（不影响发布本身）\n' "$ROLLOUT_LOG"
}

#: 中途 die 或被强杀时也留一条 —— 「这次发布没走完」本身就是结论。
on_exit() {
    local code=$?
    if [ "$RECORDED" = 0 ] && [ "$CHECK_ONLY" != 1 ]; then
        record_rollout "$LOG_VERSION" "$LOG_MODE" '' '' "$code" '未走到统计环节（脚本中途退出）'
    fi
}
trap on_exit EXIT

db_scalar() {
    sqlite3 -readonly "$DB" "$1" 2>/dev/null | head -1
}

db_rows() {
    sqlite3 -readonly -separator ' | ' "$DB" "$1" 2>/dev/null
}

check_prereqs() {
    step '体检'
    systemctl cat "$RELAY.service" >/dev/null 2>&1 || die "中继服务不存在：$RELAY"
    grep -q 'zhihui_relay_upstream' "$MAPFILE" 2>/dev/null \
        || die "反代没有定义 \$zhihui_relay_upstream（$MAPFILE）"
    [ -f "$SNIPPET" ] || die "中继片段未安装：$SNIPPET"
    grep -q 'zh-relay-inference' "$SITE" || die "站点配置没有接中继：$SITE"
    systemctl is-active --quiet "$RELAY" || die "中继没在运行：$RELAY"
    command -v sqlite3 >/dev/null 2>&1 \
        || die '缺少 sqlite3：无法统计重启窗口的请求终态'
    [ -r "$DB" ] || die "读不到请求终态表：$DB"
    printf '   ok 中继在跑、map 已定义、片段已安装、站点已接线\n'
}

current_release() {
    local target
    target=$(readlink -f "$RELEASES/current" 2>/dev/null) || return 1
    basename "$target"
}

switch_inference() {
    local target="$1" port backup
    case "$target" in
        relay) port=7867 ;;
        direct) port=7866 ;;
        *) die "未知的推理入口：$target" ;;
    esac
    # nginx 的 map 是静态文本，改推理入口只能改写这一行再 reload。
    # reload 是平滑的：旧 worker 把在途的流处理完才退出，新连接立刻按新配置走 ——
    # 所以切入口这一步本身不丢请求。2026-11-26 实测过两端：直连时请求出现在网关
    # 日志、切回后出现在中继日志。
    backup="$MAPFILE.zhihui-bak"
    cp -a "$MAPFILE" "$backup" || die "备份 $MAPFILE 失败"
    sed -i "s#http://127.0.0.1:[0-9]*#http://127.0.0.1:$port#" "$MAPFILE" || {
        cp -a "$backup" "$MAPFILE"
        die "改写 $MAPFILE 失败，已还原"
    }
    if ! nginx -t >/dev/null 2>&1; then
        cp -a "$backup" "$MAPFILE"
        die 'nginx 配置校验没过，已还原（推理入口没动）'
    fi
    if ! systemctl reload nginx; then
        cp -a "$backup" "$MAPFILE"
        nginx -t >/dev/null 2>&1 && systemctl reload nginx
        die 'nginx reload 失败，已还原推理入口'
    fi
    rm -f "$backup"
    if [ "$target" = relay ]; then
        printf '   推理入口 -> 中继（%s）\n' "$port"
    else
        printf '   推理入口 -> 直连网关（%s）\n' "$port"
    fi
}

restart_relay() {
    step "重启中继（加载含空窗重试的新代码）"
    # 中继是单进程，停它的那 1-2 秒没人接流量 —— 这是「发版不中断」最后剩下的缺口。
    # 先把推理入口切到直连网关（reload 平滑，在途的流不受影响），这 1-2 秒里新请求
    # 直接落到网关；中继起来后再切回来。网关此时没有在重启，所以不需要重试兜底。
    printf '   先把推理入口切到直连，避开中继重启的那 1-2 秒。\n'
    switch_inference direct
    systemctl restart "$RELAY" || {
        switch_inference relay
        die "重启中继失败（推理入口已切回中继）"
    }
    local pid ready=0
    for _ in $(seq 1 20); do
        sleep 0.5
        pid=$(systemctl show "$RELAY" -p MainPID --value 2>/dev/null)
        if [ -n "$pid" ] && [ "$pid" != 0 ] \
            && curl -sf -o /dev/null --max-time 2 http://127.0.0.1:7867/v1/models 2>/dev/null; then
            ready=1
            break
        fi
        # 401 也算活着：它证明中继在转发，只是我们没带令牌
        if curl -s -o /dev/null --max-time 2 -w '%{http_code}' \
            http://127.0.0.1:7867/v1/models 2>/dev/null | grep -q '^401$'; then
            ready=1
            break
        fi
    done
    if [ "$ready" != 1 ]; then
        # 中继起不来时**不要**把入口切回去 —— 那等于把流量指向一个死的端口。
        # 保持直连：服务照常，只是暂时没有重试保护，巡检会把它标成一条警告。
        printf '\n   中继没起来。推理入口保持在直连，服务不受影响。\n' >&2
        printf '   修好后重跑本脚本，它会重新尝试切回中继。\n' >&2
        die '中继重启后 10 秒仍未就绪（推理入口停在直连）'
    fi
    printf '   中继已就绪（pid %s）\n' "$pid"
    switch_inference relay
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
    local version="$1" before ready=0
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
    # 判据用网关自己记的请求终态（request_outcomes），不是 nginx 的共享日志。
    #
    # 2026-11-26 实测：`/var/log/nginx/access.log` 里混着另一个站点
    # （zhijing.kaogong.art）转发到境外上游的 7 条 502，时间正好落在本次重启
    # 窗口内，被原来的 `/v1/` 过滤算成了「本次发版丢了 7 个请求」—— 而真正
    # 进入网关的 8 条请求全部 200。`/v1/` 这个前缀两个站点都有，路径过滤不够。
    #
    # 请求终态表只收网关真正处理过的请求，天然排除别的站点。
    before=$(db_scalar 'select ifnull(max(rowid),0) from request_outcomes;')
    case "$before" in
        ''|*[!0-9]*) die "读不到请求终态表（$DB），无法证明本次发版零中断";;
    esac
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

    # 最后几条终态的落库是请求结束时写的，留 2 秒再数。
    sleep 2
    local total failures
    total=$(db_scalar "select count(*) from request_outcomes where rowid > $before;")
    case "$total" in ''|*[!0-9]*) total=0;; esac
    failures=$(db_scalar "select count(*) from request_outcomes where rowid > $before and http_status >= 500;")
    case "$failures" in ''|*[!0-9]*) failures=0;; esac

    step '重启窗口内的流量（按网关自己的请求终态）'
    printf '   请求 %s 条，5xx %s 条\n' "$total" "$failures"
    if [ "$failures" -eq 0 ]; then
        if [ "$total" -gt 0 ]; then
            printf '   —— 零中断（窗口内有真实请求）\n'
        else
            printf '   —— 窗口内 0 条请求：不能据此宣称零中断\n'
        fi
    else
        printf '   明细：\n'
        db_rows "select datetime(ts,'unixepoch','+8 hours'), method, endpoint, http_status, coalesce(error_stage,'')
                 from request_outcomes where rowid > $before and http_status >= 500
                 order by rowid limit 10;" | sed 's/^/     /'
        printf '\n   说明：中继的重试只覆盖「连接阶段失败」。若失败集中在重启第一秒\n'
        printf '   之外，多半是中继没跑上新代码，或这一段流量绕过了中继。\n'
    fi

    check_pages

    if [ "$failures" -eq 0 ]; then
        record_rollout "$version" '重启' "$total" "$failures" 0 "$([ "$total" -gt 0 ] && printf '窗口内零 5xx' || printf '窗口内 0 条请求')"
        return 0
    fi
    record_rollout "$version" '重启' "$total" "$failures" 2 '窗口内有 5xx'
    return 2
}

prestart_candidate() {
    local version="${1#v}" keep="${2:-0}"
    step '发布前先在旁边起一个候选实例'
    # 2026-11-23 真实发生过：drop-in 加了一个新版 schema 不认识的变量，重启后每
    # 3 秒崩一次，直到 StartLimitBurst 才停 —— 前后约 8 分钟完全不可用。
    # 配置校验（上一步）能挡那一类，但挡不住 import 失败、数据库迁移失败、
    # 静态资源缺失：那三样只有真启动才知道。
    #
    # 仓库里早就有候选实例的 unit（7865，WB_BACKGROUND_JOBS=0，共享入账），
    # 但发布脚本从来没用过 —— 于是每次都拿线上的 6 秒空窗去赌「这一版能起来」。
    # 起一次实测 6 秒，失败时线上一个请求都没被动过。
    if ! systemctl cat "$HELPER.service" >/dev/null 2>&1; then
        printf '   没有候选实例的 unit（%s）\n' "$HELPER"
        if [ "$keep" = 1 ]; then
            printf '   这台机器上没法蓝绿发布，退回「重启 + 中继重试」的方式。\n'
            return 1
        fi
        printf '   跳过这一步；这一版无法提前发现「起不来」，失败只能在重启后暴露。\n'
        return 0
    fi
    systemctl start "$HELPER" 2>/dev/null \
        || die "候选实例启动失败（$HELPER），已中止；线上仍在跑旧代码"

    local ready=0 code i
    for i in $(seq 1 30); do
        sleep 1.5
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
            "http://127.0.0.1:$CANDIDATE_PORT/readyz" 2>/dev/null)
        if [ "$code" = 200 ]; then
            ready=1
            break
        fi
        # unit 已经退出就没什么可等的了，直接去看日志。
        systemctl is-active --quiet "$HELPER" || break
    done

    if [ "$ready" != 1 ]; then
        systemctl stop "$HELPER" 2>/dev/null
        printf '\n   候选实例没有就绪，日志最后 20 行：\n' >&2
        journalctl -u "$HELPER" -n 20 --no-pager 2>/dev/null | sed 's/^/   /' >&2
        die '这一版起不来，已中止发布；线上仍在跑旧代码'
    fi

    local served
    served=$(curl -s --max-time 3 "http://127.0.0.1:$CANDIDATE_PORT/api/release" 2>/dev/null)
    if ! printf '%s' "$served" | grep -q "\"version\":\"$version\""; then
        systemctl stop "$HELPER" 2>/dev/null
        printf '   候选实例报告的版本不是 %s：%s\n' "$version" "$served" >&2
        die '候选实例跑的代码与本次发布不一致，已中止；线上仍在跑旧代码'
    fi

    if [ "$keep" = 1 ]; then
        printf '   候选实例就绪（版本 %s，约 %s 秒），保持运行等着接管流量\n' "$version" "$((i * 3 / 2))"
        return 0
    fi

    systemctl stop "$HELPER" 2>/dev/null
    printf '   候选实例就绪（版本 %s，约 %s 秒），已停止\n' "$version" "$((i * 3 / 2))"
    printf '   到这一步为止线上没有被动过。\n'
}

route_switch() {
    local target="$1" epoch tmp
    # 中继校验：目标必须存在、epoch 只能前进、文件不能被打成组/其他可写。
    epoch=$(sed -n 's/.*"epoch":\([0-9]*\).*/\1/p' "$ROUTE_FILE" 2>/dev/null | head -1)
    case "$epoch" in
        ''|*[!0-9]*) die "读不到中继路由文件（$ROUTE_FILE），无法切到 $target";;
    esac
    # 同目录 + rename：中继随时可能在读，半截文件会被当成 invalid_or_stale_route。
    tmp="$DATA_DIR/.relay-route.$$"
    printf '{"epoch":%d,"target":"%s"}\n' "$((epoch + 1))" "$target" > "$tmp" \
        || die "写不了路由临时文件（$tmp）"
    chmod 600 "$tmp"
    chown 10001:10001 "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$ROUTE_FILE" || { rm -f "$tmp"; die "路由切换失败（$target）"; }
    printf '   中继路由 -> %s（epoch %s）\n' "$target" "$((epoch + 1))"
}

check_pages() {
    step '页面与就绪探针'
    local url code
    for url in / /portal/ /keys/ /dashboard/ /members/; do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://zhihui.kaogong.art$url" 2>/dev/null)
        printf '   %-12s %s\n' "$url" "$code"
        [ "$code" = 200 ] || printf '   ^^ 这个页面不是 200\n'
    done
    printf '   readyz       %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:7866/readyz 2>/dev/null)"
}

rollout_blue_green() {
    local version="${1#v}" before ready=0 total failures
    before=$(db_scalar 'select ifnull(max(rowid),0) from request_outcomes;')
    case "$before" in
        ''|*[!0-9]*) die "读不到请求终态表（$DB），无法证明本次发版零中断";;
    esac

    # 候选实例先起来并一直留着 —— 这次不是用它做检查，而是让它真的接流量。
    # 起不来就没有第二个实例可切，返回 3 让调用方退回普通发布（不在这里 die：
    # 「这台机器没有候选实例」不该让发版失败）。
    prestart_candidate "$version" 1 || return 3

    step '把中继流量切到候选实例'
    route_switch new
    sleep 2
    printf '   现在新请求由候选实例服务，旧实例只负责跑完在途的流。\n'

    systemctl stop "$GATEWAY" || die "停旧实例失败（流量仍在候选实例上，服务未中断）"
    if ! systemctl start "$GATEWAY"; then
        printf '\n   主实例起不来。流量仍在候选实例上，服务没有中断。\n' >&2
        printf '   处理：修好后重跑本脚本；不要先停掉 %s，那才会真的中断。\n' "$HELPER" >&2
        die '主实例启动失败（线上仍由候选实例服务）'
    fi

    for _ in $(seq 1 40); do
        sleep 1.5
        if curl -sf --max-time 3 http://127.0.0.1:7866/api/release 2>/dev/null \
            | grep -q "\"version\":\"$version\""; then
            ready=1
            break
        fi
    done
    if [ "$ready" != 1 ]; then
        printf '\n   主实例 60 秒未就绪。流量仍在候选实例上，服务没有中断。\n' >&2
        die '主实例未就绪（路由未切回，候选实例继续服务）'
    fi

    step '主实例已就绪，把流量切回来'
    route_switch old
    sleep 1
    systemctl stop "$HELPER" 2>/dev/null

    # 最后几条终态的落库是请求结束时写的，留 2 秒再数。
    sleep 2
    total=$(db_scalar "select count(*) from request_outcomes where rowid > $before;")
    case "$total" in ''|*[!0-9]*) total=0;; esac
    failures=$(db_scalar "select count(*) from request_outcomes where rowid > $before and http_status >= 500;")
    case "$failures" in ''|*[!0-9]*) failures=0;; esac

    step '发布窗口内的流量（按网关自己的请求终态）'
    printf '   请求 %s 条，5xx %s 条\n' "$total" "$failures"
    if [ "$failures" -eq 0 ]; then
        if [ "$total" -gt 0 ]; then
            printf '   —— 零中断（窗口内有真实请求）\n'
        else
            printf '   —— 窗口内 0 条请求：不能据此宣称零中断\n'
        fi
    else
        printf '   明细：\n'
        db_rows "select datetime(ts,'unixepoch','+8 hours'), method, endpoint, http_status, coalesce(error_stage,'')
                 from request_outcomes where rowid > $before and http_status >= 500
                 order by rowid limit 10;" | sed 's/^/     /'
    fi

    check_pages
    if [ "$failures" -eq 0 ]; then
        record_rollout "$version" '蓝绿' "$total" "$failures" 0 '窗口内零 5xx'
        return 0
    fi
    record_rollout "$version" '蓝绿' "$total" "$failures" 2 '窗口内有 5xx'
    return 2
}

main() {
    local version='' relay_changed=0 check_only=0 blue_green=1 arg
    STARTED_AT=$(date '+%Y-%m-%dT%H:%M:%S%z')
    for arg in "$@"; do
        case "$arg" in
            --check) check_only=1 ;;
            --relay-changed) relay_changed=1 ;;
            --blue-green) blue_green=1 ;;
            --no-blue-green) blue_green=0 ;;
            -*) die "未知参数：$arg" ;;
            *) version="$arg" ;;
        esac
    done
    # 供 EXIT 兜底记录用：中途 die 时也要能写清「哪个版本、哪种方式」。
    CHECK_ONLY="$check_only"
    LOG_VERSION="${version#v}"
    if [ "$blue_green" = 1 ]; then LOG_MODE='蓝绿'; else LOG_MODE='重启'; fi

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

    if [ "$blue_green" = 1 ]; then
        # 蓝绿：候选实例先接流量，再重启主实例，然后切回来 —— 重启期间一个请求都不落空。
        rollout_blue_green "$version"
        local status=$?
        if [ "$status" -ne 3 ]; then
            step '完成'
            [ "$status" -eq 0 ] && printf '   v%s 已上线（蓝绿切换，重启期间流量在候选实例上）\n' "${version#v}"
            return "$status"
        fi
        printf '   改用重启方式发布。\n'
    fi

    # 再往前一步：真的起一次。配置校验只看静态契约，起不来只有真启动才知道；
    # 这一步失败时线上还在跑旧代码，代价只有几秒的 CPU。
    prestart_candidate "$version"

    restart_gateway "$version"
    local status=$?

    step '完成'
    [ "$status" -eq 0 ] && printf '   v%s 已上线，重启窗口零中断\n' "${version#v}"
    return "$status"
}

main "$@"
