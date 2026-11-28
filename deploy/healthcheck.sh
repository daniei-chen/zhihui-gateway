#!/usr/bin/env bash
# 智汇网关健康巡检：一次跑完，直接给结论。
#
# 这个脚本只读，不改任何东西。它把平时分散在十几条命令里的检查收拢成一份
# 带结论的输出，目的是「想知道现在健不健康」时不必再去翻日志和配置。
#
# 用法：sudo ./healthcheck.sh
# 退出码：0 全部正常；1 有需要处理的问题；2 有必须立即处理的问题。
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
    printf '需要 root（要读 0600 的 nginx 配置与数据库）：请用 sudo 运行\n' >&2
    exit 1
fi

SITE=/etc/nginx/sites-enabled/zhihui.kaogong.art
SNIPPET=/etc/nginx/snippets/zh-relay-inference.location.conf
MAPFILE=/etc/nginx/conf.d/zhihui-relay-upstream.conf
DB=/opt/workbuddy-manager/data/manager.db
ACCESS_LOG=/var/log/nginx/access.log
HOST=zhihui.kaogong.art

ok=0; warn=0; bad=0
pass() { ok=$((ok + 1)); printf '  ok    %s\n' "$*"; }
note() { warn=$((warn + 1)); printf '  warn  %s\n' "$*"; }
broken() { bad=$((bad + 1)); printf '  FAIL  %s\n' "$*"; }
section() { printf '\n== %s\n' "$*"; }

# 从**运行中**的 unit 读一个环境变量，读不到就用调用方给的默认值。
#
# 2026-11-25：巡检里判警告用的阈值原来硬编码成魔数，而线上真实预算由 systemd
# drop-in 决定。两者不一致时，报出来的数字没有语义 —— 例如「首字节超过 60 秒」
# 在 240 秒预算下属于正常范围，却每天都被算成一条警告。
#
# 这不是第一次因为这组数字出错：`server/config.py` 里的默认值 60 也与线上的
# 240 不一致（见 docs/OPS-PITFALLS.md 第十八条「代码里的默认值不是线上值」）。
# 所以这里统一从 `systemctl show` 取，而不是相信任何一份代码里的常量。
unit_env() {
    local value
    value=$(systemctl show "$1" -p Environment --value 2>/dev/null \
        | tr ' ' '\n' | sed -n "s/^$2=//p" | head -1)
    case "$value" in
        '' | *[!0-9]*) printf '%s' "$3" ;;
        *) printf '%s' "$value" ;;
    esac
}

section '服务'
for unit in workbuddy-web zhihui-rolling-relay nginx; do
    if systemctl is-active --quiet "$unit"; then
        pass "$unit 在运行"
    else
        broken "$unit 没在运行"
    fi
done

section '版本与就绪'
release=$(curl -sf --max-time 5 http://127.0.0.1:7866/api/release 2>/dev/null)
if printf '%s' "$release" | grep -q '"version"'; then
    version=$(printf '%s' "$release" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')
    pass "网关报告版本 $version"
    pointing=$(basename "$(readlink -f /opt/workbuddy-manager/releases/current 2>/dev/null)")
    case "$pointing" in
        *"$version"*) pass "发布指针一致（$pointing）" ;;
        *) broken "发布指针 $pointing 与运行版本 $version 不一致" ;;
    esac
else
    broken '/api/release 无响应'
fi
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:7866/readyz 2>/dev/null)
[ "$code" = 200 ] && pass "/readyz 200" || broken "/readyz 返回 $code"

section '最近一次发布'
# 发版脚本每次结束都往这里追一行（含中途退出）。它回答两个此前只能翻日志的问题：
# 最近一次发版用的是蓝绿还是重启、那次窗口里到底有没有 5xx。2026-11-26 有一次
# 远程发版命令超时，脚本的统计输出全丢 —— 结论没留痕，只能事后翻请求表反推。
ROLLOUT_LOG=/opt/workbuddy-manager/data/rollout-log.jsonl
if [ -r "$ROLLOUT_LOG" ]; then
    last=$(tail -1 "$ROLLOUT_LOG" 2>/dev/null)
    if [ -z "$last" ]; then
        printf '   发布记录是空的（%s）\n' "$ROLLOUT_LOG"
    else
        r_version=$(printf '%s' "$last" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')
        r_mode=$(printf '%s' "$last" | sed -n 's/.*"mode":"\([^"]*\)".*/\1/p')
        r_started=$(printf '%s' "$last" | sed -n 's/.*"started_at":"\([^"]*\)".*/\1/p')
        r_requests=$(printf '%s' "$last" | sed -n 's/.*"requests":\([0-9]*\|null\).*/\1/p')
        r_failures=$(printf '%s' "$last" | sed -n 's/.*"failures":\([0-9]*\|null\).*/\1/p')
        r_exit=$(printf '%s' "$last" | sed -n 's/.*"exit":\([0-9]*\).*/\1/p')
        case "$r_requests" in '' | null | *[!0-9]*) r_requests=0 ;; esac
        case "$r_failures" in '' | null | *[!0-9]*) r_failures=0 ;; esac
        case "$r_exit" in
            0)
                if [ "$r_requests" -gt 0 ]; then
                    pass "v$r_version（$r_mode）$r_started · 窗口 $r_requests 条请求、$r_failures 个 5xx"
                else
                    note "v$r_version（$r_mode）窗口内 0 条请求，不能据此宣称零中断"
                fi
                ;;
            *)
                if [ "$r_requests" -gt 0 ]; then
                    note "v$r_version（$r_mode）$r_started 退出码 $r_exit · 窗口 $r_requests 条请求、$r_failures 个 5xx"
                else
                    note "v$r_version（$r_mode）$r_started 退出码 $r_exit · 未走到统计环节"
                fi
                ;;
        esac
    fi
