"""Durable completion receipts and idempotent replay for the request ledger.

一笔请求的记账是一个事务：`request_logs` + 预算结算 + 账本事件 + 密钥累计。
事务失败（库锁、磁盘满、提交前后被 SIGKILL）时，事实不能凭空消失——本模块把
「要记的事实」原子落盘到有界的 spool，再由后台按 `request_id` 幂等重放。

边界（2026-10-28 审计 U01）：
  * spool 只存**记账元数据**，不含提示词、补全正文、Authorization 或密码；
    字段集合与 `db.add_request_log` 的列一致，另加 config_hash / 预算归属。
  * 幂等键是 `ledger_receipts.receipt_id`（= request_id，无 request_id 时用
    spool 文件名），不是调用方的 `X-Request-ID`——后者可伪造、可重复。
  * 只有「请求日志尚不存在」时才执行累加类副作用（密钥用量、日聚合、预算结算）；
    累计只应用相对持久 charged 的非负差额，人工结清不重复扣减。
    日志已存在时只从已提交事实补事件；冲突回执保留待核对，不覆盖原账。
  * 同一磁盘写不进时 spool 也写不进：这时 `spill()` 返回 None，调用方必须把
    `ledger_spool_failed` 置真并让就绪判定失败，停止继续放大新生成，而不是
    假装用量已经恢复。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import time
import uuid
from decimal import Decimal
from pathlib import Path

from . import config, db

log = logging.getLogger('workbuddy.ledgerreplay')

SCHEMA_VERSION = 1

#: 一条回执最多重试几次；超过后移入 dead/ 保留证据（不删除、不静默丢弃）。
MAX_ATTEMPTS = 5

#: 回执字段白名单——与 `db.add_request_log` 的列一一对应。
FACTS_FIELDS = (
    'ts', 'key_id', 'ip', 'model', 'mapped_model', 'status', 'prompt_tokens',
    'completion_tokens', 'latency_ms', 'first_token_ms', 'ua', 'error', 'stream',
    'credit', 'realm', 'account', 'cache_hit_tokens', 'cache_miss_tokens',
    'cache_write_tokens', 'request_id', 'endpoint', 'method', 'protocol',
    'http_status', 'terminal_state', 'error_stage', 'release_id', 'usage_source',
    'usage_complete', 'first_byte_ms', 'platform', 'actual_platform',
    'upstream_ref', 'client_request_id', 'effort', 'channel', 'session_key',
    'compatibility_version', 'compatibility_rules',
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def spool_dir() -> Path:
    """回执目录。可用 `WB_LEDGER_SPOOL_DIR` 指到**另一个挂载点**。

    与数据库同盘时无法覆盖「磁盘满」这一类故障（两者一起写不进）；这时唯一
    安全的动作是就绪失败 + 背压，而不是继续接单。
    """
    raw = os.environ.get('WB_LEDGER_SPOOL_DIR')
    if raw:
        return Path(raw)
    return Path(config.DATA_DIR) / 'ledger-spool'


def _dead_dir() -> Path:
    return spool_dir() / 'dead'


def _fsync_dir(path: Path) -> None:
    # POSIX rename durability requires the directory entry to be synced too.
    # A failed fsync is unknown persistence, not a successful spill receipt.
    if os.name == 'nt':
        return
    fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _receipt_count(directory: Path) -> int:
    """Distinguish an absent directory from unreadable/unsafe evidence.

    pathlib.glob may suppress directory access errors. Those errors cannot be
    interpreted as an empty ledger or a zero-dead-letter admission proof.
    """
    try:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            return -1
        count = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith('.receipt-') and entry.name.endswith('.tmp'):
                    # A publication may be active or may have failed after its
                    # file sync. Neither is proof of an empty/recovered channel.
                    return -1
                if entry.name.endswith('.json'):
                    if not entry.is_file(follow_symlinks=False):
                        return -1
                    count += 1
        return count
    except FileNotFoundError:
        return 0
    except OSError:
        return -1


def backlog() -> int:
    """待重放回执条数；不存在为 0，不可读为 -1（未知，禁止放行）。"""
    return _receipt_count(spool_dir())


def backpressure_limit() -> int:
    return _env_int('WB_LEDGER_BACKPRESSURE', 500)


def should_backpressure() -> bool:
    pending = backlog()
    return pending < 0 or pending >= backpressure_limit()


def build_entry(facts: dict, *, config_hash: str | None = None,
                budget: dict | None = None, release_id: str | None = None) -> dict:
    """把一次记账事实包成回执条目；只保留白名单字段。"""
    # Keep v1 accounting facts consumable by 0.0.5 on a code-only rollback.
    # That reader ignores the optional annex, but cannot accept new log kwargs.
    diagnostic_fields = ('compatibility_version', 'compatibility_rules')
    clean = {key: facts.get(key) for key in FACTS_FIELDS if key in facts and key not in diagnostic_fields}
    return {
        'schema': SCHEMA_VERSION,
        'spilled_at': int(time.time()),
        'attempts': 0,
        'facts': clean,
        'diagnostics': {key: facts[key] for key in diagnostic_fields if facts.get(key) is not None},
        'config_hash': config_hash,
        'budget': budget or {},
        'release_id': release_id,
    }


def _receipt_identity(entry: dict) -> str:
    """Compare accounting evidence, not replay bookkeeping or measured timings."""
    if (not isinstance(entry, dict) or entry.get('schema') != SCHEMA_VERSION
            or not isinstance(entry.get('facts'), dict)
            or not isinstance(entry.get('budget', {}), dict)):
        raise ValueError('invalid receipt envelope')
    volatile = {'latency_ms', 'first_token_ms', 'first_byte_ms'}
    identity = {key: entry.get(key) for key in ('schema', 'config_hash', 'release_id')}
    identity['facts'] = {key: value for key, value in entry['facts'].items() if key not in volatile}
    if entry['facts'].get('ts') is None:
        # Legacy receipts without ts use spilled_at to select the accounting
        # instant. In that case it is evidence, rather than bookkeeping.
        identity['effective_ts'] = entry.get('spilled_at')
    identity['diagnostics'] = entry.get('diagnostics', {})
    identity['budget'] = entry.get('budget', {})
    return json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _read_receipt(path: Path) -> dict:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('unsafe or oversized receipt')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as handle:
        current = os.fstat(handle.fileno())
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('receipt changed during verification')
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('receipt exceeds size limit')
    value = json.loads(raw)
    _receipt_identity(value)
    return value


def _publish_receipt(target: Path, payload: bytes) -> None:
    """Publish complete bytes without overwriting another writer's evidence."""
    tmp = target.parent / ('.receipt-' + uuid.uuid4().hex + '.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    cleanup = False
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, target)
        except FileExistsError:
            cleanup = True  # caller compares and retains a conflict separately
            raise
        cleanup = True
    finally:
        # On another publication failure retain the complete/partial evidence.
        # Its presence persistently blocks admission and paired backup until a
        # reviewed recovery reconciles it; a write probe cannot clear it.
        if cleanup:tmp.unlink(missing_ok=True)


