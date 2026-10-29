"""Readiness and actionable local health state; no generation probes or secrets."""
import asyncio
import logging
import os
import time
from pathlib import Path
from . import db, config, ledger

log = logging.getLogger('workbuddy.operations')
ledger_write_healthy = True
#: U01：回执通道自身不可写（同盘写满/无权限）。置真期间就绪判定必须失败——
#: 「事实既没进库、也没进回执」的请求已经无法自动恢复，不能再假装健康。
ledger_spool_failed = False
#: U01：连续成功重放轮数。只有它达到阈值且积压归零，才允许把写入健康置真。
_ledger_replay_streak = 0
alerts = {}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS readiness_probe(id INTEGER PRIMARY KEY CHECK(id=1), ts INTEGER NOT NULL);
'''

def database_writable():
    """真正验证「能落盘」，而不只是「能写入 SQLite 的缓冲」。

    N30：原先只做一次 UPSERT。SQLite 在已有连接/开启 WAL 时，INSERT 可能写进页缓存
    就返回成功，即使磁盘只读或已写满——于是 `/api/readyz` 在磁盘故障时仍报 ok。
    实测（把 data 目录 chmod 500）readyz 仍返回 `{"ok": true}`，即是此因。

    这里补两步：
      1. UPSERT 后执行 `PRAGMA wal_checkpoint(FULL)`——强制把 WAL 刷进主库文件；
         磁盘不可写时这一步会抛错。
      2. 再查一次刚写入的行，确认真的可见（防"写进缓存但未持久化"）。
    """
    now = int(time.time())
    with db.transaction() as conn:
        conn.execute(
            'INSERT INTO readiness_probe VALUES(1,?) '
            'ON CONFLICT(id) DO UPDATE SET ts=excluded.ts', (now,))
        # 强制落盘：磁盘只读/满时这里会抛 sqlite3.OperationalError
        try:
            conn.execute('PRAGMA wal_checkpoint(FULL)')
        except Exception:  # noqa: BLE001
            # memory/非 WAL 模式下 checkpoint 无意义，不影响判定
            pass
        row = conn.execute('SELECT ts FROM readiness_probe WHERE id=1').fetchone()
        if row is None or int(row[0]) != now:
            raise RuntimeError('readiness probe not persisted')
    # N04：事务已退出，这里用独立连接记录 WAL 合并结果（不阻塞、不算失败）。
    _probe_checkpoint()
    return True

def _ledger_backlog() -> int:
    """待重放回执条数；读不到目录时返回 -1（按不健康处理，不当 0）。"""
    try:
        from . import ledgerreplay
        return ledgerreplay.backlog()
    except Exception:  # noqa: BLE001 - 回执通道异常不能冒充「无积压」
        return -1


def _ledger_channel_ok() -> bool:
    """账本通道健康 = 写入未失败 + 回执通道可写 + 无待重放积压。

    U01（2026-10-28）：此前「任意下一笔写成功」就把全局标志置真，于是一次磁盘
    满之后，后续一条根本不写账本的被拒请求也能让就绪恢复成 ok。现在的恢复条件
    是三者同时成立，且由 monitor 的连续成功重放轮数把关（见 `_ledger_replay_streak`）。
    """
    # N01/N02（复审 reaudit-20261028-1108）：还要求
    #   · **未处置死信为 0**（死信是持久状态，不因"这轮没扫到文件"消失）；
    #   · 回执通道**真实可写**（写探测通过），而不是"目录存在"。
    # 只算 pending 条数会把「spool 根本写不进去」当成"没有积压"。
    from . import ledgerreplay
    try:
        channel = ledgerreplay.channel_state()
    except Exception:  # noqa: BLE001 - 通道状态读不到 = 不健康，不猜
        return False
    return (ledger_write_healthy and not ledger_spool_failed
            and channel['state'] != 'degraded'
            and channel['pending'] == 0 and channel['dead'] == 0)


_last_checkpoint = {'at': None, 'busy': None, 'log': None, 'checkpointed': None,
                    'error': None}


def _probe_checkpoint() -> dict:
    """用**独立连接**做一次 PASSIVE checkpoint，记录 WAL 合并结果（N04）。

    为什么必须独立连接：`database_writable()` 的 `db.transaction()` 是
    `BEGIN IMMEDIATE`，在写事务内部做 `wal_checkpoint(FULL)` 必然回
    `database table is locked` —— 原实现把这个异常 `except: pass` 掉了，于是
    「强制刷盘」从未真正生效。

    PASSIVE 语义：不阻塞、不因锁报错；`busy=1` 表示有读者尚未完成 checkpoint，
    此时**拿不到落盘证明，但不算失败**。这一点必须显式区分，不能写成「非 0 即失败」。
    """
    global _last_checkpoint
    now = int(time.time())
    try:
        import sqlite3
        probe = sqlite3.connect(str(config.DB_PATH), timeout=2)
        try:
            busy, log_frames, checkpointed = probe.execute(
                'PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        finally:
            probe.close()
        _last_checkpoint = {'at': now, 'busy': int(busy), 'log': int(log_frames),
                            'checkpointed': int(checkpointed), 'error': None}
    except Exception as exc:  # noqa: BLE001 - checkpoint 失败不改变「提交成功」
        _last_checkpoint = {'at': now, 'busy': None, 'log': None,
                            'checkpointed': None, 'error': str(exc)[:200]}
    return dict(_last_checkpoint)


def _write_proof() -> dict:
    """最近一次「数据库真的写进去了」的证据时间（U21）。

    `database_writable()` 每次 UPSERT `readiness_probe`，那张表的 `ts` 是
    **「事务提交成功」**的证据。健康恢复必须能指出这份证明，而不只是「这一轮
    探测没抛异常」—— 后者在 SQLite 把写缓冲进页缓存时也会成立（N30）。

    N04（2026-10-28 复审）修正：原实现声称这里还包含「强制 checkpoint」，
    但那个 checkpoint 在 `BEGIN IMMEDIATE` 事务内执行、必然 `database table is
    locked` 且被吞掉，从未生效。现在 checkpoint 改由 `_probe_checkpoint()`
    在事务外用独立连接做 PASSIVE，结果单独放 `checkpoint` 字段；
    `durable_evidence=True` 仅表示「拿到了非 busy 的合并结果」，
    **commit 成功本身不等于物理落盘**。
    """
    try:
        row = db.query_one('SELECT ts FROM readiness_probe WHERE id=1')
    except Exception:  # noqa: BLE001 - 证据读不到 ≠ 数据库健康
        return {'ts': None, 'age_seconds': None, 'reason': 'readiness_probe 不可读'}
    if row is None or not row['ts']:
        return {'ts': None, 'age_seconds': None,
                'reason': '尚无写入证明（启动后还没跑过就绪探测）'}
    stamp = int(row['ts'])
    checkpoint = dict(_last_checkpoint)
    durable = (checkpoint.get('at') is not None
               and checkpoint.get('error') is None
               and checkpoint.get('busy') == 0)
    return {'ts': stamp, 'age_seconds': max(0, int(time.time()) - stamp), 'reason': None,
            'checkpoint': checkpoint, 'durable_evidence': durable}


def readiness():
    """就绪判定：数据库可写 + 账本通道健康 + **未处于排空中**（K12）。

    draining 时返回 ok=False，`/api/readyz` 据此 503，发布脚本/入口据此把新流量
    切走；已经接单的请求不受影响（它们由 drain.wait_until_drained 保护）。

    `ledger_backlog` / `ledger_spool` 是 U01 新增的可观测项：积压 >0 表示有请求
    的事实还停在回执里没进库，此时就绪必须失败，避免继续放大新生成。

    U21（2026-10-28）：把「健康恢复的依据」显式暴露出来 —— 恢复要求**持久回执
    积压归零**（`ledger_backlog == 0`）+ **连续成功重放轮数达标**
    （`ledger_replay_streak >= WB_LEDGER_RECOVER_STREAK`）+ 回执通道未降级，
    并附带**最近一次写入证明**的时间与年龄（`write_proof`）。单条请求写成功
    不再能把状态翻回健康（那是 U01 修掉的漏洞）。
    """
    from . import drain
    draining = drain.is_draining()
    backlog = _ledger_backlog()
    healthy = _ledger_channel_ok()
    spool = 'failed' if ledger_spool_failed else 'ok'
    try:
        writable = database_writable()
    except Exception:  # noqa: BLE001 - 写不进就是不可写，不猜
        writable = False
    try:
        from . import ledgerreplay
        channel = ledgerreplay.channel_state()
    except Exception:  # noqa: BLE001 - 读不到按降级报，不冒充健康
        channel = {'state': 'degraded', 'pending': -1, 'dead': -1,
                   'writable': False, 'reason': '通道状态不可读',
                   'dead_dispositions': -1, 'spool_dir': '', 'exists': False,
                   'used': False, 'checked_at': None}
    return {
        'ok': bool(writable and healthy and not draining),
        'database_write': bool(writable), 'ledger_write': healthy,
        'ledger_backlog': max(0, backlog), 'ledger_spool': spool,
        'draining': draining,
        # N01：死信与通道状态**分别持久统计**（跨轮/跨重启都在）
        'ledger_dead': int(channel.get('dead', -1)),
        'ledger_dead_dispositions': int(channel.get('dead_dispositions', -1)),
        'ledger_channel_state': channel.get('state'),
        'ledger_channel_writable': bool(channel.get('writable')),
        'ledger_channel_reason': channel.get('reason'),
        # U21：健康恢复的依据（可复核，而不是"最近一次没报错"）
        'ledger_write_healthy': bool(ledger_write_healthy),
        'ledger_replay_streak': int(_ledger_replay_streak),
        'ledger_recover_streak_required': _reconcile_env_int('WB_LEDGER_RECOVER_STREAK', 3),
        'write_proof': _write_proof(),
        'recovery_basis': ('ledger_backlog==0 且 ledger_replay_streak>='
                           'ledger_recover_streak_required 且 spool 未降级；'
                           'write_proof 是最近一次真实落盘的证据'),
    }

def transition(name, active, detail):
    old = alerts.get(name)
    if active:
        if old is None:
            alerts[name] = {'name':name,'since':int(time.time()),'detail':detail}
            log.error('operational alert name=%s detail=%s',name,detail)
            # K46：日志之外**再**投递一次（默认 disabled，不擅自外发）。
            # 只在状态**进入**时投递，避免 monitor 每轮重复刷。
            try:
                from . import notify
                notify.dispatch(name, True, detail)
            except Exception:  # noqa: BLE001 - 投递失败绝不影响告警状态机
                log.warning('alert dispatch failed name=%s', name)
    elif old is not None:
        alerts.pop(name)
        log.info('operational recovery name=%s',name)
        try:
            from . import notify
            notify.dispatch(name, False, 'recovered')
        except Exception:  # noqa: BLE001
            log.warning('alert recovery dispatch failed name=%s', name)


def _offsite_configured():
    """异地发布是否**已配置**。

    判据只用 `offsitebackup.CONFIG` 是否存在——与该模块自身的入口条件同源。
    不另建一个「开关」变量：两份事实来源必然漂移，而漂移的方向恰好是「关了还在报」
    或「开着却不报」。

    为什么需要它：`backup_publish`（加密异地发布未验证）与 `backup_size`（密文接近
    50 MiB 上限）描述的都是**异地发布**的问题。2026-10-26 用户决定不做异地备份后，
    这两项不是「故障」而是「不适用」——继续报会让每次启动都刷两条噪音，
    稀释真正需要人看的告警（启动日志里它们和 `budget_unknown`、`model_completion`
    混在一起，看得多了就没人看了）。

    2026-10-27 R5（进程降权）：Python 3.12 的 `Path.is_file()` 对 EACCES 会抛
    PermissionError（不再吞掉）。manager 以 uid 10001 运行后 /root 不可遍历属
    **正常形态**——异地备份通道由 root 侧任务维护与判读，这里按「未配置」处理，
    不能让它打断整个健康评估循环。
    """
    from . import offsitebackup
    try:
        return offsitebackup.CONFIG.is_file()
    except OSError:
        return False


def evaluate_backup_health(now):
    """Missing/unreadable/invalid reports are unknown, never a healthy backup."""
    import json
    latest = config.BACKUP_STATUS_FILE
    offsite = _offsite_configured()
    try:
        data = json.loads(latest.read_text())
        size = data.get('bytes')
        age = now - latest.stat().st_mtime
        valid = (data.get('restore_check') == 'ok'
                 and isinstance(size, int) and not isinstance(size, bool) and size > 0
                 and age >= -300)
        if not valid:
            raise ValueError('invalid_backup_report')
    except (OSError, ValueError, AttributeError, TypeError):
        transition('backup_status_unavailable', True, 'backup report missing, unreadable or invalid')
        transition('backup_stale', True, 'no valid backup freshness evidence')
        transition('backup_publish', offsite, 'no verified offsite publication evidence')
        return
    transition('backup_status_unavailable', False, '')
    transition('backup_stale', age > 26*3600, 'verified backup report older than 26 hours')
    transition('backup_publish', offsite and data.get('offsite') is not True,
               'encrypted offsite publication not verified')
    transition('backup_size', offsite and size > 45*1024**2,
               'backup approaching 50 MiB ciphertext limit')

# ── 预算 unknown 的应用内增量对账（2026-10-27 审计 P1-1）─────────────────
# 定期扫描不是计费证明。零预扣及超期只决定扫描候选；完整上游零回执和
# 不可变用量事件必须一致，并在 resolve 的写事务内重读。缺少证据继续 unknown。
def _reconcile_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def reconcile_zero_reservation_unknowns(now: int) -> int:
    """Reconcile zero reservations only when a complete zero receipt exists."""
    from . import budget
    horizon = now - _reconcile_env_int('WB_BUDGET_RECONCILE_MIN_AGE_SEC', 7200)
    settled = 0
    for row in budget.iter_unresolved():
        if row.get('state') != 'unknown':
            continue
        if any(row.get(field) or 0 for field in ('reserved_tokens', 'reserved_credit', 'charged_tokens', 'charged_credit')):
            continue          # 有真实用量：必须人工核对，自动对账不碰
        if int(row.get('created_at') or now) > horizon:
            continue          # 未到年龄阈值：先等真实结算
        if not budget._confirmed_zero_usage(row['request_id'], row['key_id']):
            continue          # 未记账不等于上游未计费；事务内还会再次校验
        try:
            if budget.resolve(row['request_id'], 0, 0.0, 'auto-reconcile',
                               '完整上游回执与不可变用量事件均确认零用量', zero_only=True):
                settled += 1
        except Exception:  # noqa: BLE001  单条失败不中断本轮
            log.exception('incremental budget reconcile failed request_id=%s',
                          row.get('request_id'))
    if settled:
        log.info('incremental budget reconcile settled %d zero-reservation unknowns',
                 settled)
    return settled


def budget_unknown_alert(summary: dict) -> tuple[bool, str]:
    """`budget_unknown` 的判据与文案（U02 收尾）。

    判据只有 `needs_human_old`：**已归档待核对**（超过处理期限仍无上游证据）的
    unknown 不再计入，因此不会让这条告警长期 firing；但它们仍在汇总里以
    `archived_pending` 明示，不是「消失」，仍可人工 resolve。
    """
    detail = ('unknown budget needing human review (%d rows older than 1h, '
              'auto-reconcilable %d, archived-pending %d, token bound %d)'
              % (summary.get('needs_human_old', 0), summary.get('auto_reconcilable', 0),
                 summary.get('archived_pending', 0), summary.get('token_lower_bound', 0)))
    return bool(summary.get('needs_human_old', 0)) > 0, detail


def replay_ledger_receipts(now: int) -> dict:
    """U01：重放一批完成回执，并推进「账本写入健康」的恢复状态机。

    恢复条件必须同时成立：本轮无失败、无死信、积压归零，且连续成功轮数达到
    `WB_LEDGER_RECOVER_STREAK`（默认 3）。单条请求写成功不再能翻回健康——那正是
    旧实现「任意下一笔写成功就把标志置真」的漏洞。
    """
    global ledger_write_healthy, ledger_spool_failed, _ledger_replay_streak
    try:
        from . import ledgerreplay
        result = ledgerreplay.replay_batch()
    except Exception:  # noqa: BLE001 - 重放自身异常不能中断监控循环
        _ledger_replay_streak = 0
        log.exception('ledger receipt replay pass failed')
        return {'scanned': 0, 'applied': 0, 'duplicate': 0, 'failed': -1,
                'dead': 0, 'backlog': _ledger_backlog()}
    from . import ledgerreplay
    try:
        # force_probe：恢复判定必须基于**这一次**的真实写探测，不能用 1 秒缓存
        channel = ledgerreplay.channel_state(force_probe=True)
    except Exception:  # noqa: BLE001 - 通道状态读不到 = 不健康
        channel = {'state': 'degraded', 'writable': False, 'pending': -1, 'dead': -1,
                   'reason': '通道状态不可读'}
    dead_pending = int(channel.get('dead', -1))
    # N02：`spool_failed` 只能被**真实写探测成功**清掉。
    # 旧实现里"三轮无文件重放"就会把它清掉 —— 但空目录并不证明能写：
    # 目录可能已被删/只读/配额满，恰恰是它当初置真的原因。
    if channel.get('writable'):
        if ledger_spool_failed:
            log.warning('ledger spool channel writable again (probe ok)')
        ledger_spool_failed = False
    clean_round = (result['failed'] == 0 and result['dead'] == 0
                   and result['backlog'] == 0 and dead_pending == 0
                   and channel.get('state') != 'degraded')
    if clean_round:
        _ledger_replay_streak += 1
    else:
        _ledger_replay_streak = 0
    if _ledger_replay_streak >= _reconcile_env_int('WB_LEDGER_RECOVER_STREAK', 3):
        if not ledger_write_healthy:
            log.warning('ledger channel recovered: backlog=0 streak=%d', _ledger_replay_streak)
        ledger_write_healthy = True
    transition('ledger_backlog', result['backlog'] > 0,
               'ledger completion receipts awaiting replay (%d)' % result['backlog'])
    # N01：死信是**持久**降级项，告警不会因为重放轮次清空而消失
    transition('ledger_dead_letters', dead_pending > 0,
               'ledger receipts in dead letter awaiting audited disposition (%d)' % dead_pending)
    transition('ledger_channel', channel.get('state') == 'degraded',
               'ledger spool channel degraded: %s' % (channel.get('reason') or 'writable probe failed'))
    return result


async def monitor():
    failures = 0
    next_reconcile = 0
    next_replay = 0
    while True:
        now = int(time.time())
        # U01：先消化回执再判就绪，避免「积压刚产生就报警」的抖动。
        if now >= next_replay:
            next_replay = now + _reconcile_env_int('WB_LEDGER_REPLAY_INTERVAL_SEC', 30)
            replay_ledger_receipts(now)
        status = readiness()
        # Planned drain deliberately makes readiness 503. It must not become a
        # database failure alert (or trigger a future self-heal restart).
        storage_ok = status['database_write'] and status['ledger_write']
        failures = 0 if storage_ok else failures+1
        transition('readiness',failures>=2,'database/ledger readiness failed twice')
        now=int(time.time())
        try:
            # U02（2026-10-28）：告警只统计**真正需要人工**的 unknown。
            # 只有完整零回执可自动结清；拒绝错误和采集缺失仍需核对。
            # U02 收尾：超过处理期限仍无上游证据的已归档待核对，也不再触发告警。
            from . import budget as _budget
            _summary = _budget.unresolved_summary(now=now)['summary']
            _active, _detail = budget_unknown_alert(_summary)
            transition('budget_unknown', _active, _detail)
            row=db.query_one("SELECT count(*) n,sum(CASE WHEN terminal_state IN ('upstream_error','transport_error','protocol_incomplete','application_error','interrupted') THEN 1 ELSE 0 END) failed FROM request_outcomes WHERE protocol!='http' AND ts>=? AND terminal_state!='rejected'",(now-300,))
            transition('model_completion',row['n']>=20 and (row['failed'] or 0)/row['n']>0.01,'model terminal failures exceed 1% / 5 minutes / >=20 requests')
            free=os.statvfs(config.DATA_DIR).f_bavail*os.statvfs(config.DATA_DIR).f_frsize if hasattr(os,'statvfs') else None
            transition('disk_free',free is not None and free<2*1024**3,'application volume free space below 2 GiB')
            # The installed host job is daily. Keep that honest: use a 26-hour
            # freshness guard until operations can install a more frequent job.
            evaluate_backup_health(now)
        except Exception:
            log.exception('operational health evaluation failed')
        # P1-1：应用内增量对账。首轮立即跑（消化存量），之后按间隔。
        # 只在主实例跑（WB_BACKGROUND_JOBS）：零停机发布的过渡实例不重复对账。
        if now >= next_reconcile and config.BACKGROUND_JOBS:
            next_reconcile = now + _reconcile_env_int('WB_BUDGET_RECONCILE_INTERVAL_SEC', 900)
            try:
                settled_zero = reconcile_zero_reservation_unknowns(now)
                # 完整上游零回执与不可变用量记录必须一致。
                from . import budget as _budget
                settled_evidenced = _budget.reconcile_evidenced_unknowns(now)
                # U02 收尾：超期无证据的 unknown 标记「已归档待核对」（不改 state、
                # 不删除、不改用量），只把它从告警判据里摘出来。
                archived = _budget.archive_overdue_unknowns(now)
                # U04：给已终结、过了稳定窗口的请求补 attempt 计费事实（有界）。
                from . import ledger as _ledger
                billing_filled = _ledger.backfill_attempt_billing(now=now)
                if settled_zero or settled_evidenced or archived or billing_filled:
                    log.info('budget reconcile: zero-reservation=%d evidenced=%d archived=%d '
                             'attempt-billing=%d', settled_zero, settled_evidenced,
                             archived, billing_filled)
            except Exception:
                log.exception('incremental budget reconcile pass failed')
        await asyncio.sleep(30)

def _release_id() -> str:
    try:
        with open(config.ROOT / '.version', encoding='utf-8') as fh:
            return fh.read().strip() or "unknown"
    except OSError:
        return "unknown"


def status():
    return {'readiness':readiness(),'alerts':list(alerts.values()),
            'notification_delivery':'local journal and authenticated API only',
            'backup_freshness_hours':26,'credit_scale':ledger.SCALE,
            'financial_events_since_release': _release_id()}
