from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from . import archival, config, retention


log = logging.getLogger('workbuddy.archives')
_lock = threading.Lock()
_stop = threading.Event()
_wake = threading.Event()
_thread = None
_state = {'sampled': False, 'running': False, 'last_at': None, 'ok': None,
          'error': None, 'pending': None, 'deleted_rows': 0,
          # 审计链校验结果（见下方 `_check_audit_chain`）：
          #   chain_intact   True/False，None 表示还没跑过
          #   chain_checked_at  上次校验的 epoch 秒
          #   chain_events  校验时看到的行数
          #   chain_error  校验本身失败时的异常类型（与「链断了」是两回事）
          'chain_intact': None, 'chain_checked_at': None,
          'chain_events': None, 'chain_error': None}
#: 后台归档逐个处理的表。
#:
#: `retention` 里**已经**为 `budget_reservations` 定义了完整的归档策略
#: （180 天、只动 `state IN ('settled','released')`、未决与 unknown 永不归档），
#: 但这里一直只列了两张日志表 —— 于是那条策略从未被接线执行，预算预留表
#: 只能无限增长（生产上 193k 行 / 27 MB）。策略与执行分开写在两个文件里，
#: 漏一处不会有任何报错，只会悄悄不生效。
DATASETS = ('request_logs', 'request_outcomes', 'budget_reservations')


def notify():
    _wake.set()


def status():
    with _lock:
        result = dict(_state)
    enabled = bool(config.ARCHIVE_ENABLED and config.BACKGROUND_JOBS)
    age = None if result['last_at'] is None else max(0, int(time.time()) - result['last_at'])
    result.update(enabled=enabled, age_seconds=age,
                  worker_alive=bool(_thread is not None and _thread.is_alive()),
                  fresh=bool(age is not None and age <= max(600, config.ARCHIVE_INTERVAL_SEC * 3)),
                  automatic_release_unknown=False, automatic_archive_deletion=False,
                  interval_seconds=config.ARCHIVE_INTERVAL_SEC, batch_rows=config.ARCHIVE_BATCH_ROWS)
    return result


def run_once(path: Path | None = None, out_dir: Path | None = None, *, max_rows: int | None = None):
    path = Path(path or config.DB_PATH).absolute()
    out_dir = Path(out_dir or (config.DATA_DIR / 'archives'))
    if out_dir.is_symlink():
        raise ValueError('archive directory must not be a symlink')
    if path.is_symlink() or not path.is_file():
        raise ValueError('archive source must be an existing regular database')
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(out_dir).free < 256 * 1024 * 1024:
        raise OSError('archive free space below safety floor')
    rows = max(1, min(1000, int(max_rows or config.ARCHIVE_BATCH_ROWS)))
    connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0.25)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA synchronous=FULL')
    started = time.monotonic()
    try:
        retention.assert_immutable_triggers(connection)
        results, pending = [], {}
        for name in DATASETS:
            definition = retention.dataset(name)
            cutoff = int(time.time()) - int(definition.retention_days) * 86400
            sql, arguments = definition.select_sql(cutoff, 0, 1,
                                                    retention.active_reservation_guard(connection, definition))
            remaining = connection.execute(sql, arguments).fetchone()
            unfinished = connection.execute(
                "SELECT watermark_id,sha256 FROM archive_runs WHERE dataset=? AND state='verified' "
                'AND rows_archived>0 ORDER BY id LIMIT 1', (name,)).fetchone()
            result = None
            if unfinished is not None:
                result = archival.delete_archived(connection, name, watermark=int(unfinished['watermark_id']),
                                                   sha=unfinished['sha256'], confirm_sha=unfinished['sha256'], batch=50)
            elif remaining is not None:
                receipt = archival.archive_dataset(connection, name, out_dir=out_dir, before_ts=cutoff,
                                                     batch=50, max_rows=rows, restart_scan=True)
                if receipt['state'] != 'verified':
                    raise ValueError('archive verification failed')
                result = archival.delete_archived(connection, name, watermark=receipt['watermark'],
                                                   sha=receipt['sha256'], confirm_sha=receipt['sha256'], batch=50)
            if result is not None:
                results.append(result)
            pending[name] = connection.execute(sql, arguments).fetchone() is not None
        return {'ok': True, 'runs': results, 'pending': pending,
                'deleted_rows': sum(result['deleted_rows'] for result in results),
                'duration_ms': round((time.monotonic() - started) * 1000, 2)}
    finally:
        connection.close()


