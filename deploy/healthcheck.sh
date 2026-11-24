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

section '反代与中继'
grep -q 'zhihui_relay_upstream' "$MAPFILE" 2>/dev/null \
    && pass '推理流量变量已定义' || broken "缺少 \$zhihui_relay_upstream（$MAPFILE）"
[ -f "$SNIPPET" ] && pass '中继片段已安装' || broken "中继片段缺失：$SNIPPET"
grep -q 'zh-relay-inference' "$SITE" 2>/dev/null \
    && pass '站点已接中继' || broken '站点没有接中继，发布时会打断在途调用'
relay_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:7867/v1/models 2>/dev/null)
[ "$relay_code" = 401 ] && pass '中继在线并转发正常' || broken "中继探测返回 $relay_code（期望 401）"

section '页面'
for url in / /portal/ /keys/ /dashboard/ /members/; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$HOST$url" 2>/dev/null)
    [ "$code" = 200 ] && pass "$url 200" || broken "$url 返回 $code"
done

section '近 24 小时错误率'
if [ -r "$ACCESS_LOG" ]; then
    since=$(date -u -d '24 hours ago' '+%d/%b/%Y:%H:%M:%S' 2>/dev/null)
    total=$(awk -v s="$since" '$4 > "["s' "$ACCESS_LOG" 2>/dev/null | wc -l)
    errors=$(awk -v s="$since" '$4 > "["s && $9 ~ /^5/' "$ACCESS_LOG" 2>/dev/null | wc -l)
    if [ "$total" -gt 0 ]; then
        rate=$(awk -v e="$errors" -v t="$total" 'BEGIN{printf "%.2f", 100*e/t}')
        printf '  请求 %s，5xx %s（%s%%）\n' "$total" "$errors" "$rate"
        if [ "$errors" -eq 0 ]; then
            pass '近 24 小时无 5xx'
        elif [ "$(awk -v r="$rate" 'BEGIN{print (r<1)?1:0}')" = 1 ]; then
            note "5xx 占比 $rate%，低于 1% —— 多半是推理调用在上游侧失败"
        else
            broken "5xx 占比 $rate%，需要排查"
        fi
    else
        note '近 24 小时没有请求记录'
    fi
else
    note "读不到 $ACCESS_LOG"
fi

section '磁盘'
used=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$used" ]; then
    if [ "$used" -lt 80 ]; then
        pass "根分区已用 ${used}%"
    elif [ "$used" -lt 90 ]; then
        note "根分区已用 ${used}%，该清理了"
    else
        broken "根分区已用 ${used}%，随时可能写满"
    fi
fi
journal=$(journalctl --disk-usage 2>/dev/null | grep -o '[0-9.]*[MG]' | head -1)
[ -n "$journal" ] && printf '  journal 占用 %s（超过 500M 可考虑清理）\n' "$journal"
console_count=$(ls /var/www/zhihui-console-releases 2>/dev/null | wc -l)
printf '  控制台历史版本 %s 个' "$console_count"
[ "$console_count" -gt 8 ] && printf '（偏多，可清理旧版）\n' || printf '\n'
release_count=$(ls /opt/workbuddy-manager/releases 2>/dev/null | grep -c '^v1')
printf '  应用历史版本 %s 个\n' "$release_count"

section '数据库'
if [ -r "$DB" ]; then
    size=$(du -h "$DB" 2>/dev/null | cut -f1)
    pages=$(sqlite3 -readonly "$DB" 'pragma page_count;' 2>/dev/null)
    free=$(sqlite3 -readonly "$DB" 'pragma freelist_count;' 2>/dev/null)
    mode=$(sqlite3 -readonly "$DB" 'pragma journal_mode;' 2>/dev/null)
    printf '  %s，%s 页，空闲 %s 页，journal=%s\n' "$size" "$pages" "$free" "$mode"
    [ "$mode" = wal ] && pass 'WAL 模式（读写不互相阻塞）' || note "journal 模式为 $mode"
    if [ -n "$free" ] && [ "$free" -gt 200 ]; then
        note "空闲页 $free 偏多，可考虑 VACUUM 回收空间"
    else
        pass '碎片很少，无需 VACUUM'
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
