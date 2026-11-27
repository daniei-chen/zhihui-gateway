#!/usr/bin/env bash
# 发布预构建控制台：资源先入池、原子切换 HTML、逐页验收；失败恢复原指针。
# nginx 的 /_next/static 可能使用独立静态池，必须在新 HTML 可见前准备好资源。
# 旧 hash 资源保留，支持已打开的浏览器；本脚本不清理历史版本或资源。
# 用法：sudo deploy/publish-console.sh --web /tmp/webout.tar.gz --version 1.6.27
set -euo pipefail

WEB_TAR=""
VERSION=""
RELEASES_DIR="/var/www/zhihui-console-releases"
CURRENT_LINK="/var/www/zhihui-console-current"
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
        --current-link) CURRENT_LINK="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
die() { say "!! $*" >&2; exit 1; }
[ -n "$WEB_TAR" ] && [ -n "$VERSION" ] || { echo "用法：$0 --web <webout.tar.gz> --version <X.Y.Z>" >&2; exit 2; }
[ -f "$WEB_TAR" ] || { echo "找不到 $WEB_TAR" >&2; exit 2; }
[ -d "$STATIC_POOL" ] || { echo "静态池目录不存在：$STATIC_POOL（用 --static-pool 指定）" >&2; exit 2; }
[[ "$VERSION" =~ ^[0-9]+(\.[0-9]+){2}([-.][A-Za-z0-9.-]+)?$ ]] || { echo "版本号格式无效" >&2; exit 2; }
COMPACT="${VERSION//./}"
TS="$(date '+%Y%m%dT%H%M%S')"
TARGET="$RELEASES_DIR/v${COMPACT}-$TS-$$"
say "== 发布控制台 v$VERSION =="
say "   目标目录 $TARGET"
if [ "$DRY_RUN" = 1 ]; then
    say "[dry-run] 校验归档并解包到新目录 $TARGET"
    say "[dry-run] 先同步 _next/static 到 $STATIC_POOL，再核对全部文件大小与摘要"
    say "[dry-run] 原子切换 $CURRENT_LINK，并验证页面及资源；失败恢复原指针"
    say "OK（dry-run；未创建目录、锁文件或请求 HTTP）"
    exit 0
fi

command -v python3 >/dev/null || die '缺少 python3，无法校验资源'
command -v flock >/dev/null || die '缺少 flock，无法串行发布'
[ -d "$(dirname "$CURRENT_LINK")" ] || die '当前指针的父目录不存在'
exec 9>"${CURRENT_LINK}.publish.lock"
flock -n 9 || die '已有控制台发布持有此入口的锁'

OLD_TARGET=''
if [ -e "$CURRENT_LINK" ] || [ -L "$CURRENT_LINK" ]; then
    [ -L "$CURRENT_LINK" ] && [ -d "$CURRENT_LINK" ] || die '当前入口不是有效的目录软链接'
    OLD_TARGET=$(readlink "$CURRENT_LINK")
fi
WORK_DIR=$(mktemp -d "${CURRENT_LINK}.publish.XXXXXX")
SWITCHED=0

atomic_link() {
    ln -s "$1" "$WORK_DIR/current" || return 1
    mv -Tf "$WORK_DIR/current" "$CURRENT_LINK"
}

finish() {
    local status=$? current
    trap - EXIT INT TERM
    if [ "$status" -ne 0 ] && [ "$SWITCHED" = 1 ]; then
        current=$(readlink "$CURRENT_LINK" 2>/dev/null || true)
        if [ "$current" = "$TARGET" ]; then
            if [ -n "$OLD_TARGET" ]; then
                if atomic_link "$OLD_TARGET"; then
                    say "   发布未通过，已恢复原指针 -> $OLD_TARGET" >&2
                else
                    say "!! 恢复原指针失败，请人工核实 $CURRENT_LINK" >&2
                fi
            else
                rm -f "$CURRENT_LINK"
                say '   首次发布未通过，已撤回未验收的入口' >&2
            fi
        elif [ "$current" != "$OLD_TARGET" ]; then
            say '!! 当前指针已被其他维护动作改变，不覆盖；请核实入口状态' >&2
        fi
    fi
    rm -rf "$WORK_DIR"
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 1) 解包前拒绝越界路径、链接和特殊文件。静态包只允许一个顶层目录。
python3 - "$WEB_TAR" <<'PY'
import sys, tarfile
from pathlib import PurePosixPath

roots = set()
seen = set()
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    for member in archive:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or '\\' in member.name
                or not path.parts or not (member.isdir() or member.isfile())):
            raise SystemExit('静态包包含不安全的路径或非普通文件')
        roots.add(path.parts[0])
        relative = path.parts[1:]
        if member.isfile() and (not relative or relative in seen):
            raise SystemExit('静态包包含重复文件或无效顶层文件')
        if member.isfile():
            seen.add(relative)
if len(roots) != 1 or ('index.html',) not in seen:
    raise SystemExit('静态包必须包含单个顶层目录及 index.html')
PY
mkdir -p "$RELEASES_DIR"
mkdir "$TARGET"
tar -xzf "$WEB_TAR" -C "$TARGET" --strip-components=1
chown -R www-data:www-data "$TARGET"
chmod -R u=rwX,go=rX "$TARGET"
[ -f "$TARGET/index.html" ] || die '解包后没有 index.html'
[ -d "$TARGET/_next/static" ] || die '解包后没有 _next/static'

