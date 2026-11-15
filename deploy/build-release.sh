#!/bin/bash
# 从一个已推送的 commit 组装发布目录。
#
# ## 为什么需要它
#
# 2026-11-15：一天里手写了**五个**临时构建脚本，每个都在 `/tmp`，每个都少了点东西 ——
# 有一个把 GitHub 的 403 响应体当成文件内容写进了产物。**临时脚本不会积累防护。**
#
# 这个脚本把那些防护固定下来：**先验内容形状，再验哈希，最后才组装。**
#
# ## 为什么不用 GitHub Contents API
#
# 它按请求计数，一小时六十次。构建一次要七八个文件，跑两三次就把额度用光，
# 然后**下载会静默返回错误页**（就是上面那次的事故）。
#
# 改用 partial clone：`git clone --filter=blob:none --depth 1`，文件由
# `git cat-file` 按需取。**git 协议不占 API 额度。**
#
# ## 三道校验
#
# 1. **存在**：文件非空。这一关最弱 —— 因为先复制了上一版，文件永远非空。
# 2. **形状**：JSON 必须以 `{` 开头；任何文件不许出现 HTTP 错误响应的标记。
#    **这一关不依赖任何人填的期望值**，所以在期望值本身写错时仍然有效。
# 3. **哈希**：与调用方给的清单逐条比对。
#
# 三道都过才组装。任何一道失败就中止，**不产出一个「能用但内容不对」的目录**。
#
# ## 用法
#
#     build-release.sh <版本> <commit> <上一版目录> <哈希清单> [clone 缓存目录]
#
# 例：
#     build-release.sh v1.2.170 abc1234 /opt/.../releases/v1.2.169 /tmp/expected.txt
#
# 退出码：0 成功；1 校验失败；2 用法错误；3 目录已存在
set -uo pipefail

if [ $# -lt 4 ]; then
    echo "用法: $0 <版本> <commit> <上一版目录> <哈希清单> [clone 缓存目录]" >&2
    exit 2
fi

VERSION=$1
COMMIT=$2
PREV=$3
MANIFEST=$4
CACHE=${5:-/tmp/zhihui-build-cache}
RELEASES=$(dirname "$PREV")
DST="$RELEASES/$VERSION"
OUT="$CACHE/out-$VERSION"
REPO_URL=${ZHIHUI_REPO_URL:-https://github.com/daniei-chen/zhihui-gateway}
BRANCH=${ZHIHUI_REPO_BRANCH:-v1.2.30-hardening}
# 接口回退时要用到的 owner/repo 段，从 URL 推导，避免再维护第二处配置。
REPO_SLUG=${ZHIHUI_REPO_SLUG:-${REPO_URL#https://github.com/}}
REPO_SLUG=${REPO_SLUG%.git}

if [ -e "$DST" ]; then
    echo "REFUSE: 目标已存在: $DST" >&2
    exit 3
fi
if [ ! -d "$PREV" ]; then
    echo "FAIL: 上一版目录不存在: $PREV" >&2
    exit 2
fi
if [ ! -f "$MANIFEST" ]; then
    echo "FAIL: 哈希清单不存在: $MANIFEST" >&2
    exit 2
fi
if [ ! -d "$PREV/web/out" ]; then
    echo "FAIL: 上一版缺 web/out —— 前端产物无法在主机重建，必须从上一版继承" >&2
    exit 2
fi

mkdir -p "$CACHE"
if [ ! -d "$CACHE/repo/.git" ]; then
    echo "[build] 首次克隆（blob 懒加载，不占 API 额度）"
    git clone --no-checkout --filter=blob:none --depth 1 \
        --branch "$BRANCH" "$REPO_URL" "$CACHE/repo" || {
        echo "FAIL: 克隆失败" >&2; exit 1; }
fi
cd "$CACHE/repo" || { echo "FAIL: 进不了缓存仓库" >&2; exit 1; }

HEAD_SHA=$(git rev-parse HEAD 2>/dev/null || echo '')
if [ "$HEAD_SHA" != "$COMMIT" ]; then
    echo "[build] 缓存仓库在 $HEAD_SHA，需要 $COMMIT —— 重新克隆"
    # 必须先离开待删目录再删。
    #
    # 第一次跑这个脚本时就是这么挂的：`rm -rf "$CACHE/repo"` 执行时，bash 的
    # 当前目录正位于该目录内部，删完之后 cwd 变成一个不存在的 inode ——
    # 之后**任何**命令都报 "Unable to read current working directory"，
    # 连 `git clone` 都起不来，而错误信息看起来像是网络问题。
    cd / || exit 1
    rm -rf "$CACHE/repo"
    git clone --no-checkout --filter=blob:none --depth 1 \
        --branch "$BRANCH" "$REPO_URL" "$CACHE/repo" || {
        echo "FAIL: 克隆失败" >&2; exit 1; }
    cd "$CACHE/repo" || exit 1
    HEAD_SHA=$(git rev-parse HEAD 2>/dev/null || echo '')
    if [ "$HEAD_SHA" != "$COMMIT" ]; then
        echo "FAIL: 仍然对不上（想要 $COMMIT，得到 $HEAD_SHA）" >&2
        exit 1
    fi
fi

# 取文件：默认走 git 对象，克隆不通时退回接口。
#
# 这两条路都真实用过。克隆走 git 协议、不占请求额度，但**在中国到 GitHub 的链路上
# 会卡住** —— 2026-11-15 有两次卡到八十多秒无进展，缓存目录只有 1 MB。
# 而接口按请求计数（一小时六十次），够构建一次（七八个文件），但连续构建几次就会耗尽，
# **耗尽后下载会静默返回错误页**（那正是上一步判污染要拦的东西）。
#
# 所以：**先试不占额度的那条，卡住就换计数的这条**，两条都要过同样三道校验。
fetch_one() {
    local rel=$1 out=$2
    if git cat-file blob "HEAD:$rel" > "$out" 2>/dev/null && [ -s "$out" ]; then
        return 0
    fi
    local api="https://api.github.com/repos/${REPO_SLUG}/contents/$rel?ref=$COMMIT"
    local code
    code=$(curl -sS -m 45 -o "$out" -w '%{http_code}' \
                -H 'Accept: application/vnd.github.raw' "$api" 2>/dev/null || echo 000)
    [ "$code" = "200" ] && [ -s "$out" ]
}

rm -rf "$OUT"; mkdir -p "$OUT"
fail=0
count=0

while IFS= read -r line; do
    case "$line" in ''|'#'*) continue;; esac
    expected=$(echo "$line" | awk '{print $1}')
    rel=$(echo "$line" | awk '{print $2}')
    [ -z "$rel" ] && continue
    count=$((count + 1))

    mkdir -p "$OUT/$(dirname "$rel")"
    if ! fetch_one "$rel" "$OUT/$rel"; then
        echo "  FETCH-FAIL   $rel"
        fail=$((fail + 1)); continue
    fi

    # 第二关：形状。不依赖期望值。
    #
    # 这一关三次都和「自指」撞上，每次都是同一个形状：**被检查的东西里装着被检查的标记**。
    #
    # 1. 标记用了「文件不存在」这种普通词 —— 注释和断言里一写就命中。
    # 2. 去掉了普通词，但**这段注释本身写着那三个标记名**，脚本检查自己必然命中。
    # 3. 改成结构化匹配之后，**一份文档引用了错误响应的原文**来描述这个坑 —— 又命中。
    #
    # 前两次的解法是收紧标记。第三次不能那样解：**文档必须能引用错误形态，
    # 否则它根本说不清发生过什么。** 所以这里按文件类型分流。
    #
    # 只看代码与配置：它们里面不该出现错误响应原文。
    # 文档（.md）跳过这一关，仍受存在性与哈希约束。
    case "$rel" in
        *.md) ;;
        *)
            if grep -qiE '"message":[[:space:]]*"API rate limit|"documentation_url":"https://docs\.github\.com' "$OUT/$rel"; then
                echo "  POLLUTED     $rel  （内容是 HTTP 错误响应，不是文件）"
                fail=$((fail + 1)); continue
            fi
            if grep -qE '^Bad credentials$' "$OUT/$rel"; then
                echo "  POLLUTED     $rel  （内容是鉴权失败响应，不是文件）"
                fail=$((fail + 1)); continue
            fi
            ;;
    esac
    case "$rel" in
        *.json) grep -q '^{' "$OUT/$rel" || {
            echo "  NOT-JSON     $rel"; fail=$((fail + 1)); continue; };;
        *.md)   head -c 1 "$OUT/$rel" | grep -qE '[#A-Za-z*|-]' || {
            echo "  NOT-MARKDOWN $rel"; fail=$((fail + 1)); continue; };;
    esac

    # 第三关：哈希。
    actual=$(sha256sum "$OUT/$rel" | cut -d' ' -f1)
    if [ "$actual" != "$expected" ]; then
        echo "  HASH-MISMATCH $rel"
        echo "                期望 $expected"
        echo "                实际 $actual"
        fail=$((fail + 1))
    fi
