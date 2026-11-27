"""DAN-19：拒绝请求的不可变账本行由后台补齐，不占请求返回路径。

## 为什么单独一个模块

`ledger.record()` 写的是**不可变**的 `usage_events`（`no_update` / `no_delete`
触发器）。拒绝请求（IP 拦截 / 密钥校验失败 / 密钥并发满 / 限流）以前在请求
返回路径上同步写它 —— 一次 `BEGIN IMMEDIATE` + `synchronous=FULL` 提交，外加
一条不可变表的 INSERT。而**拒绝速率没有上限**：限流只对「放行」的请求占窗口
额度，被拒的请求照样每次都要付一次账本提交。

现在的分工：

* `_record` 仍把拒绝请求的**请求日志**（`request_logs`，可归档、可清理）写在
  结算事务里 —— 那是排障界面要的事实，也是本条唯一的持久事实；
* 不可变账本行由本模块从 `request_logs` **补齐**：请求已经返回，补齐失败也不
  影响响应，下一轮监控再补。

## 幂等与崩溃

幂等键是 `usage_events` 的 `UNIQUE(request_id, attempt, kind)`：`ledger.record()`
用 `INSERT OR IGNORE`，补多少次都只落一行；本模块先按 `request_id` 排除已有
账本行，所以正常情况下不会重复尝试。

崩溃不丢：补齐的来源是 `request_logs` 里**已提交**的行，进程重启后照样能补。
补的只是「拒绝请求没有上游用量」的那一类行 —— 有计费事实的请求仍然在请求路径
上同步写，因为那不能延后。
"""

from __future__ import annotations

import time

from . import db

#: 补齐窗口（小时）。比最长一次维护窗口大得多，够覆盖「补齐停了半天」。
WINDOW_HOURS = 24
#: 单次补齐的行数上限：monitor 每轮调一次，不需要一次扫完。
BATCH = 200

#: 只补「网关在转发前就拒绝」的那类行：`terminal_state='rejected'` 且没有
#: 上游用量。`request_logs` 里没有用量列可供区分，所以按 `terminal_state` 取，
#: 再用 `counted` 口径兜底 —— 有 key 且（pt+ct 或 credit）为真的行本来就不会
#: 走到这里（它们在请求路径上已经同步写过）。
_SQL = """
SELECT r.id, r.request_id, r.key_id, r.model, r.realm, r.prompt_tokens,
       r.completion_tokens, r.credit, r.terminal_state, r.release_id
  FROM request_logs AS r
 WHERE r.terminal_state = 'rejected'
   AND r.request_id IS NOT NULL
   AND r.ts >= ?
   AND NOT EXISTS (SELECT 1 FROM usage_events AS u WHERE u.request_id = r.request_id)
 ORDER BY r.ts ASC, r.id ASC
 LIMIT ?
"""


def backfill(*, window_hours: int | None = None, batch: int | None = None,
             now: int | None = None) -> int:
    """把「已提交但还没有账本行」的拒绝请求补进 `usage_events`。

    返回本次**尝试**写入的行数（`ledger.record` 自身幂等，重复调用不会多写）。
    单行写入失败会抛出 —— 由调用方（monitor）记日志并在下一轮重试，失败的行
    仍留在 `request_logs` 里，不会丢。
    """
    from . import ledger

    hours = WINDOW_HOURS if window_hours is None else max(1, int(window_hours))
    limit = BATCH if batch is None else max(1, min(1000, int(batch)))
    since = int(time.time() if now is None else now) - hours * 3600
    rows = db.query(_SQL, (since, limit))
    written = 0
    for row in rows:
        pt = max(0, int(row['prompt_tokens'] or 0))
        ct = max(0, int(row['completion_tokens'] or 0))
        credit = row['credit']
        ledger.record(
            row['request_id'], row['key_id'], row['model'], row['realm'],
            pt, ct, credit,
            bool(row['key_id'] and (pt + ct or credit)),
            row['terminal_state'], row['release_id'],
        )
        written += 1
    return written
