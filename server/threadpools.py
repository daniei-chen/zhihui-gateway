"""为阻塞工作分池。

## 为什么需要它

`asyncio.to_thread` 用的是事件循环的默认执行器，CPython 在 2 vCPU 上把它定成
`min(32, cpu+4) = 6` 个线程。而推理主路径上一个请求要占 **6 次**线程跳转：
反序列化、请求体预授权、转发准备、派发校验、上游头构造、请求性能记录。

**实例心跳也排在同一个队列里。** 本地租约预算是 28 秒、心跳间隔 10 秒，所以只要
执行器队列上积累十几秒的阻塞任务（每次转发准备若因存储抖动变成一二百毫秒，
六十个任务就够），心跳就会超时 → 租约失守 → 进程进入「停止准入」状态，而且
**这条路径不会自愈**：`drain.resume()` 在 shutdown 开始后永远返回 False，只能重启。

于是「存储抖一下」被放大成「全站 503 且必须人工重启」。

## 做法

* `blocking`：承载推理主路径的阻塞工作，默认 16 个线程，替换掉默认执行器的 6 个；
* `heartbeat`：**单线程**，只跑实例心跳与租约续期，永远不会被业务请求排队挤住。

心跳这条不共享是关键：它的失败代价是全局性的，而它自己只需要一毫秒级的一条 SQL。
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

_DEFAULT_BLOCKING = 16


def _blocking_workers() -> int:
    raw = os.environ.get('WB_BLOCKING_THREADS')
    try:
        value = int(raw) if raw else _DEFAULT_BLOCKING
    except (TypeError, ValueError):
        value = _DEFAULT_BLOCKING
    return max(4, min(64, value))


_blocking = None
_heartbeat = None


def blocking_pool() -> ThreadPoolExecutor:
    """推理主路径的阻塞工作池（进程内单例）。"""
    global _blocking
    if _blocking is None:
        _blocking = ThreadPoolExecutor(max_workers=_blocking_workers(),
                                       thread_name_prefix='wb-blocking')
    return _blocking


def heartbeat_pool() -> ThreadPoolExecutor:
    """实例心跳专用池。单线程，不与任何业务请求共享。"""
    global _heartbeat
    if _heartbeat is None:
        _heartbeat = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wb-heartbeat')
    return _heartbeat


def reset() -> None:
    """测试用：丢弃已创建的池（不等待，避免拖慢用例）。"""
    global _blocking, _heartbeat
    for pool in (_blocking, _heartbeat):
        if pool is not None:
            pool.shutdown(wait=False)
    _blocking = _heartbeat = None
