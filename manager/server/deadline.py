"""K21 · 请求级时间预算、重试边界与取消传递（Manager → 上游这一跳）。

## 审计时的基线（后续实现已经改变行为）

* `config.py`：`UPSTREAM_TIMEOUT = _env_int('WB_UPSTREAM_TIMEOUT', 300)`；生产实际
  环境值 600（审计包 §5 勘误：不是 300，也不成立「Go 450s 一定先断」）。
* `gateway.py::_chat`：非流式与流式都调
  `config.http_client(config.UPSTREAM_TIMEOUT, connect=5, …)`
  → **连接**有 5s 预算；**读**预算 = UPSTREAM_TIMEOUT（httpx 的 read timeout 是
  「每次 read 的间隔」，流式下等价于**流中空闲**预算）。
* 缺的三项：
  1. **首包**没有单独口径 —— 只有 read 超时兜底，慢首包与慢流中段无法区分；
  2. **总时长**没有上界 —— 只要上游持续吐字节，流可以一直续命；
  3. **重试策略不存在**：全仓没有对上游的自动重试（`grep retry` 只命中错误文案
     与 `Retry-After`）。这既是好事（不会重放已开始的生成），也是风险：将来谁
     加一次「顺手重试」都没有拦截点。本模块把「哪些阶段可以重试」写成策略 + 测试。
* 取消链是有的：`observability.close_stream` 用
  `anyio.move_on_after(5, shield=True)` 做有界关闭；`gateway._record` 在
  `client_cancelled` 时把状态写成 499，`budget.settle/finish` 把未决预留置
  `unknown` 而不是释放。取消预算字段已显式化，但关闭窗口的统一接入仍待验证。

## 五类预算

| 预算 | 默认 | 覆盖什么 | 失败后终态 |
|---|---|---|---|
| 连接 | 5s | TCP/TLS 建立 | `transport_error`（可重试，见下） |
| 首包 | 60s | 请求发出 → 第一个字节 | `transport_error` |
| 流中空闲 | 600s | 两个数据块之间的间隔 | `transport_error` |
| 总时长 | 600s | 请求开始 → 结束（含生成） | `transport_error` |
| 客户端取消 | 5s | 收到取消后关闭上游的有界窗口 | `client_cancelled`（499） |

这些预算会改变超时行为：首包默认 60s 会比原生产 600s 更早终止慢首包；
总时长上限也会终止持续输出超过总预算的流。上线前须结合实际延迟分布显式配置，
不能把它们描述成“线上行为不变”。当前已覆盖响应头等待、流迭代与非流式总时长；
各兼容协议及取消关闭预算仍需逐链验收。

## 重试边界（为什么只有两个阶段可以重试）

* `connect` / `send_request`：只有异常类型证明尚未发送到上游时才允许重试
  （ConnectError、ConnectTimeout、PoolTimeout）。不能仅按所处阶段判断安全；
  写入失败、连接重置和响应协议错误可能发生在上游已经接收请求之后，不自动重放。
* `first_byte`：等待首包期间上游可能已经开始生成 —— 重试等于第二次扣费，**禁止**。
* `streaming`：已开始生成，任何重放都会重复输出/重复计费，**禁止**。
"""
from __future__ import annotations

import asyncio
import os
import math
import random
import time
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Callable

# 阶段
PHASE_CONNECT = 'connect'
PHASE_SEND = 'send_request'
PHASE_FIRST_BYTE = 'first_byte'
PHASE_STREAMING = 'streaming'
PHASE_DONE = 'done'
PHASES = (PHASE_CONNECT, PHASE_SEND, PHASE_FIRST_BYTE, PHASE_STREAMING, PHASE_DONE)

#: 唯一允许自动重试的阶段：**生成尚未开始**
SAFE_RETRY_PHASES = frozenset({PHASE_CONNECT})
#: 明确禁止重试的阶段（写出来是为了可测试、可引用）
NEVER_RETRY_PHASES = frozenset({PHASE_SEND, PHASE_FIRST_BYTE, PHASE_STREAMING, PHASE_DONE})

#: 可重试的错误类名（按类名判断，避免把 httpx 变成硬依赖；也覆盖 stdlib 情形）
RETRYABLE_ERROR_NAMES = frozenset({
    'ConnectError', 'ConnectTimeout', 'PoolTimeout',
})