def _retain_spill_conflict(payload: bytes, directory: Path, name: str) -> None:
    dead = directory / 'dead'
    dead.mkdir(mode=0o700, exist_ok=True)
    info = dead.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise OSError('unsafe dead-letter directory')
    # Dead letters are counted by admission and never automatically replayed.
    # A distinct schema also prevents accidental application if moved to root.
    target = dead / (name[:90] + '-conflict-' + uuid.uuid4().hex + '.json')
    evidence = {'schema': 'spill-conflict-v1', 'request_id': name,
                'reason': 'existing receipt is unsafe or differs from incoming evidence',
                'incoming': json.loads(payload)}
    _publish_receipt(target, json.dumps(evidence, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())
    _fsync_dir(dead)
    _fsync_dir(directory)


def _move_dead(path: Path) -> None:
    """Retain every contradictory receipt, including repeated late conflicts."""
    directory = _dead_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise OSError('unsafe dead-letter directory')
    target = directory / (path.stem[:100] + '-' + uuid.uuid4().hex + '.json')
    os.link(path, target, follow_symlinks=False)
    _fsync_dir(directory)
    path.unlink()
    _fsync_dir(path.parent)


def spill(entry: dict) -> str | None:
    """原子写入一条回执；成功返回路径，失败返回 None（调用方据此降级）。

    文件名用 request_id（无则随机），因此同一请求反复失败只会留下一份回执。
    """
    facts = entry.get('facts') or {}
    name = str(facts.get('request_id') or ('spool-' + uuid.uuid4().hex))
    directory = spool_dir()
    try:
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', name) or name in ('.', '..'):
            raise ValueError('unsafe receipt execution id')
        target = directory / (name + '.json')
        expected = _receipt_identity(entry)
        payload = json.dumps(entry, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(payload) > 1024 * 1024:
            raise ValueError('receipt exceeds size limit')
        from . import spoolidentity
        identity = spoolidentity.check(directory, create=True)
        if not identity['ok']:
            raise OSError(identity['reason'])
        directory.mkdir(parents=True, exist_ok=True)
        try:
            _publish_receipt(target, payload)
        except FileExistsError:
            try:
                matches = _receipt_identity(_read_receipt(target)) == expected
            except (OSError, ValueError, TypeError):
                matches = False
            if not matches:
                _retain_spill_conflict(payload, directory, name)
                log.error('ledger spool conflict retained request_id=%s', facts.get('request_id'))
                return None
        # Also sync on a matching retry: a previous publication may have
        # completed but failed the directory sync. Existence is not durability.
        _fsync_dir(directory)
        # N02：记下「这个通道确实被用过」。目录**从未使用**与**用过之后不见了**
        # 是两件事：前者是常态（功能没触发），后者是降级（写不进/被删/权限变了）。
        _mark_used(directory)
        return str(target)
    except (OSError, ValueError, TypeError):
        log.exception('ledger spool write failed request_id=%s', facts.get('request_id'))
        return None


class ReplayConflict(ValueError):
    """Retain contradictory receipts for review instead of rewriting confirmed usage."""


def _settle(entry: dict) -> dict:
    """重放预算结算：按预留当前状态决定 settled / released / unknown。

    不走 `budget.settle()`：它按 `owner`+`fence` 校验归属，重放进程已不是原
    owner，会被判成 fenced。这里只依据持久化状态推进，且不重复扣减。
    """
    facts = entry.get('facts') or {}
    from . import budget, ledger
    pt = int(facts.get('prompt_tokens') or 0)
    ct = int(facts.get('completion_tokens') or 0)
    credit_units = ledger.units(facts.get('credit'))
    if pt < 0 or ct < 0 or (credit_units is not None and credit_units < 0):
        raise ReplayConflict('negative receipt usage')
    event_ts = int(facts.get('ts') if facts.get('ts') is not None else entry.get('spilled_at') or time.time())
    plan = {'tokens': pt + ct, 'credit_units': credit_units or 0, 'ts': event_ts}
    request_id = facts.get('request_id')
    row = db.query_one('SELECT * FROM budget_reservations WHERE request_id=?', (request_id,)) if request_id else None
    if row is None:
        return plan
    key_id = facts.get('key_id')
    if key_id != row['key_id']:
        raise ReplayConflict('receipt key differs from its budget reservation')
    charged_tokens = int(row['charged_tokens'] or 0)
    charged_units = ledger.units(row['charged_credit'] or 0) or 0
    complete = bool(facts.get('usage_complete'))
    final = row['state'] in ('settled', 'released')
    if final:
        # A manual/automatic resolution is authoritative even when it confirmed
        # zero. Weaker partial evidence may be retained, never charged again.
        if ((complete and pt + ct != charged_tokens) or pt + ct > charged_tokens
                or (credit_units is not None and credit_units != charged_units)):
            raise ReplayConflict('receipt conflicts with confirmed budget usage')
        plan.update(tokens=0, credit_units=0)
    else:
        if complete and pt + ct < charged_tokens:
            raise ReplayConflict('complete receipt would erase charged tokens')
        if credit_units is not None and credit_units < charged_units:
            raise ReplayConflict('receipt would erase charged credit')
        target_tokens = max(pt + ct, charged_tokens)
        target_credit = Decimal(max(credit_units or 0, charged_units)) / ledger.SCALE
        td, cd = budget.usage_delta(target_tokens, target_credit, charged_tokens, row['charged_credit'])
        plan.update(tokens=td, credit_units=cd)
    # A previous charge without a log was assigned to the reservation's day.
    # Do not silently move a confirmed charge across the midnight boundary.
    if (charged_tokens or charged_units) and db.day_of(event_ts) != db.day_of(row['created_at']):
        raise ReplayConflict('receipt accounting day differs from prior charged evidence')
    if final:
        return plan
    quota = db.query_one('SELECT quota_credit,daily_credit_quota FROM api_keys WHERE id=?', (key_id,)) if key_id else None
    known = facts.get('terminal_state') == 'completed' and complete
    if quota and (quota['quota_credit'] or quota['daily_credit_quota']) and facts.get('credit') is None:
        known = False
    next_state = ('settled' if known else 'released'
                  if row['state'] == 'reserved' and facts.get('usage_source') != 'upstream' else 'unknown')
    db.execute(
        'UPDATE budget_reservations SET state=?,charged_tokens=?,charged_credit=?,updated_at=? '
        'WHERE request_id=?',
        (next_state,
         charged_tokens + plan['tokens'],
         (charged_units + plan['credit_units']) / ledger.SCALE, int(time.time()), request_id))
    return plan


def _touch(entry: dict, plan: dict) -> None:
    """重放密钥用量与日/小时聚合（与原 `keysvc.touch` + `db.bump_usage` 同口径）。"""
    from . import budget
    facts = entry.get('facts') or {}
    key_id = facts.get('key_id')
    if not key_id:
        return
    status = int(facts.get('status') or 0)
    pt = max(0, int(facts.get('prompt_tokens') or 0))
    ct = max(0, int(facts.get('completion_tokens') or 0))
    credit = facts.get('credit')
    event_ts = int(facts.get('ts') if facts.get('ts') is not None else entry.get('spilled_at') or time.time())
    if not (status < 400 or pt + ct > 0 or credit):
        return
    ip = facts.get('ip')
    if ip:
        db.execute('INSERT OR IGNORE INTO api_key_ips(key_id, ip, first_seen) VALUES(?,?,?)',
                   (key_id, ip, event_ts))
    db.execute('UPDATE api_keys SET last_used_at=max(coalesce(last_used_at,0),?) WHERE id=?', (event_ts, key_id))
    total = pt + ct
    budget.charge_delta(key_id, plan['tokens'], plan['credit_units'], plan['ts'])
    if total or credit:
        db.bump_usage(key_id, facts.get('model') or '', pt, ct, credit,
                      realm=facts.get('realm'),
                      cache=(facts.get('cache_hit_tokens') or 0,
                             facts.get('cache_miss_tokens') or 0), ts=event_ts)


def _events(entry: dict, *, counted: bool | None = None) -> None:
    """补不可变账本事件；两张表都是 INSERT OR IGNORE，天然幂等。"""
    from . import ledger
    facts = entry.get('facts') or {}
    request_id = facts.get('request_id')
    if not request_id:
        return
    pt = int(facts.get('prompt_tokens') or 0)
    ct = int(facts.get('completion_tokens') or 0)
    credit = facts.get('credit')
    ledger.record(request_id, facts.get('key_id'), facts.get('model'), facts.get('realm'),
                  pt, ct, credit,
                  bool(facts.get('key_id') and (pt + ct or credit)) if counted is None else counted,
                  facts.get('terminal_state'), entry.get('release_id'))
    config_hash = entry.get('config_hash')
    if config_hash:
        db.execute(
            'INSERT OR IGNORE INTO forwarding_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (request_id, 1, 'terminal', int(time.time()), facts.get('upstream_ref'),
             facts.get('model'), facts.get('mapped_model'), facts.get('realm'),
             facts.get('protocol'), None, config_hash, facts.get('terminal_state')))


def apply(entry: dict, receipt_id: str) -> str:
    """在一个事务里幂等应用一条回执。

    返回 'applied' / 'duplicate'。异常向上抛，由 `replay_batch` 保留回执并计数。
    """
    facts = entry.get('facts') or {}
    request_id = facts.get('request_id')
    with db.transaction():
        receipt = db.query_one('SELECT request_id FROM ledger_receipts WHERE receipt_id=?', (receipt_id,))
        existing = db.query_one('SELECT * FROM request_logs WHERE request_id=?', (request_id,)) if request_id else None
        if receipt:
            if receipt['request_id'] != request_id or existing is None:
                raise ReplayConflict('duplicate receipt cannot be matched to committed request facts')
            _validate_existing(facts, existing)
            has_portal = db.query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='portal_spend_receipts'")
            portal = db.query_one('SELECT owner_id FROM portal_spend_receipts WHERE request_id=?', (request_id,)) if has_portal else None
            supplied_owner = (entry.get('budget') or {}).get('portal_owner') or None
            if supplied_owner != (portal['owner_id'] if portal else None):
                raise ReplayConflict('duplicate receipt differs from committed wallet owner')
            return 'duplicate'
        if existing is None:
            if request_id and db.query_one('SELECT 1 FROM usage_events WHERE request_id=?', (request_id,)):
                raise ReplayConflict('immutable usage exists without its request log; review required')
            annex = entry.get('diagnostics')
            diagnostics = {key: value for key, value in annex.items()
                           if key in ('compatibility_version', 'compatibility_rules')
                           and isinstance(value, str) and len(value) <= 4096} if isinstance(annex, dict) else {}
            db.add_request_log(**dict(facts, **diagnostics))
            plan = _settle(entry)
            _touch(entry, plan)
            _events(entry, counted=bool(facts.get('key_id') and (plan['tokens'] or plan['credit_units'])))
        else:
            _validate_existing(facts, existing)
            # Attribution labels and errors can be enriched later. Backfill only
            # the canonical committed facts, never a stale receipt's replacements.
            canonical = dict(entry, facts={name: existing[name] for name in FACTS_FIELDS})
            _events(canonical)
        from . import portalwallet
        portalwallet.replay_receipt(entry, facts if existing is None else dict(existing))
        db.execute(
            'INSERT INTO ledger_receipts(receipt_id,request_id,attempt,applied_at,source,release_id) '
            'VALUES(?,?,?,?,?,?)',
            (receipt_id, request_id, int(entry.get('attempts') or 0) + 1,
             int(time.time()), 'spool-replay', entry.get('release_id')))
    return 'applied'


def _validate_existing(facts: dict, existing) -> None:
    from . import ledger
    for name in ('key_id', 'prompt_tokens', 'completion_tokens', 'usage_complete', 'usage_source'):
        if facts.get(name) != existing[name]:
            raise ReplayConflict('receipt differs from committed accounting facts: ' + name)
    if ledger.units(facts.get('credit')) != ledger.units(existing['credit']):
        raise ReplayConflict('receipt differs from committed accounting credit')
    if facts.get('ts') is None or existing['ts'] is None or db.day_of(facts['ts']) != db.day_of(existing['ts']):
        raise ReplayConflict('receipt differs from committed accounting day')


def replay_batch(limit: int | None = None) -> dict:
    """Only one replay reader may own read→apply→remove at a time."""
    from .services import credentiallease
    root = Path(config.DATA_DIR)
    root.mkdir(parents=True, exist_ok=True)
    try:
        # Keep this private lock outside the snapshotted receipt namespace.
        with credentiallease.mutation(root / '.ledger-replay'):
            return _replay_batch(limit)
    except OSError as exc:
        return {'scanned': 0, 'applied': 0, 'duplicate': 0, 'failed': 1,
                'dead': 0, 'backlog': backlog(), 'dead_pending': dead_letters(),
                'channel_error': type(exc).__name__, 'replay_busy_or_unavailable': True}


def _replay_batch(limit: int | None = None) -> dict:
    """重放一批回执：成功即删除文件；失败保留并累计 attempts，超限移入 dead/。"""
    directory = spool_dir()
    from . import spoolidentity
    identity = spoolidentity.check(directory)
    if not identity['ok']:
        return {'applied': 0, 'duplicate': 0, 'failed': 1, 'dead': 0,
                'pending': backlog(), 'channel_error': identity['reason']}
    batch = limit if limit is not None else _env_int('WB_LEDGER_REPLAY_BATCH', 50)
    try:
        files = sorted(directory.glob('*.json'))[:batch]
    except OSError:
        files = []
    applied = duplicate = failed = dead = 0
    for path in files:
        # N-03（2026-10-28 增量审计）：坏回执与应用失败分开处理。
        # 解析失败/schema 不支持的文件**永远不会成功**，留在 spool 只会让
        # backlog>0 持续 5 个重放轮（约 2.5 分钟）把就绪判定压住；直接进
        # dead-letter 保留证据，同时不阻塞重放。
        try:
            entry = json.loads(path.read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001 - 单条失败不中断整批
            failed += 1
            log.exception('ledger receipt unreadable file=%s -> dead letter', path.name)
            try:
                _move_dead(path)
                dead += 1
            except OSError:
                log.exception('failed to move unreadable receipt file=%s', path.name)
            continue
        if not isinstance(entry, dict) or entry.get('schema') != SCHEMA_VERSION:
            failed += 1
            log.error('ledger receipt schema unsupported file=%s -> dead letter', path.name)
            try:
                _move_dead(path)
                dead += 1
            except OSError:
                log.exception('failed to move unsupported receipt file=%s', path.name)
            continue
        try:
            outcome = apply(entry, path.stem)
            if outcome == 'applied':
                applied += 1
            else:
                duplicate += 1
            path.unlink()
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
            failed += 1
            log.exception('ledger replay failed file=%s', path.name)
            try:
                attempts = MAX_ATTEMPTS if isinstance(exc, ReplayConflict) else int(entry.get('attempts') or 0) + 1
                entry['attempts'] = attempts
                if attempts >= MAX_ATTEMPTS:
                    _move_dead(path)
                    dead += 1
                    log.error('ledger replay moved to dead letter file=%s attempts=%d',
                              path.name, attempts)
                else:
                    # N-03（2026-10-28 增量审计）：回写必须原子。若在此刻崩溃或
                    # 写半截，下次 json.loads 会失败、bookkeeping 也失败，文件
                    # 永久滞留 spool（attempts 恒 0，既不应用也不进 dead-letter）。
                    tmp = path.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(entry, ensure_ascii=False,
                                    separators=(",", ":")), encoding="utf-8")
                    os.replace(tmp, path)
            except Exception:  # noqa: BLE001
                log.exception('ledger replay bookkeeping failed file=%s', path.name)
    return {'scanned': len(files), 'applied': applied, 'duplicate': duplicate,
            'failed': failed, 'dead': dead, 'backlog': backlog(),
            # N01：`dead` 是**本轮**移入死信的条数；`dead_pending` 是**累计未处置**
            # 的死信条数（来自文件系统，跨轮/跨重启都在）。健康判定看后者。
            'dead_pending': dead_letters()}


# ── N01/N02（2026-10-28 复审 reaudit-20261028-1108）：死信与通道状态 ────────
#
# 两个缺口（都已复现）：
#   N01 死信未纳入健康判定：5 次 apply 失败把回执移进 dead/ 后，**三轮空重放**
#       就让 readiness 恢复 ok —— 因为健康只看 spool/*.json 的条数，dead/ 从不计数。
#   N02 通道失败未进入准入：把 spool 指到不可创建的目录 → spill() 返回 None、
#       should_backpressure() 仍为 False（backlog() 把 OSError 当成 0）→ 新请求
#       照收；三轮无文件重放又把 `spool_failed` 清掉，readiness 恢复 ok。
#
# 现在的口径：
#   · `pending`（待重放）与 `dead`（未处置死信）**分别持久统计**（都来自文件系统）；
#   · 死信只能通过 `dispose_dead_letters()` **带 actor + reason 的处置**消账，
#     处置留痕在 `dead/dispositions.jsonl`，文件移到 `dead/processed/`（不删除）；
#   · 通道"可写"靠**真实写探测**（建临时文件 → fsync → 删除）证明，1 秒级缓存；
#     目录不存在且从未用过 = 正常（`absent`），用过之后不见了/写不进 = 降级；
#   · `admission_block()` 把这三类情况翻译成新请求的明确可恢复错误码。
_USED_MARKER = '.spool-used'
_PROBE_PREFIX = '.probe-'
_PROBE_TTL_SEC = 1.0
#: 缓存键里带**目录路径**：换了 `WB_LEDGER_SPOOL_DIR` 就必须重新探测
_PROBE_CACHE: dict = {'at': 0.0, 'key': '', 'ok': None, 'reason': ''}
_PROCESSED_DIR = 'processed'
_DISPOSITIONS_FILE = 'dispositions.jsonl'

#: 准入阻断码（写进 BudgetRejected.code，调用方据此分支；都是**可恢复**的）
CODE_BACKPRESSURE = 'ledger_backpressure'
CODE_SPOOL_UNWRITABLE = 'ledger_spool_unwritable'
CODE_DEAD_PENDING = 'ledger_dead_letters_pending'


def dead_dir() -> Path:
    return _dead_dir()


def _used_marker() -> Path:
    """「回执通道被用过」的标记，放在 **DATA_DIR** 而不是 spool 目录里。

    放在 spool 目录里的话，目录被删/换挂载点后标记一起消失，就看不出
    「用过之后不见了」；放 DATA_DIR 才能跨目录变更保留这份记忆。
    """
    return Path(config.DATA_DIR) / _USED_MARKER


def _mark_used(directory: Path) -> None:
    marker = _used_marker()
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(int(time.time())), encoding='utf-8')
    except OSError:
        # 标记写不进去不影响主流程：`channel_state()` 还有写探测兜底
        pass