def _check_audit_chain() -> None:
    """每个归档周期顺手校验一次 `admin_events` 的哈希链。

    ## 为什么需要单独做

    `ledger.verify_chain()` 早就存在，但**生产里从来没有被调用过**：
    它只在 `retention.verify_admin_chain()` 里被用到，而那条路径挂在归档流程上 ——
    归档按 90 天阈值触发，而库里最老的数据还没到那个年龄。

    **于是「审计链有没有被篡改」这件事，从上线到现在一次都没验证过。**
    链本身是完整的（2026-11-15 手工验过：25993 行、创世 1 个、断链 0），
    但那是**有人去查**才发现的，不是系统自己说的。

    ## 为什么可以放进周期里

    实测全表 25993 行、断链检测 0.02 秒。归档周期默认 300 秒，
    这点开销可以忽略，比「等归档触发」可靠得多。

    ## 两种失败要分开

    - `chain_intact=False`：**链真的断了** —— 有人改过历史
    - `chain_error=...`：**校验本身没跑成**（库读不了等）—— 磁盘/权限问题

    混在一起会让人以为「链断了」，而实际上只是没查成。
    """
    try:
        from . import db, ledger
        row = db.query_one('SELECT count(*) AS n FROM admin_events')
        intact = bool(ledger.verify_chain())
        with _lock:
            _state.update(chain_intact=intact, chain_checked_at=int(time.time()),
                          chain_events=int(row['n']) if row else None, chain_error=None)
        if not intact:
            log.error('admin_events hash chain is broken; audit history was modified')
        # `chain_error`（校验本身没跑成）与 `chain_intact=False`（链真断了）
        # 是两件事，但都意味着「审计完整性现在无法保证」，所以都进告警，
        # detail 里说清是哪一种。
        _report_audit_chain(intact, None)
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        with _lock:
            _state.update(chain_error=type(exc).__name__)
        log.warning('audit chain check failed: %s', type(exc).__name__)
        _report_audit_chain(None, type(exc).__name__)


def _report_audit_chain(intact: bool | None, error: str | None) -> None:
    """审计链异常接入运维告警。

    调用方已经做过 try/except，这里再包一层：告警失败不能连累校验本身。
    """
    try:
        from . import operational
        if intact is False:
            operational.transition('audit_chain_broken', True,
                                   '管理事件哈希链断裂——历史被改动过')
        elif error is not None:
            operational.transition('audit_chain_broken', True,
                                   f'审计链校验未能完成：{error}')
        else:
            operational.transition('audit_chain_broken', False, '审计链完整')
    except Exception:  # noqa: BLE001
        log.warning('audit chain health reporting failed')


def _loop():

    next_run = time.monotonic() + 30
    while not _stop.is_set():
        _wake.wait(min(1, max(0, next_run - time.monotonic())))
        _wake.clear()
        if _stop.is_set():
            break
        if time.monotonic() < next_run:
            continue
        with _lock:
            _state['running'] = True
        try:
            result = run_once()
            with _lock:
                _state.update(sampled=True, last_at=int(time.time()), ok=True, error=None,
                              pending=result['pending'], deleted_rows=result['deleted_rows'],
                              duration_ms=result['duration_ms'])
            _report_health(True, '归档周期正常')
        except (Exception, SystemExit) as exc:
            with _lock:
                _state.update(sampled=True, last_at=int(time.time()), ok=False, error=type(exc).__name__)
            log.error('archive cycle failed: %s; source rows remain protected', type(exc).__name__)
            # 2026-11-25：原来这里**只写日志**。数据是安全的（失败时一行都不删），
            # 但「归档连续失败」这件事没有任何人会知道 —— 日志没人盯，而
            # status() 里的 fresh 只判断「多久没跑过」，一个每 300 秒都失败的任务
            # 永远 fresh=True。等到有人发现时只会是磁盘满。
            _report_health(False, f'归档周期失败：{type(exc).__name__}（源数据未删除）')
        finally:
            with _lock:
                _state['running'] = False
            _check_audit_chain()
            next_run = time.monotonic() + config.ARCHIVE_INTERVAL_SEC


def _report_health(ok: bool, detail: str) -> None:
    """把归档周期成败接入运维告警。**失败绝不影响归档本身**。"""
    try:
        from . import operational
        operational.transition('archive_cycle_failed', not ok, detail)
    except Exception:  # noqa: BLE001 - 告警路径不能反过来影响归档
        log.warning('archive health reporting failed')


def start():
    global _thread
    if not config.ARCHIVE_ENABLED:
        return
    if _thread is not None and _thread.is_alive():
        raise RuntimeError('archive worker already running')
    _stop.clear()
    _thread = threading.Thread(target=_loop, name='wb-archive', daemon=True)
    _thread.start()


def stop():
    global _thread
    _stop.set()
    _wake.set()
    if _thread is not None:
        _thread.join(timeout=10)
        if _thread.is_alive():
            raise RuntimeError('archive worker did not stop cleanly')
    _thread = None
