#!/usr/bin/env bash
# fork-sync-go.sh —— Go 侧本地定制的清单与同步（保守模式）
#
# 关键教训（2026-10-22）：第一次升级 v2.8.3 时只搬了「未纳入 git 的新增文件」，
# 漏掉了 10 个**已跟踪文件里的本地修改**（slowdegrade / skipExhausted /
# creditsKnown 等），导致慢速降权、额度耗尽过滤整块功能静默消失。
# 因此本脚本把两类分开：新增文件可以直接复制，修改过的文件必须走 git 三方合并。
#
# 用法：
#   fork-sync-go.sh list                      # 列清单（新增 / 修改）
#   fork-sync-go.sh import <dir> [--apply]    # 复制「新增文件」到目标源码树
#   fork-sync-go.sh check <dir>               # 校验目标是否含全部新增文件
#   fork-sync-go.sh modified                  # 列出必须走 merge 的文件
set -uo pipefail

SOURCE="${SOURCE:-/opt/workbuddy2api.bak-20261022-0920}"   # 升级前的权威副本
MANIFEST="/home/agentuser/wbfork/repo/fork-manifest.json"

_added_files() {
  python3 -c "
import json,sys
d=json.load(open('$MANIFEST'))
for x in d.get('go_files_added',[]): print(x['path'])
"
}
_modified_files() {
  python3 -c "
import json,sys
d=json.load(open('$MANIFEST'))
for x in d.get('go_files_modified',[]): print(x['path'])
"
}

cmd=${1:-list}; shift || true

case "$cmd" in
  list)
    echo "新增文件（可直接复制，权威副本 $SOURCE）："
    while read -r f; do
      [ -z "$f" ] && continue
      if [ -f "$SOURCE/$f" ]; then printf '  [有] %s\n' "$f"; else printf '  [缺] %s\n' "$f"; fi
    done < <(_added_files)
    echo
    echo "修改过的文件（必须 git merge，禁止直接覆盖）："
    while read -r f; do
      [ -z "$f" ] && continue
      n=$(grep -c "本地定制" "$SOURCE/$f" 2>/dev/null || echo 0)
      printf '  %s（%s 处本地定制标记）\n' "$f" "$n"
    done < <(_modified_files)
    ;;
  modified)
    _modified_files
    ;;
  import)
    src=${1:?用法: import <dir> [--apply]}
    apply=0; [ "${2:-}" = "--apply" ] && apply=1
    echo "源(权威): $SOURCE"; echo "目标(新上游): $src"
    while read -r f; do
      [ -z "$f" ] && continue
      if [ ! -f "$SOURCE/$f" ]; then echo "  权威副本缺: $f"; continue; fi
      if [ -f "$src/$f" ] && cmp -s "$SOURCE/$f" "$src/$f"; then echo "  已一致: $f"; continue; fi
      if [ "$apply" -eq 1 ]; then
        mkdir -p "$src/$(dirname "$f")"; cp -a "$SOURCE/$f" "$src/$f"; echo "  已复制: $f"
      else
        echo "  待复制: $f"
      fi
    done < <(_added_files)
    echo
    echo "注意：以下文件含本地修改，请用 git merge 而不是复制："
    _modified_files | sed 's/^/  - /'
    if [ "$apply" -eq 0 ]; then echo; echo "以上为演练。加 --apply 实际写入新增文件。"; fi
    ;;
  check)
    src=${1:?用法: check <dir>}
    bad=0
    while read -r f; do
      [ -z "$f" ] && continue
      [ -f "$src/$f" ] || { echo "  [缺] $f"; bad=$((bad+1)); }
    done < <(_added_files)
    [ "$bad" -eq 0 ] && echo "新增文件全部在位" || { echo "缺失 $bad 个"; exit 1; }
    ;;
  *)
    echo "用法: $0 {list|import|check|modified}" >&2; exit 2;;
esac
