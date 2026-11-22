"""积分余额快照（只记录，不扣费）。

2026-11-22 实测：Trae 按**积分**结算，但上游**不返回单次消耗多少积分**——
连发两次请求（257 / 267 tokens）后账号余额一点没动，`updated_at` 也没变，
说明积分是周期批量结算的。所以现在无法逐次记 credit。

在搞清楚「多少 token 折多少积分」之前，先把余额按小时存下来。有了
「某时段 token 消耗」与「同时段积分减少」两条时间序列，就能反推换算系数 ——
这也是接入第三方平台时唯一能用的校验手段。

本模块**只写快照**，不参与任何扣费判定。
"""
from __future__ import annotations

import json
import logging
import time

from . import db, externalplatforms

log = logging.getLogger('server.credit_watch')

TICK_SECONDS = 3600        # 积分是小时级/日级结算，一分钟一次只白占磁盘
STARTUP_DELAY_SECONDS = 90
DEFAULT_LOW_RATIO = 0.2    # 可被设置项 `credit_low_ratio` 覆盖
CRITICAL_RATIO = 0.05

DDL = (
    "CREATE TABLE IF NOT EXISTS platform_credit_snapshots("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "platform TEXT NOT NULL,"
    "account TEXT,"          # NULL = 平台级单账号（直连平台通常如此）
    "plan TEXT,"
    "total REAL, used REAL, remain REAL,"
    "captured_at INTEGER NOT NULL,"
    "raw TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_pcs_platform_ts "
    "ON platform_credit_snapshots(platform, captured_at)",
)


def ensure_schema() -> None:
    for statement in DDL:
        db.execute(statement)


def _ratio(remain, total):
    try:
        remain, total = float(remain), float(total)
    except (TypeError, ValueError):
        return None
    return None if total <= 0 else remain / total


def _snapshot_rows() -> list:
    """从平台视图取带积分字段的账号；任何失败都返回空列表，绝不抛。"""
    rows = []
    try:
        view = externalplatforms.trae_accounts()
    except Exception as exc:  # noqa: BLE001 - 采集失败不该影响在线请求
        log.warning('trae 账号视图读取失败，本轮跳过：%s', exc)
        return rows
    for account in (view or {}).get('accounts') or []:
        if not isinstance(account, dict):
            continue
        total, remain = account.get('credits_size'), account.get('credits_remain')
        if total is None and remain is None:
            continue
        rows.append({
            'platform': 'trae',
            'account': account.get('uid') or None,
            'plan': account.get('credits_plan') or account.get('plan') or '',
            'total': total,
            # used 优先用上游给的；缺了才用 total-remain 反算。
            'used': (account.get('credits_used')
                     if account.get('credits_used') is not None
                     else (None if total is None or remain is None
                           else float(total) - float(remain))),
            'remain': remain,
        })
    return rows


def capture_once() -> int:
    rows = _snapshot_rows()
    if not rows:
        return 0
    now = int(time.time())
    for row in rows:
        db.execute(
            'INSERT INTO platform_credit_snapshots'
            '(platform, account, plan, total, used, remain, captured_at, raw)'
            ' VALUES(?,?,?,?,?,?,?,?)',
            (row['platform'], row['account'], row['plan'], row['total'],
             row['used'], row['remain'], now,
             json.dumps(row, ensure_ascii=False)))
    return len(rows)


def latest(platform: str = 'trae'):
    return db.query_one(
        'SELECT platform, account, plan, total, used, remain, captured_at'
        ' FROM platform_credit_snapshots WHERE platform=?'
        ' ORDER BY captured_at DESC, id DESC LIMIT 1', (platform,))


def low_ratio_threshold() -> float:
    """告警阈值；设置项写坏就退回默认值（不让运维配置把告警关掉）。"""
    try:
        value = float(db.get_setting('credit_low_ratio'))
    except (TypeError, ValueError):
        return DEFAULT_LOW_RATIO
    return value if 0 < value <= 1 else DEFAULT_LOW_RATIO


def alerts() -> list:
    """积分余额告警：`[(告警名, 是否触发, 文案)]`。

    2026-11-22 实测唯一 Trae 账号：积分 528.5/3796（13.9%），已用 86.1%，
    每日签到只补 100；而系统原先**只有「账号数=1」的告警**。积分见底那天
    平台直接不可用且无提前通知，所以要按余额比例单独告警。
    数据缺失时不告警 —— 宁可不说，也不制造假信号。
    """
    row = latest('trae')
    if not row:
        return [('account_credits_trae', False, '尚无积分快照')]
    ratio = _ratio(row['remain'], row['total'])
    if ratio is None:
        return [('account_credits_trae', False, '积分口径不完整（缺 total/remain）')]
    threshold = low_ratio_threshold()
    detail = ('trae 积分剩余 %.1f/%.1f（%.1f%%），已用 %.1f%%，每日签到补充有限；'
              '余额耗尽会让整个平台不可用'
              % (row['remain'] or 0, row['total'] or 0, ratio * 100, (1 - ratio) * 100))
    if ratio < CRITICAL_RATIO:
        return [('account_credits_trae', True, '[即将耗尽] ' + detail)]
    if ratio < threshold:
        return [('account_credits_trae', True, detail)]
    return [('account_credits_trae', False,
             'trae 积分剩余 %.1f%%（阈值 %.0f%%）' % (ratio * 100, threshold * 100))]


async def _loop() -> None:
    import asyncio
    from starlette.concurrency import run_in_threadpool
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            written = await run_in_threadpool(capture_once)
            if written:
                log.info('积分快照已记录 %d 条', written)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception('积分快照采集失败（不影响在线请求）')
        await asyncio.sleep(TICK_SECONDS)


def start():
    """在 lifespan 里调用，返回可取消的 Task。"""
    import asyncio
    try:
        ensure_schema()
    except Exception:  # noqa: BLE001
        log.exception('积分快照建表失败')
    log.info('积分快照采集已启动（每 %s 秒一次）', TICK_SECONDS)
    return asyncio.create_task(_loop(), name='credit-watch')
