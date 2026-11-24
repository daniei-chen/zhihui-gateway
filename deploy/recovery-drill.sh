#!/bin/bash
# 灾备恢复演练：从每日备份解出 manager 的代码与数据，在**隔离目录 + 隔离端口**
# 启动一个真实实例，验证它能否起来并响应健康检查。
#
# 全程不碰生产：不同端口（7870）、不同数据目录、不注册 systemd、不写 /opt。
set -uo pipefail

BK=/root/backups/workbuddy-daily/recovery-20261116-053001.tar.gz
WORK=/var/tmp/dr-drill
PORT=7870

say() { printf '[drill] %s\n' "$*"; }

say "1) 清理并解包（只取 manager/ 前缀）"
rm -rf "$WORK"
mkdir -p "$WORK"
tar xzf "$BK" -C "$WORK" --strip-components=1 manager/ 2>/dev/null || {
    say "解包失败"; exit 1; }
say "   解出: $(find "$WORK" -type f | wc -l) 个文件"

say "2) 校验关键文件"
for f in data/manager.db data/users.json server/main.py server/serve.py; do
    if [ -f "$WORK/$f" ]; then
        say "   OK $f ($(stat -c%s "$WORK/$f") 字节)"
    else
        say "   缺失 $f"; exit 1
    fi
done

say "3) 数据库完整性检查（只读）"
DB="$WORK/data/manager.db"
if sqlite3 "$DB" 'PRAGMA integrity_check;' | grep -qx ok; then
    say "   integrity_check = ok"
else
    say "   integrity_check 失败"; sqlite3 "$DB" 'PRAGMA integrity_check;' | head -5; exit 1
fi
ROWS=$(sqlite3 "$DB" 'SELECT count(*) FROM request_logs;' 2>/dev/null)
say "   request_logs = $ROWS 行"
VERSION=$(sqlite3 "$DB" "SELECT value FROM settings WHERE key='quota.daily-seed.v1';" 2>/dev/null)
say "   配额种子标记 = ${VERSION:-<无>}"

say "4) 检查解出的代码是否与当前发布同源"
REL=/opt/workbuddy-manager/releases/current
if [ -f "$WORK/server/main.py" ]; then
    A=$(sha256sum "$WORK/server/main.py" | cut -d' ' -f1)
    B=$(sha256sum "$REL/server/main.py" | cut -d' ' -f1)
    say "   备份内 main.py = ${A:0:16}"
    say "   现役   main.py = ${B:0:16}"
    [ "$A" = "$B" ] && say "   同源" || say "   **不同源**（备份代码与现役不一致）"
fi

say "5) 用隔离数据目录 + 隔离端口启动（$PORT）"
chmod -R u+rwX "$WORK" 2>/dev/null
export WB_MANAGER_HOST=127.0.0.1
export WB_MANAGER_PORT=$PORT
export WB_DATA_DIR="$WORK/data"
export WB_STATIC_DIR="$WORK/web/out"
export WB_AUTH_DIR=/opt/workbuddy2api/auths
export WB_UPSTREAM_CONFIG=/opt/workbuddy2api/config.json
export WB_SECURE_COOKIE=auto
export WB_TRUST_PROXY=1
export WB_ENABLE_DOCS=0
export WB_BACKGROUND_JOBS=0
export WB_GATEWAY_MAX_CONCURRENCY=8
export PYTHONPATH=/opt/workbuddy-manager
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX=/tmp/dr-pycache

cd /opt/workbuddy-manager || exit 1
setsid nohup /opt/workbuddy-manager/venv/bin/python -m server.serve \
    --host 127.0.0.1 --port "$PORT" --timeout-graceful-shutdown 60 \
    > "$WORK/drill.log" 2>&1 < /dev/null &
echo $! > "$WORK/drill.pid"
say "   pid=$(cat "$WORK/drill.pid")，等待就绪…"

for i in $(seq 1 30); do
    sleep 2
    CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || echo 000)
    [ "$CODE" = "200" ] && break
done

say "6) 健康检查"
H=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || echo 000)
R=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$PORT/readyz" 2>/dev/null || echo 000)
say "   healthz=$H  readyz=$R"

if [ "$H" = "200" ]; then
    say "   响应体: $(curl -s -m 5 "http://127.0.0.1:$PORT/healthz")"
    say "   数据面 /v1/models: $(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$PORT/v1/models")（401=需密钥，符合预期）"
    say "   面板 /api/me: $(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$PORT/api/me")（401=未登录，符合预期）"
    DRILL_OK=1
else
    say "   启动失败，日志尾部："
    tail -25 "$WORK/drill.log"
    DRILL_OK=0
fi

say "7) 停止演练实例"
kill "$(cat "$WORK/drill.pid")" 2>/dev/null
sleep 3
kill -9 "$(cat "$WORK/drill.pid")" 2>/dev/null
say "   已停止"

say ""
if [ "${DRILL_OK:-0}" = "1" ]; then
    say "== 演练通过：备份可恢复并启动 =="
    exit 0
else
    say "== 演练失败 =="
    exit 1
fi
