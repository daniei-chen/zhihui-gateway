#!/bin/bash
# 把恢复相关的脚本从发布目录安装到 systemd 实际执行的位置。
#
# ## 为什么需要它
#
# 2026-11-15：修掉了快照清理任务的一个崩溃（遇到半成品目录就整个中止），
# 部署后发现**修复没有生效** —— 因为 systemd 执行的是
# `/opt/zhihui-release/recovery/recovery_housekeeping.py`，
# 而发布目录里那份才是新的。**两处文件，只有一处被更新。**
#
# 更要紧的是：**没有任何部署脚本负责这个目录**。它是手工维护的。
# 也就是说，那次修复只要遇到一次重装或换机就会丢失，**而丢失之后不会报错** ——
# 快照清理会重新变成每天失败一次、没有人被通知到。
#
# 所以这个脚本存在的意义不是「复制几个文件」，而是**让那个修复成为可重复的**。
#
# ## 它做什么
#
# 1. 从发布目录的 `deploy/` 里取 `recovery_*.py`
# 2. 备份目标处已有的同名文件（带时间戳后缀）
# 3. 以 0700 / root:root 安装
# 4. 安装后**回读校验哈希**
#
# ## 幂等
#
# 重复运行是安全的：内容相同时仍然重装一次，但备份会跳过（不重复堆积）。
#
# ## 用法
#
#     install-recovery-scripts.sh [发布目录]
#
# 省略时用 `releases/current`。退出码：0 成功；1 有文件校验失败；2 用法或环境错误。
set -uo pipefail

SRC_RELEASE=${1:-/opt/workbuddy-manager/releases/current}
SRC_DIR="$SRC_RELEASE/deploy"
DST_DIR=/opt/zhihui-release/recovery

if [ ! -d "$SRC_DIR" ]; then
    echo "FAIL: 源目录不存在: $SRC_DIR" >&2
    exit 2
fi
if [ ! -d "$DST_DIR" ]; then
    echo "FAIL: 目标目录不存在: $DST_DIR" >&2
    exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "FAIL: 需要 root" >&2
    exit 2
fi

shopt -s nullglob
sources=("$SRC_DIR"/recovery_*.py)
if [ ${#sources[@]} -eq 0 ]; then
    echo "FAIL: 源目录里没有 recovery_*.py" >&2
    exit 2
fi

stamp=$(date +%Y%m%d-%H%M%S)
installed=0
failed=0

for src in "${sources[@]}"; do
    name=$(basename "$src")
    dst="$DST_DIR/$name"

    # 只在内容确实不同时才备份 —— 重复运行不该堆出一堆一样的备份。
    if [ -f "$dst" ]; then
        if cmp -s "$src" "$dst"; then
            echo "  unchanged  $name"
            continue
        fi
        cp -p "$dst" "$dst.bak-$stamp" 2>/dev/null \
            && echo "  backed-up  $name -> $name.bak-$stamp"
    fi

    if install -o 0 -g 0 -m 0700 "$src" "$dst"; then
        want=$(sha256sum "$src" | cut -d' ' -f1)
        got=$(sha256sum "$dst" | cut -d' ' -f1)
        if [ "$want" = "$got" ]; then
            echo "  installed  $name  $got"
            installed=$((installed + 1))
        else
            echo "  MISMATCH   $name（写入后哈希不一致）" >&2
            failed=$((failed + 1))
        fi
    else
        echo "  FAILED     $name" >&2
        failed=$((failed + 1))
    fi
done

echo "RECOVERY-INSTALL installed=$installed failed=$failed"
[ "$failed" -eq 0 ] || exit 1
exit 0