def _spool_used(directory: Path) -> bool:
    try:
        return _used_marker().exists() or (directory / _USED_MARKER).exists()
    except OSError:
        return False


def dead_letters() -> int:
    """**未处置**的死信条数（`dead/*.json`；`dead/processed/` 里的不算）。

    读不到就返回 -1（= 不确定），由调用方按"不健康"处理 —— **不能**返回 0：
    0 的意思是"确认没有死信"，而读不到并不等于没有。
    """
    return _receipt_count(_dead_dir())


def dead_dispositions() -> int:
    """已处置死信条数（`dead/dispositions.jsonl` 的行数；读不到返回 -1）。"""
    path = _dead_dir() / _DISPOSITIONS_FILE
    try:
        with open(path, encoding='utf-8') as handle:
            return sum(1 for line in handle if line.strip())
    except FileNotFoundError:
        return 0
    except OSError:
        return -1


def channel_probe(*, force: bool = False) -> dict:
    """真实写探测：在 spool 目录建临时文件 → fsync → 删除。

    「目录存在且没报错」不等于可写（只读挂载、配额满、权限被改都能通过 is_dir()）。
    结果缓存 `_PROBE_TTL_SEC` 秒：`/api/readyz` 会被频繁轮询，不能每个请求都做
    一次文件系统写。
    """
    now = time.monotonic()
    directory = spool_dir()
    from . import spoolidentity
    identity = spoolidentity.check(directory, create=True)
    if not identity['ok']:
        return {'ok': False, 'reason': identity['reason']}
    cache_key = str(directory)
    if (not force and _PROBE_CACHE['ok'] is not None
            and _PROBE_CACHE['key'] == cache_key
            and now - _PROBE_CACHE['at'] < _PROBE_TTL_SEC):
        return {'ok': bool(_PROBE_CACHE['ok']), 'reason': _PROBE_CACHE['reason']}
    reason = ''
    ok = False
    probe = directory / (_PROBE_PREFIX + uuid.uuid4().hex)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(probe, 'w', encoding='utf-8') as handle:
            handle.write('probe')
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_dir(directory)
        ok = True
    except OSError as exc:
        reason = f'{type(exc).__name__}: {exc}'[:200]
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    _PROBE_CACHE.update({'at': now, 'key': cache_key, 'ok': ok, 'reason': reason})
    return {'ok': ok, 'reason': reason}


