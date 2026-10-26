"""K28：跨进程 task lease（唯一执行权）+ 发布状态的双向原子握手。

本模块只有标准库依赖；对 Manager 运行时（`db`/`config`）的引用全部在函数内
惰性导入，因此 `ops/release-lock/release_lock.py` 可以用裸 `sqlite3` 连接复用
同一份发布协议（SQL 与判定只有一处，不靠两边各写一遍再漂移）。

## 为什么需要

`services/dailyplan.py` 的调度器是**进程内** asyncio task，去重只有
`settings.daily_tasks_plan[task].last_run`（`YYYY-MM-DD-HH` 戳）。两个 Manager
进程（滚动升级的新旧进程、误起的第二个实例）会各自判定「到点」并各跑一遍：
签到幂等还好，领奖/做任务会重复上报。`services/filelock.py` 明确只有进程内语义。
这里用 SQLite 的单行租约把「谁有权执行某个 task」变成跨进程、可失效、可接管的
事实。

## 语义（逐条对应任务卡契约）

* `identity()`：宿主名 + 进程启动时生成的随机标识（与 `budgetowner.identity()`
  同风格，fork 后 pid 变化会重建）。每进程唯一，且带一个只有该进程持有的 OS
  文件锁 —— 这是 `owner_alive()` 能给出 True/False 的依据。
* `claim(task, ttl, guard=None)`：`BEGIN IMMEDIATE` 事务内先查
  `settings['publish.state']`（默认开启，避免调用方漏传），再调 `guard(conn)`
  （返回非空字符串即拒绝，dailyplan 用它叠加内核 flock 探测），最后判断
  「无行 / 自持 / lease_until 已过期」→ 写入 owner、lease_until、fence+1。
  两个并发进程只能有一个成功：SQLite 的写事务把两者串行化。
* `renew(task, fence, ttl)` / `release(task, fence)`：带 fence 条件更新，fence
  不匹配一律拒绝 —— 旧 owner 在租约被接管后不能再续租、也不能释放别人的租约。
* `still_holding(task, fence)`：状态写入前的守卫（fence 匹配且未过期）。
* `owner_alive(owner)`：同机可判断时返回 True/False，跨机或信息不足返回 None
  （**不猜**，与 `budgetowner.alive()` 同口径）。

## fence 单调性：为什么 release 不删行

行一旦建立就保留；`release` 只把 `owner` 清空、`lease_until` 置 0，**fence 只在
claim 时 +1，永不回退**。若 release 删行，fence 会从 1 重新开始：一个早已被
接管、随后「复活」的旧进程（fence=1）会误写/误释放新 owner 的租约（经典 ABA）。
保留行 + 单调 fence 让「旧 fence 永远失效」。

## 发布互斥：两边都在同一个写事务里「检查对方 + 写自己」

单机权威是内核 flock（`ops/release-lock/release_lock.py` 持有它直到发布结束），
DB 里的 `settings['publish.state']` 是给调度方与跨副本观察用的第二信号。顺序：

  发布方：flock 成功 → BEGIN IMMEDIATE → `begin_publish()`（检查活跃 lease，
          有则等待/失败 → 写 publish.state）→ COMMIT → 执行发布
          → 清理 publish.state → 释放 flock
  调度方：BEGIN IMMEDIATE → `guard()` 检查 publish.state（未过期）+ 探测 flock
          → 插入/续租 task lease → COMMIT

两边都在**同一个 SQLite 写事务**里完成「检查对方 + 写自己」，BEGIN IMMEDIATE
把两段串行化，因此不存在「双方都检查通过」的窗口：

  · 调度方先提交 lease → 发布方在事务内看到活跃 lease → 等待或明确失败；
  · 发布方先提交 publish.state → 调度方在事务内看到 → 不 claim；
  · 「调度方探测 flock 通过之后、发布方才启动」：调度方随后写入 lease 并提交，
    发布方在自己的事务里看到这个 lease → 等待。**结局是发布方等待，不是双方
    执行** —— 这正是「探测通过后发布启动」竞态的处置（见 test_task_leases.py
    的 test_publish_started_after_probe_only_one_side_runs）。

`publish.state` 带 `lease_until`：发布方崩溃留下的残留记录会自然过期，不会永久
卡住调度器；`schedule` 侧一律以「未过期」为准。
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from pathlib import Path

#: 一次租约的默认存活时间（秒）。必须覆盖单次任务最长执行时间：dailyplan 用它
#: 作为 claim 的 TTL，并在执行期间按 TTL/3 续租，所以这只影响「崩溃后多久可被
#: 接管」与「执行中不作为的上限」，不影响正常运行。
DEFAULT_TTL = max(1, int(os.environ.get('ZH_TASK_LEASE_TTL', '300') or 300))

#: settings 里发布状态的键。
PUBLISH_KEY = 'publish.state'

#: 发布状态默认有效期（秒）：发布方崩溃后残留记录在这之后失效。
PUBLISH_TTL = max(1, int(os.environ.get('ZH_PUBLISH_LEASE_TTL', '900') or 900))

DDL = (
    '''CREATE TABLE IF NOT EXISTS task_leases (
  task       TEXT PRIMARY KEY,
  owner      TEXT NOT NULL,
  lease_until INTEGER NOT NULL,
  fence      INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  detail     TEXT
)''',
    'CREATE INDEX IF NOT EXISTS idx_task_leases_until ON task_leases(lease_until)',
)

_OWNER: str | None = None
_OWNER_PID: int | None = None
_OWNER_FILE = None
_OWNER_DATABASE: str | None = None


class PublishBlocked(RuntimeError):
    """发布方在等待窗口内仍有活跃 task lease（或反之）。"""

    def __init__(self, message: str, blockers: list | None = None) -> None:
        super().__init__(message)
        self.blockers = list(blockers or [])


# ── owner 身份 ─────────────────────────────────────────────────────────


def _owner_dir(database: Path) -> Path:
    """owner 锁文件目录（与 budgetowner 同风格：700、拒绝符号链接）。"""
    target = Path(database).parent / '.task-owners'
    target.mkdir(mode=0o700, exist_ok=True)
    if target.is_symlink():
        raise RuntimeError('unsafe task owner directory')
    return target


def _lock(handle) -> None:
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _try_lock(handle) -> bool:
    try:
        _lock(handle)
        return True
    except OSError:
        return False


def _release_identity_file() -> None:
    global _OWNER_FILE
    if _OWNER_FILE is not None:
        try:
            _OWNER_FILE.close()          # 关闭即释放 OS 锁
        except Exception:  # noqa: BLE001
            pass
    _OWNER_FILE = None


def identity() -> str:
    """本进程的 owner 标识：`<host>:<32 hex>`，每进程唯一。

    fork 之后 pid 变化 → 重建身份（子进程不能沿用父进程的 owner，否则两个进程
    会同时「自持」同一租约）。锁文件由本进程持有；进程死亡时内核自动释放，
    `owner_alive()` 据此判断。
    """
    global _OWNER, _OWNER_PID, _OWNER_FILE, _OWNER_DATABASE
    from . import config  # 惰性：release_lock 只用发布协议，不触发 Manager 运行时

    pid = os.getpid()
    database = str(config.DB_PATH)
    if _OWNER is not None and _OWNER_PID == pid and _OWNER_DATABASE == database:
        return _OWNER
    _release_identity_file()
    _OWNER = socket.gethostname() + ':' + uuid.uuid4().hex
    _OWNER_PID = pid
    _OWNER_DATABASE = database
    path = _owner_dir(config.DB_PATH) / _OWNER.replace(':', '_')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    _OWNER_FILE = os.fdopen(fd, 'r+b')
    _OWNER_FILE.write(b'1')
    _OWNER_FILE.flush()
    _lock(_OWNER_FILE)
    return _OWNER


def reset_identity() -> None:
    """测试/工具用：丢弃当前进程身份（下一次 identity() 会新建）。"""
    global _OWNER, _OWNER_PID, _OWNER_DATABASE
    _release_identity_file()
    _OWNER = None
    _OWNER_PID = None
    _OWNER_DATABASE = None


def owner_alive(owner: str | None) -> bool | None:
    """本机 owner 是否仍活着；跨机/信息不足返回 None（不猜）。

    * 跨机（host 部分不是本机）→ None。lease 过期才是接管判据，这里只用于
      日志/界面提示，不参与正确性判断。
    * 锁文件不存在 → None（可能来自别的副本、或被清理过，不能断言已死）。
    * 能拿到锁 → False（进程已消失，残留文件）；拿不到 → True。
    """
    if not owner or ':' not in owner:
        return None
    host, _, token = owner.partition(':')
    if host != socket.gethostname():
        return None
    if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        return None
    try:
        from . import config
        path = _owner_dir(config.DB_PATH) / owner.replace(':', '_')
    except Exception:  # noqa: BLE001
        return None
    try:
        fd = os.open(str(path), os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        with os.fdopen(fd, 'r+b') as handle:
            return not _try_lock(handle)
    except OSError:
        return None


# ── 表初始化（迁移 v7 复用同一份 DDL）─────────────────────────────────


def initialize(conn: sqlite3.Connection | None = None) -> None:
    """幂等建表。迁移步骤与模块自用都走这里，避免两份 DDL 漂移。"""
    if conn is not None:
        for statement in DDL:
            conn.execute(statement)
        return
    from . import db
    for statement in DDL:
        db.execute(statement)


# ── claim / renew / release ────────────────────────────────────────────


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else int(now)


def _row(conn: sqlite3.Connection, task: str):
    return conn.execute(
        'SELECT task, owner, lease_until, fence, updated_at, detail '
        'FROM task_leases WHERE task=?', (task,)).fetchone()


def describe(task: str) -> dict | None:
    """当前租约的只读快照（排障/测试/日志用）；没有行返回 None。"""
    from . import db
    row = db.query_one('SELECT task, owner, lease_until, fence, updated_at, detail '
                       'FROM task_leases WHERE task=?', (task,))
    if row is None:
        return None
    return {'task': row['task'], 'owner': row['owner'],
            'lease_until': int(row['lease_until'] or 0),
            'fence': int(row['fence'] or 0), 'updated_at': int(row['updated_at'] or 0),
            'detail': row['detail'],
            'alive': owner_alive(row['owner'])}


def claim(task: str, ttl_seconds: int | None = None, *, guard=None,
          owner: str | None = None, now: int | None = None,
          detail: str = '', respect_publish: bool = True) -> tuple[bool, int]:
    """尝试取得 task 的唯一执行权，返回 `(claimed, fence)`。

    事务内的判定顺序（全部在同一个 BEGIN IMMEDIATE 里）：
      1. `respect_publish=True`（默认）时先查 `settings['publish.state']`：
         发布中就不 claim —— 这是调度方必须有的检查，默认开启以免调用方漏传；
      2. `guard(conn)`：可选的 `callable(conn) -> str | None`，返回非空即拒绝
         （dailyplan 用它再叠加内核 flock 探测，见模块 docstring）；
      3. 无行 / 自持 / lease_until 已过期 → 写 owner、lease_until、fence+1。

    owner/now 只给测试与工具注入；生产调用一律用 `identity()` 与本机时钟。
    """
    if not task or not isinstance(task, str):
        raise ValueError('task 必须是非空字符串')
    ttl = int(DEFAULT_TTL if ttl_seconds is None else ttl_seconds)
    if ttl <= 0:
        raise ValueError('ttl 必须为正数')
    me = owner or identity()
    stamp = _now(now)
    initialize()
    from . import db
    with db.transaction() as conn:
        blocked = False
        if respect_publish and publish_state(conn, now=stamp) is not None:
            blocked = True
        if not blocked and guard is not None and guard(conn):
            blocked = True
        if blocked:
            row = _row(conn, task)
            return False, int(row['fence'] or 0) if row is not None else 0
        row = _row(conn, task)
        if row is None:
            conn.execute(
                'INSERT INTO task_leases(task, owner, lease_until, fence, updated_at, detail) '
                'VALUES(?,?,?,?,?,?)',
                (task, me, stamp + ttl, 1, stamp, detail or 'claimed'))
            return True, 1
        current_owner = str(row['owner'] or '')
        until = int(row['lease_until'] or 0)
        fence = int(row['fence'] or 0)
        # 只有「没人持有 / 自己持有 / 已过期」才能接管；否则如实拒绝。
        if current_owner and current_owner != me and until > stamp:
            return False, fence
        new_fence = fence + 1
        changed = conn.execute(
            'UPDATE task_leases SET owner=?, lease_until=?, fence=?, updated_at=?, detail=? '
            'WHERE task=? AND fence=?',
            (me, stamp + ttl, new_fence, stamp,
             detail or ('renewed' if current_owner == me else 'takeover'), task, fence)
        ).rowcount
        if changed != 1:
            # BEGIN IMMEDIATE 下不应发生；保留判断以便未来换隔离级别时仍是安全的。
            return False, fence
        return True, new_fence


def renew(task: str, fence: int, ttl_seconds: int | None = None, *,
          owner: str | None = None, now: int | None = None) -> bool:
    """带 fence 续租；fence/owner 不匹配 → False（旧 owner 已失效）。"""
    ttl = int(DEFAULT_TTL if ttl_seconds is None else ttl_seconds)
    if ttl <= 0:
        raise ValueError('ttl 必须为正数')
    me = owner or identity()
    stamp = _now(now)
    initialize()
    from . import db
    with db.transaction() as conn:
        changed = conn.execute(
            'UPDATE task_leases SET lease_until=?, updated_at=? '
            'WHERE task=? AND fence=? AND owner=?',
            (stamp + ttl, stamp, task, int(fence), me)).rowcount
        return changed == 1


def release(task: str, fence: int, *, owner: str | None = None,
            now: int | None = None) -> bool:
    """带 fence 释放：owner 清空、lease_until 归零，fence 不回退（防 ABA）。"""
    me = owner or identity()
    stamp = _now(now)
    initialize()
    from . import db
    with db.transaction() as conn:
        changed = conn.execute(
            "UPDATE task_leases SET owner='', lease_until=0, updated_at=?, detail='released' "
            'WHERE task=? AND fence=? AND owner=?',
            (stamp, task, int(fence), me)).rowcount
        return changed == 1


def still_holding(task: str, fence: int, *, owner: str | None = None,
                  now: int | None = None) -> bool:
    """当前持有者仍是本进程、fence 匹配、且租约未过期。

    所有需要「带 fence 写状态」的调用方在写之前用它检查（见
    `dailyplan.mark_ran_fenced`）；为 False 时必须放弃写入。
    """
    me = owner or identity()
    stamp = _now(now)
    from . import db
    row = db.query_one(
        'SELECT owner, lease_until FROM task_leases WHERE task=? AND fence=?',
        (task, int(fence)))
    if row is None or str(row['owner'] or '') != me:
        return False
    return int(row['lease_until'] or 0) > stamp


def active_leases(conn: sqlite3.Connection, *, now: int | None = None) -> list[dict]:
    """未过期的租约列表（发布方检查用；纯 conn，不依赖 Manager 运行时）。"""
    stamp = _now(now)
    try:
        rows = conn.execute(
            'SELECT task, owner, lease_until, fence FROM task_leases '
            'WHERE lease_until > ? ORDER BY task', (stamp,)).fetchall()
    except sqlite3.Error:
        return []           # 旧库还没有这张表 = 不存在任何租约
    return [{'task': str(r[0]), 'owner': str(r[1] or ''),
             'lease_until': int(r[2] or 0), 'fence': int(r[3] or 0)} for r in rows]


# ── 发布状态（publish.state）：双向握手协议，纯 conn ────────────────────


def publish_state(conn: sqlite3.Connection, *, now: int | None = None) -> dict | None:
    """未过期的发布状态；无记录/已过期/格式坏 → None（按「没有发布」处理）。"""
    stamp = _now(now)
    try:
        row = conn.execute('SELECT value FROM settings WHERE key=?',
                           (PUBLISH_KEY,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    raw = row[0]
    try:
        state = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or str(state.get('state')) != 'publishing':
        return None
    try:
        until = int(state.get('lease_until') or 0)
    except (TypeError, ValueError):
        return None
    return state if until > stamp else None


def begin_publish(conn: sqlite3.Connection, owner: str,
                  ttl_seconds: int | None = None, *, now: int | None = None) -> dict:
    """在调用方已开的写事务内：检查活跃租约 → 写 publish.state。

    有活跃租约时抛 `PublishBlocked`（调用方决定等待还是失败退出）；这样「检查
    租约」与「写 publish.state」在同一事务里完成，不会出现检查后才被插入的窗口。
    """
    stamp = _now(now)
    ttl = int(PUBLISH_TTL if ttl_seconds is None else ttl_seconds)
    if ttl <= 0:
        raise ValueError('ttl 必须为正数')
    blockers = active_leases(conn, now=stamp)
    if blockers:
        raise PublishBlocked(
            '仍有活跃的每日任务租约，暂不能开始发布：'
            + '；'.join(f"{b['task']} owner={b['owner']} until={b['lease_until']}"
                        for b in blockers),
            blockers)
    state = {'state': 'publishing', 'owner': str(owner), 'started_at': stamp,
             'lease_until': stamp + ttl}
    conn.execute(
        'INSERT INTO settings(key, value) VALUES(?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (PUBLISH_KEY, json.dumps(state, ensure_ascii=False)))
    return state


def renew_publish(conn: sqlite3.Connection, ttl_seconds: int | None = None, *,
                  now: int | None = None) -> bool:
    """延长 publish.state 有效期（发布阶段多、总时长可能超过 TTL）。"""
    stamp = _now(now)
    ttl = int(PUBLISH_TTL if ttl_seconds is None else ttl_seconds)
    state = publish_state(conn, now=stamp)
    if state is None:
        return False
    state['lease_until'] = stamp + ttl
    conn.execute('UPDATE settings SET value=? WHERE key=?',
                 (json.dumps(state, ensure_ascii=False), PUBLISH_KEY))
    return True


def end_publish(conn: sqlite3.Connection, owner: str | None = None) -> bool:
    """清除发布状态；给了 owner 时只清除自己的（不误清后续发布）。"""
    row = conn.execute('SELECT value FROM settings WHERE key=?',
                       (PUBLISH_KEY,)).fetchone()
    if row is None:
        return False
    if owner is not None:
        try:
            state = json.loads(row[0])
        except (TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get('owner') not in (None, owner):
            return False
    conn.execute('DELETE FROM settings WHERE key=?', (PUBLISH_KEY,))
    return True


# ── 调度方守卫（dailyplan 用）───────────────────────────────────────────


def publish_block_reason(conn: sqlite3.Connection, *,
                         release_probe=None, now: int | None = None) -> str | None:
    """claim 事务内的发布互斥守卫：任一为真就返回拒绝原因。

    * `publish.state` 未过期（DB 侧信号，跨副本也能看到）；
    * `release_probe()` 为真（默认由调用方注入 `dailyplan.release_in_progress`，
      它探测内核 flock）。默认 None = 不做内核探测（release_lock 之类没有
      dailyplan 的调用方）。
    """
    state = publish_state(conn, now=now)
    if state is not None:
        return (f"发布进行中：owner={state.get('owner')} "
                f"until={state.get('lease_until')}")
    if release_probe is not None and release_probe():
        return '发布锁被持有（内核 flock）'
    return None
