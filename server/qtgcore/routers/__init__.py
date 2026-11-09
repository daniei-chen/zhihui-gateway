"""qtgcore 路由包。

2026-10-27 审计 H-001（第一步）：原来这里有
    from . import actions, auth, health, read
但 actions / auth / health / read 四个模块的**全部端点**都不在
`server/main.py` 的 `_QTGP_ROUTE_PREFIXES` 白名单内（连同 adapter / anthropic /
authz / capabilities / data_admin / ip_guard / system，合计 11 个文件、67 条路由、
白名单命中 0 次），因此已整文件删除。

保留为空包：可达的 compat* 模块由 main.py 直接 import，不需要经过本文件。
"""
__all__ = []
