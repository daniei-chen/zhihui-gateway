"""账号批量操作的出口闸门（2026-11-19 事故后新增）。

## 为什么要有这个模块

2026-11-19 11:24–11:25，48 个 workbuddy 账号里有 38 个在两分钟内被腾讯
`idsvc` 批量吊销（`refresh token revoked`）。事后从上游日志还原出的时间线：

    10:00          activity 任务对 48 个账号各发 5 条上报 = 240 次/分钟，全部成功
    11:24          外部请求量突增到 12 次/分钟，覆盖 18 个不同账号
    11:25:23      开始成批 401 session_dead
    11:25:58      38 个账号全灭

对照组实验（同一台机器、同一个出口 IP、同一套请求头）：

    活账号 032d2815 → 刷新成功 code=0
    死账号 2d08e459 → refresh token revoked

**结论**：吊销是账号级的，不是 IP 级的；触发条件是「短时间内大量不同账号
的请求」。上游 workbuddy2api **没有任何出站限速实现**（只有被动记录限流
状态），所以它会把「批量操作」原样放大成对腾讯的批量请求。

这个模块在**网关侧**补上那道闸门：所有会放大成批量上游调用的**写操作**
（续期 / 登录 / 签到）必须经过这里排队与限速。

## 什么该拦、什么不该拦（2026-11-19 修正）

第一版把「刷新积分」也拦了，那是错的，并且**把正常功能防死了**：用户点一次
刷新积分就收到 429，而那个动作根本不会把账号刷死。

判据是「这个动作会不会产生新的 refreshToken / 触发一次登录授权」：

| 动作 | 会碰 refreshToken？ | 过闸门？ |
| --- | --- | --- |
| `refresh-credits`（查询余额） | 否，纯读 | **不过** |
| `checkin-all`（签到） | 否，只读 accessToken | **不过** |
| `refresh-tokens`（换 token） | 是 | 过 |
| `refresh-plan/run`（全量续期） | 是 | 过 |

事故当天的错误是 `refresh token revoked` —— 那是**续期**侧被吊销。
查询余额与签到都走别的接口（`billing_base`），不产生新 refreshToken，
不具备「把账号刷死」的能力。

**把读操作也拦住的代价是真实的**：用户点不动按钮、以为系统坏了，
而攻击面并没有因为这一拦而变小。签到那次的实际后果是
「账号数超过 10 就报 400」，用户完全无法批量签到。

## 设计要点

1. **不是简单的计数器**：用「滑动窗口 + 最小间隔」双约束。
   最小间隔保证不会在一秒内连发（风控最容易抓的特征），
   滑动窗口保证不会在一分钟内超过上限。

2. **批量操作必须分批**：`refresh-credits` 这类「对全部账号发起真实查询」
   的接口，必须显式声明批次大小与批间间隔，不允许一次性扇出。

3. **AI 无授权不得触发**：这是本次事故的直接诉求。凡是带
   `X-Agent-Automation` 头（或 UA 标记为自动化客户端）的请求，
   批量接口一律拒绝，除非请求里显式带上人工确认令牌。
   理由：AI 循环调用一次批量接口就能把整个账号池打光，而它不会累、
   不会犹豫、不会觉得「这样是不是太猛了」。

4. **失败要快、要明确**：被闸门拦下时返回 429 并说明还要等多久，
   不静默排队到超时——静默排队会让调用方（尤其是 AI）以为成功、
   然后立刻重试，反而更糟。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Deque

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# ── 默认参数（全部可用环境变量覆盖，便于不同部署调松紧）───────────────

#: 每分钟最多多少个「会打到腾讯」的账号级动作。
#: 48 个账号的池子，60/分钟意味着「刷一遍全池至少 48 秒」——这已经比
#: 事故当天的 240 次/分钟温和四倍，但仍留出可用的操作体感。
DEFAULT_PER_MINUTE = int(os.environ.get('WB_ACCOUNT_ACTION_PER_MINUTE', '60'))

#: 两个动作之间的最小间隔（秒）。1.0 秒 = 不会出现「同一秒内连打多个账号」。
#: 事故当天 11:25:23 那一秒里有 2 个账号同时被吊销，这个参数就是防它。
DEFAULT_MIN_INTERVAL = float(os.environ.get('WB_ACCOUNT_ACTION_MIN_INTERVAL', '1.0'))

#: 单次批量调用最多处理多少个账号。超过就要求调用方分批。
DEFAULT_BATCH_MAX = int(os.environ.get('WB_ACCOUNT_BATCH_MAX', '10'))

#: 批次之间的间隔（秒）。10 个一批、批间 12 秒 → 每分钟约 50 个，
#: 落在 per-minute 上限内，且不会形成连续脉冲。
DEFAULT_BATCH_GAP = float(os.environ.get('WB_ACCOUNT_BATCH_GAP', '12.0'))

#: 自动化客户端必须带的人工确认令牌值。空 = 未配置 = 一律拒绝自动化批量调用。
AUTOMATION_TOKEN = os.environ.get('WB_ACCOUNT_AUTOMATION_TOKEN', '').strip()


class _SlidingWindow:
    """滑动窗口 + 最小间隔。单事件循环内使用，不需要锁。"""

    def __init__(self, per_minute: int, min_interval: float) -> None:
        self.per_minute = max(1, per_minute)
        self.min_interval = max(0.0, min_interval)
        self.events: Deque[float] = deque()
        # 用 None 而不是 0.0 表示「还没记录过」：`time.monotonic()` 在小值域上
        # 可以合法返回 0.0（测试里就传 0.0），拿 0 当哨兵会让第一次最小间隔
        # 检查被静默跳过 —— 那正是「同一秒打多个账号」要挡住的形态。
        self.last: float | None = None

    def _trim(self, now: float) -> None:
        cutoff = now - 60.0
        while self.events and self.events[0] < cutoff:
            self.events.popleft()

    def check(self, now: float | None = None) -> tuple[bool, str, float]:
        """能不能再放一个动作。返回 (ok, 原因, 建议等待秒数)。"""
        now = time.monotonic() if now is None else now
        self._trim(now)
        if len(self.events) >= self.per_minute:
            wait = 60.0 - (now - self.events[0])
            return False, f'最近一分钟已执行 {len(self.events)} 个账号级动作（上限 {self.per_minute}）', max(0.0, wait)
        if self.min_interval and self.last is not None and (now - self.last) < self.min_interval:
            wait = self.min_interval - (now - self.last)
            return False, f'距离上一个动作不足 {self.min_interval:g} 秒', max(0.0, wait)
        return True, '', 0.0

    def record(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.events.append(now)
        self.last = now

    def snapshot(self) -> dict:
        now = time.monotonic()
        self._trim(now)
        return {
            'used_last_minute': len(self.events),
            'per_minute': self.per_minute,
            'min_interval_sec': self.min_interval,
            'seconds_since_last': (now - self.last) if self.last is not None else None,
        }


_WINDOW = _SlidingWindow(DEFAULT_PER_MINUTE, DEFAULT_MIN_INTERVAL)


def _is_automation(request: Request) -> bool:
    """请求是否来自自动化客户端（AI / 脚本）。

    判据刻意宽松：**宁可误判成自动化、也不放过**。因为误判的代价是
    「AI 需要多一步人工确认」，而漏判的代价是「整个账号池被打光」。
    """
    if request.headers.get('x-agent-automation'):
        return True
    ua = (request.headers.get('user-agent') or '').lower()
    markers = ('python-requests', 'httpx', 'aiohttp', 'curl/', 'wget/', 'go-http-client',
               'node-fetch', 'axios', 'okhttp', 'postmanruntime')
    return any(m in ua for m in markers)


def guard_account_action(
    request: Request,
    *,
    count: int = 1,
    what: str = '账号操作',
    batch: bool = False,
) -> None:
    """账号级动作的统一闸门。不通过就抛 429/403，绝不静默放行。

    - `count`：本次要打多少个账号。
    - `batch=True`：调用方声明这是「对多个账号扇出」的批量动作，
      此时额外要求 `count <= DEFAULT_BATCH_MAX`，并检查自动化令牌。
    """
    if count <= 0:
        return

    # 1. 自动化客户端不得触发批量动作（本次事故的直接诉求）。
    if batch and _is_automation(request):
        if not AUTOMATION_TOKEN:
            # 403 的 detail 必须是**固定句子**：这些文案由前端按整串精确匹配翻译，
            # 带变量的句子永远匹配不上，英文界面下会静默退回中文原文。具体是哪个
            # 动作（`what`）记进日志，用户看正文已经知道该怎么做。
            logger.warning('账号批量动作被拒 scope=%s reason=automation_client', what)
            raise HTTPException(
                status_code=403,
                detail=('检测到自动化客户端，批量账号操作已被禁止。'
                        '该限制在 2026-11-19 批量吊销事故后加入 —— '
                        '一次批量扇出会同时打到多个上游账号，是风控的主要触发特征。'
                        '如需执行，请人工在界面操作，或配置 WB_ACCOUNT_AUTOMATION_TOKEN '
                        '并在请求头 X-Automation-Token 中携带。'),
            )
        if request.headers.get('x-automation-token') != AUTOMATION_TOKEN:
            logger.warning('账号批量动作被拒 scope=%s reason=automation_token_mismatch', what)
            raise HTTPException(
                status_code=403,
                detail='自动化令牌缺失或不匹配，批量账号操作被拒绝。',
            )

    # 2. 单次批量不得超过批大小上限。
    if batch and count > DEFAULT_BATCH_MAX:
        raise HTTPException(
            status_code=400,
            detail=(f'{what}：单次最多处理 {DEFAULT_BATCH_MAX} 个账号，本次请求 {count} 个。'
                    f'请分批提交，批间隔至少 {DEFAULT_BATCH_GAP:g} 秒 —— '
                    '集中扇出会被上游判定为异常调用并吊销账号。'),
        )

    # 3. 滑动窗口 + 最小间隔。
    ok, reason, wait = _WINDOW.check()
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=(f'{what}：出口节流中 —— {reason}。请 {wait:.0f} 秒后重试。'
                    f'（当前上限：{DEFAULT_PER_MINUTE} 个账号级动作/分钟，'
                    f'最小间隔 {DEFAULT_MIN_INTERVAL:g} 秒）'),
            headers={'Retry-After': str(int(wait) + 1)},
        )
    _WINDOW.record()


async def guard_batch_sequence(
    request: Request,
    count: int,
    *,
    what: str = '账号批量操作',
    gap: float | None = None,
) -> None:
    """批量动作的完整闸门：先过策略检查，再按批间间隔 sleep。

    这是给「确实要处理多个账号」的调用方用的。它不会一次放行全部，
    而是**在调用方循环内部**逐个放行——调用方每处理一个账号调一次
    `await guard_batch_sequence(...)`，由本函数决定是否要等。
    """
    guard_account_action(request, count=1, what=what, batch=False)
    step = DEFAULT_BATCH_GAP if gap is None else max(0.0, gap)
    if step:
        await asyncio.sleep(step / max(1, DEFAULT_BATCH_MAX))


def throttle_snapshot() -> dict:
    """当前闸门状态，给 /api/accounts/throttle 这类只读接口用。"""
    return {
        **_WINDOW.snapshot(),
        'batch_max': DEFAULT_BATCH_MAX,
        'batch_gap_sec': DEFAULT_BATCH_GAP,
        'automation_batch_blocked': not AUTOMATION_TOKEN,
    }
