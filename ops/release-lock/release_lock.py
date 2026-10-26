"""Hold a POSIX release mutex throughout a child command and record progress.

This coordinates cooperating maintenance commands on one machine. It is not
an authorization boundary against processes running as the same Unix user.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

TRANSITIONS = {
    'prepare': {'drain', 'switch', 'verify', 'rollback'},
    'drain': {'switch', 'rollback'}, 'switch': {'verify', 'rollback'},
    'verify': {'done', 'rollback'}, 'rollback': {'done', 'failed'},
    'done': set(), 'failed': set(),
}

#: 发布方在「有活跃每日任务租约」时的退出码（与 75 = lock busy 区分开）。
EXIT_TASK_LEASE_BUSY = 76


class PublishBlocked(RuntimeError):
    """等待窗口内仍有活跃的每日任务租约。"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def paths() -> tuple[Path, Path]:
    lock = Path(os.environ.get('ZH_RELEASE_LOCK_FILE', '/run/zhihui-release.lock'))
    state = Path(os.environ.get('ZH_RELEASE_STATE_FILE', '/run/zhihui-release.state.json'))
    if not lock.is_absolute() or not state.is_absolute() or lock == state:
        raise ValueError('Lock and state paths must be distinct, absolute paths')
    return lock, state


def open_file(path: Path) -> int:
    # Never unlink or replace a flock inode: that creates independent locks.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('Lock must be a regular file')
    return fd


# ── K28：与每日任务调度器的双向原子握手（DB 侧第二信号）─────────────────
#
# 内核 flock 是单机权威；`settings['publish.state']` 是给调度方（以及跨副本/
# 跨机的观察者）看的第二信号。两边都必须在**同一个 SQLite 写事务**里完成
# 「检查对方 + 写自己」，否则会留下「双方都检查通过」的窗口：
#   · 调度方：BEGIN IMMEDIATE → 查 publish.state + 探测 flock → 抢 task lease
#   · 发布方：持 flock → BEGIN IMMEDIATE → 等活跃 lease 结束 → 写 publish.state
# 协议实现只在 manager/server/tasklease.py 一处（这里懒加载复用，不重写 SQL）。


def manager_db_path() -> Path | None:
    """**显式配置**的 Manager 主库路径；未配置返回 None（只依赖内核 flock）。

    为什么不猜默认部署路径：这个工具在宿主机上运行，Manager 的部署根因环境而
    异（lab 里有快照目录，生产在 /opt），猜错会写到别的库；在隔离测试里甚至会
    污染真实数据目录。所以 DB 侧发布信号只在显式声明时启用：

        ZH_MANAGER_DB=/opt/workbuddy-manager/data/manager.db   （优先）
        WB_DB=...                                              （Manager 自身变量）

    未配置时内核 flock 仍是单机权威（调度方也会探测它），只是跨副本观察者看不到
    publish.state。发布 wrapper 的部署步骤应在环境里设置 ZH_MANAGER_DB。
    """
    for name in ('ZH_MANAGER_DB', 'WB_DB'):
        value = os.environ.get(name, '').strip()
        if value:
            candidate = Path(value)
            return candidate if candidate.is_file() else None
    return None


def tasklease_module():
    """懒加载发布协议实现（DDL / 判定 / SQL 只有一处，不在这里重写）。"""
    root = Path(__file__).resolve().parents[2]
    for base in (root / 'manager', Path('/opt/workbuddy-manager')):
        if (base / 'server' / 'tasklease.py').is_file():
            if str(base) not in sys.path:
                sys.path.insert(0, str(base))
            from server import tasklease  # noqa: PLC0415
            return tasklease
    raise RuntimeError('找不到 manager/server/tasklease.py：发布互斥需要 Manager 侧协议实现')


def _publish_owner() -> str:
    return 'release:' + socket.gethostname() + ':' + secrets.token_hex(8)