else
    printf '   还没有发布记录（%s）；下一次发版后才会出现\n' "$ROLLOUT_LOG"
fi

section '配置与发布树'
# 2026-11-25：这两项原来都没有，而它们各自造成过或差点造成真实故障。
#
# 1) 配置校验：2026-11-23 23:25 有一个 drop-in 先加了 WB_TRAFFIC_TAP，
#    而当时代码 schema 还不认识它，服务在「启动→被拒→3 秒后重启」之间
#    循环了约 8 分钟。现在能在这里提前看到「当前配置会被拒」。
# 2) 字节码污染：发布树完整性校验拒绝任何 __pycache__，而检查工具
#    自己 import 发布树代码时就会生成它。见 verify-runtime-config.py 顶部。
current=$(readlink -f /opt/workbuddy-manager/releases/current 2>/dev/null)
if [ -n "$current" ] && [ -x /opt/workbuddy-manager/deploy/verify-runtime-config.py ]; then
    if /opt/workbuddy-manager/deploy/verify-runtime-config.py "$current" workbuddy-web \
        >/tmp/hc-config.$$ 2>&1; then
        pass "配置校验通过（$(basename "$current")）"
    else
        broken "当前配置会被 $(basename "$current") 拒绝：$(sed -n '2p' /tmp/hc-config.$$ 2>/dev/null)"
    fi
    rm -f /tmp/hc-config.$$
else
    note '找不到发布目录或配置校验工具，跳过'
fi
stale=$(find /opt/workbuddy-manager/releases -maxdepth 3 -name __pycache__ -type d 2>/dev/null | wc -l)
[ "$stale" = 0 ] && pass '发布树没有字节码残留' \
    || note "$stale 个历史版本目录里有 __pycache__（发布树校验会拒绝这些版本）"

section '反代与中继'
grep -q 'zhihui_relay_upstream' "$MAPFILE" 2>/dev/null \
    && pass '推理流量变量已定义' || broken "缺少 \$zhihui_relay_upstream（$MAPFILE）"
[ -f "$SNIPPET" ] && pass '中继片段已安装' || broken "中继片段缺失：$SNIPPET"
grep -q 'zh-relay-inference' "$SITE" 2>/dev/null \
    && pass '站点已接中继' || broken '站点没有接中继，发布时会打断在途调用'
relay_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:7867/v1/models 2>/dev/null)
[ "$relay_code" = 401 ] && pass '中继在线并转发正常' || broken "中继探测返回 $relay_code（期望 401）"
# 推理入口现在指向哪里。只看「中继在线」不够：入口停在直连时中继也是活的，
# 但流量根本没经过它 —— 2026-11-26 起发布脚本会在重启中继期间临时切到直连，
# 万一脚本被打断，入口就会留在那儿，这一项负责把它显示出来。
entry_port=$(sed -n 's#.*http://127\.0\.0\.1:\([0-9]*\).*#\1#p' "$MAPFILE" 2>/dev/null | head -1)
case "$entry_port" in
    7867) pass '推理入口指向中继（7867）' ;;
    7866) note '推理入口直连网关（7866）：暂时没有中继重试保护（重启中继期间属正常）' ;;
    '') broken "读不出推理入口（$MAPFILE）" ;;
    *) broken "推理入口指向未知端口 $entry_port" ;;
esac

section '页面'
for url in / /portal/ /keys/ /dashboard/ /members/; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$HOST$url" 2>/dev/null)
    [ "$code" = 200 ] && pass "$url 200" || broken "$url 返回 $code"
done

