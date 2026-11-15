"""Opt-in, same-host admission across compatible Manager generations.

This database contains resource ownership, never balances or billing evidence.
All peers must use an identical policy. No TTL is interpreted as process death:
only the existing lifetime OS lock can prove an owner has exited. Unknown
owners retain their reservations. This module does not elect background jobs.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

#: `_reap` 的最小间隔。取值权衡见 `_reap` 的注释：死 owner 的租约最多多滞留这么久。
_REAP_INTERVAL_S = 10.0


class Unavailable(RuntimeError):
    pass


class Coordinator:
    def __init__(self, path, policy, *, owner=None, alive=None):
        from . import budgetowner
        self.path = Path(path)
        self.policy = dict(policy)
        required = {'active', 'large', 'bytes', 'preauth', 'queue', 'queue_per_key',
                    'key_default', 'key_quota'}
        if (set(self.policy) != required or any(type(v) is not int or v < 1
                                               for v in self.policy.values())):
            raise ValueError('invalid_shared_admission_policy')
        # Old peers reserved request bodies only. They cannot safely share the
        # same byte budget with a peer that also reserves decoded/translated
        # responses. A policy mismatch requires a proven-drained migration.
        self.encoded = json.dumps(dict(self.policy,response_budget_protocol=1),sort_keys=True,separators=(',',':'))
        self._owner = owner or budgetowner.identity
        self._alive = alive or budgetowner.alive
        #: 上一次清理死 owner 租约的单调时间。`_reap` 曾经在**每个**事务里跑一次
        #: `SELECT DISTINCT owner` 加每个 owner 一次 `open/flock/close`，纯粹是
        #: syscall 开销；而"某个进程已经死了"这件事晚十秒发现不会让任何活着的人
        #: 被错误拒绝（活的 owner 自己还持有租约）。
        self._last_reap = 0.0
        self.local = threading.local()
        self.connections = []
        self.pending_releases = set()
        self.mutex = threading.Lock()
        self.pid = os.getpid()
        self.healthy = False
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _connect(self):
        if os.getpid() != self.pid:
            raise Unavailable('shared_admission_fork_not_supported')
        connection = getattr(self.local, 'connection', None)
        if connection is None:
            # 25 ms 的写锁预算太短：另一个 peer 只要持锁超过 25 ms，`reserve` 就抛
            # `database is locked`，被上层吞成 `False`，最终报"容量已满" —— 把
            # "协调器抖了一下"误报成"该扩容了"。500 ms 让瞬时争用走重试而不是报错。
            connection = sqlite3.connect(self.path, timeout=0.5, isolation_level=None,
                                         check_same_thread=False)
            connection.row_factory = sqlite3.Row
            # NORMAL 而不是 FULL：这张库里的每一行都是"某个活进程当前占用的租约"，
            # 判活靠操作系统文件锁（`budgetowner.alive`），不靠磁盘持久性。掉电丢掉
            # 最后几条写，对应的进程也一起没了，_reap 下次就会回收。用 FULL 意味着
            # 每个准入事务都要等一次 fsync —— 而准入是每个请求都要走的同步路径。
            connection.execute('PRAGMA synchronous=NORMAL')
            self.local.connection = connection
            with self.mutex:
                self.connections.append(connection)
        return connection

    def _initialize(self):
        # Only in the application's private data directory; never accept symlinks.
        if not self.path.parent.is_dir() or self.path.parent.is_symlink():
            raise Unavailable('unsafe_shared_admission_directory')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or os.name == 'posix' and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
                raise Unavailable('unsafe_shared_admission_database')
        finally:
            os.close(fd)
        self.inode = (info.st_dev, info.st_ino)
        self.coordinator_id = hashlib.sha256(
            (str(self.path.resolve()) + ':' + str(self.inode)).encode()).hexdigest()
        connection = self._connect()
        connection.execute('PRAGMA journal_mode=WAL')
        connection.executescript('''
CREATE TABLE IF NOT EXISTS admission_policy(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admission_leases(
 request_id TEXT PRIMARY KEY, owner TEXT NOT NULL, key_id INTEGER,
 preauth INTEGER NOT NULL DEFAULT 0, admitted INTEGER NOT NULL DEFAULT 0,
 queued INTEGER NOT NULL DEFAULT 0, body_bytes INTEGER NOT NULL DEFAULT 0,
 large INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS admission_owner ON admission_leases(owner);
CREATE INDEX IF NOT EXISTS admission_key ON admission_leases(key_id);
''')
        with self._transaction() as conn:
            self._reap(conn)
            row = conn.execute('SELECT value FROM admission_policy WHERE id=1').fetchone()
            if row and row[0] != self.encoded:
                raise Unavailable('shared_admission_policy_conflict')
            conn.execute('INSERT INTO admission_policy VALUES(1,?) '
                         'ON CONFLICT(id) DO UPDATE SET value=excluded.value', (self.encoded,))

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            info = self.path.lstat()
            if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != self.inode:
                self.healthy = False
                raise Unavailable('shared_admission_database_replaced')
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT value FROM admission_policy WHERE id=1').fetchone()
            if row and row[0] != self.encoded:
                raise Unavailable('shared_admission_policy_changed')
            with self.mutex:
                pending = tuple(self.pending_releases)
            for request_id in pending:
                conn.execute('DELETE FROM admission_leases WHERE request_id=? AND owner=?',
                             (request_id, self._owner()))
            yield conn
            conn.commit()
            with self.mutex:
                self.pending_releases.difference_update(pending)
            self.healthy = True
        except BaseException as exc:
            if conn.in_transaction:
                conn.rollback()
            if isinstance(exc, sqlite3.Error):
                self.healthy = False
                raise Unavailable('shared_admission_storage_unavailable') from None
            raise

    def _reap(self, conn):
        """清理已死 owner 的租约 —— 按时间间隔，不是每个事务。

        一次 `SELECT DISTINCT owner` 加每个 owner 一次 `open/flock/close` 全是
        syscall，而它检测的是"某个进程已经不在了"。这种事件的代价是它那一批租约
        多滞留十秒，而不是有人被错误拒绝：活着的 owner 自己还持有租约，`_reap`
        只删 `_alive(...) is False` 的那些。
        """
        now = time.monotonic()
        if now - self._last_reap < _REAP_INTERVAL_S:
            return
        self._last_reap = now
        own = self._owner()
        for row in conn.execute('SELECT DISTINCT owner FROM admission_leases').fetchall():
            if row[0] != own and self._alive(row[0]) is False:
                conn.execute('DELETE FROM admission_leases WHERE owner=?', (row[0],))

    def _row(self, conn, request_id):
        row = conn.execute('SELECT * FROM admission_leases WHERE request_id=?', (request_id,)).fetchone()
        if row is not None and row['owner'] != self._owner():
            raise Unavailable('shared_admission_owner_mismatch')
        return row

    def _ensure(self, conn, request_id):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError('invalid_admission_request_id')
        self._row(conn, request_id)
        conn.execute('INSERT OR IGNORE INTO admission_leases(request_id,owner) VALUES(?,?)',
                     (request_id, self._owner()))

    def preauthorize(self, request_id):
        with self._transaction() as conn:
            self._reap(conn)
            self._ensure(conn, request_id)
            row = self._row(conn, request_id)
            if row['preauth']:
                return True
            count = conn.execute('SELECT COALESCE(SUM(preauth),0) FROM admission_leases').fetchone()[0]
            if count >= self.policy['preauth']:
                return False
            conn.execute('UPDATE admission_leases SET preauth=1 WHERE request_id=?', (request_id,))
            return True

    def end_preauthorize(self, request_id):
        with self._transaction() as conn:
            self._row(conn, request_id)
            conn.execute('UPDATE admission_leases SET preauth=0 WHERE request_id=?', (request_id,))

    def key(self, request_id, key_id, limit):
        if type(key_id) is not int or key_id < 1 or limit not in (
                self.policy['key_default'], self.policy['key_quota']):
            raise ValueError('invalid_shared_key_policy')
        with self._transaction() as conn:
            self._reap(conn)
            self._ensure(conn, request_id)
            row = self._row(conn, request_id)
            if row['key_id'] is not None:
                if row['key_id'] != key_id:
                    raise Unavailable('shared_admission_key_changed')
                return True
            count = conn.execute('SELECT COUNT(*) FROM admission_leases WHERE key_id=?', (key_id,)).fetchone()[0]
            if count >= limit:
                return False
            conn.execute('UPDATE admission_leases SET key_id=? WHERE request_id=?', (key_id, request_id))
            return True

    def reserve(self, request_id, body_bytes, large):
        if type(body_bytes) is not int or body_bytes < 0 or type(large) is not bool:
            raise ValueError('invalid_shared_body_reservation')
        with self._transaction() as conn:
            self._reap(conn)
            self._ensure(conn, request_id)
            row = self._row(conn, request_id)
            sums = conn.execute('SELECT COALESCE(SUM(admitted),0), COALESCE(SUM(large),0), '
                                'COALESCE(SUM(body_bytes),0) FROM admission_leases').fetchone()
            # Reservations only grow until the response and owned cleanup finish.
            retained = max(body_bytes, row['body_bytes'])
            is_large = bool(large or row['large'])
            if (sums[0] + (not row['admitted']) > self.policy['active']
                    or sums[1] + is_large - row['large'] > self.policy['large']
                    or sums[2] + retained - row['body_bytes'] > self.policy['bytes']):
                return False
            conn.execute('UPDATE admission_leases SET admitted=1, queued=0, body_bytes=?, large=? '
                         'WHERE request_id=?', (retained, int(is_large), request_id))
            return True

    def queue(self, request_id):
        with self._transaction() as conn:
            self._ensure(conn, request_id)
            row = self._row(conn, request_id)
            if row['queued']:
                return True
            count = conn.execute('SELECT COALESCE(SUM(queued),0) FROM admission_leases').fetchone()[0]
            key_count = conn.execute('SELECT COALESCE(SUM(queued),0) FROM admission_leases '
                                     'WHERE key_id IS ?', (row['key_id'],)).fetchone()[0]
            if count >= self.policy['queue'] or key_count >= self.policy['queue_per_key']:
                return False
            conn.execute('UPDATE admission_leases SET queued=1 WHERE request_id=?', (request_id,))
            return True

    def end_queue(self, request_id):
        with self._transaction() as conn:
            self._row(conn, request_id)
            conn.execute('UPDATE admission_leases SET queued=0 WHERE request_id=?', (request_id,))

    def release(self, request_id):
        try:
            with self._transaction() as conn:
                self._row(conn, request_id)
                conn.execute('DELETE FROM admission_leases WHERE request_id=?', (request_id,))
        except Unavailable as exc:
            if str(exc) == 'shared_admission_storage_unavailable':
                # Only finished local requests enter this list. Retry atomically
                # on the next successful operation, never reclaim a live stream.
                with self.mutex:
                    self.pending_releases.add(request_id)
            raise

    def close(self):
        """After all owned work has joined; intended for lifecycle shutdown/tests."""
        with self.mutex:
            for conn in self.connections:
                conn.close()
            self.connections.clear()
        self.local = threading.local()

    def probe(self):
        # Tests writer availability without fabricating a request or billing row.
        # Also retries completed local releases after transient SQLite contention.
        with self._transaction():
            pass
        return self.snapshot()

    def snapshot(self):
        # Read only: a health endpoint must not silently reclaim someone else's work.
        try:
            info = self.path.lstat()
            if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != self.inode:
                self.healthy = False
                raise Unavailable('shared_admission_database_replaced')
            conn = self._connect()
            row = conn.execute('SELECT COUNT(*),COALESCE(SUM(admitted),0),COALESCE(SUM(large),0),'
                               'COALESCE(SUM(body_bytes),0),COALESCE(SUM(preauth),0),'
                               'COALESCE(SUM(queued),0) FROM admission_leases').fetchone()
            return dict(zip(('leases', 'active', 'large', 'body_bytes', 'preauth', 'queued'), row),
                        scope='same_host_shared', healthy=self.healthy and not self.pending_releases,
                        coordinator_id=self.coordinator_id,
                        policy_sha256=hashlib.sha256(self.encoded.encode()).hexdigest(),
                        cleanup_pending=len(self.pending_releases), limits=dict(self.policy))
        except sqlite3.Error:
            raise Unavailable('shared_admission_snapshot_unavailable') from None