def open_publish_state(database: Path, owner: str) -> sqlite3.Connection:
    """持 flock 之后调用：等活跃租约结束并在同一个写事务里写 publish.state。

    等待策略由 ZH_RELEASE_WAIT_LEASE_SECONDS 控制（默认 60s，0 = 不等待）。
    超时抛 PublishBlocked —— 调用方必须**明确失败退出**，不允许带着活跃任务
    继续发布。返回保持打开的连接，发布结束时用它清理状态。
    """
    tasklease = tasklease_module()
    wait_seconds = float(os.environ.get('ZH_RELEASE_WAIT_LEASE_SECONDS', '60') or 0)
    ttl = int(os.environ.get('ZH_PUBLISH_LEASE_TTL', str(tasklease.PUBLISH_TTL))
              or tasklease.PUBLISH_TTL)
    conn = sqlite3.connect(str(database), timeout=30, isolation_level=None)
    conn.execute('PRAGMA busy_timeout=30000')
    deadline = time.monotonic() + max(0.0, wait_seconds)
    blocked = None
    while True:
        conn.execute('BEGIN IMMEDIATE')
        try:
            tasklease.begin_publish(conn, owner, ttl)
            conn.execute('COMMIT')
            return conn
        except tasklease.PublishBlocked as error:
            conn.execute('ROLLBACK')
            blocked = error
        except BaseException:
            try:
                conn.execute('ROLLBACK')
            finally:
                conn.close()
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            conn.close()
            raise PublishBlocked(str(blocked)) from blocked
        time.sleep(min(1.0, max(0.05, remaining)))


def close_publish_state(conn: sqlite3.Connection | None, owner: str) -> None:
    """清除自己写的发布状态（owner 不匹配就不动，避免清掉后续发布）。"""
    if conn is None:
        return
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            tasklease_module().end_publish(conn, owner)
        finally:
            conn.execute('COMMIT')
    except sqlite3.Error as error:
        print(f'warning: failed to clear publish.state: {error}', file=sys.stderr)
        try:
            conn.execute('ROLLBACK')
        except sqlite3.Error:
            pass
    finally:
        conn.close()


def touch_publish_state(conn: sqlite3.Connection) -> None:
    """发布阶段推进时续期；单次发布可能长于 publish.state 的 TTL。"""
    if conn is None:
        return
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            tasklease_module().renew_publish(conn)
        finally:
            conn.execute('COMMIT')
    except sqlite3.Error:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.Error:
            pass


