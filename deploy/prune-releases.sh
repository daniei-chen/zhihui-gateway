#!/bin/bash
# 按数量上限清理历史 release 目录。
#
# ## 为什么需要
#
# 每次部署都会在 `releases/` 下留一个新目录。2026-11-15 实测 **146 个目录占了 4.9 GB**，
# 根分区到 **94%**，而其中绝大多数是几个月前的一次性候选版本，永远不会再被启动。
#
# 没有清理时它只增不减。
#
# ## 保留规则
#
# 按**版本号语义**排序。保留集合：
#   1. `current` 指向的那个（**永远保留**）
#   2. 版本号最高的 N 个（默认 8）
#   3. 任何**正被进程使用**的（cwd 落在该目录里的）
#
# 其它一律视为候选。
#
# ## 两个曾经写错的地方（都验证过会产生真实破坏）
#
# **一、忘了排序。** 只做 `>> "$SORTED"` 追加，读回来时按 glob 的字母序处理，
# 于是 `v0.0.10+stable` 排在 `v1.2.105` 前面，被当成「最新」保留 ——
# 而真正的新版本全进了删除清单。**第一版 dry-run 就是这样，幸好验证了保留清单。**
#
# **二、版本号比较用了字符串序。** `v1.2.100` 的字符串排在 `v1.2.99` 之前，
# 直接用会删掉最新版。所以先把版本补零成 `A.BBB.CCC` 再比较。
#
# 现在这两个都有明确处理，并且 dry-run 会打印完整的 KEEP 与 DROP 清单供人核对。
#
# ## 为什么默认 dry-run
#
# 删目录不可逆。默认只打印计划，加 `--apply` 才真动手。
#
# ## 性能
#
# 「谁在用哪个目录」只扫一遍 `/proc` 并落成一张表；不是每个目录各扫一遍。
# 后者的实测是 146 个目录跑两分钟还没完。
#
# ## 用法
#
#   prune-releases.sh                    # 看计划（保留 8 个）
#   prune-releases.sh --keep 5           # 看计划（保留 5 个）
#   prune-releases.sh --keep 5 --apply   # 真删
set -uo pipefail

ROOT=/opt/workbuddy-manager/releases
KEEP=8
APPLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --keep)  KEEP="${2:?--keep 需要一个数字}"; shift 2 ;;
        --apply) APPLY=1; shift ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

case "$KEEP" in
    ''|*[!0-9]*) echo "--keep 必须是正整数" >&2; exit 2 ;;
esac
[ "$KEEP" -ge 1 ] || { echo "--keep 至少为 1" >&2; exit 2; }
[ -d "$ROOT" ] || { echo "目录不存在：$ROOT" >&2; exit 1; }

SORTED=$(mktemp /tmp/prune-sorted.XXXXXX)
PINNED=$(mktemp /tmp/prune-pinned.XXXXXX)
DROP=$(mktemp /tmp/prune-drop.XXXXXX)
BUSY=$(mktemp /tmp/prune-busy.XXXXXX)
cleanup() { rm -f "$SORTED" "$PINNED" "$DROP" "$BUSY"; }
trap cleanup EXIT INT TERM

CURRENT=""
if [ -L "$ROOT/current" ]; then
    CURRENT=$(readlink -f "$ROOT/current" 2>/dev/null || true)
fi

sort_key() {
    local name
    name=$(basename "$1")
    if [[ "$name" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)(\+.*)?$ ]]; then
        printf '%03d.%03d.%03d' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
    else
        printf '000.000.000'
    fi
}