section '近 24 小时错误率'
# 数据源改成本地数据库的 request_outcomes，不再读 nginx 日志。原因有两个：
#
#   1. 日志按天轮转，`date -d '24 hours ago'` 在每天刚过零点时取不到昨天的行，
#      实际统计窗口会缩到几小时，「近 24 小时」名不副实；
#   2. 原先拿 nginx 的 `$4`（形如 `[25/Nov/2026:10:30:01`）与 `date` 输出做字符串
#      比较，既多拼了一个 `[`（日志字段本身已带），又用了 `-u`（UTC）去比 `+0800`
#      的时间戳 —— 两个错误叠加后比较恒为真，`total` 实际是整个文件的行数。
#      日志越大，错误率被稀释得越厉害，这个数就彻底失去意义了。
#
# request_outcomes 是应用自己写的每一个请求的终态，带 http_status 与 ts，不受日志
# 轮转影响，也不会被文件行数带偏。
if [ -r "$DB" ]; then
    total=$(sqlite3 -readonly "$DB" \
        "SELECT count(*) FROM request_outcomes WHERE ts > strftime('%s','now')-86400;" 2>/dev/null)
    # 分母不能直接用总行数：这张表里绝大部分是健康探针（2026-11-26 实测 24 小时
    # 12,216 条中，/api/readyz 8,375、/api/healthz 1,812、/api/release 892 ——
    # 探针占了七成以上）。拿总行数当分母，错误率会被稀释三成，读起来比实际好。
    # 探针本身不算「没机会出错」，它们就该被排除在用户体验之外。
    inference=$(sqlite3 -readonly "$DB" \
        "SELECT count(*) FROM request_outcomes WHERE ts > strftime('%s','now')-86400
         AND endpoint LIKE '/v1/%';" 2>/dev/null)
    errors=$(sqlite3 -readonly "$DB" \
        "SELECT count(*) FROM request_outcomes WHERE ts > strftime('%s','now')-86400 AND http_status >= 500;" 2>/dev/null)
    if [ "${total:-0}" -gt 0 ] 2>/dev/null; then
        base=${inference:-0}
        case "$base" in '' | *[!0-9]*) base=0 ;; esac
        [ "$base" -gt 0 ] || base=$total
        rate=$(awk -v e="${errors:-0}" -v t="$base" 'BEGIN{printf "%.2f", 100*e/t}')
        printf '  请求 %s（其中推理 %s），5xx %s（按推理算 %s%%）\n' \
            "$total" "${inference:-0}" "${errors:-0}" "$rate"
        if [ "${errors:-0}" -eq 0 ]; then
            pass '近 24 小时无 5xx'
        elif [ "$(awk -v r="$rate" 'BEGIN{print (r<1)?1:0}')" = 1 ]; then
            # 低于 1% 记 ok，不记 warn。
            #
            # 原来的判断是：「有 5xx 但低于 1%」→ note（即 warn）。这在 LLM 网关
            # 上几乎是**每天都成立**的 —— 上游侧本身就会零星失败，8,000 次请求里
            # 有七八个 5xx 是常态。结果这一项常年挂在警告里，真正的异常反而被淹没。
            #
            # 规范写得很清楚：报「零中断」时只限定在**部署窗口**内，全天有失败是
            # 正常的，那不是发版责任。巡检这一行的职责是判断「能不能用」——
            # 低于 1% 能用，就该是 ok；占比数字仍照常打印出来，不会被藏起来。
            printf '  低于 1%%，属上游侧正常波动\n'
            pass '近 24 小时 5xx 低于 1%'
        else
            broken "5xx 占比 $rate%，需要排查"
        fi
    else
        note '近 24 小时没有请求记录'
    fi
else
    note "读不到 $DB"
fi

section '磁盘'
used=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
avail=$(df -h / 2>/dev/null | tail -1 | awk '{print $4}')
if [ -n "$used" ]; then
    # 阈值刻意比「审计要求」更早触发（80/90 而非 85/92）：这台机器 59G 根分区
    # 已用 65%，数据库每天涨十几 MB，等真到 92% 再喊就只剩几小时窗口。
    # 文案必须带「还剩多少」与「该清什么」，否则看到百分比仍然不知道该动哪里。
    if [ "$used" -lt 80 ]; then
        pass "根分区已用 ${used}%，剩余 ${avail}"
    elif [ "$used" -lt 90 ]; then
        note "根分区已用 ${used}%，剩余 ${avail}；可清理：/opt/workbuddy-manager/releases 与 /var/www/zhihui-console-releases 的旧版本、journalctl --vacuum-size=500M"
    else
        broken "根分区已用 ${used}%，剩余 ${avail}，随时可能写满；立即清理旧版本并按需扩容"
    fi
