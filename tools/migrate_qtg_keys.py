#!/usr/bin/env python3
"""K01-S1 · 把 QTG 旧 key 迁移到 Manager（命令行入口 / 薄壳）。

用法::

    # 先看会发生什么（只读，一个字节都不写）
    python tools/migrate_qtg_keys.py \
        --qtg-db  <qtg.db> --manager-db <manager.db> --dry-run

    # 真正执行（幂等，可重复跑）
    python tools/migrate_qtg_keys.py \
        --qtg-db  <qtg.db> --manager-db <manager.db>

安全约定（实现见 `manager/server/legacymig.cli`）：
  · 只打印**计数**与**哈希前 8 位指纹**，绝不打印明文 key / 完整哈希 / 备注；
  · QTG 库以只读方式打开（迁移绝不写 QTG）；
  · 只写 `--manager-db` 指向的库；工具不连接网络、不碰生产路径；
  · 建议对 Manager 的**停机副本**执行：Manager 用 WAL，在线进程与迁移同时
    写同一个库会抢锁；生产操作须按发布审批流程另行授权。

迁移映射、回退方式与未决项见 `manager/server/LEGACY-KEYS.md`。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'manager') not in sys.path:
    sys.path.insert(0, str(ROOT / 'manager'))

from server.legacymig import cli  # noqa: E402  （路径先注入，再导入业务模块）


if __name__ == '__main__':
    raise SystemExit(cli())