collect_busy() {
    local pid cwd rel
    for pid in /proc/[0-9]*; do
        cwd=$(readlink "$pid/cwd" 2>/dev/null) || continue
        case "$cwd" in
            "$ROOT"/*)
                rel=${cwd#"$ROOT"/}
                printf '%s\n' "${rel%%/*}"
                ;;
        esac
    done | sort -u > "$BUSY"
}

is_busy() {
    grep -qxF "$1" "$BUSY" 2>/dev/null
}

echo "根目录：$ROOT"
echo "保留数量：$KEEP"
echo "current：${CURRENT:-（无符号链接）}"

collect_busy
echo "在用的目录：$(wc -l < "$BUSY" | tr -d ' ') 个"
echo

for dir in "$ROOT"/*/; do
    [ -d "$dir" ] || continue
    name=$(basename "$dir")
    [ "$name" = current ] && continue
    real=$(readlink -f "$dir" 2>/dev/null || echo "$dir")

    if [ -n "$CURRENT" ] && [ "$real" = "$CURRENT" ]; then
        echo "PIN   current          $name"
        printf '%s\n' "$name" >> "$PINNED"
        continue
    fi
    if is_busy "$name"; then
        echo "PIN   in-use          $name"
        printf '%s\n' "$name" >> "$PINNED"
        continue
    fi
    printf '%s\t%s\n' "$(sort_key "$dir")" "$dir" >> "$SORTED"
done

TOTAL=$(wc -l < "$SORTED" | tr -d ' ')
PINNED_N=$(wc -l < "$PINNED" | tr -d ' ')

sort -r "$SORTED" -o "$SORTED"
KEPT_N=0
while IFS=$'\t' read -r key dir; do
    [ -n "$dir" ] || continue
    name=$(basename "$dir")
    if [ "$KEPT_N" -lt "$KEEP" ]; then
        echo "KEEP  newest #$((KEPT_N + 1))     $name"
        KEPT_N=$((KEPT_N + 1))
    else
        echo "DROP  stale            $name"
        printf '%s\n' "$dir" >> "$DROP"
    fi
done < "$SORTED"

DROPN=$(wc -l < "$DROP" | tr -d ' ')
DROPMB=0
if [ "$DROPN" -gt 0 ]; then
    while IFS= read -r d; do
        mb=$(du -sm "$d" 2>/dev/null | awk '{print $1}')
        DROPMB=$((DROPMB + ${mb:-0}))
    done < "$DROP"
fi

echo
echo "统计：候选 $TOTAL 个，保留最新 $KEPT_N 个；另 PIN 住 $PINNED_N 个；待删 $DROPN 个，约 ${DROPMB} MB"

BAD=0
while IFS= read -r d; do
    [ -n "$d" ] || continue
    name=$(basename "$d")
    if [ -n "$CURRENT" ] && [ "$(readlink -f "$d" 2>/dev/null)" = "$CURRENT" ]; then
        echo "!! 自检失败：待删清单里有 current（$name）" >&2
        BAD=$((BAD + 1))
    fi
    if grep -qxF "$name" "$PINNED"; then
        echo "!! 自检失败：待删清单里有 PIN 住的（$name）" >&2
        BAD=$((BAD + 1))
    fi
done < "$DROP"

if [ "$BAD" -gt 0 ]; then
    echo "!! 自检未通过，拒绝执行任何删除" >&2
    exit 1
fi

if [ "$APPLY" -eq 0 ]; then
    echo
    echo "（dry-run：未删除任何东西。加 --apply 才会执行）"
    exit 0
fi

echo
echo "开始删除…"
FAILED=0
DONE=0
while IFS= read -r d; do
    [ -n "$d" ] || continue
    name=$(basename "$d")
    real=$(readlink -f "$d" 2>/dev/null || echo "$d")
    if [ -n "$CURRENT" ] && [ "$real" = "$CURRENT" ]; then
        echo "  跳过（是 current）：$d" >&2
        FAILED=$((FAILED + 1))
        continue
    fi
    if is_busy "$name"; then
        echo "  跳过（被进程使用）：$d" >&2
        FAILED=$((FAILED + 1))
        continue
    fi
    if rm -rf -- "$d" 2>/dev/null; then
        echo "  removed $name"
        DONE=$((DONE + 1))
    else
        echo "  失败：$name" >&2
        FAILED=$((FAILED + 1))
    fi
done < "$DROP"

echo
echo "完成：删除 $DONE 个，跳过或失败 $FAILED 个"
df -m / 2>/dev/null | tail -1 | awk '{print "当前可用：" $4 " MB（已用 " $5 "）"}'
[ "$FAILED" -eq 0 ]