# 2) 只补缺失资源，旧页面仍在服务；同名内容是否相同必须验算，不能假设。
say "   同步 _next/static 到静态池 $STATIC_POOL"
mkdir -p "$STATIC_POOL/_next/static"
cp -a --update=none "$TARGET/_next/static/." "$STATIC_POOL/_next/static/" 2>/dev/null \
    || cp -a -n "$TARGET/_next/static/." "$STATIC_POOL/_next/static/"
python3 - "$TARGET" "$STATIC_POOL" "$WORK_DIR/manifest.json" "$PAGES" <<'PY'
import hashlib, json, sys
from pathlib import Path

source, pool, manifest_path = map(Path, sys.argv[1:4])
for page in sys.argv[4].split():
    route = page.strip('/')
    if not any(path.is_file() for path in (source / route / 'index.html', source / (route + '.html'))):
        raise SystemExit('候选包缺少必需页面：' + page)
def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()

manifest = {}
for path in sorted((source / '_next/static').rglob('*')):
    if path.is_symlink():
        raise SystemExit('候选静态目录不能包含符号链接')
    if not path.is_file():
        continue
    relative = path.relative_to(source)
    destination = pool / relative
    expected = {'size': path.stat().st_size, 'sha256': digest(path)}
    if (destination.is_symlink() or not destination.is_file()
            or destination.stat().st_size != expected['size']
            or digest(destination) != expected['sha256']):
        raise SystemExit(f'静态池资源缺失或与候选内容不一致：{relative}')
    manifest['/' + relative.as_posix()] = expected
if not any(Path(name).suffix in {'.js', '.css'} for name in manifest):
    raise SystemExit('候选静态资源清单为空')
manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
PY

# 3) 同文件系统 rename 原子替换指针；失败或被中止由 EXIT trap 恢复旧指针。
# 必须先启用回退：rename 已成功、mv 尚未返回时也可能收到 INT/TERM。
SWITCHED=1
atomic_link "$TARGET"
say "   已切换 $CURRENT_LINK -> $TARGET"

# 4) 必需页面须为 200，并引用本次候选页面的资源；零引用/旧缓存页不是成功。
total=0
declare -A VERIFIED_ASSETS=()
for page in $PAGES; do
    code=$(curl -sS -o "$WORK_DIR/page.html" -w '%{http_code}' --max-time 10 "$BASE_URL$page") \
        || die "页面请求失败：$page"
    [ "$code" = 200 ] || die "页面状态错误：$page HTTP $code"
    refs=$(python3 - "$TARGET" "$WORK_DIR/manifest.json" "$WORK_DIR/page.html" "$page" <<'PY'
import json, sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = set()
    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name not in {'src', 'href'} or not value:
                continue
            url = urlsplit(value)
            if (not url.scheme and not url.netloc and url.path.startswith('/_next/static/')
                    and Path(url.path).suffix in {'.js', '.css'}):
                self.refs.add(value)

def references(path):
    parsed = References()
    parsed.feed(path.read_text(encoding='utf-8'))
    return parsed.refs

target, manifest_path, actual_path = map(Path, sys.argv[1:4])
route = sys.argv[4].strip('/')
candidates = [target / route / 'index.html', target / (route + '.html')]
expected_path = next((path for path in candidates if path.is_file()), None)
if expected_path is None:
    raise SystemExit('候选包缺少必需页面：' + sys.argv[4])
expected, actual = references(expected_path), references(actual_path)
if not expected or not actual or expected != actual:
    raise SystemExit('页面引用为空或不属于本次候选页面：' + sys.argv[4])
manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
for reference in sorted(actual):
    if urlsplit(reference).path not in manifest:
        raise SystemExit('页面引用不在候选清单内：' + reference)
    print(reference)
PY
    ) || die "页面内容校验失败：$page"
    while IFS= read -r ref; do
        total=$((total+1))
        # 同一个 hash 资源只下载校验一次，避免小带宽主机重复下载公共 chunk。
        [ "${VERIFIED_ASSETS[$ref]:-0}" = 1 ] && continue
        code=$(curl -sS -o "$WORK_DIR/asset" -w '%{http_code}' --max-time 10 "$BASE_URL$ref") \
            || die "静态资源请求失败：$ref"
        [ "$code" = 200 ] || die "静态资源状态错误：$page -> $ref HTTP $code"
        python3 - "$WORK_DIR/manifest.json" "$WORK_DIR/asset" "$ref" <<'PY'
import hashlib, json, sys
from pathlib import Path
from urllib.parse import urlsplit

expected = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))[urlsplit(sys.argv[3]).path]
asset = Path(sys.argv[2])
digest = hashlib.sha256()
with asset.open('rb') as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b''):
        digest.update(block)
if asset.stat().st_size != expected['size'] or digest.hexdigest() != expected['sha256']:
    raise SystemExit('HTTP 静态资源与候选摘要不一致：' + sys.argv[3])
PY
        VERIFIED_ASSETS["$ref"]=1
    done <<< "$refs"
done
say "   校验静态资源 $total 个引用，全部通过"
say "OK：v$VERSION 控制台已发布并通过校验"