done < "$MANIFEST"

if [ "$count" -eq 0 ]; then
    echo "FAIL: 清单里没有条目: $MANIFEST" >&2
    exit 2
fi
if [ "$fail" -ne 0 ]; then
    echo "ABORT: $count 个文件里 $fail 个没通过校验，不组装" >&2
    rm -rf "$OUT"
    exit 1
fi

# 组装：复制上一版（继承前端产物与未变更文件），再覆盖变更过的。
cp -a "$PREV" "$DST" || { echo "FAIL: 复制上一版失败" >&2; exit 1; }
while IFS= read -r line; do
    case "$line" in ''|'#'*) continue;; esac
    rel=$(echo "$line" | awk '{print $2}')
    [ -z "$rel" ] && continue
    mkdir -p "$DST/$(dirname "$rel")"
    cp "$OUT/$rel" "$DST/$rel"
    chmod 644 "$DST/$rel"
done < "$MANIFEST"

# 可执行位在这个流程里会被 `cp` 丢掉，逐次恢复。
#
# **用通配而不是列举。** 第一版是手写清单，v1.2.175 加了新脚本却忘了加进清单，
# 结果它落地后是 `Permission denied` —— 而当次发布没有报错，因为没人去执行它。
# 列举式清单的问题不是「这次漏了」，是**每次新增都要记得回来改**，
# 而漏掉时不会有任何声音。
for exe in "$DST"/deploy/*.sh "$DST"/deploy/*.py "$DST"/server/*.py; do
    [ -f "$exe" ] && chmod 755 "$exe"
done
# server 下的 Python 不全是入口脚本，上面的通配会给过多权限；把非入口的收回 644。
# 入口只有 backup_application.py（部署时被直接执行）。
for f in "$DST"/server/*.py; do
    case "$(basename "$f")" in
        backup_application.py) chmod 755 "$f";;
        *) chmod 644 "$f";;
    esac
done
cp "$DST/CHANGELOG.md" "$DST/server/CHANGELOG.md" 2>/dev/null
chmod 644 "$DST/server/CHANGELOG.md" 2>/dev/null
printf '%s\n' "$VERSION" > "$DST/.version"
chmod 644 "$DST/.version"
chown -R 0:0 "$DST/web/out" 2>/dev/null || true
chmod -R 755 "$DST/web/out" 2>/dev/null || true

echo "BUILD-OK $VERSION（$count 个文件通过三道校验）"
