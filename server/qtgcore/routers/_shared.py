"""compat 路由共用的两个私有 helper（U28 收敛，2026-10-28）。

## 收敛了什么

`_rt` 曾在 **5 个** compat 路由里各写一份（`return request.app.state.rt`，字节完全
相同），`_no` 曾在 **2 个**里各写一份（409 + 「本部署不支持…」文案，也完全相同）。

复制粘贴的代价不是行数，而是**改一处忘一处**：`_no` 的状态码或文案一旦要统一调整，
5 个文件里改 4 个就等于亲手制造了一个新的行为差异 —— 而这正是台账 U28 要收的口子。

## 边界

* 只搬**字节完全相同**、且没有任何跨模块 import 的私有 helper；
* 各 compat 路由仍然 `from ._shared import _rt, _no` 再导出 —— `compat._rt` 这类
  符号**继续可用**，调用点一行不改（不删符号是这次收敛的前提）；
* 不改任何响应内容、状态码或路由注册。
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


def _rt(request: Request):
    """取运行时对象（`app.state.rt`，由 lifespan 注入）。"""
    return request.app.state.rt


def _no(what: str, why: str = "") -> JSONResponse:
    """「本部署不支持 X」的统一 409 响应。"""
    detail = f"本部署不支持{what}"
    if why:
        detail += f"：{why}"
    return JSONResponse(status_code=409, content={"detail": detail})
