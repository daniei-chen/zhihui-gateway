from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time

from . import config, product, release_identity
from .services import updater


PHASES = frozenset({'candidate_verified', 'backup_verified', 'drained', 'activated',
                    'ready_verified', 'rolled_back', 'failed'})
_archive_lock = threading.Lock()


def _snapshot(path: Path, maximum: int):
    information = path.lstat()
    if not stat.S_ISREG(information.st_mode) or information.st_nlink != 1 or information.st_size > maximum:
        raise ValueError('unsupported release history file')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        current = os.fstat(handle.fileno())
        if (current.st_dev, current.st_ino) != (information.st_dev, information.st_ino):
            raise ValueError('release history changed')
        content = handle.read(maximum + 1)
        if len(content) > maximum:
            raise ValueError('release history limit exceeded')
    return information, content


def _exists(path: Path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _timestamp(value):
    return value if type(value) is int and 0 <= value <= 253402300799 else None


def read_status(*, include_logs: bool = False):
    identity = product.metadata(config.ROOT, managed=updater.managed_release())
    result = {'available': True, 'running': False, 'ok': None, 'step': '', 'logs': [],
              'version': updater.current_version(), 'release_id': release_identity.current(),
              'product': identity, 'updater_found': False, 'upstream_dir': '',
              'record_state': 'absent', 'record_verification': 'not-verified-by-panel',
              'log_tail': '', 'legacy_history_available': False, 'legacy_history_blocked': False}
    if include_logs:
        result['legacy_history_available'] = any(_exists(path) for path in (updater.STATUS_FILE, updater.LOG_FILE))
        result['legacy_history_blocked'] = _exists(updater.LOCK_FILE)
    path = config.DATA_DIR / 'zhihui-release-status.json'
    try:
        _information, content = _snapshot(path, 256 * 1024)
        receipt = json.loads(content)
    except FileNotFoundError:
        return result
    except (OSError, ValueError, TypeError):
        result['record_state'] = 'unavailable'
        return result
    if (not isinstance(receipt, dict) or type(receipt.get('schema')) is not int or receipt.get('schema') != 1
            or receipt.get('repository') != product.REPOSITORY
            or receipt.get('release_id') != result['release_id']):
        result['record_state'] = 'identity_mismatch'
        return result
    if (type(receipt.get('running')) is not bool
            or receipt.get('ok') is not None and type(receipt.get('ok')) is not bool):
        result['record_state'] = 'unavailable'
        return result
    result.update(record_state='reported', running=receipt.get('running') is True,
                  ok=receipt.get('ok') if type(receipt.get('ok')) is bool else None,
                  started_at=_timestamp(receipt.get('started_at')),
                  finished_at=_timestamp(receipt.get('finished_at')))
    phase = receipt.get('phase')
    result['step'] = phase if isinstance(phase, str) and phase in PHASES else ''
    events = receipt.get('events')
    if include_logs and isinstance(events, list):
        result['logs'] = [{'ts': _timestamp(entry.get('at')) or 0,
                           'level': 'error' if entry.get('phase') == 'failed' else 'info',
                           'text': entry['phase']}
                          for entry in events[-100:] if isinstance(entry, dict)
                          and isinstance(entry.get('phase'), str) and entry['phase'] in PHASES]
    return result


def _sync_directory(path):
    if os.name == 'nt':
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def archive_legacy_history():
    with _archive_lock:
        if _exists(updater.LOCK_FILE):
            raise ValueError('旧升级器锁仍存在，不能把未知活性当作已结束；请先由运维核实。')
        snapshots = {}
        for path, maximum in ((updater.STATUS_FILE, 256 * 1024), (updater.LOG_FILE, 16 * 1024 * 1024)):
            try:
                snapshots[path] = _snapshot(path, maximum)
            except FileNotFoundError:
                continue
        if not snapshots:
            return {'ok': True, 'archived': 0, 'message': '没有需要归档的旧升级器记录。'}
        if updater.STATUS_FILE not in snapshots:
            raise ValueError('缺少旧任务结束状态，不能据此确认活性；已保留旧日志。')
        if updater.STATUS_FILE in snapshots:
            try:
                status = json.loads(snapshots[updater.STATUS_FILE][1])
            except (ValueError, TypeError) as exc:
                raise ValueError('旧升级状态不可确认，保留原文件，未归档。') from exc
            if not isinstance(status, dict) or status.get('running') is not False:
                raise ValueError('旧升级任务活性未知或仍在运行，保留原文件。')
            if status.get('pid') and updater._pid_alive(status['pid']):
                raise ValueError('旧任务进程仍存活或活性未知，保留原文件。')
        history = config.DATA_DIR / 'release-history'
        if history.is_symlink():
            raise ValueError('历史目录不能是符号链接。')
        history.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt' and history.stat().st_mode & 0o077:
            raise ValueError('历史目录权限过宽，保留原记录。')
        destination = Path(tempfile.mkdtemp(prefix='legacy-updater-', dir=history))
        records = {}
        for path, (_information, content) in snapshots.items():
            target = destination / path.name
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            _written, copied = _snapshot(target, max(len(content), 1))
            if copied != content:
                raise ValueError('旧记录副本校验失败，保留原文件。')
            records[path.name] = {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
        manifest = {'schema': 1, 'kind': 'legacy-updater-history', 'repository': product.REPOSITORY,
                    'archived_at': int(time.time()), 'files': records,
                    'business_data_modified': False}
        descriptor = os.open(destination / 'manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(manifest, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(destination)
        _sync_directory(history)
        for path, (before, content) in snapshots.items():
            after, current = _snapshot(path, max(len(content), 1))
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or current != content):
                raise ValueError('归档期间旧文件发生变化，已保留副本且未移除原记录。')
        if _exists(updater.LOCK_FILE):
            raise ValueError('归档期间出现旧升级器锁，已保留副本和原记录。')
        for path in snapshots:
            path.unlink()
        _sync_directory(config.DATA_DIR)
        return {'ok': True, 'archived': len(snapshots), 'message': '旧升级器展示记录已归档，账号、配置及业务账本未改动。'}
