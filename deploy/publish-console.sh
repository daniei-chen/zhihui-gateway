#!/usr/bin/env bash
# 发布控制台（web/out）到 nginx。
#
# ## 为什么需要脚本，而不是三条手工命令
#
# 2026-11-24 线上出过一次「页面资源已更新 / 我的页面不见了」：控制台的 HTML 与
# 页面级 chunk 从 `zhihui-console-current` 提供，但 **`/_next/static/*` 是从另一
# 个缓存池目录提供的**（nginx 里 `location ^~ /_next/static/` 指向
# `/var/cache/zhihui-static-performance-*`，找不到才 fallback 到
# `/var/www/zhihui-next-pool`）。
#
# 只切 symlink、不把新的 `_next/static` 同步进那个池子，结果就是：hash 没变的
# chunk 一切正常，**凡是版本变了导致重新打包的 chunk 全部 404**。页面能打开一小块
# 然后就崩，报错文案恰好是"页面资源已更新，刷新一次即可" —— 但刷新也没用，
# 因为服务端确实没有那个文件。
#
# 所以发布控制台是三件事，不是一个：切目录、**同步静态池**、逐个校验。
# 少做第二件，症状会在用户浏览器里随机出现，而且看起来像前端 bug。
#
# 用法（宿主机，root）：
#   sudo deploy/publish-console.sh --web /tmp/webout-146.tar.gz --version 1.5.46
#   sudo deploy/publish-console.sh --web /tmp/webout-146.tar.gz --version 1.5.46 --dry-run
set -euo pipefail

WEB_TAR=""
VERSION=""
RELEASES_DIR="/var/www/zhihui-console-releases"
CURRENT_LINK="/var/www/zhihui-console-current"
# nginx 的 `location ^~ /_next/static/` 实际 root；换过路径就用 --static-pool 指定。
STATIC_POOL="/var/cache/zhihui-static-performance-20261104"
BASE_URL="https://zhihui.kaogong.art"
PAGES="/dashboard/ /accounts/ /keys/ /stats/ /portal/ /settings/system/ /claim /login"
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --web) WEB_TAR="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --static-pool) STATIC_POOL="$2"; shift 2 ;;
        --base-url) BASE_URL="$2"; shift 2 ;;
        --releases-dir) RELEASES_DIR="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

[ -n "$WEB_TAR" ] && [ -n "$VERSION" ] || { echo "用法：$0 --web <webout.tar.gz> --version <X.Y.Z>" >&2; exit 2; }
[ -f "$WEB_TAR" ] || { echo "找不到 $WEB_TAR" >&2; exit 2; }
[ -d "$STATIC_POOL" ] || { echo "静态池目录不存在：$STATIC_POOL（用 --static-pool 指定）" >&2; exit 2; }
COMPACT="${VERSION//./}"
TS="$(date '+%Y%m%dT%H%M%S')"
TARGET="$RELEASES_DIR/v${COMPACT}-$TS"

say() { printf '%s\n' "$*"; }
run() { if [ "$DRY_RUN" = 1 ]; then say "[dry-run] $*"; else "$@"; fi; }

say "== 发布控制台 v$VERSION =="
say "   目标目录 $TARGET"

# 1) 解包到新目录（不就地覆盖：旧目录留着，出问题能立刻切回去）
run mkdir -p "$TARGET"
run tar -xzf "$WEB_TAR" -C "$TARGET" --strip-components=1
run chown -R www-data:www-data "$TARGET"
[ "$DRY_RUN" = 1 ] || { chmod -R u=rwX,go=rX "$TARGET"; }
[ "$DRY_RUN" = 1 ] || [ -f "$TARGET/index.html" ] || { echo "解包后没有 index.html，中止" >&2; exit 1; }

# 2) 切 symlink（相对/绝对都可，nginx root 指到这里）
run ln -sfn "$TARGET" "$CURRENT_LINK"
if [ "$DRY_RUN" = 1 ]; then
    say "[dry-run] symlink 未切换"
else
    say "   已切换 $CURRENT_LINK -> $TARGET"
fi

# 3) 同步静态池 —— **这一步漏了就会出现首页能开、点进去白屏**
#    只补缺失文件：池子里是按内容 hash 命名的，同名必然同内容，不需要覆盖。
say "   同步 _next/static 到静态池 $STATIC_POOL"
if [ "$DRY_RUN" = 1 ]; then
    say "[dry-run] cp -a -n $TARGET/_next/static/. $STATIC_POOL/_next/static/"
else
    mkdir -p "$STATIC_POOL/_next/static"
    # `-n` 在新 coreutils 上会警告「行为不可移植」；优先用 --update=none，
    # 老版本不支持时回退。两者语义一致：只补缺失文件，不覆盖已有。
    cp -a --update=none "$TARGET/_next/static/." "$STATIC_POOL/_next/static/" 2>/dev/null \
        || cp -a -n "$TARGET/_next/static/." "$STATIC_POOL/_next/static/"
    chown -R www-data:www-data "$STATIC_POOL/_next/static"
fi

# 4) 逐个校验：把每个页面的 HTML 里引用的 /_next/static 链接全打一遍，非 200 就失败。
#    「页面 200」不算通过 —— 崩掉的正是页面里引用的 chunk。
if [ "$DRY_RUN" = 1 ]; then
    say "[dry-run] 跳过 HTTP 校验"
    say "OK（dry-run）"
    exit 0
fi
total=0; bad=0
for page in $PAGES; do
    refs="$(curl -s --max-time 10 "$BASE_URL$page" | grep -o '/_next/static/[^"]*\.\(js\|css\)' | sort -u || true)"
    for ref in $refs; do
        total=$((total+1))
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE_URL$ref" || echo 000)"
        if [ "$code" != "200" ]; then
            bad=$((bad+1))
            say "   BAD $code $page -> $ref"
        fi
    done
done
say "   校验静态资源 $total 个，失败 $bad 个"
if [ "$bad" != "0" ]; then
    say "!! 有资源 404：检查静态池路径是否与 nginx 的 location ^~ /_next/static/ 一致" >&2
    say "   回退：ln -sfn \$(readlink -f /var/www/zhihui-console-releases/<上一个>) $CURRENT_LINK" >&2
    exit 1
fi
say "OK：v$VERSION 控制台已发布并通过校验"