def channel_state(*, force_probe: bool = False, include_dispositions: bool = True) -> dict:
    """回执通道的**持久**状态（给就绪判定与准入用）。

    `state`：
      · `absent`   —— 目录不存在且**从未用过**：功能没触发，正常；
      · `ok`       —— 可写、无未处置死信；
      · `degraded` —— 写不进 / 目录用过之后读不到 / 有未处置死信。
    """
    directory = spool_dir()
    try:
        exists = directory.is_dir()
    except OSError:
        exists = False
    used = _spool_used(directory)
    # Verify the registered identity BEFORE any write probe can create a directory.
    probe = channel_probe(force=force_probe)
    pending = backlog()
    dead = dead_letters()
    if not probe['ok']:
        state = 'degraded'
    elif dead != 0 or pending < 0 or dead < 0:
        state = 'degraded'
    elif not used:
        state = 'absent'
    else:
        state = 'ok'
    reason = probe['reason']
    if state == 'degraded' and not reason:
        if dead > 0:
            reason = f'{dead} 条未处置死信（dead/ 里仍有回执）'
        elif pending < 0 or dead < 0:
            reason = '回执目录不可读（无法确认积压/死信条数）'
    return {
        'state': state,
        'spool_dir': str(directory),
        'exists': exists,
        'used': used,
        'writable': bool(probe['ok']),
        'pending': pending,
        'dead': dead,
        # Historical disposition counts are a diagnostics field, not an
        # admission predicate. Do not reread this unbounded history per request.
        'dead_dispositions': dead_dispositions() if include_dispositions else None,
        'reason': reason or None,
        'checked_at': int(time.time()),
    }


