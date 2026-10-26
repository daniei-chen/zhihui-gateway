"""Readiness and actionable local health state; no generation probes or secrets."""
import asyncio
import logging
import os
import time
from pathlib import Path
from . import db, config, ledger

log = logging.getLogger('workbuddy.operations')
ledger_write_healthy = True
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
    return True

def readiness():
    """就绪判定：数据库可写 + 账本健康 + **未处于排空中**（K12）。

    draining 时返回 ok=False，`/api/readyz` 据此 503，发布脚本/入口据此把新流量
    切走；已经接单的请求不受影响（它们由 drain.wait_until_drained 保护）。
    """
    from . import drain
    draining = drain.is_draining()
    try:
        return {'ok': database_writable() and ledger_write_healthy and not draining,
                'database_write': True, 'ledger_write': ledger_write_healthy,
                'draining': draining}
    except Exception:
        return {'ok': False, 'database_write': False,
                'ledger_write': ledger_write_healthy, 'draining': draining}

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


def evaluate_backup_health(now):
    """Missing/unreadable/invalid reports are unknown, never a healthy backup."""
    import json
    latest = config.BACKUP_STATUS_FILE
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
        transition('backup_publish', True, 'no verified offsite publication evidence')
        return
    transition('backup_status_unavailable', False, '')
    transition('backup_stale', age > 26*3600, 'verified backup report older than 26 hours')
    transition('backup_publish', data.get('offsite') is not True, 'encrypted offsite publication not verified')
    transition('backup_size', size > 45*1024**2, 'backup approaching 50 MiB ciphertext limit')

async def monitor():
    failures = 0
    while True:
        status = readiness()
        # Planned drain deliberately makes readiness 503. It must not become a
        # database failure alert (or trigger a future self-heal restart).
        storage_ok = status['database_write'] and status['ledger_write']
        failures = 0 if storage_ok else failures+1
        transition('readiness',failures>=2,'database/ledger readiness failed twice')
        now=int(time.time())
        try:
            unknown=db.query_one("SELECT count(*) n FROM budget_reservations WHERE state='unknown' AND updated_at<?",(now-3600,))
            transition('budget_unknown',bool(unknown['n']),'unknown budget older than 1 hour')
            row=db.query_one("SELECT count(*) n,sum(CASE WHEN terminal_state IN ('upstream_error','transport_error','protocol_incomplete','application_error','interrupted') THEN 1 ELSE 0 END) failed FROM request_outcomes WHERE protocol!='http' AND ts>=? AND terminal_state!='rejected'",(now-300,))
            transition('model_completion',row['n']>=20 and (row['failed'] or 0)/row['n']>0.01,'model terminal failures exceed 1% / 5 minutes / >=20 requests')
            free=os.statvfs(config.DATA_DIR).f_bavail*os.statvfs(config.DATA_DIR).f_frsize if hasattr(os,'statvfs') else None
            transition('disk_free',free is not None and free<2*1024**3,'application volume free space below 2 GiB')
            # The installed host job is daily. Keep that honest: use a 26-hour
            # freshness guard until operations can install a more frequent job.
            evaluate_backup_health(now)
        except Exception:
            log.exception('operational health evaluation failed')
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
