# -*- coding: utf-8 -*-
"""已退役（2026-10-27 补丁收编，审计 P2-11）。

原四段猴子补丁的去向：
  A/B/C 并发阈值 —— 更早已由源码直读 env 取代（C-004）；
  D 本地审计表保留期 —— request_outcomes 清理已在 server/db.py 的
    _prune_side_tables；attempt/forwarding/admin 等哈希链事件表刻意
    保持只增不减（不可变触发器是防篡改设计，删除旧事件会破坏链验证）；
  E 弱口令巡检异步化 —— 已进 server/main.py lifespan（后台线程）。

本文件保留为 no-op：systemd 的 PYTHONPATH 仍指向本目录，留空即零补丁。
如需应急热修，恢复备份 /opt/backups/p2-11-sitecustomize-20261027/ 后重启。
"""


def _noop() -> None:
    return None
