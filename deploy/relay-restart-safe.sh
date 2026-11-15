#!/bin/bash
# 不停机重启转发层。
#
# 为什么需要它：转发层是独立进程，不在滚动交接范围内。uvicorn 收到 SIGTERM 后
# 走优雅关闭，会等既有连接（含 nginx 的 keep-alive 空闲连接）自然结束 —— 实测
# 从发出停止到新进程就绪有约 11 秒的窗口，那段时间所有推理请求得到 502。
#
# 做法：入站流量的目标是一个 map 变量。先把变量切到主服务（直连），reload 之后
# 新连接不再经过转发层；此时重启转发层不会影响任何在途请求；就绪后再切回来。
# reload 是平滑的（旧 worker 处理完存量连接才退出），本身不产生中断。
#
# 前置条件：/etc/nginx/conf.d/zhihui-relay-upstream.conf 已存在，且
# zhihui-rolling-relay.location.conf 用的是 `proxy_pass $zhihui_relay_upstream`。
set -euo pipefail

MAP=/etc/nginx/conf.d/zhihui-relay-upstream.conf
DIRECT=http://127.0.0.1:7866
RELAY=http://127.0.0.1:7867
UNIT=zhihui-rolling-relay.service

switch_to() {
    local target="$1"
    printf 'map $host $zhihui_relay_upstream {\n    default %s;\n}\n' "$target" > "$MAP"
    # 配置写坏就绝不 reload：保留上一次的配置，本次调用以非零退出。
    nginx -t >/dev/null 2>&1 || { echo "nginx -t 失败，未 reload（当前仍指向上次的目标）" >&2; return 1; }
    nginx -s reload
}

ready() {
    # 401 = 进程已经在监听并处理请求（这个端点需要认证）。
    local code
    code=$(curl -s -o /dev/null -m 2 -w '%{http_code}' "$RELAY/v1/models" 2>/dev/null || true)
    [ "$code" = "401" ] || [ "$code" = "200" ]
}

echo "1/4 把入站流量切到主服务（直连 $DIRECT）"
switch_to "$DIRECT"
sleep 1

echo "2/4 重启 $UNIT"
systemctl restart "$UNIT"

echo "3/4 等转发层就绪"
for _ in $(seq 1 30); do
    if ready; then break; fi
    sleep 1
done
if ! ready; then
    echo "转发层 30 秒内未就绪；入站仍直连主服务，请检查后再切回" >&2
    exit 1
fi
echo "    转发层已就绪"

echo "4/4 把入站流量交回转发层（$RELAY）"
switch_to "$RELAY"
echo "完成：转发层已重启，入站已回到转发层"
