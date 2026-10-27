"""R3-L2：实例租约（instance lease）—— 双实例准入/owner 交接的所有权事实。

## 为什么要单独一张表（方案 B，Lead 已批准）

`tasklease` 的租约是「**每日任务**执行权」，`tasklease.active_leases(conn)` 没有
kind 过滤，`begin_publish()` 直接用它判定「是否有活跃任务」。把「Manager 实例」
塞进同表会让发布被自己的实例租约永久阻塞 —— 那段握手已由 K28 用 31/31 + 竞态 +
崩溃证据验证过，不值得为省一张表去改它。因此：

* 本模块独立一张 `instance_leases`（v8 EXPAND，纯新增）；
* `begin_publish()` **继续只读 `task_leases`**，不查实例租约（见 `test_instance_leases.py`
  的对照测试①）；
* 两套租约的 fence / 过期互不干扰（对照测试③）。

## 与 tasklease 的关系：同一套已证算法，独立常量

claim 的判定与 tasklease 相同（`BEGIN IMMEDIATE` 内判断「无行 / 自持 / 已过期」→
写 owner+lease_until+fence+1；release 只清 owner/lease_until、**fence 不回退**，
防 ABA）。但 **TTL 常量必须独立**（§1.6.2 硬要求 2）：

* `ZH_INSTANCE_HEARTBEAT_SEC` 默认 **10s**（心跳间隔）
* `ZH_INSTANCE_LEASE_TTL_SEC` 默认 **30s**（过期时间）

不能复用 `tasklease.DEFAULT_TTL`（300s）：实例心跳是「进程还活着」的快信号，
30s 过期意味着崩溃后最多 30s 可被接管；任务租约 300s 是「一个任务最长跑多久」。
两者语义不同，混用会让崩溃接管慢 10 倍。

## 状态机（§1.6.3）

```text
candidate --claim--> serving --begin_drain--> draining --finalize_handoff--> handoff
                                                                    --release--> stopped
```

* `role` 由 DDL 的 CHECK 约束（candidate/serving/draining/handoff/stopped）；
* 不变量：**同一 DB 写者域最多一个 `role=serving`**，由交接时序保证（本模块不做选举）；
* `release` 不删行、fence 单调递增（旧实例被接管后其旧 fence 的写入一律被拒）；
* 异常：A SIGKILL → lease 过期 → B claim 成功后 fence+1；入口切换失败 → 回退
  `drain.resume()`（role 回 serving）。

## 观测（drain_snapshot）

`drain.begin()` / `drain.resume()` / `finalize_handoff()` 会把一份 JSON 快照写进
`drain_snapshot`：四类在途计数（inference_stream / inference / write / read）、
readyz 是否 503、以及单调时钟时间戳。这样交接时间线可以事后从 DB 复核，
不必依赖某一方的日志。
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from pathlib import Path

#: 心跳间隔（秒）。与任务租约的 300s 不同：实例存活是快信号。
HEARTBEAT_SEC = max(1, int(os.environ.get('ZH_INSTANCE_HEARTBEAT_SEC', '10') or 10))

#: 实例租约过期时间（秒）。崩溃后最多这么久可被接管。
LEASE_TTL_SEC = max(2, int(os.environ.get('ZH_INSTANCE_LEASE_TTL_SEC', '30') or 30))

ROLES = ('candidate', 'serving', 'draining', 'handoff', 'stopped')

DDL = (
    '''CREATE TABLE IF NOT EXISTS instance_leases (
  instance_id    TEXT PRIMARY KEY,
  owner          TEXT NOT NULL,
  lease_until    INTEGER NOT NULL,
  fence          INTEGER NOT NULL DEFAULT 0,
  updated_at     INTEGER NOT NULL,
  source_sha     TEXT NOT NULL DEFAULT '',
  build_id       TEXT NOT NULL DEFAULT '',
  host           TEXT NOT NULL DEFAULT '',
  pid            INTEGER NOT NULL DEFAULT 0,
  start_ticks    TEXT NOT NULL DEFAULT '',
  role           TEXT NOT NULL DEFAULT 'candidate'
                 CHECK (role IN ('candidate','serving','draining','handoff','stopped')),
  phase          TEXT NOT NULL DEFAULT 'starting',
  drain_snapshot TEXT NOT NULL DEFAULT '{}',
  detail         TEXT
)''',
    'CREATE INDEX IF NOT EXISTS idx_instance_leases_until '
    'ON instance_leases(lease_until)',
    'CREATE INDEX IF NOT EXISTS idx_instance_leases_role_until '
    'ON instance_leases(role, lease_until)',
    'CREATE INDEX IF NOT EXISTS idx_instance_leases_host ON instance_leases(host)',
)

_IDENTITY: dict | None = None
_IDENTITY_PID: int | None = None


# ── 身份 ───────────────────────────────────────────────────────────────


def start_ticks() -> str:
    """本进程的 `/proc/<pid>/stat` 启动 ticks（防 PID 复用）。

    读不到（非 Linux / 无 /proc）返回空串 —— instance_id 里这一位为空时，
    接管判定只看 lease_until，不做「PID 是否还是那个进程」的推断。
    """
    try:
        fields = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()
        return str(fields[19])
    except (OSError, IndexError, ValueError):
        return ''


def identity() -> dict:
    """本进程的实例身份（每进程唯一，fork 后重建）。

    `instance_id = host:pid:start_ticks:uuid`：host 隔离不同机器、pid+start_ticks
    防 PID 复用、uuid 保证同一 pid 的两次启动也不同。`owner` 与 tasklease 同风格
    （`host:uuid`），供 `owner_alive()` 使用。
    """
    global _IDENTITY, _IDENTITY_PID
    pid = os.getpid()
    if _IDENTITY is not None and _IDENTITY_PID == pid:
        return dict(_IDENTITY)
    host = socket.gethostname()
    token = uuid.uuid4().hex
    _IDENTITY = {'instance_id': f'{host}:{pid}:{start_ticks() or "-"}:{token}',
                 'owner': f'{host}:{token}', 'host': host, 'pid': pid,
                 'start_ticks': start_ticks(), 'token': token}
    _IDENTITY_PID = pid
    return dict(_IDENTITY)


def reset_identity() -> None:
    """测试/实验用：丢弃当前进程身份，下一次 identity() 会生成新的。"""
    global _IDENTITY, _IDENTITY_PID
    _IDENTITY = None
    _IDENTITY_PID = None


def owner_alive(owner: str | None) -> bool | None:
    """本机 owner 是否活着；跨机 / 信息不足返回 None（与 tasklease 同口径）。

    owner 形式是 `host:uuid`；uuid 不能反推 pid，所以**不再猜**：本机且有 pid
    记录时由 `describe()` 的 pid/start_ticks 判断，其余返回 None。
    这个函数只用于展示，不参与接管判定（判定用 lease_until）。
    """
    if not owner or ':' not in owner:
        return None
    host, _, token = owner.partition(':')
    if host != socket.gethostname():
        return None
    if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        return None
    # 只有「本进程自己」能确定；别的 owner 需要 pid（不在 owner 串里）
    if owner == identity()['owner']:
        return True
    return None


def pid_alive(pid: int, ticks: str = '') -> bool | None:
    """按 pid（+ 启动 ticks）判断进程是否还活着；不可判返回 None。

    注意「没有 /proc」和「有 /proc 但没有这个 pid」是两件事：前者（Windows/macOS）
    是**不可判**，后者才是已退出。第一版把两者都当 False，会把活着的进程报成死的。
    """
    if not pid or pid <= 0:
        return None
    if not Path('/proc').is_dir():
        return None                 # 没有 /proc 的环境：不猜
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError:
        return False                # /proc 可用但没有这个 pid → 已退出
    except (OSError, IndexError, ValueError):
        return None
    if fields and fields[0] == 'Z':
        return False                # 僵尸：已退出，只是还没被回收
    if ticks and str(fields[19]) != str(ticks):
        return False                # pid 被复用：不是原来那个进程
    return True


# ── 表初始化（迁移 v8 复用同一份 DDL）─────────────────────────────────


def initialize(conn: sqlite3.Connection | None = None) -> None:
    """幂等建表。迁移步骤与模块自用都走这里，避免两份 DDL 漂移。"""
    if conn is not None:
        for statement in DDL:
            conn.execute(statement)
        return
    from . import db
    for statement in DDL:
        db.execute(statement)


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else int(now)


def _row(conn: sqlite3.Connection, instance_id: str):
    return conn.execute(
        'SELECT instance_id, owner, lease_until, fence, updated_at, source_sha, build_id, '
        'host, pid, start_ticks, role, phase, drain_snapshot, detail '
        'FROM instance_leases WHERE instance_id=?', (instance_id,)).fetchone()


def _snapshot_text(snapshot: dict | None) -> str:
    if not snapshot:
        return '{}'
    try:
        return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)[:4000]
    except (TypeError, ValueError):
        return '{}'


# ── claim / heartbeat / 状态机 ─────────────────────────────────────────


def claim(instance_id: str | None = None, *, ttl_seconds: int | None = None,
          role: str = 'serving', phase: str = 'started',
          source_sha: str = '', build_id: str = '', detail: str = '',
          snapshot: dict | None = None,
          owner: str | None = None, now: int | None = None) -> tuple[bool, int]:
    """取得/续期本实例的租约，返回 `(claimed, fence)`。

    `BEGIN IMMEDIATE` 内判断「无行 / 自持 / lease_until 已过期」→ 写
    owner、lease_until、fence+1。**不做选举**：能不能成为 serving 由交接时序
    保证（§1.6.3 不变量），本函数只负责「这个实例还持有自己的所有权」。
    """
    if role not in ROLES:
        raise ValueError(f'未知 role：{role}')
    ttl = int(LEASE_TTL_SEC if ttl_seconds is None else ttl_seconds)
    if ttl <= 0:
        raise ValueError('ttl 必须为正数')
    me = identity()
    identity_value = instance_id or me['instance_id']
    initialize()
    from . import db
    with db.transaction() as conn:
        # Lock/BEGIN IMMEDIATE may wait beyond expiry: decide ownership now.
        stamp = _now(now)
        row = _row(conn, identity_value)
        if row is None:
            conn.execute(
                'INSERT INTO instance_leases(instance_id, owner, lease_until, fence, '
                'updated_at, source_sha, build_id, host, pid, start_ticks, role, phase, '
                'drain_snapshot, detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identity_value, owner or me['owner'], stamp + ttl, 1, stamp,
                 source_sha or '', build_id or '', me['host'], me['pid'],
                 me['start_ticks'], role, phase, _snapshot_text(snapshot), detail or 'claimed'))
            return True, 1
        current_owner = str(row['owner'] or '')
        until = int(row['lease_until'] or 0)
        fence = int(row['fence'] or 0)
        if current_owner and current_owner != (owner or me['owner']) and until > stamp:
            return False, fence        # 别人持有且未过期
        new_fence = fence + 1
        changed = conn.execute(
            'UPDATE instance_leases SET owner=?, lease_until=?, fence=?, updated_at=?, '
            'source_sha=?, build_id=?, role=?, phase=?, drain_snapshot=?, detail=? '
            'WHERE instance_id=? AND fence=?',
            (owner or me['owner'], stamp + ttl, new_fence, stamp,
             source_sha or row['source_sha'], build_id or row['build_id'],
             role, phase, _snapshot_text(snapshot) or row['drain_snapshot'],
             detail or 'renewed', identity_value, fence)).rowcount
        if changed != 1:
            return False, fence
        return True, new_fence


def heartbeat(instance_id: str | None = None, fence: int | None = None, *,
              ttl_seconds: int | None = None, owner: str | None = None,
              now: int | None = None) -> bool:
    """带 fence 续期；fence/owner 不匹配 → False（旧实例被接管后失效）。"""
    ttl = int(LEASE_TTL_SEC if ttl_seconds is None else ttl_seconds)
    if ttl <= 0:
        raise ValueError('ttl 必须为正数')
    me = identity()
    identity_value = instance_id or me['instance_id']
    initialize()
    from . import db
    with db.transaction() as conn:
        # Lock/BEGIN IMMEDIATE may wait beyond expiry: decide ownership now.
        stamp = _now(now)
        if fence is None:
            changed = conn.execute(
                'UPDATE instance_leases SET lease_until=?, updated_at=? '
                'WHERE instance_id=? AND owner=? AND lease_until>?',
                (stamp + ttl, stamp, identity_value, owner or me['owner'], stamp)).rowcount
        else:
            changed = conn.execute(
                'UPDATE instance_leases SET lease_until=?, updated_at=? '
                'WHERE instance_id=? AND fence=? AND owner=? AND lease_until>?',
                (stamp + ttl, stamp, identity_value, int(fence),
                 owner or me['owner'], stamp)).rowcount
        return changed == 1


def set_role(instance_id: str | None = None, fence: int | None = None, *,
             role: str, phase: str = '', snapshot: dict | None = None,
             detail: str = '', owner: str | None = None,
             now: int | None = None) -> bool:
    """带 fence 更新 role/phase/drain_snapshot（状态机迁移的唯一入口）。"""
    if role not in ROLES:
        raise ValueError(f'未知 role：{role}')
    me = identity()
    identity_value = instance_id or me['instance_id']
    initialize()
    from . import db
    with db.transaction() as conn:
        # Lock/BEGIN IMMEDIATE may wait beyond expiry: decide ownership now.
        stamp = _now(now)
        row = _row(conn, identity_value)
        if row is None:
            return False
        if fence is not None and int(row['fence'] or 0) != int(fence):
            return False
        if str(row['owner'] or '') != (owner or me['owner']):
            return False
        if int(row['lease_until'] or 0) <= stamp:
            # 租约已过期：过期实例（SIGKILL 后假死 / 心跳停摆）不得再改状态。
            # 否则「A 的旧 fence 写入被拒」这条 §1.6.3 的不变量就不成立。
            return False
        sets = ['role=?', 'updated_at=?']
        args: list[object] = [role, stamp]
        if phase:
            sets.append('phase=?')
            args.append(phase)
        if snapshot is not None:
            sets.append('drain_snapshot=?')
            args.append(_snapshot_text(snapshot))
        if detail:
            sets.append('detail=?')
            args.append(detail[:500])
        sql = f'UPDATE instance_leases SET {", ".join(sets)} WHERE instance_id=?'
        args.append(identity_value)
        if fence is not None:
            sql += ' AND fence=?'
            args.append(int(fence))
        return conn.execute(sql, tuple(args)).rowcount == 1


def begin_drain(instance_id: str | None = None, fence: int | None = None, *,
                snapshot: dict | None = None, owner: str | None = None,
                now: int | None = None) -> bool:
    """serving → draining（写排空快照：四类在途计数 + readyz 状态）。"""
    return set_role(instance_id, fence, role='draining', phase='draining',
                    snapshot=snapshot, detail='drain begin', owner=owner, now=now)


def finalize_handoff(instance_id: str | None = None, fence: int | None = None, *,
                     snapshot: dict | None = None, owner: str | None = None,
                     now: int | None = None) -> bool:
    """draining → handoff：在途已归零、写最终快照，准备释放所有权。"""
    return set_role(instance_id, fence, role='handoff', phase='handoff',
                    snapshot=snapshot, detail='handoff', owner=owner, now=now)


def resume_serving(instance_id: str | None = None, fence: int | None = None, *,
                   snapshot: dict | None = None, owner: str | None = None,
                   now: int | None = None) -> bool:
    """draining → serving：入口切换失败时的回退（`drain.resume()`）。"""
    return set_role(instance_id, fence, role='serving', phase='serving',
                    snapshot=snapshot, detail='drain resumed', owner=owner, now=now)


def release(instance_id: str | None = None, fence: int | None = None, *,
            snapshot: dict | None = None, owner: str | None = None,
            now: int | None = None) -> bool:
    """带 fence 释放所有权：owner 清空、lease_until 归零、role=stopped；fence 不回退。"""
    me = identity()
    identity_value = instance_id or me['instance_id']
    initialize()
    from . import db
    with db.transaction() as conn:
        # Lock/BEGIN IMMEDIATE may wait beyond expiry: decide ownership now.
        stamp = _now(now)
        assignments = ["owner=''", 'lease_until=0', "role='stopped'", "phase='stopped'",
                       'updated_at=?']
        args: list[object] = [stamp]
        if snapshot is not None:
            assignments.append('drain_snapshot=?')
            args.append(_snapshot_text(snapshot))
        args.extend([identity_value, owner or me['owner']])
        sql = (f'UPDATE instance_leases SET {", ".join(assignments)} '
               'WHERE instance_id=? AND owner=?')
        if fence is not None:
            sql += ' AND fence=?'
            args.append(int(fence))
        return conn.execute(sql, tuple(args)).rowcount == 1


def still_holding(instance_id: str | None = None, fence: int | None = None, *,
                  owner: str | None = None, now: int | None = None) -> bool:
    """fence 匹配、owner 是自己、租约未过期 —— 状态写入前的守卫。"""
    me = identity()
    identity_value = instance_id or me['instance_id']
    from . import db
    row = db.query_one('SELECT owner, lease_until, fence FROM instance_leases '
                       'WHERE instance_id=?', (identity_value,))
    if row is None:
        return False
    if fence is not None and int(row['fence'] or 0) != int(fence):
        return False
    if str(row['owner'] or '') != (owner or me['owner']):
        return False
    return int(row['lease_until'] or 0) > _now(now)


# ── 查询 ───────────────────────────────────────────────────────────────


def describe(instance_id: str | None = None) -> dict | None:
    """单个实例的当前租约快照；不存在返回 None。"""
    me = identity()
    identity_value = instance_id or me['instance_id']
    from . import db
    row = db.query_one(
        'SELECT * FROM instance_leases WHERE instance_id=?', (identity_value,))
    if row is None:
        return None
    item = dict(row)
    item['lease_until'] = int(item.get('lease_until') or 0)
    item['fence'] = int(item.get('fence') or 0)
    item['updated_at'] = int(item.get('updated_at') or 0)
    item['pid'] = int(item.get('pid') or 0)
    try:
        item['drain_snapshot'] = json.loads(item.get('drain_snapshot') or '{}')
    except (TypeError, ValueError):
        item['drain_snapshot'] = {}
    item['alive'] = pid_alive(item['pid'], str(item.get('start_ticks') or ''))
    return item


def active_instances(role: str | None = None, host: str | None = None, *,
                     now: int | None = None) -> list[dict]:
    """未过期的实例租约（可按 role / host 过滤）。

    **注意**：本函数只被交接观测/运维查询使用。`begin_publish()` 不调用它
    （发布互斥只看 `task_leases`，见模块 docstring 与对照测试①）。
    """
    stamp = _now(now)
    sql = ('SELECT instance_id, owner, lease_until, fence, updated_at, role, phase, host, '
           'pid, start_ticks, source_sha, build_id, detail FROM instance_leases '
           'WHERE lease_until > ?')
    args: list[object] = [stamp]
    if role is not None:
        sql += ' AND role=?'
        args.append(role)
    if host is not None:
        sql += ' AND host=?'
        args.append(host)
    sql += ' ORDER BY role, updated_at'
    from . import db
    items = []
    for row in db.query(sql, tuple(args)):
        item = dict(row)
        item['lease_until'] = int(item.get('lease_until') or 0)
        item['fence'] = int(item.get('fence') or 0)
        item['pid'] = int(item.get('pid') or 0)
        items.append(item)
    return items


# ── drain 联动（serving ↔ draining）────────────────────────────────────


def sync_role(role: str, snapshot: dict | None = None) -> bool:
    """把本进程实例租约的 role 同步成 drain 的状态（drain.py 调用）。

    drain 可以在**没有**实例租约的情况下工作（例如测试或未启用交接的部署）：
    此时 describe() 为 None，本函数静默返回 False，不影响 drain 本身
    —— readyz 语义、在途计数也都不经过这里。
    """
    from . import db
    me = identity()
    row = db.query_one('SELECT fence, role FROM instance_leases WHERE instance_id=?',
                       (me['instance_id'],))
    if row is None:
        return False
    phase = {'draining': 'draining', 'serving': 'serving'}.get(role, role)
    return set_role(me['instance_id'], int(row['fence'] or 0),
                    role=role, phase=phase, snapshot=snapshot)


def current_roles() -> dict:
    """排障用：本进程租约 + 全部活跃实例（不参与任何判定）。"""
    return {'self': describe(), 'active': active_instances()}
