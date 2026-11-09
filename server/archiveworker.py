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
          'error': None, 'pending': None, 'deleted_rows': 0}
DATASETS = ('request_logs', 'request_outcomes')


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
        except (Exception, SystemExit) as exc:
            with _lock:
                _state.update(sampled=True, last_at=int(time.time()), ok=False, error=type(exc).__name__)
            log.error('archive cycle failed: %s; source rows remain protected', type(exc).__name__)
        finally:
            with _lock:
                _state['running'] = False
            next_run = time.monotonic() + config.ARCHIVE_INTERVAL_SEC


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