@contextmanager
def state_guard(path: Path):
    fd = open_file(path.with_name(path.name + '.mutex'))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_state(path: Path, state: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', delete=False) as out:
            temporary = Path(out.name)
            json.dump(state, out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def process_info(pid: int) -> tuple[int, str]:
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return int(fields[1]), fields[19]  # PPID and process start ticks.


def descendant_of(pid: int, owner: int) -> bool:
    seen = set()
    while pid > 1 and pid not in seen:
        if pid == owner:
            return True
        seen.add(pid)
        pid = process_info(pid)[0]
    return False


def active(lock_path: Path) -> bool:
    fd = open_file(lock_path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def stage(new_stage: str) -> int:
    lock_path, state_path = paths()
    with state_guard(state_path):
        state = json.loads(state_path.read_text())
        owner = int(state['owner_pid'])
        supplied = os.environ.get('ZH_RELEASE_LEASE', '')
        if (state.get('status') != 'running' or not supplied
                or not secrets.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(),
                                              state['lease_sha256'])
                or process_info(owner)[1] != state['owner_start_ticks']
                or not descendant_of(os.getpid(), owner)):
            raise ValueError('Stage update requires the active release child lease')
        inherited = os.fstat(int(os.environ['ZH_RELEASE_LOCK_FD']))
        expected = lock_path.stat(follow_symlinks=False)
        if ((inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino)
                or not active(lock_path)):
            raise ValueError('Release child did not inherit the held kernel lock')
        previous = state['stage']
        if new_stage not in TRANSITIONS.get(previous, set()):
            raise ValueError(f'Invalid release transition: {previous} -> {new_stage}')
        state['stage'] = new_stage
        state['updated_at'] = now()
        state['history'].append({'stage': new_stage, 'at': state['updated_at']})
        write_state(state_path, state)
    # K28：长发布要续期 publish.state（TTL 默认 900s）；DB 不存在（只发 web/nginx）
    # 时跳过，此时内核 flock 仍是单机权威。
    database = manager_db_path()
    if database is not None:
        try:
            conn = sqlite3.connect(str(database), timeout=30, isolation_level=None)
        except sqlite3.Error:
            conn = None
        if conn is not None:
            try:
                touch_publish_state(conn)
            finally:
                conn.close()
    print(json.dumps({'release_id': state['release_id'], 'stage': new_stage}))
    return 0


def run(release_id: str, targets: str, command: list[str]) -> int:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', release_id):
        raise ValueError('Invalid release identifier')
    # argparse consumes the separator before the REMAINDER positional.
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        raise ValueError('run requires -- COMMAND ARG...')
    selected = targets.split(',')
    if any(item not in {'manager', 'qtg', 'web', 'nginx', 'engine', 'ops'} for item in selected):
        raise ValueError('Invalid release targets')
    lock_path, state_path = paths()
    fd = open_file(lock_path)
    child = None
    installed_handlers = {}
    exit_code = 1
    publish_conn = None
    publish_owner = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another release or maintenance command holds the lock', file=sys.stderr)
            return 75
        # K28 双向握手：内核锁已在手；DB 侧再确认没有活跃的每日任务租约（有则
        # 按 ZH_RELEASE_WAIT_LEASE_SECONDS 等待，超时明确失败退出 76），然后写
        # settings['publish.state']。调度方在 claim 事务内会看到它。
        database = manager_db_path()
        if database is not None:
            publish_owner = _publish_owner()
            try:
                publish_conn = open_publish_state(database, publish_owner)
            except PublishBlocked as error:
                print('Refusing to start a release while a daily task holds a lease: '
                      f'{error}', file=sys.stderr)
                return EXIT_TASK_LEASE_BUSY
        lease = secrets.token_hex(32)
        state = {
            'release_id': release_id, 'targets': selected,
            'operator': os.environ.get('SUDO_USER') or os.environ.get('USER', 'unknown'),
            'owner_pid': os.getpid(), 'owner_start_ticks': process_info(os.getpid())[1],
            'lease_sha256': hashlib.sha256(lease.encode()).hexdigest(),
            'stage': 'prepare', 'status': 'running', 'started_at': now(),
            'history': [{'stage': 'prepare', 'at': now()}],
        }
        with state_guard(state_path):
            write_state(state_path, state)

        def forward(signum, _frame):
            if child is not None and child.poll() is None:
                try:
                    os.killpg(child.pid, signum)
                except ProcessLookupError:
                    pass

        for signum in (signal.SIGTERM, signal.SIGINT):
            installed_handlers[signum] = signal.signal(signum, forward)
        env = {**os.environ, 'ZH_RELEASE_LEASE': lease, 'ZH_RELEASE_LOCK_FD': str(fd)}
        try:
            child = subprocess.Popen(command, env=env, pass_fds=(fd,), start_new_session=True)
            result = child.wait()
            exit_code = result if result >= 0 else 128 - result
        finally:
            # Success of the command alone cannot prove switch/verification succeeded.
            with state_guard(state_path):
                record = json.loads(state_path.read_text())
                record.update(status='finished', exit_code=exit_code, finished_at=now())
                write_state(state_path, record)
        return exit_code
    finally:
        close_publish_state(publish_conn, publish_owner or '')
        for signum, handler in installed_handlers.items():
            signal.signal(signum, handler)
        # Surviving descendants must continue to block a new release.
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    execute = sub.add_parser('run')
    execute.add_argument('release_id')
    execute.add_argument('targets')
    execute.add_argument('command', nargs=argparse.REMAINDER)
    phase = sub.add_parser('stage')
    phase.add_argument('phase', choices=tuple(TRANSITIONS))
    sub.add_parser('status')
    sub.add_parser('acquire').add_argument('args', nargs=argparse.REMAINDER)
    sub.add_parser('release')
    args = parser.parse_args()
    try:
        if args.operation in {'acquire', 'release'}:
            raise ValueError('Standalone acquire/release is unsafe; use run ID TARGETS -- COMMAND')
        if args.operation == 'run':
            if len(sys.argv) < 6 or sys.argv[4] != '--':
                raise ValueError('run requires -- COMMAND ARG...')
            return run(args.release_id, args.targets, args.command)
        if args.operation == 'stage':
            return stage(args.phase)
        lock_path, state_path = paths()
        with state_guard(state_path):
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            state['active'] = active(lock_path)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 64


if __name__ == '__main__':
    raise SystemExit(main())