fi
journal=$(journalctl --disk-usage 2>/dev/null | grep -o '[0-9.]*[MG]' | head -1)
[ -n "$journal" ] && printf '  journal 占用 %s（超过 500M 可考虑清理）\n' "$journal"
console_count=$(ls /var/www/zhihui-console-releases 2>/dev/null | wc -l)
printf '  控制台历史版本 %s 个' "$console_count"
[ "$console_count" -gt 8 ] && printf '（偏多，可清理旧版）\n' || printf '\n'
# 发布目录是大头：每版约 41 MB，2026-11-26 实测 65 个版本占 2.6 GB。控制台那行
# 早就有了，发布目录没有 —— 而它比控制台大一个量级，磁盘告警的提示里却只写了个路径。
release_count=$(ls -1 /opt/workbuddy-manager/releases 2>/dev/null | grep -cE '^v[0-9]+\.[0-9]+\.[0-9]+$')
release_mb=$(du -sm /opt/workbuddy-manager/releases 2>/dev/null | cut -f1)
if [ -n "$release_count" ]; then
    printf '  发布版本 %s 个、%s MB' "$release_count" "${release_mb:-?}"
    if [ "${release_count:-0}" -gt 8 ] 2>/dev/null; then
        printf '（偏多，release.py prune 可清理）\n'
    else
        printf '\n'
    fi
fi

# inode：空间够但 inode 用完时，所有「看起来正常」的写入都会失败，且不会先变慢。
# 2026-11-25 实测：已用 26%，其中 releases 下 31 个版本占 5.8 万，年增约 17%，
# 短期没有风险 —— 但它不会渐进恶化，是直接 ENOSPC，所以放一条观测在这里。
inodes=$(df --output=ipcent / 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$inodes" ]; then
    if [ "$inodes" -lt 80 ]; then
        pass "inode 已用 ${inodes}%"
    else
        note "inode 已用 ${inodes}% —— 磁盘空间可能还够，但建不了新文件；先清理旧版本"
    fi
fi

# OOM：进程被内核杀掉时来不及写自己的日志，只有内核记得。
# 2026-11-25 实测：7 天内 4 次，全部是备份进程撞上 512MB 的 cgroup 限制。
# 备份另有基于报告新鲜度的健康告警，但「谁被杀了」只有这里能看见 ——
# 那次备份失败后，全局仍判为健康（前一份报告未超 26 小时），是合理的，
# 但如果连续两次失败，就需要这条线索来解释原因。
oom=$(journalctl --since '-24 hours' --no-pager 2>/dev/null | grep -ci 'out of memory.*killed process')
if [ "${oom:-0}" -gt 0 ]; then
    note "近 24 小时 ${oom} 次内存不足杀进程（journalctl -k 可见是谁）"
else
    pass '近 24 小时无内存不足杀进程'
fi
release_count=$(ls /opt/workbuddy-manager/releases 2>/dev/null | grep -c '^v1')
printf '  应用历史版本 %s 个\n' "$release_count"

section '上游额度'
# 额度快照由后台任务定时采集。没有这一段时，账号额度将尽只能等用户撞上 429 才发现
# —— 而那时已经有人在生产上失败了。阈值 20% / 10% 是按充值周期定的：低于两成还
# 来得及在工作时间处理，低于一成基本就是当天的事了。
if [ -r "$DB" ]; then
    latest=$(sqlite3 -readonly "$DB" 'select max(captured_at) from platform_credit_snapshots;' 2>/dev/null)
    if [ -n "$latest" ]; then
        rows=$(sqlite3 -readonly -separator '|' "$DB" \
            "select s.platform||'/'||coalesce(s.account,'-'), ifnull(s.total,0), ifnull(s.remain,0),
                    ifnull((select p.remain from platform_credit_snapshots p
                            where p.platform=s.platform
                              and ifnull(p.account,'')=ifnull(s.account,'')
                              and p.captured_at <= $latest - 86400
                            order by p.captured_at desc limit 1), -1)
             from platform_credit_snapshots s where s.captured_at=$latest
             order by s.remain*1.0/nullif(s.total,0) asc;" 2>/dev/null)
        # 用 herestring 而不是管道：管道会开子 shell，里面的 pass/note/broken
        # 改不到外层的计数器，结论那一行的数字就会少算。
        while IFS='|' read -r who total remain prev; do
            [ -n "$who" ] || continue
            case "$total" in ''|0|0.0|*[!0-9.]*) continue ;; esac
            pct=$(awk -v r="$remain" -v t="$total" 'BEGIN{printf "%.0f", 100*r/t}')
            # 只报百分比不够：一个 24% 但早就不再被使用的账号，和一个 91% 但每天掉
            # 一大截的池子，看起来同样「需要担心」—— 实际上前者不用管、后者要动手。
            # 2026-11-26 的告警噪声就是这么来的：trae 报「剩余 24%」已持续 19 小时，
            # 而它近 24 小时的消耗是 0。
            trend=''
            case "$prev" in
                '' | -1 | *[!0-9.]*) ;;
                *)
                    trend=$(awk -v r="$remain" -v p="$prev" 'BEGIN{
                        d = p - r
                        if (d < -0.5) { printf "，近 24 小时增加了 %.0f", -d }
                        else if (d <= 0.5) { printf "，近 24 小时没有变化" }
                        else if (r <= 0) { printf "，近 24 小时用了 %.0f，已用完", d }
                        else {
                            days = r / d
                            if (days > 365) printf "，近 24 小时用了 %.0f，按这速度一年以上才用完", d
                            else if (days < 1) printf "，近 24 小时用了 %.0f，不到 1 天就用完", d
                            else printf "，近 24 小时用了 %.0f，按这速度约 %.0f 天用完", d, days
                        }
                    }')
                    ;;
            esac
            if [ "$pct" -lt 10 ]; then
                broken "$who 剩余 $remain/$total（${pct}%${trend}）—— 即将耗尽"
            elif [ "$pct" -lt 20 ]; then
                note "$who 剩余 $remain/$total（${pct}%${trend}）—— 该充值了"
            else
                pass "$who 剩余 $remain/$total（${pct}%${trend}）"
            fi
        done <<< "$rows"
        age=$(( $(date +%s) - latest ))
        if [ "$age" -gt 86400 ]; then
            note "额度快照已 $((age / 3600)) 小时未更新，上面的数字可能是旧的"
        fi
    else
        note '没有额度快照，无法判断上游还剩多少'
    fi
