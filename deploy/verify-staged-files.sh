#!/bin/bash
# 校验一个已组装的发布目录里，指定文件的内容与期望哈希一致。
#
# ## 为什么需要它
#
# 2026-11-15：发布组装的做法是「复制上一版 → 逐个下载变更文件 → 检查文件非空」。
# 那次 GitHub API 返回 403（速率限制），下载失败。**但因为是先复制的，文件仍然
# 非空**，于是「非空」这一关通过了 —— 有一个文件静默地留在了上一版。
#
# 症状极难发现：服务照常运行、完整性闭包 ok、接口全部正常，只有一个元数据文件
# 里的版本号慢了一版。
#
# **「文件存在」与「文件正确」是两件事。** 这个脚本校验后者。
#
# ## 用法
#
#     verify-staged-files.sh <目标目录> <期望哈希清单>
#
# 清单每行 `<sha256>  <相对路径>`（与 `sha256sum` 输出同格式，可直接由它生成）。
# 空行与 `#` 开头的行会被忽略。
#
# 退出码：0 全部一致；1 有差异；2 用法或输入错误。
#
# ## 为什么不用 `sha256sum -c`
#
# 它其实可以，但它对「文件缺失」与「内容不符」报的是同一类错误，而且输出是英文的
# 逐行日志 —— 在这个流程里，需要一眼看出「哪个文件错了、错在哪」，以及**在文件缺失
# 时明确区分出来**（缺失通常意味着下载失败，而不是内容被改）。
set -uo pipefail

if [ $# -ne 2 ]; then
    echo "用法: $0 <目标目录> <期望哈希清单>" >&2
    exit 2
fi

TARGET=$1
MANIFEST=$2

if [ ! -d "$TARGET" ]; then
    echo "FAIL: 目标目录不存在: $TARGET" >&2
    exit 2
fi
if [ ! -r "$MANIFEST" ]; then
    echo "FAIL: 清单不可读: $MANIFEST" >&2
    exit 2
fi

checked=0
missing=0
differ=0
declare -a problems=()

while IFS= read -r line || [ -n "$line" ]; do
    # 去掉行尾回车（清单可能是 CRLF）
    line=${line%$'\r'}
    case "$line" in
        ''|'#'*) continue ;;
    esac
    expected=${line%% *}
    rel=${line#* }
    # 兼容 `sha256sum` 的两空格分隔与路径前可能的 `*`
    rel=${rel# }
    rel=${rel#\*}
    if [ -z "$expected" ] || [ -z "$rel" ]; then
        continue
    fi
    checked=$((checked + 1))
    path="$TARGET/$rel"
    if [ ! -f "$path" ]; then
        missing=$((missing + 1))
        problems+=("MISSING  $rel")
        continue
    fi
    actual=$(sha256sum "$path" 2>/dev/null | cut -d' ' -f1)
    if [ "$actual" != "$expected" ]; then
        differ=$((differ + 1))
        problems+=("DIFFER   $rel")
        problems+=("         期望 ${expected:0:16}…  实际 ${actual:0:16}…")
    fi
done < "$MANIFEST"

if [ "$checked" -eq 0 ]; then
    echo "FAIL: 清单里没有任何可校验的条目: $MANIFEST" >&2
    exit 2
fi

if [ "$missing" -eq 0 ] && [ "$differ" -eq 0 ]; then
    echo "STAGED-FILES-OK checked=$checked"
    exit 0
fi

echo "STAGED-FILES-FAIL checked=$checked missing=$missing differ=$differ" >&2
for item in "${problems[@]}"; do
    echo "  $item" >&2
done
echo "" >&2
echo "缺失通常意味着下载没成功（旧文件被复制过来掩盖了这一点）；" >&2
echo "内容不符意味着下载到了别的版本。两种情况都不该继续部署。" >&2
exit 1