class BudgetExceeded(Exception):
    """某一类时间预算用尽。`kind` 是 connect/first_byte/stream_idle/total。"""

    def __init__(self, kind: str, detail: str = ''):
        super().__init__(f'时间预算用尽：{kind}' + (f'（{detail}）' if detail else ''))
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class Budgets:
    """五类预算的显式取值（秒）。"""

    connect_s: float = 5.0
    first_byte_s: float = 60.0
    stream_idle_s: float = 600.0
    total_s: float = 600.0
    client_cancel_s: float = 5.0

    def __post_init__(self) -> None:
        for name in ('connect_s', 'first_byte_s', 'stream_idle_s', 'total_s',
                     'client_cancel_s'):
            value = getattr(self, name)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f'{name} 必须是正数：{value!r}')

    def normalized(self) -> 'Budgets':
        """把互相矛盾的环境配置**夹到自洽区间**，而不是让请求随机超时。

        `total_s` 是所有预算的上界：它小于首包/空闲预算时，实际生效的是 total
        （否则会先被 total 打断，配置含义就反了）。这里显式夹紧并保留可观测性
        （调用方可用 `normalized() != budgets` 判断是否发生了夹取）。
        """
        total = self.total_s
        idle = min(self.stream_idle_s, total)
        first = min(self.first_byte_s, total)
        return replace(self, total_s=total, stream_idle_s=idle, first_byte_s=first,
                       connect_s=min(self.connect_s, total))

    @classmethod
    def from_config(cls, upstream_timeout: float | None = None) -> 'Budgets':
        """从环境变量构造（缺省值保持与现状一致）。

        * `WB_CONNECT_TIMEOUT_S`      默认 5（与现有 `connect=5` 一致）
        * `WB_FIRST_BYTE_TIMEOUT_S`   默认 = 读超时（旧版 first_byte 只记指标、无硬预算；60s cap 会造成慢上游 502，2026-10-26 回归修复）
        * `WB_STREAM_IDLE_TIMEOUT_S`  默认读超时（httpx read timeout 语义）
        * `WB_TOTAL_TIMEOUT_S`        默认读超时
        * `WB_CLIENT_CANCEL_S`        默认 5（与 close_stream 的 shield 一致）
        """
        def _num(name: str, default: float) -> float:
            raw = os.environ.get(name, '').strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError as exc:
                raise ValueError(f'{name} must be a positive finite number') from exc
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be a positive finite number')
            return value

        base = float(upstream_timeout or _num('WB_UPSTREAM_TIMEOUT', 300.0))
        return cls(
            connect_s=_num('WB_CONNECT_TIMEOUT_S', 5.0),
            first_byte_s=_num('WB_FIRST_BYTE_TIMEOUT_S', base),
            stream_idle_s=_num('WB_STREAM_IDLE_TIMEOUT_S', base),
            total_s=_num('WB_TOTAL_TIMEOUT_S', base),
            client_cancel_s=_num('WB_CLIENT_CANCEL_S', 5.0),
        ).normalized()


@dataclass(frozen=True)
class RetryPolicy:
    """有界、只在安全阶段重试。默认 `max_attempts=1` = 不改变现有行为。"""

    max_attempts: int = 1
    backoff_s: float = 0.2
    max_backoff_s: float = 2.0
    jitter_s: float = 0.1

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError('max_attempts 必须 >= 1')

    @classmethod
    def from_config(cls) -> 'RetryPolicy':
        raw = os.environ.get('WB_SAFE_RETRY_MAX', '').strip()
        try:
            max_attempts = int(raw) if raw else 1
        except ValueError:
            max_attempts = 1
        return cls(max_attempts=max(1, min(3, max_attempts)))

    def delay(self, attempt: int, *, rand: Callable[[], float] = random.random) -> float:
        """指数退避 + 抖动；上限由 `max_backoff_s` 控制（避免放大上游故障）。"""
        base = self.backoff_s * (2 ** max(0, attempt - 1))
        jitter = self.jitter_s * rand()
        return min(self.max_backoff_s, base) + jitter


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    reason: str
    next_attempt: int
    delay_s: float = 0.0


def _looks_retryable(error: BaseException | None) -> bool:
    if error is None:
        return True
    names = {cls.__name__ for cls in type(error).__mro__}
    return bool(names & RETRYABLE_ERROR_NAMES)


def failure_phase(error: BaseException) -> str:
    """Only a failed connection/pool acquisition proves no request was sent.

    ReadError, WriteError and protocol/connection resets are ambiguous: the
    upstream may already have accepted and billed the generation.
    """
    names = {cls.__name__ for cls in type(error).__mro__}
    return PHASE_CONNECT if names & RETRYABLE_ERROR_NAMES else PHASE_FIRST_BYTE


