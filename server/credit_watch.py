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
import os
import subprocess
import time

from . import db, externalplatforms

log = logging.getLogger('server.credit_watch')

TICK_SECONDS = 3600        # 积分是小时级/日级结算，一分钟一次只白占磁盘
STARTUP_DELAY_SECONDS = 90
DEFAULT_LOW_RATIO = 0.2    # 可被设置项 `credit_low_ratio` 覆盖
CRITICAL_RATIO = 0.05
#: WorkBuddy 账号池的「单号余额偏低」线：低于这个数就点名。
WB_LOW_ACCOUNT = 1000
WB_CREDIT_TIMEOUT = 90

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


def _wb_container() -> str:
    """wb2api 容器名；没配就返回空串（本机没有 WorkBuddy 时静默跳过）。"""
    return (os.environ.get('WB2API_CONTAINER') or '').strip()


def _wb_pool_snapshot() -> dict | None:
    """一次取回 WorkBuddy **整池**余额。

    为什么走容器里的 `./credit`，而不是像 trae 那样逐个问上游：

      · 一次 docker exec 拿 23 个账号，比 23 次独立上游请求快两个数量级；
      · 不占用 `CREDITS_CONCURRENCY` 的名额，不会把面板的实时查询挤排队；
      · 巡检本身不该让上游多收请求 —— 积分是钱，少打一次是一次。

    只做汇总：23 个账号逐条入库，一年就是 20 万行，而趋势只需要池级数字。
    明细放进 `raw`，需要点名某个号时仍查得到。

    任何失败都返回 None，绝不抛 —— 采集挂了不该影响在线请求。
    """
    container = _wb_container()
    if not container:
        return None
    try:
        proc = subprocess.run(
            ['docker', 'exec', container, 'sh', '-c',
             'cd /app && ./credit -json 2>/dev/null'],
            capture_output=True, timeout=WB_CREDIT_TIMEOUT, check=False)
    except Exception as exc:  # noqa: BLE001 - 采集失败降级为「本轮跳过」
        log.warning('workbuddy 积分采集执行失败，本轮跳过：%s', exc)
        return None
    if proc.returncode != 0:
        log.warning('workbuddy 积分采集返回 %s，本轮跳过', proc.returncode)
        return None
    try:
        payload = json.loads(proc.stdout.decode('utf-8', 'replace') or '{}')
    except ValueError as exc:
        log.warning('workbuddy 积分返回不是 JSON，本轮跳过：%s', exc)
        return None
    accounts = payload.get('accounts')
    if not isinstance(accounts, list) or not accounts:
        return None
    total = used = remain = 0.0
    ok_rows, failed, lows = [], [], []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        if not item.get('ok'):
            # 单个号上游拒绝（session 失效等）不影响整池统计，
            # 但要在 raw 里留痕，否则「池子变少」会没有解释。
            failed.append({'uid': item.get('uid'), 'nickname': item.get('nickname'),
                           'error': str(item.get('error') or '')[:120]})
            continue
        size, rem, use = item.get('size'), item.get('remain'), item.get('used')
        if not isinstance(rem, (int, float)):
            continue
        total += float(size or 0)
        remain += float(rem)
        used += float(use or 0)
        row = {'uid': item.get('uid'), 'nickname': item.get('nickname'),
               'remain': rem, 'used': use, 'size': size}
        ok_rows.append(row)
        if isinstance(rem, (int, float)) and rem < WB_LOW_ACCOUNT:
            lows.append(row)
    if not ok_rows:
        return None
    return {
        'platform': 'workbuddy',
        'account': 'pool',
        'plan': '%d 个可用账号' % len(ok_rows),
        'total': total or None,
        'used': used or None,
        'remain': remain,
        'accounts_ok': len(ok_rows),
        # 明细整份留在 raw 里：面板要按 uid 把真实余额贴回账号列表，
        # 而 wb2api 自己的 credits 字段滞后可达数小时（多数时候就是 0）。
        # 23 个账号约 2KB，一小时一条完全可接受。
        'accounts': ok_rows,
        'accounts_failed': failed,
        'low_accounts': lows,
    }


def capture_once() -> int:
    rows = _snapshot_rows()
    wb = _wb_pool_snapshot()
    if wb:
        rows.append(wb)
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


def latest(platform: str = 'trae', with_raw: bool = False):
    cols = ('platform, account, plan, total, used, remain, captured_at'
            + (', raw' if with_raw else ''))
    return db.query_one(
        'SELECT ' + cols +
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


def workbuddy_alerts() -> list:
    """WorkBuddy 账号池的积分告警。

    与 trae 分开两个告警名：两个平台的积分体系、补充渠道、耗尽后果都不同，
    混在一条里会让「哪一边快没了」看不出来。

    池级比例看趋势，单号余额看「这一个号是不是马上要空」——
    后者尤其重要：池子总计 15 万分时，某个号剩 600 分照样会在一次长上下文
    请求里被打空，然后被 `skip_exhausted` 摘出池子，而总量看起来还很健康。
    """
    row = latest('workbuddy', with_raw=True)
    if not row:
        return [('account_credits_workbuddy', False, '尚无 WorkBuddy 积分快照')]
    # `db.query_one` 返回的是 sqlite3.Row —— **它没有 .get()**，
    # 只有下标和 .keys()。2026-11-23 首次上线时这里写成了 row.get('raw')，
    # 采集成功、告警却在装配阶段抛 AttributeError，把后面所有告警一起跳过。
    # 统一转成 dict，后面按下标/键取都安全。
    row = dict(row)
    lines = []
    ratio = _ratio(row['remain'], row['total'])
    if ratio is None:
        lines.append(('account_credits_workbuddy', False,
                      'WorkBuddy 积分口径不完整（缺 total/remain）'))
    else:
        threshold = low_ratio_threshold()
        detail = ('workbuddy 池剩余 %.0f/%.0f（%.1f%%），%s'
                  % (row['remain'] or 0, row['total'] or 0, ratio * 100,
                     row['plan'] or '账号数未知'))
        active = ratio < threshold
        if ratio < CRITICAL_RATIO:
            lines.append(('account_credits_workbuddy', True, '[即将耗尽] ' + detail))
        elif active:
            lines.append(('account_credits_workbuddy', True, detail))
        else:
            lines.append(('account_credits_workbuddy', False,
                          'workbuddy 池剩余 %.1f%%（阈值 %.0f%%）'
                          % (ratio * 100, threshold * 100)))
    # 单号余额：从 raw 里取最近一次采集的明细
    lows = []
    try:
        raw = json.loads(row.get('raw') or '{}') if row.get('raw') else {}
        lows = raw.get('low_accounts') or []
        failed = raw.get('accounts_failed') or []
    except (ValueError, TypeError):
        lows, failed = [], []
    if lows:
        names = '、'.join(str(a.get('nickname') or a.get('uid') or '?')
                          for a in lows[:5])
        lines.append(('workbuddy_low_accounts', True,
                      '%d 个 WorkBuddy 账号余额低于 %d 分（%s），下一次长上下文请求就可能清零'
                      % (len(lows), WB_LOW_ACCOUNT, names)))
    else:
        lines.append(('workbuddy_low_accounts', False,
                      '所有 WorkBuddy 账号余额均高于 %d 分' % WB_LOW_ACCOUNT))
    if failed:
        lines.append(('workbuddy_credit_unreadable', True,
                      '%d 个 WorkBuddy 账号余额读不到（上游拒绝）：%s'
                      % (len(failed),
                         '、'.join(str(a.get('nickname') or '?') for a in failed[:5]))))
    return lines


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
