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
#: 脚本自身所在目录 —— 配置校验工具与它同目录，不能靠调用者的 cwd 找。
SELF_DIR=$(cd "$(dirname "$0")" && pwd)

step() { printf '\n== %s\n' "$*"; }
die() { printf '!! %s\n' "$*" >&2; exit 1; }

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

    [ "$failures" -eq 0 ] || return 2
    return 0
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
            die '蓝绿发布需要候选实例，但这台机器上没有它的 unit'
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
    prestart_candidate "$version" 1

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
    [ "$failures" -eq 0 ] || return 2
    return 0
}

main() {
    local version='' relay_changed=0 check_only=0 blue_green=0 arg
    for arg in "$@"; do
        case "$arg" in
            --check) check_only=1 ;;
            --relay-changed) relay_changed=1 ;;
            --blue-green) blue_green=1 ;;
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

    if [ "$blue_green" = 1 ]; then
        # 蓝绿：候选实例先接流量，再重启主实例，然后切回来 —— 重启期间一个请求都不落空。
        rollout_blue_green "$version"
        local status=$?
        step '完成'
        [ "$status" -eq 0 ] && printf '   v%s 已上线（蓝绿切换，重启期间流量在候选实例上）\n' "${version#v}"
        return "$status"
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