def decide_retry(policy: RetryPolicy, *, phase: str, attempt: int,
                 generated_started: bool, error: BaseException | None = None,
                 retryable: bool | None = None) -> RetryDecision:
    """唯一的重试判定入口。

    拒绝顺序有意如此（先硬约束、后策略）：
    1. 生成已开始 → 永不重试；
    2. 阶段不在安全集合 → 永不重试；
    3. 次数用尽 → 不重试；
    4. 错误不可重试 → 不重试；
    5. 否则重试（带退避）。
    """
    if phase not in PHASES:
        return RetryDecision(False, f'未知阶段 {phase!r}', attempt)
    if generated_started:
        return RetryDecision(False, '生成已开始：禁止透明重放（会重复输出/重复计费）',
                             attempt)
    if phase in NEVER_RETRY_PHASES:
        return RetryDecision(False, f'阶段 {phase} 不在安全重试集合内', attempt)
    if phase not in SAFE_RETRY_PHASES:
        return RetryDecision(False, f'阶段 {phase} 不是安全重试阶段', attempt)
    if attempt >= policy.max_attempts:
        return RetryDecision(False, f'已达最大尝试次数 {policy.max_attempts}', attempt)
    if retryable is None:
        retryable = _looks_retryable(error)
    if not retryable:
        name = type(error).__name__ if error is not None else 'unknown'
        return RetryDecision(False, f'错误类型不可重试：{name}', attempt)
    return RetryDecision(True, f'{phase} 阶段可重试', attempt + 1,
                         policy.delay(attempt))


class Deadline:
    """一次请求的时间轴；被 `iterate_with_budgets()` 消费。"""

    def __init__(self, budgets: Budgets, *, clock: Callable[[], float] = time.monotonic):
        self.budgets = budgets.normalized()
        self._clock = clock
        self.started = clock()
        self.first_byte_at: float | None = None
        self.phase = PHASE_CONNECT

    # ── 时间读数 ──────────────────────────────────────────────────────
    def elapsed(self) -> float:
        return max(0.0, self._clock() - self.started)

    def mark_first_byte(self) -> None:
        if self.first_byte_at is None:
            self.first_byte_at = self._clock()

    @property
    def first_byte_seen(self) -> bool:
        return self.first_byte_at is not None

    def remaining_total(self) -> float:
        return max(0.0, self.budgets.total_s - self.elapsed())

    def first_byte_remaining(self) -> float:
        if self.first_byte_seen or self.first_byte_at is not None:
            return self.remaining_total()
        # 首包预算从请求开始算（连接阶段也算在内，避免「连接慢 + 首包慢」叠加）
        return max(0.0, self.budgets.first_byte_s - self.elapsed())

    def next_timeout(self) -> tuple[float, str]:
        """下一段等待的超时与**触发时会归因到哪一类预算**。"""
        if not self.first_byte_seen:
            candidates = ((self.first_byte_remaining(), 'first_byte'),
                          (self.remaining_total(), 'total'))
        else:
            candidates = ((self.budgets.stream_idle_s, 'stream_idle'),
                          (self.remaining_total(), 'total'))
        timeout, kind = min(candidates, key=lambda item: item[0])
        return timeout, kind

    def check_total(self) -> None:
        if self.elapsed() > self.budgets.total_s:
            raise BudgetExceeded('total', f'已 {self.elapsed():.1f}s')


async def await_headers(awaitable, deadline: Deadline):
    """Include response-header wait in the first-byte and total budgets."""
    timeout, kind = deadline.next_timeout()
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except asyncio.TimeoutError as exc:
        raise BudgetExceeded(kind, f'waiting for response headers exceeded {timeout:.3f}s') from exc


async def await_total(awaitable, deadline: Deadline):
    try:
        return await asyncio.wait_for(awaitable, deadline.remaining_total())
    except asyncio.TimeoutError as exc:
        raise BudgetExceeded('total', 'request exceeded its total deadline') from exc


async def iterate_with_budgets(iterator: AsyncIterator[bytes], deadline: Deadline, *,
                               on_first_chunk: Callable[[], None] | None = None,
                               timeout_override: float | None = None) -> AsyncIterator[bytes]:
    """在五类预算下迭代上游字节流。

    * 每块之前用 `asyncio.wait_for` 施加「首包 / 流中空闲 / 总时长」三者中最紧的
      那个；超时抛 `BudgetExceeded(kind=…)`，由调用方映射成终态；
    * 迭代结束（StopAsyncIteration）正常返回；
    * 不吞取消：`asyncio.CancelledError` 原样向上抛，保证客户端取消能一路传回
      上游（外层 `observability.close_stream` 负责有界关闭）。
    """
    while True:
        deadline.check_total()
        if timeout_override is not None:
            timeout, kind = timeout_override, 'stream_idle'
        else:
            timeout, kind = deadline.next_timeout()
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            raise BudgetExceeded(kind, f'等待 {timeout:.1f}s 超时') from exc
        if not deadline.first_byte_seen:
            deadline.mark_first_byte()
            deadline.phase = PHASE_STREAMING
            if on_first_chunk is not None:
                on_first_chunk()
        yield chunk
