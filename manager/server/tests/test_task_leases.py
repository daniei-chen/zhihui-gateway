"""K28：跨进程 task lease（唯一执行权）+ 发布互斥双向握手的回归测试。

覆盖任务卡要求：
  1. claim 互斥（单进程 + **真实双进程 subprocess** 并发）；
  2. fence 隔离：被接管后旧 owner 的 renew/release/状态写入全部拒绝；
  3. 过期接管与 ABA 防护（fence 只增不减）；
  4. 进程崩溃恢复（kill 掉持租约的进程 → TTL 过期 → 另一进程接管）；
  5. 发布互斥双向握手：publish.state 阻止 claim；活跃租约阻止发布；
     以及「探测通过之后发布才启动」的竞态最终只有一方执行；
  6. dailyplan 集成：抢不到租约不执行任务；执行完带 fence 写 last_run 并释放；
     租约失效时旧 owner 不写状态；
  7. v7 迁移（表存在 / 幂等）在 test_schema_versioning 里另有断言。

真实双进程测试用 subprocess + 独立临时 WB_DB / WB_DATA_DIR；不写仓库、不写
真实数据目录。父进程与子进程都通过 `ZH_ISO_ROOT` 的临时目录传递屏障与计数器
文件，因此隔离运行器的路径约束仍然成立。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import config, db, tasklease
from server.services import dailyplan

MANAGER_DIR = Path(__file__).resolve().parents[2]

#: 子进程脚本：只用标准库 + manager 源码，行为由环境变量选择。
CHILD_SCRIPT = r'''
import json, os, sqlite3, sys, time
from pathlib import Path

MANAGER = os.environ['K28_MANAGER']
DB = Path(os.environ['K28_DB'])
ACTION = os.environ['K28_ACTION']
TASK = os.environ.get('K28_TASK', 'daily')
TTL = int(os.environ.get('K28_TTL', '30'))
SHARED = Path(os.environ['K28_SHARED'])
sys.path.insert(0, MANAGER)

from server import config, db, tasklease

config.DB_PATH = DB
db._conn = None
db.connect()


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def wait_for(path, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def bump(name):
    target = SHARED / name
    with target.open('a', encoding='utf-8') as handle:
        handle.write(str(os.getpid()) + '\n')


def barrier(name, expected=2, timeout=20.0):
    """启动屏障：直到目录里出现 expected 个 <name>-<pid> 才继续。"""
    marker = SHARED / f'{name}-{os.getpid()}'
    marker.write_text('1', encoding='utf-8')
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(list(SHARED.glob(f'{name}-*'))) >= expected:
            return True
        time.sleep(0.02)
    return False


if ACTION == 'claim_once':
    barrier('ready')
    claimed, fence = tasklease.claim(TASK, TTL)
    if claimed:
        bump('executed.log')
    emit({'pid': os.getpid(), 'claimed': claimed, 'fence': fence,
          'owner': tasklease.identity()})

elif ACTION == 'claim_then_hold':
    claimed, fence = tasklease.claim(TASK, TTL)
    emit({'pid': os.getpid(), 'claimed': claimed, 'fence': fence,
          'owner': tasklease.identity()})
    while True:
        time.sleep(0.5)

elif ACTION == 'probe_then_claim':
    probe = SHARED / 'probe-ok'
    conn = sqlite3.connect(str(DB))
    reason = tasklease.publish_block_reason(conn)
    conn.close()
    if reason is None:
        probe.write_text('1', encoding='utf-8')
    wait_for(SHARED / 'publish-done', timeout=20.0)
    claimed, fence = tasklease.claim(
        TASK, TTL, guard=lambda c: tasklease.publish_block_reason(c),
        detail='scheduler after probe')
    if claimed:
        bump('executed.log')
    emit({'pid': os.getpid(), 'probe_reason': reason, 'claimed': claimed,
          'fence': fence, 'owner': tasklease.identity()})

elif ACTION == 'publish_after_probe':
    wait_for(SHARED / 'probe-ok', timeout=20.0)
    conn = sqlite3.connect(str(DB), timeout=30, isolation_level=None)
    conn.execute('BEGIN IMMEDIATE')
    try:
        state = tasklease.begin_publish(conn, 'release:test-host:child', 60)
        conn.execute('COMMIT')
        blocked = None
    except tasklease.PublishBlocked as error:
        conn.execute('ROLLBACK')
        state = None
        blocked = str(error)
    conn.close()
    (SHARED / 'publish-done').write_text('1', encoding='utf-8')
    if state is not None:
        bump('published.log')
        time.sleep(3.0)          # 保持发布中的状态，给调度方一个观察窗口
        conn = sqlite3.connect(str(DB), timeout=30, isolation_level=None)
        conn.execute('BEGIN IMMEDIATE')
        tasklease.end_publish(conn, 'release:test-host:child')
        conn.execute('COMMIT')
        conn.close()
    emit({'pid': os.getpid(), 'published': state is not None,
          'blocked': blocked, 'state': state})

elif ACTION == 'publish_while_lease':
    conn = sqlite3.connect(str(DB), timeout=30, isolation_level=None)
    wait_for(SHARED / 'lease-held', timeout=20.0)
    conn.execute('BEGIN IMMEDIATE')
    try:
        tasklease.begin_publish(conn, 'release:test-host:child', 60)
        conn.execute('COMMIT')
        emit({'published': True, 'blocked': None})
    except tasklease.PublishBlocked as error:
        conn.execute('ROLLBACK')
        emit({'published': False, 'blocked': str(error)})
    finally:
        conn.close()

else:
    raise SystemExit(f'unknown action: {ACTION}')
'''


class _LeaseCase(unittest.TestCase):
    """独立临时 SQLite + 真实 db.connect()（含 v7 迁移）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_db = config.DB_PATH
        self.original_data = getattr(config, 'DATA_DIR', None)
        tasklease.reset_identity()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = self.root / 'task-lease.db'
        db._conn = None
        db.connect()
        self.addCleanup(self._cleanup)
        self.shared = self.root / 'shared'
        self.shared.mkdir()

    def _cleanup(self) -> None:
        tasklease.reset_identity()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = self.original_db
        if self.original_data is not None:
            config.DATA_DIR = self.original_data
        self.tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def child(self, action: str, *, task: str = 'daily', ttl: int = 30,
              env_extra: dict | None = None) -> dict:
        env = dict(os.environ)
        env.update({
            'K28_MANAGER': str(MANAGER_DIR),
            'K28_DB': str(config.DB_PATH),
            'K28_ACTION': action,
            'K28_TASK': task,
            'K28_TTL': str(ttl),
            'K28_SHARED': str(self.shared),
            'PYTHONIOENCODING': 'utf-8',
        })
        env.update(env_extra or {})
        return env

    def run_child(self, action: str, *, timeout: float = 90.0, **kwargs) -> subprocess.CompletedProcess:
        script = self.root / 'k28_child.py'
        if not script.exists():
            script.write_text(CHILD_SCRIPT, encoding='utf-8')
        return subprocess.run([sys.executable, '-B', str(script)], cwd=str(self.root),
                              env=self.child(action, **kwargs), capture_output=True,
                              text=True, encoding='utf-8', timeout=timeout)

    def spawn_child(self, action: str, **kwargs) -> subprocess.Popen:
        script = self.root / 'k28_child.py'
        if not script.exists():
            script.write_text(CHILD_SCRIPT, encoding='utf-8')
        return subprocess.Popen([sys.executable, '-B', str(script)], cwd=str(self.root),
                                env=self.child(action, **kwargs),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8')

    @staticmethod
    def child_json(process: subprocess.Popen | subprocess.CompletedProcess) -> dict:
        out = process.stdout or ''
        lines = [line for line in out.splitlines() if line.strip().startswith('{')]
        if not lines:
            raise AssertionError(f'子进程没有输出 JSON：stdout={out!r} stderr={(process.stderr or "")[-2000:]!r}')
        return json.loads(lines[-1])

    def lease_row(self, task: str) -> dict | None:
        row = db.query_one('SELECT * FROM task_leases WHERE task=?', (task,))
        return dict(row) if row is not None else None


class TaskLeaseUnitTest(_LeaseCase):
    """claim / fence / 过期接管 / ABA（单进程，真实 SQLite）。"""

    def test_first_claim_wins_and_second_owner_is_refused(self) -> None:
        owner_a = 'host-a:' + 'a' * 32
        owner_b = 'host-b:' + 'b' * 32
        claimed, fence = tasklease.claim('daily', 60, owner=owner_a)
        self.assertTrue(claimed)
        self.assertEqual(fence, 1)
        blocked, same_fence = tasklease.claim('daily', 60, owner=owner_b)
        self.assertFalse(blocked, '第二个 owner 不得抢到未过期的租约')
        self.assertEqual(same_fence, 1)
        row = self.lease_row('daily')
        self.assertEqual((row['owner'], row['fence']), (owner_a, 1))

    def test_self_reclaim_bumps_fence(self) -> None:
        owner = 'host-a:' + 'a' * 32
        _, fence = tasklease.claim('daily', 60, owner=owner)
        again, next_fence = tasklease.claim('daily', 60, owner=owner)
        self.assertTrue(again)
        self.assertEqual(next_fence, fence + 1)
        self.assertFalse(tasklease.renew('daily', fence, 60, owner=owner),
                         '旧 fence 在自持重入后必须失效')

    def test_expired_lease_is_taken_over_with_higher_fence(self) -> None:
        owner_a = 'host-a:' + 'a' * 32
        owner_b = 'host-b:' + 'b' * 32
        claimed, fence = tasklease.claim('daily', 60, owner=owner_a)
        self.assertTrue(claimed)
        db.execute('UPDATE task_leases SET lease_until=? WHERE task=?',
                   (int(time.time()) - 5, 'daily'))
        taken, next_fence = tasklease.claim('daily', 60, owner=owner_b)
        self.assertTrue(taken, '过期租约必须可被接管')
        self.assertEqual(next_fence, fence + 1, 'fence 必须递增（单调，防 ABA）')
        row = self.lease_row('daily')
        self.assertEqual(row['owner'], owner_b)

    def test_fence_isolation_rejects_stale_owner(self) -> None:
        owner_a = 'host-a:' + 'a' * 32
        owner_b = 'host-b:' + 'b' * 32
        _, fence_a = tasklease.claim('daily', 60, owner=owner_a)
        db.execute('UPDATE task_leases SET lease_until=? WHERE task=?',
                   (int(time.time()) - 5, 'daily'))
        _, fence_b = tasklease.claim('daily', 60, owner=owner_b)
        self.assertGreater(fence_b, fence_a)
        # 旧 owner 的三类写入必须全部被拒
        self.assertFalse(tasklease.renew('daily', fence_a, 60, owner=owner_a))
        self.assertFalse(tasklease.release('daily', fence_a, owner=owner_a))
        self.assertFalse(tasklease.still_holding('daily', fence_a, owner=owner_a))
        self.assertTrue(tasklease.still_holding('daily', fence_b, owner=owner_b))
        # 新 owner 仍然有效
        self.assertTrue(tasklease.renew('daily', fence_b, 60, owner=owner_b))
        self.assertTrue(tasklease.release('daily', fence_b, owner=owner_b))

    def test_release_then_reclaim_keeps_fence_monotonic(self) -> None:
        owner_a = 'host-a:' + 'a' * 32
        owner_b = 'host-b:' + 'b' * 32
        _, first = tasklease.claim('daily', 60, owner=owner_a)
        self.assertTrue(tasklease.release('daily', first, owner=owner_a))
        row = self.lease_row('daily')
        self.assertEqual((row['owner'], row['lease_until']), ('', 0),
                         'release 只清 owner/lease_until，不删行')
        released, second = tasklease.claim('daily', 60, owner=owner_b)
        self.assertTrue(released, '释放后应能再次 claim')
        self.assertEqual(second, first + 1, 'fence 不能因为 release 回到 1（ABA）')
        # 复活的旧 owner 用旧 fence 做任何事都必须失败
        self.assertFalse(tasklease.release('daily', first, owner=owner_a))
        self.assertIsNotNone(self.lease_row('daily'))

    def test_describe_and_owner_alive(self) -> None:
        me = tasklease.identity()
        claimed, fence = tasklease.claim('daily', 60)
        self.assertTrue(claimed)
        info = tasklease.describe('daily')
        self.assertEqual(info['owner'], me)
        self.assertEqual(info['fence'], fence)
        self.assertIs(tasklease.owner_alive(me), True, '本机活着的 owner 应判定存活')
        self.assertIsNone(tasklease.owner_alive('other-host:' + 'c' * 32),
                          '跨机 owner 不能猜生死')
        self.assertIsNone(tasklease.owner_alive('not-a-valid-owner'))
        self.assertIsNone(tasklease.owner_alive(''))

    def test_unknown_task_state_when_no_row(self) -> None:
        self.assertIsNone(tasklease.describe('never-claimed'))
        self.assertTrue(tasklease.claim('fresh', 60)[0])
        self.assertEqual(tasklease.describe('never-claimed'), None)

    def test_publish_state_blocks_claim_and_expiry_unblocks_it(self) -> None:
        with db.transaction() as conn:
            state = tasklease.begin_publish(conn, 'release:host:abc', 60)
        self.assertEqual(state['state'], 'publishing')
        blocked, _ = tasklease.claim('daily', 60)
        self.assertFalse(blocked, 'publish.state 未过期时不得 claim')
        # 过期后按「没有发布」处理（发布方崩溃的残留不能永久卡住调度）
        db.execute("UPDATE settings SET value=? WHERE key=?",
                   (json.dumps({'state': 'publishing', 'owner': 'release:x',
                                'started_at': 1, 'lease_until': int(time.time()) - 1}),
                    tasklease.PUBLISH_KEY))
        allowed, fence = tasklease.claim('daily', 60)
        self.assertTrue(allowed, f'过期 publish.state 不应阻止 claim（fence={fence}）')

    def test_begin_publish_refuses_active_lease_then_succeeds(self) -> None:
        claimed, fence = tasklease.claim('daily', 60)
        self.assertTrue(claimed)
        with self.assertRaises(tasklease.PublishBlocked) as caught:
            with db.transaction() as conn:
                tasklease.begin_publish(conn, 'release:host:abc', 60)
        self.assertIn('daily', str(caught.exception))
        self.assertIn('daily', [b['task'] for b in caught.exception.blockers])
        self.assertIsNone(tasklease.publish_state(db.connect()),
                          '被拒绝时不能留下 publish.state')
        self.assertTrue(tasklease.release('daily', fence))
        with db.transaction() as conn:
            state = tasklease.begin_publish(conn, 'release:host:abc', 60)
        self.assertEqual(state['state'], 'publishing')
        with db.transaction() as conn:
            self.assertTrue(tasklease.end_publish(conn, 'release:host:abc'))
        self.assertIsNone(tasklease.publish_state(db.connect()))

    def test_end_publish_does_not_clear_another_owner(self) -> None:
        with db.transaction() as conn:
            tasklease.begin_publish(conn, 'release:host:first', 60)
        with db.transaction() as conn:
            self.assertFalse(tasklease.end_publish(conn, 'release:host:second'))
        self.assertIsNotNone(tasklease.publish_state(db.connect()))
        with db.transaction() as conn:
            self.assertTrue(tasklease.end_publish(conn, 'release:host:first'))

    def test_publish_block_reason_reports_release_probe(self) -> None:
        conn = db.connect()
        self.assertIsNone(tasklease.publish_block_reason(conn, release_probe=lambda: False))
        self.assertIn('flock',
                      tasklease.publish_block_reason(conn, release_probe=lambda: True) or '')


class TaskLeaseMultiprocessTest(_LeaseCase):
    """真实双进程：claim 互斥、崩溃恢复。"""

    def test_two_processes_race_for_one_lease(self) -> None:
        first = self.spawn_child('claim_once', task='daily', ttl=60)
        second = self.spawn_child('claim_once', task='daily', ttl=60)
        try:
            out_first, err_first = first.communicate(timeout=90)
            out_second, err_second = second.communicate(timeout=90)
        finally:
            for process in (first, second):
                if process.poll() is None:
                    process.kill()
        results = []
        for out, err, code in ((out_first, err_first, first.returncode),
                               (out_second, err_second, second.returncode)):
            self.assertEqual(code, 0, f'子进程失败：{err[-2000:]}')
            lines = [line for line in out.splitlines() if line.strip().startswith('{')]
            self.assertTrue(lines, out)
            results.append(json.loads(lines[-1]))
        winners = [item for item in results if item['claimed']]
        self.assertEqual(len(winners), 1,
                         f'同一 task 只能有一个进程拿到租约：{results}')
        self.assertEqual(winners[0]['fence'], 1)
        executed = (self.shared / 'executed.log').read_text(encoding='utf-8').split()
        self.assertEqual(len(executed), 1, '外部动作必须只发生一次')
        row = self.lease_row('daily')
        self.assertEqual(row['owner'], winners[0]['owner'])
        # 子进程已退出：锁文件残留但锁已释放 → 同机可判定为「已死」（False）。
        self.assertIs(tasklease.owner_alive(row['owner']), False,
                      f"已退出子进程的 owner 应判定为 False：{row['owner']}")

    def test_crashed_holder_lease_expires_and_is_taken_over(self) -> None:
        process = self.spawn_child('claim_then_hold', task='daily', ttl=3)
        try:
            first_payload = _read_first_json(process, timeout=60)
            self.assertIsNotNone(first_payload, '持锁子进程没有就绪输出')
            process.kill()
            process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
        self.assertTrue(first_payload['claimed'])
        self.assertEqual(first_payload['fence'], 1)
        self.assertIs(tasklease.owner_alive(first_payload['owner']), False,
                      '被 kill 的进程必须可判定为已死（本机，锁文件已释放）')
        # 未过期时不能接管
        blocked, _ = tasklease.claim('daily', 30)
        self.assertFalse(blocked, 'TTL 未到期前不得接管')
        time.sleep(3.2)
        taken, fence = tasklease.claim('daily', 30)
        self.assertTrue(taken, '崩溃进程的租约过期后必须可被接管')
        self.assertEqual(fence, 2)
        row = self.lease_row('daily')
        self.assertEqual(row['owner'], tasklease.identity())


def _read_first_json(process: subprocess.Popen, timeout: float) -> dict | None:
    """读子进程的第一行 JSON（带超时，不消费后续行）。"""
    import threading
    holder: list[str] = []

    def read() -> None:
        holder.append(process.stdout.readline() if process.stdout else '')

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(timeout)
    if not holder or not holder[0].strip():
        return None
    return json.loads(holder[0])


class PublishHandshakeTest(_LeaseCase):
    """发布互斥：双向握手与「探测通过后发布启动」的竞态。"""

    def test_publish_while_task_lease_blocks_and_reports(self) -> None:
        claimed, fence = tasklease.claim('daily', 120)
        self.assertTrue(claimed)
        (self.shared / 'lease-held').write_text('1', encoding='utf-8')
        process = self.run_child('publish_while_lease', timeout=90)
        self.assertEqual(process.returncode, 0, process.stderr[-2000:])
        payload = self.child_json(process)
        self.assertFalse(payload['published'], '有活跃任务租约时不得开始发布')
        self.assertIn('daily', payload['blocked'] or '')
        self.assertIsNone(tasklease.publish_state(db.connect()))
        self.assertTrue(tasklease.release('daily', fence))

    def test_scheduler_wins_when_it_claims_before_publish(self) -> None:
        claimed, fence = tasklease.claim('race', 120)
        self.assertTrue(claimed)
        (self.shared / 'lease-held').write_text('1', encoding='utf-8')
        process = self.run_child('publish_while_lease', timeout=90)
        payload = self.child_json(process)
        self.assertFalse(payload['published'])
        self.assertEqual(self.lease_row('race')['owner'], tasklease.identity())

    def test_publish_started_after_probe_only_one_side_runs(self) -> None:
        """核心竞态：调度方探测通过 → 发布方启动 → 最终只有发布方执行。

        调度方的探测（publish_block_reason）在发布状态写入之前返回 None；随后
        发布方在同一个 SQLite 写事务里写 publish.state。调度方真正 claim 时会在
        **事务内重新探测**并看到发布状态 → 放弃。于是外部动作只有发布方一侧。
        """
        scheduler = self.spawn_child('probe_then_claim', task='race', ttl=120)
        publisher = self.spawn_child('publish_after_probe', task='race', ttl=120)
        try:
            sched_out, sched_err = scheduler.communicate(timeout=120)
            pub_out, pub_err = publisher.communicate(timeout=120)
        finally:
            for process in (scheduler, publisher):
                if process.poll() is None:
                    process.kill()

        def payload(process, out, err):
            self.assertEqual(process.returncode, 0, err[-2000:])
            lines = [line for line in out.splitlines() if line.strip().startswith('{')]
            self.assertTrue(lines, out)
            return json.loads(lines[-1])

        sched = payload(scheduler, sched_out, sched_err)
        pub = payload(publisher, pub_out, pub_err)
        self.assertIsNone(sched['probe_reason'], '探测阶段应通过（此时还没有发布状态）')
        self.assertFalse(sched['claimed'],
                         '发布启动后调度方必须放弃 claim（事务内重新探测生效）')
        self.assertTrue(pub['published'], '发布方应成功写 publish.state')
        executed = (self.shared / 'executed.log')
        self.assertFalse(executed.exists(), '调度方不得执行任何任务')
        self.assertTrue((self.shared / 'published.log').exists(), '只有发布方执行')
        # 发布结束后状态清理，调度方下一轮可以正常 claim
        self.assertIsNone(tasklease.publish_state(db.connect()))
        allowed, fence = tasklease.claim('race', 120)
        self.assertTrue(allowed, f'发布结束后调度方必须能 claim（fence={fence}）')


class DailyPlanLeaseTest(_LeaseCase):
    """dailyplan 的接入：抢不到不执行、执行后带 fence 写戳并释放、失效不写。"""

    def setUp(self) -> None:
        super().setUp()
        # 隔离宿主的发布锁探测（跨平台差异，别掉进"Windows 绿 Linux 红"）：
        # `dailyplan.release_in_progress()` 会对 `/run/zhihui-release.lock`
        # 做带 O_RDWR 的 os.open。隔离运行器的路径守卫把 `/run` 视为受保护
        # 路径 → PermissionError → 该函数的 `except OSError: return True`
        # 保守分支认定"正在发布" → 所有 claim 被 `_publish_block` 挡住。
        # Windows 没有 fcntl，函数恒返回 False，所以这个环境依赖在那边被
        # 完全掩盖。这里只把探测路径改到本测试的临时目录（文件不存在 =
        # 没有发布），断言与生产行为都不变。
        patcher = mock.patch.object(dailyplan, '_RELEASE_LOCK',
                                    self.root / 'release.lock')
        patcher.start()
        self.addCleanup(patcher.stop)

    def _plan(self) -> dict:
        plan = dailyplan.get_plan()
        for task in dailyplan.TASKS:
            plan[task]['enabled'] = True
            plan[task]['hour'] = int(time.localtime().tm_hour)
            plan[task]['last_run'] = ''
        return plan

    def test_claim_refused_skips_task_execution(self) -> None:
        plan = self._plan()
        other = 'other-host:' + 'd' * 32
        claimed, _ = tasklease.claim('claim', 120, owner=other)
        self.assertTrue(claimed)
        calls: list[str] = []

        async def fake_run(task):
            calls.append(task)
            return True, 'should not run'

        with mock.patch.object(dailyplan, 'run_task', fake_run):
            ran = asyncio.run(dailyplan._run_claimed('claim', plan))
        self.assertFalse(ran)
        self.assertEqual(calls, [], '租约被别的 owner 持有时不得执行任务')
        self.assertEqual(dailyplan.get_plan()['claim']['last_run'], '',
                         '跳过的任务不能写 last_run')

    def test_success_writes_fenced_last_run_and_releases(self) -> None:
        plan = self._plan()
        calls: list[str] = []

        async def fake_run(task):
            calls.append(task)
            return True, 'ok'

        with mock.patch.object(dailyplan, 'run_task', fake_run):
            ran = asyncio.run(dailyplan._run_claimed('claim', plan))
        self.assertTrue(ran)
        self.assertEqual(calls, ['claim'])
        row = self.lease_row('claim')
        self.assertEqual(row['owner'], '', '任务结束后必须释放租约')
        self.assertEqual(dailyplan.get_plan()['claim']['last_run'],
                         dailyplan._stamp(int(time.time())),
                         '成功执行后必须带 fence 写 last_run')

    def test_lease_taken_over_during_run_does_not_write_last_run(self) -> None:
        """执行期间被接管：旧 owner 的 last_run 写入必须失败（不覆盖新 owner）。"""
        plan = self._plan()
        thrown: list[bool] = []

        async def fake_run(task):
            # 模拟执行期间租约过期并被另一个进程接管
            db.execute('UPDATE task_leases SET lease_until=? WHERE task=?',
                       (int(time.time()) - 5, task))
            taken, _ = tasklease.claim(task, 120, owner='other-host:' + 'e' * 32)
            thrown.append(taken)
            return True, 'ok'

        with mock.patch.object(dailyplan, 'run_task', fake_run):
            ran = asyncio.run(dailyplan._run_claimed('claim', plan))
        self.assertTrue(ran)
        self.assertEqual(thrown, [True])
        self.assertEqual(dailyplan.get_plan()['claim']['last_run'], '',
                         'fence 失效后旧 owner 不得写 last_run')
        self.assertEqual(self.lease_row('claim')['owner'], 'other-host:' + 'e' * 32,
                         '旧 owner 的 release 不能删掉新 owner 的租约')

    def test_exception_still_releases_lease_and_stamps(self) -> None:
        plan = self._plan()

        async def boom(task):
            raise RuntimeError('synthetic failure')

        with mock.patch.object(dailyplan, 'run_task', boom):
            ran = asyncio.run(dailyplan._run_claimed('claim', plan))
        self.assertTrue(ran)
        self.assertEqual(self.lease_row('claim')['owner'], '')
        self.assertEqual(dailyplan.get_plan()['claim']['last_run'],
                         dailyplan._stamp(int(time.time())),
                         '失败也记本小时已跑（避免每分钟重试刷屏）')

    def test_publish_state_blocks_scheduler_claim(self) -> None:
        with db.transaction() as conn:
            tasklease.begin_publish(conn, 'release:test-host:unit', 120)
        plan = self._plan()
        calls: list[str] = []

        async def fake_run(task):
            calls.append(task)
            return True, 'should not run'

        with mock.patch.object(dailyplan, 'run_task', fake_run):
            ran = asyncio.run(dailyplan._run_claimed('claim', plan))
        self.assertFalse(ran)
        self.assertEqual(calls, [], '发布期间不得执行每日任务')
        self.assertIsNone(self.lease_row('claim'))
        with db.transaction() as conn:
            tasklease.end_publish(conn, 'release:test-host:unit')

    def test_mark_ran_fenced_rejects_when_not_holding(self) -> None:
        claimed, fence = tasklease.claim('claim', 120)
        self.assertTrue(claimed)
        self.assertTrue(dailyplan.mark_ran_fenced('claim', fence))
        self.assertFalse(dailyplan.mark_ran_fenced('claim', fence + 99),
                         'fence 不匹配时不得写 last_run')
        db.execute('UPDATE task_leases SET lease_until=? WHERE task=?',
                   (int(time.time()) - 5, 'claim'))
        self.assertFalse(dailyplan.mark_ran_fenced('claim', fence),
                         '租约过期后不得写 last_run')


if __name__ == '__main__':
    unittest.main()
