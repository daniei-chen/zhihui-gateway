#!/bin/bash
# 校验前端产物调用的每个 API 路径，后端都注册了。
#
# ## 为什么需要
#
# 前端产物是**预构建**的，无法在这台机器重建。所以「前端调了一个后端没有的路径」
# 这种情况**在构建时不会被发现** —— 要等用户点下去看到 404。
#
# ## 这个审计我一开始做错了四次
#
# 1. **逐个 curl 探测**：`/api/accounts/checkin-all` 返回 404，但它其实存在 ——
#    被 `/api/accounts/{account_id}` 先匹配了（`account_id = "checkin-all"`）。
#    **「路由不存在」和「资源不存在」都是 404，探测分不开。**
# 2. **grep 完整路径**：后端写的是 `'/check-models'`（相对于 router 的 prefix），
#    完整路径 `/api/keys/check-models` 在源码里根本不存在这个字符串。
# 3. **取 OpenAPI 规范**：`/openapi.json` 返回 404 —— 开发文档端点在线上是关闭的。
#    （**这是对的**，生产环境不该暴露 API 结构。）
# 4. **import 应用列路由**：被命令守卫拦下（`python <路径>` 判为内联解释器代码）。
#
# ## 正确的做法，以及两个必须处理的坑
#
# **坑一：prefix 必须与路径在同一个文件里配对。**
# 跨文件猜 prefix 会把 `/api/keys` 和 `/check-models` 拼错。
#
# **坑二：空路径不是「跳过」，那是集合端点。**
# 第一版有 `[ -z "$route" ] && continue`，于是 `@router.get('')`（即 `/api/keys`
# 本身）被全部丢掉，对比结果凭空多出六个「前端有、后端无」的假缺失 ——
# **而那六个实际都返回 401（存在）**，正是它们揭穿了这个问题。
# **跳过条件必须是「prefix 与路径都为空」，而不是「路径为空」。**
#
# ## 用法
#
#     check-route-coverage.sh [前端产物目录] [后端源码目录]
#
# 默认值对应线上布局。退出码：0 全部覆盖；1 有前端路径后端未注册。
set -uo pipefail

FRONTEND=${1:-/opt/workbuddy-manager/releases/current/web/out}
BACKEND=${2:-/opt/workbuddy-manager/releases/current/server}

if [ ! -d "$FRONTEND" ]; then
    echo "FAIL: 前端产物目录不存在: $FRONTEND" >&2
    exit 2
fi
if [ ! -d "$BACKEND" ]; then
    echo "FAIL: 后端源码目录不存在: $BACKEND" >&2
    exit 2
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# ── 前端调用的路径 ───────────────────────────────────────────────
#
# 两种写法都要抓：
#
#   1. 带引号的字面量："/api/accounts"
#   2. **模板字符串**：`/api/keys/${e}/reset-usage`
#
# 第二版之前只抓了第一种，于是报告「89 个全部覆盖」—— **而它漏掉了所有
# 用模板字符串拼出来的路径**。后来在一个反向查询里发现三个危险操作
# （清冷却、重置用量、撤销红包）在前端是调用着的，只是写法不同。
#
# 那种漏报比「没检查」更危险：**它给出一个绿色的结论。**
grep -rhoE '"/api/[a-z0-9/_-]+"' "$FRONTEND"/_next/static/chunks/*.js 2>/dev/null \
    | tr -d '"' >> "$WORK/fe_raw.txt"
grep -rhoE '`/api/[a-z0-9/_{}$.-]+`' "$FRONTEND"/_next/static/chunks/*.js 2>/dev/null \
    | tr -d '`' | sed 's/\${[^}]*}/{}/g' >> "$WORK/fe_raw.txt"
sort -u "$WORK/fe_raw.txt" > "$WORK/fe.txt"

if [ ! -s "$WORK/fe.txt" ]; then
    echo "FAIL: 在前端产物里一个 API 路径都没找到（产物目录对吗？）" >&2
    exit 2
fi

# ── 后端注册的路径：prefix 与路径按文件配对 ──────────────────────
: > "$WORK/be.txt"
shopt -s nullglob
for f in "$BACKEND"/routers/*.py "$BACKEND"/main.py; do
    [ -f "$f" ] || continue
    prefix=$(grep -oE "APIRouter\(prefix='[^']*'" "$f" 2>/dev/null \
             | head -1 | sed "s/.*prefix='//; s/'$//")
    while IFS= read -r route; do
        # 注意：空路径是合法集合端点，只有 prefix 与路径都为空才跳过
        [ -z "${prefix}${route}" ] && continue
        printf '%s\n' "${prefix}${route}" >> "$WORK/be.txt"
    done < <(grep -oE "@router\.(get|post|put|delete|patch)\('[^']*'" "$f" 2>/dev/null \
             | sed "s/^[^']*'//; s/'$//")
    while IFS= read -r route; do
        [ -z "${prefix}${route}" ] && continue
        printf '%s\n' "${prefix}${route}" >> "$WORK/be.txt"
    done < <(grep -oE "@app\.(get|post|put|delete|patch)\('[^']*'" "$f" 2>/dev/null \
             | sed "s/^[^']*'//; s/'$//")
done
sort -u "$WORK/be.txt" -o "$WORK/be.txt"

if [ ! -s "$WORK/be.txt" ]; then
    echo "FAIL: 在后端源码里一条路由都没提取到（提取规则与代码不符？）" >&2
    exit 2
fi

# ── 归一化：路径参数与尾斜杠都不该算差异 ────────────────────────
normalise() {
    sed -e 's/{[^}]*}/{}/g' -e 's:/$::' | sort -u
}
normalise < "$WORK/fe.txt" > "$WORK/fe.n"
normalise < "$WORK/be.txt" > "$WORK/be.n"

missing=$(comm -23 "$WORK/fe.n" "$WORK/be.n")

fe_count=$(wc -l < "$WORK/fe.n")
be_count=$(wc -l < "$WORK/be.n")

if [ -z "$missing" ]; then
    echo "ROUTE-COVERAGE-OK frontend=$fe_count backend=$be_count"
    exit 0
fi

echo "ROUTE-COVERAGE-FAIL frontend=$fe_count backend=$be_count" >&2
echo "以下路径前端会调用，但后端没有注册 —— 用户点下去就是 404：" >&2
printf '  %s\n' $missing >&2
exit 1