else
    note "读不到数据库 $DB"
fi

section '上游可用性'
# 「调用失败」和「调用慢」是两件事，不能都塞进一个错误率百分比里。
# 近 24 小时的 5xx 只有 8 条、占比 0.10%，看上去很健康 —— 但其中大部分是
# 「上游没有可用账号」，客户端拿到的是 503，用户侧就是「服务暂时不可用」，
# 而且是连续 30 秒内重试全败。这类失败会被平均值淹没，必须单独可见。
#
# 口径说明：这些是**上游的业务码**（网关按 A6 规则透传并包成 503），
# 不是我们自己的判断 —— 它代表「上游说没人可用」，不是「我们说不行」。
# 处置方向不同：前者要看账号池与额度，后者要查网关自身。
#
# 为什么有两个码：`no_healthy_account` 与 `no_available_account` 都表示
# 「池子里没有能用的账号」，但来自上游不同分支。只查前者会漏掉后者 ——
# 实测 7 天内 `doubaowork/deepseek-v4.1-flash` 这个模型 15 次失败全是后者，
# 如果只盯前者，整类失败在巡检里就是隐形的。
if [ -r "$DB" ]; then
    nh=$(sqlite3 -readonly "$DB" \
        "SELECT count(*) FROM request_outcomes
          WHERE ts > strftime('%s','now')-86400
            AND error_code IN ('no_healthy_account','no_available_account');" 2>/dev/null)
    if [ -z "${nh:-}" ]; then
        note '查不到上游可用性数据'
    elif [ "$nh" -eq 0 ]; then
        pass '近 24 小时没有「上游无可用账号」的失败'
    else
        note "近 24 小时 $nh 次「上游无可用账号」（503）—— 上游侧失败，用户会看到服务暂不可用"
    fi
fi

section '容量拒绝'
# 5xx 归因分三类，只有一类是发版责任，另有一类**是运营责任**：
#   · 部署窗口    —— 落在重启前后 15 秒内，发版失败
#   · 上游瞬时失败 —— 上游侧，记下来即可
#   · 容量拒绝    —— 我们自己没接住，必须扩容或限流
# 前两类在别处已经有专门段落；第三类此前只被并进总错误率，看不见。
# 实测 11-01 单日 262 次容量拒绝、之后连日零星 —— 那种日子在总错误率里
# 只是「占比略高」，看不出是自己在拒绝人。
#
# 这里用 broken 而不是 note：容量拒绝不是「上游不配合」，是我们接不住，
# 属于必须处理的项。
if [ -r "$DB" ]; then
    cap=$(sqlite3 -readonly "$DB" \
        "SELECT count(*) FROM request_outcomes
          WHERE ts > strftime('%s','now')-86400
            AND error_code='gateway.capacity.rejected';" 2>/dev/null)
    if [ -z "${cap:-}" ]; then
        note '查不到容量拒绝数据'
    elif [ "$cap" -eq 0 ]; then
        pass '近 24 小时没有容量拒绝'
    else
        broken "近 24 小时 $cap 次容量拒绝（503）—— 这是我们的责任，需要扩容或限流"
    fi
fi

section '首字节（TTFT）'
# 只看流式请求。非流式请求的「首字节」就是最后一个字节 —— 实测它与总延迟只差 2 ms，
# 混进来会把「生成 5816 个 token 用了 118 秒」误报成「首字节慢」。那不是缺陷，
# 是用户选了非流式又要求长输出。两者的判据不同，必须分开算。
# 分位数用 ORDER BY + OFFSET（SQLite 3.45 没有 percentile_cont），
# 5 千行排序在这个数据量上是毫秒级，不必预先聚合。
if [ -r "$DB" ]; then
    # 阈值取自线上真实预算。读不到才退回 60 —— 退回去的那个 60 只作为兜底，
    # 不再被当成有意义的判断标准。
    fb_budget=$(unit_env workbuddy-web WB_FIRST_BYTE_TIMEOUT_S 60)
    fb=$(sqlite3 -readonly -separator '|' "$DB" "
        WITH s AS (SELECT first_byte_ms v FROM request_logs
                   WHERE ts > strftime('%s','now')-86400 AND first_byte_ms IS NOT NULL
                     AND stream=1
                   ORDER BY first_byte_ms)
        SELECT (SELECT count(*) FROM s),
               (SELECT v FROM s LIMIT 1 OFFSET (SELECT count(*)/2 FROM s)),
               (SELECT v FROM s LIMIT 1 OFFSET (SELECT count(*)*95/100 FROM s)),
               (SELECT max(v) FROM s),
               (SELECT count(*) FROM s WHERE v > $((fb_budget * 1000)));" 2>/dev/null)
    IFS='|' read -r fbn fbp50 fbp95 fbmax fbover <<< "$fb"
    if [ -n "${fbn:-}" ] && [ "$fbn" -gt 0 ]; then
        printf '  流式 %s 次：p50 %sms，p95 %sms，最长 %sms（预算 %s 秒）\n' \
            "$fbn" "$fbp50" "$fbp95" "$fbmax" "$fb_budget"
        if [ "${fbover:-0}" -gt 0 ]; then
            note "$fbover 次流式请求首字节超过了 ${fb_budget} 秒预算（最长 $((fbmax / 1000)) 秒）—— 这些本该被判超时"
        else
            pass "流式请求全部在 ${fb_budget} 秒预算内给出首字节"
        fi
    else
        note '近 24 小时没有流式首字节记录'
    fi
    # 非流式单独一行：这里能看的是总时长，不是 TTFT。
    nf=$(sqlite3 -readonly -separator '|' "$DB" "
        SELECT count(*), round(avg(latency_ms)), max(latency_ms) FROM request_logs
        WHERE ts > strftime('%s','now')-86400 AND stream<>1;" 2>/dev/null)
    IFS='|' read -r nfn nfavg nfmax <<< "$nf"
    if [ -n "${nfn:-}" ] && [ "$nfn" -gt 0 ]; then
        printf '  非流式 %s 次：平均 %sms，最长 %sms（首字节即末尾，按总时长看）\n' "$nfn" "$nfavg" "$nfmax"
    fi
fi

section '运行告警'
# 应用内有 18 项告警（备份、归档、预算对账、账本回执、门户账本……）。它们只存在
# `operational.alerts` 这个内存字典里：`transition()` 进入时写一条日志就不再重复，
# 所以**持续 active 的告警在日志里只有开头一行**。人工跑巡检时因此看不见它们 ——
# 脚本报「全部正常」而实际有告警挂着。这个端点专为巡检而开：只允许回环访问、
# 不返回 detail（detail 里有账号名与上游原文）。
#
# 判为 note 而不是 broken：这些是**预警**（余额见底、备份将过期、回执积压），
# 表达「需要关注」；而巡检这一行的职责是判断「能不能用」—— 服务、页面、错误率、
# 数据库、上游额度这些**硬故障**已由上面各段覆盖。把预警也判成 FAIL，巡检会长期
# 不绿，反而没人再看它。
alerts_json=$(curl -sf --max-time 5 http://127.0.0.1:7866/api/health/alerts 2>/dev/null)
if [ -n "$alerts_json" ]; then
    # 取**顶层** count（当前 active 告警数），不能取「最后一个 count」。
    # 原先用 `sed -n 's/.*"count":.../\1/p'`：`.*` 是贪婪的，会命中最后一个
    # `"count":`；而 DAN-41 在响应里加了 `notify.dropped.count`，于是巡检
    # 读到的是「累计丢弃数」而不是「active 数」——重启后 active 告警从
    # operational_alerts 恢复、丢弃计数归零，巡检会报「应用内告警全部为空」
    # 而实际有告警挂着。这里按结构取第一个 `"count":`（顶层字段）的数值。
    acount=$(printf '%s' "$alerts_json" | awk -F'"count":' '{ if (match($2, /[0-9]+/)) print substr($2, RSTART, RLENGTH); else print "" }')
    if [ -z "${acount:-}" ]; then
        note '告警快照格式无法解析'
    elif [ "$acount" -eq 0 ]; then
        pass '应用内告警全部为空'
    else
        anames=$(printf '%s' "$alerts_json" \
            | grep -o '"name":"[^"]*"' | sed 's/"name":"//; s/"$//' | tr '\n' ' ')
        # 端点刻意不给 detail（里面可能有账号名与上游错误原文，只对回环开也不该
        # 顺手扩大暴露面）。但巡检是宿主机上的 root，库就在本地 —— 直接从
        # `operational_alerts` 取 detail，比「按名字去日志里翻」实际得多：
        # `transition()` 进入时只写一行，持续 active 的告警在日志里早就滚没了。
        #
        # 2026-11-26 之前这里只有一行「N 项预警：名字1 名字2」，看到
        # `workbuddy_low_accounts` 仍然不知道该不该处理 —— 而那条 detail 里
        # 写着「2 个账号余额低于 1000 分（ì 801、慢点 897）；其中 慢点 掉得最快，
        # 按这速度约 18 天用完」，本来就已经能回答这个问题。
        alert_rows=$(sqlite3 -readonly -separator '|' "$DB" \
            "SELECT name, datetime(since,'unixepoch','localtime'), detail
             FROM operational_alerts ORDER BY since;" 2>/dev/null)
        if [ -n "$alert_rows" ]; then
            printf '%s\n' "$alert_rows" | while IFS='|' read -r aname asince adetail; do
                [ -n "$aname" ] || continue
                printf '  · %s（自 %s）\n' "$aname" "${asince:-未知}"
                [ -n "$adetail" ] && printf '    %s\n' "$adetail"
            done
            note "$acount 项预警（明细见上）"
        else
            note "$acount 项预警：$anames（取不到明细，按名字查日志）"
        fi
    fi
else
    note '取不到告警快照（端点未上线或服务未起）'
fi

section '恢复快照'
# 每日 04:20 由 zhihui-recovery.timer 触发一次完整加密快照（含真密钥解密校验）。
#
# 2026-11-26：它连续两天失败（11-24、11-26），而巡检报的是「ok 28 / 问题 0」——
# 因为这一段根本不存在，没人看得见。失败原因还在发布树里：
# `server/backup_application.py` 首行带 CRLF，内核直接执行时找不到 `python\r`
# 这个解释器，错误却报成「文件不存在」。
#
# 这不是「备份有点旧」，而是**灾难恢复能力已经归零**，所以快照过期与 service
# failed 都判 FAIL，不判 warn —— warn 会被当成「待观察」，而它等不起。
SNAPSHOT_ROOT=/root/backups/zhihui-v2/snapshots
# 用 find 而不是 `ls */`：巡检可能被非 root 调用，`*/` 会因读不到 700 目录而
# 不展开，脚本会误报「没有任何恢复快照」。
newest_snapshot=$(find "$SNAPSHOT_ROOT" -mindepth 1 -maxdepth 1 -type d \
    -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
if [ -z "$newest_snapshot" ]; then
    broken "没有任何恢复快照（$SNAPSHOT_ROOT 为空）—— 出事时没有可回滚的完整备份"
else
    snap_age_h=$(( ( $(date +%s) - $(stat -c %Y "$newest_snapshot") ) / 3600 ))
    snap_name=$(basename "$newest_snapshot")
    snap_size=$(du -sh "$newest_snapshot" 2>/dev/null | cut -f1)
    if [ "$snap_age_h" -le 36 ]; then
        pass "最近快照 $snap_name（${snap_age_h} 小时前，${snap_size}）"
    else
        broken "最近快照 $snap_name 已是 ${snap_age_h} 小时前（阈值 36 小时）：每日快照没有成功"
    fi
fi
recovery_state=$(systemctl is-active zhihui-recovery.service 2>/dev/null)
case "$recovery_state" in
    failed)
        broken "zhihui-recovery.service 处于 failed（$(systemctl show zhihui-recovery.service -p Result --value 2>/dev/null)）：journalctl -u zhihui-recovery -n 30 看原因"
        ;;
    activating)
        note 'zhihui-recovery.service 正在跑（完整快照要几分钟）'
        ;;
    *)
        # oneshot 跑完就是 inactive —— "没在运行"不等于"上次失败了"，
        # 只有 Result 能回答后者。写 inactive 会让人以为备份没在跑。
        recovery_result=$(systemctl show zhihui-recovery.service -p Result --value 2>/dev/null)
        if [ "$recovery_result" = success ]; then
            pass "zhihui-recovery.service 上次运行成功（跑完即退出，属正常）"
        else
            note "zhihui-recovery.service 状态 $recovery_state（Result=${recovery_result:-未知}）"
        fi
        ;;
esac

# 每日应用备份（05:30 cron，明文 tar + 配对账本）。
#
# 「恢复快照」查的是加密快照（04:20 timer），这是**另一套**：它把主库、门户库与
# 配对账本打进一个 tar，生成时真的解出来校验过一次 —— `latest.json` 里的
# `restore_check` 就是那次的结果。两套互不替代：加密快照防的是介质损坏，这套防的
# 是「库写坏了但文件还在」。
#
# 2026-11-26 之前这里只有加密快照那一套，应用备份跑没跑没人看。而它在 11-23 与
# 11-25 各失败过一次（首行 CRLF 让内核找不到解释器；OOM 撞上 512MB 限制），
# 全靠人翻 /var/log/wb2api-backup.log 才发现 —— 每天 05:50 的日志里躺着几行
# FileNotFoundError，而巡检报「问题 0」。
APP_BACKUP_LATEST=/root/backups/workbuddy-daily/latest.json
if [ -r "$APP_BACKUP_LATEST" ]; then
    app_age_h=$(( ( $(date +%s) - $(stat -c %Y "$APP_BACKUP_LATEST") ) / 3600 ))
    app_check=$(grep -o '"restore_check": "[^"]*"' "$APP_BACKUP_LATEST" 2>/dev/null | tail -1 | cut -d'"' -f4)
    app_bytes=$(grep -o '"bytes": [0-9]*' "$APP_BACKUP_LATEST" 2>/dev/null | head -1 | cut -d' ' -f2)
    app_mb=$(( ${app_bytes:-0} / 1048576 ))
    # 阈值 50 小时而不是 26：备份每天一次，撞上维护窗口不该立刻判 FAIL。
    # 但连续两天没成功就是真的停了 —— 那时 50 小时已经过去。
    if [ "$app_age_h" -gt 50 ]; then
        broken "应用备份已是 ${app_age_h} 小时前（阈值 50 小时）：每天 05:30 的备份没成功，查 /var/log/wb2api-backup.log"
    elif [ "$app_age_h" -gt 26 ]; then
        note "应用备份 ${app_age_h} 小时前（${app_mb} MB，恢复校验 ${app_check:-未知}）—— 比每日一次慢了些"
    else
        pass "应用备份 ${app_age_h} 小时前（${app_mb} MB，恢复校验 ${app_check:-未知}）"
    fi
elif [ "$(id -u)" -ne 0 ]; then
    note '应用备份在 /root 下，非 root 跑巡检看不到（用 sudo）'
else
    broken '找不到应用备份 latest.json —— 每天 05:30 的备份没有产出'
fi

section '数据库'
if [ -r "$DB" ]; then
    size=$(du -h "$DB" 2>/dev/null | cut -f1)
    pages=$(sqlite3 -readonly "$DB" 'pragma page_count;' 2>/dev/null)
    free=$(sqlite3 -readonly "$DB" 'pragma freelist_count;' 2>/dev/null)
    mode=$(sqlite3 -readonly "$DB" 'pragma journal_mode;' 2>/dev/null)
    printf '  %s，%s 页，空闲 %s 页，journal=%s\n' "$size" "$pages" "$free" "$mode"
    [ "$mode" = wal ] && pass 'WAL 模式（读写不互相阻塞）' || note "journal 模式为 $mode"
    if [ -n "$free" ] && [ -n "$pages" ] && [ "$pages" -gt 0 ]; then
        # 2026-11-25：阈值原来是绝对页数 `free > 200`。在 185,588 页的库上
        # 200 页只有 0.108% —— 等于「只要有一点空闲页就建议 VACUUM」。
        #
        # 而规范第六节明令不做这件事：「对 700 MB 级数据库无脑 VACUUM ——
        # 先看空闲页；空闲页少说明没有碎片，VACUUM 只锁表不解决任何问题」。
        #
        # 也就是说，巡检原来在建议一个规范禁止的操作。改判比例，并且把
        # 代价写进文案 —— 只说「可考虑 VACUUM」而不说「它会全程锁表」，
        # 等于把决策成本藏起来。
        permille=$((free * 1000 / pages))
        if [ "$permille" -ge 100 ]; then
            note "空闲页 $free/$pages（$((permille / 10)).$((permille % 10))%）—— 回收空间需要 VACUUM，它会全程锁表，挑低峰做"
        else
            pass "碎片很少（空闲 $free/$pages，$((permille / 10)).$((permille % 10))%），VACUUM 不值得做"
        fi
    else
        note '取不到页数，无法判断碎片情况'
    fi
    rows=$(sqlite3 -readonly "$DB" 'select count(*) from request_logs;' 2>/dev/null)
    oldest=$(sqlite3 -readonly "$DB" "select date(min(ts),'unixepoch','+8 hours') from request_logs;" 2>/dev/null)
    printf '  请求记录 %s 条，最早 %s\n' "$rows" "$oldest"
else
    broken "读不到数据库 $DB"
fi

section '结论'
printf '  ok %s 项，警告 %s 项，问题 %s 项\n' "$ok" "$warn" "$bad"
if [ "$bad" -gt 0 ]; then
    printf '  => 有必须处理的问题，见上面的 FAIL 行\n'
    exit 2
elif [ "$warn" -gt 0 ]; then
    printf '  => 整体可用，但有值得关注的项\n'
    exit 1
fi
printf '  => 全部正常\n'
exit 0