def admission_block() -> dict:
    """新请求准入判定（N02）：**不只看积压条数**，还要看通道是否可用。

    返回 `{'blocked', 'code', 'reason'}`；`code` 是稳定可分支的字符串，且三类
    都是**可恢复**的（积压会重放掉、死信可处置、目录可修复后探测通过即恢复）。
    """
    state = channel_state(include_dispositions=False)
    if not state['writable'] or state['pending'] < 0 or state['dead'] < 0:
        return {'blocked': True, 'code': CODE_SPOOL_UNWRITABLE,
                'reason': ('账本回执通道无法核实读写状态（%s），暂停新生成以免事实丢失；'
                           '修复目录/磁盘后可自动恢复' % (state['reason'] or '写探测失败'))}
    if state['dead'] > 0:
        return {'blocked': True, 'code': CODE_DEAD_PENDING,
                'reason': ('存在 %d 条未处置死信，暂停新生成；'
                           '请用 dispose_dead_letters() 带原因处置后恢复'
                           % state['dead'])}
    if state['pending'] >= backpressure_limit():
        return {'blocked': True, 'code': CODE_BACKPRESSURE,
                'reason': '账本回执积压（%d 条），暂停新生成以等待重放' % state['pending']}
    return {'blocked': False, 'code': '', 'reason': ''}


def dispose_dead_letters(actor: str, reason: str) -> dict:
    """把 `dead/` 里的回执**带审计地**标记为已处置。

    N01 的恢复条件：死信不能因为"这轮没扫到文件"就消失，必须有**人**说明为什么
    可以不再处理它。处置动作：
      1. 每条回执追加一行 JSON 到 `dead/dispositions.jsonl`（ts / actor / reason /
         文件名 / sha256）—— 追加 + fsync，重启后仍在；
      2. 文件移到 `dead/processed/`（**不删除**，证据保留，但不再计入未处置）。
    处置后 `dead_letters()` 归零，`channel_state()` 才会回到 ok。
    """
    actor_text = str(actor or '').strip()
    reason_text = str(reason or '').strip()
    if not actor_text or not reason_text:
        raise ValueError('处置死信需要 actor 与 reason（可审计，缺一不可）')
    directory = _dead_dir()
    try:
        files = sorted(directory.glob('*.json'))
    except OSError as exc:
        raise ValueError(f'死信目录不可读：{exc}') from exc
    if not files:
        return {'disposed': 0, 'remaining': 0, 'dispositions': dead_dispositions()}
    processed = directory / _PROCESSED_DIR
    processed.mkdir(parents=True, exist_ok=True)
    ledger_path = directory / _DISPOSITIONS_FILE
    disposed = 0
    with open(ledger_path, 'a', encoding='utf-8') as log_handle:
        for path in files:
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                digest = None
            record = {'ts': int(time.time()), 'actor': actor_text[:64],
                      'reason': reason_text[:500], 'file': path.name,
                      'sha256': digest, 'schema': SCHEMA_VERSION}
            log_handle.write(json.dumps(record, ensure_ascii=False) + '\n')
            log_handle.flush()
            os.fsync(log_handle.fileno())
            try:
                os.replace(path, processed / path.name)
                disposed += 1
            except OSError:
                log.exception('dispose dead letter failed file=%s', path.name)
    _fsync_dir(processed)
    _fsync_dir(directory)
    return {'disposed': disposed, 'remaining': dead_letters(),
            'dispositions': dead_dispositions(), 'ledger': str(ledger_path)}


def _cli(argv: list[str]) -> int:
    """运维入口：`python -m server.ledgerreplay status|dispose --actor A --reason R`。"""
    import argparse
    parser = argparse.ArgumentParser(description='账本回执通道运维（只读状态 / 处置死信）')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='打印通道状态（JSON）')
    dispose = sub.add_parser('dispose', help='带原因处置 dead/ 里的回执（可审计）')
    dispose.add_argument('--actor', required=True)
    dispose.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    if args.command == 'status':
        print(json.dumps(channel_state(force_probe=True), ensure_ascii=False, indent=2))
        return 0
    result = dispose_dead_letters(args.actor, args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def recover(limit: int = 200) -> dict:
    """启动时消化存量回执；失败不阻断启动（monitor 会继续重试）。"""
    result = replay_batch(limit)
    if result['scanned'] or result['backlog']:
        log.warning('ledger spool recovery scanned=%d applied=%d duplicate=%d failed=%d backlog=%d',
                    result['scanned'], result['applied'], result['duplicate'],
                    result['failed'], result['backlog'])
    return result
