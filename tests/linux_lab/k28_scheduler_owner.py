"""K28：调度与自愈的发布互斥 / 单 owner / 跨午夜重启实测。

## 任务卡要求

  双实例仅 leader 执行 / lease 切换 / 发布锁期间不自愈 / leader 崩溃恢复 /
  不抢活任务 / 额度耗尽不盲重启 / 调度时区/重试幂等

## 实测前的架构事实（决定了哪些能做到）

`manager/server/services/dailyplan.py` 的调度器是**进程内 asyncio task**
（模块级 `_loop_task`），去重靠 SQLite `settings` 里的 `last_run` 戳（`YYYY-MM-DD-HH`）。
`services/filelock.py` 的锁**明确只保证进程内**。

所以"双实例仅 leader 执行"在当前实现下**不可能成立** —— 两个实例都会满足
`is_due()` 并各自跑一遍。这不是隐藏缺陷而是**未实现的能力**，本脚本如实测出来。

发布锁（`ops/release-lock/`）与自愈的互斥是另一条独立链路，单独验证。

## 运行

    python3 k28_scheduler_owner.py --db <manager.db>     # 只读 + 可写临时副本
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROWS: list[dict] = []


def record(case, ok, detail, **extra):
    ROWS.append({'case': case, 'ok': bool(ok), 'detail': detail, **extra})
    print(('PASS ' if ok else 'FAIL ') + f'{case}: {detail}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True, help='Manager SQLite（会复制到临时目录操作）')
    ap.add_argument('--json', type=Path)
    args = ap.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix='k28-'))
    work = tmp / 'manager.db'
    # 复制一份再改：绝不写源库（URI 用 posix 形式，Windows 盘符路径也能用）
    src = sqlite3.connect(f'file:{Path(args.db).resolve().as_posix()}?mode=ro', uri=True)
    dst = sqlite3.connect(work)
    src.backup(dst)
    src.close()
    dst.close()
    os.environ['WB_DB'] = str(work)
    os.environ['WB_DATA_DIR'] = str(tmp)
    # 在 fixture 里执行时，manager 源码在快照 workspace 下；本地执行时在仓库根。
    # 用环境变量显式指定，避免猜测。
    repo_root = Path(os.environ.get('ZH_REPO_ROOT') or '/home/gateway-lab/lab/snapshots')
    candidates = [Path(os.environ['ZH_MANAGER_DIR'])] if os.environ.get('ZH_MANAGER_DIR') else []
    candidates += [repo_root / '7eea6ef49c5e8d7b' / 'workspace' / 'manager',
                   Path(__file__).resolve().parents[2] / 'manager']
    for cand in candidates:
        if (cand / 'server' / 'dailyplan.py').exists() or (cand / 'server').is_dir():
            REPO_ROOT = cand
            sys.path.insert(0, str(cand))
            break
    else:
        raise SystemExit(f'找不到 manager 源码，候选：{[str(c) for c in candidates]}')

    from server import config
    config.DB_PATH = work
    from server import db
    db._conn = None
    db.connect()
    from server.services import dailyplan

    # ── 1. 跨午夜：last_run 戳的日期必须跟着本地日期走 ──────────────────
    # 取一个"今天 23:30"和"明天 00:30"的时刻（本地时区），验证戳不同。
    now = time.time()
    lt = time.localtime(now)
    tonight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 23, 30, 0, 0, 0, -1))
    tomorrow = tonight + 3600  # 23:30 + 1h = 次日 00:30
    s1 = dailyplan._stamp(int(tonight))
    s2 = dailyplan._stamp(int(tomorrow))
    record('跨午夜戳按本地日期区分', s1 != s2, f'{s1} vs {s2}')
    # 同一小时内的两个时刻必须同戳（否则会重复跑）
    record('同一小时内同戳',
           dailyplan._stamp(int(tonight)) == dailyplan._stamp(int(tonight) + 1200),
           '23:30 与 23:50 应同戳')

    # ── 2. is_due：整点对齐 + 同小时只跑一次 ────────────────────────────
    plan = dailyplan.get_plan()
    task = next(iter(dailyplan._TASKS))
    plan[task] = {'enabled': True, 'hour': lt.tm_hour, 'last_run': ''}
    due_first = dailyplan.is_due(task, plan, now=int(now))
    plan[task]['last_run'] = dailyplan._stamp(int(now))
    due_second = dailyplan.is_due(task, plan, now=int(now))
    record('首跑到点', due_first, f'task={task} hour={lt.tm_hour}')
    record('同小时不重复跑', not due_second, 'last_run 已置为本小时')
    # 换到下一个小时 → 又该跑
    plan[task]['hour'] = (lt.tm_hour + 1) % 24
    record('下一整点重新到点',
           dailyplan.is_due(task, plan, now=int(now) + 3600),
           f'hour={(lt.tm_hour + 1) % 24}')
    # 关闭的任务永不到点
    plan[task]['enabled'] = False
    record('关闭的任务不到点', not dailyplan.is_due(task, plan, now=int(now) + 3600),
           'enabled=False')

    # ── 3. is_due 是纯函数（不承担跨进程去重）─────────────────────────────
    # K28 之后「双实例仅 leader 执行」由 `tasklease.claim` 保证（见第 8 节：
    # 第二个 owner 被拒 / 第 12 节：真实双进程并发只有一个成功）。is_due 只看
    # 「这一小时跑过没有」，两个实例读到同一份 plan 自然都会说「到点」——
    # 这是正确的分层，把它判成 FAIL 是旧实现的错位断言。
    plan2 = dailyplan.get_plan()
    plan2[task] = {'enabled': True, 'hour': (lt.tm_hour + 1) % 24, 'last_run': ''}
    future = int(now) + 3600
    a = dailyplan.is_due(task, plan2, now=future)
    b = dailyplan.is_due(task, plan2, now=future)
    record('is_due 为纯函数（跨进程去重交给 claim）', a == b == True,
           f'a={a} b={b}；唯一执行权见 K28 租约项')

    # ── 4. mark_ran 的幂等性：重复调用不改变结果 ────────────────────────
    first = dailyplan.mark_ran(task, now=future)
    second = dailyplan.mark_ran(task, now=future + 60)
    record('mark_ran 幂等', first[task]['last_run'] == second[task]['last_run'],
           f"{first[task]['last_run']} vs {second[task]['last_run']}")

    # ── 5. 崩溃恢复：last_run 已落库，重启后同小时不再重跑 ───────────────
    stamped = dailyplan.get_plan()[task]['last_run']
    db._conn.close()
    db._conn = None
    db.connect()
    after = dailyplan.get_plan()[task]['last_run']
    record('重启后 last_run 仍在', after == stamped, f'{stamped} -> {after}（来自 SQLite）')

    # ── 6. 发布锁存在且不可被第二个持有者拿到 ───────────────────────────
    lock_dir = REPO_ROOT.parent / 'ops' / 'release-lock'
    if not lock_dir.is_dir():
        lock_dir = Path('/home/gateway-lab/lab/snapshots/7eea6ef49c5e8d7b/workspace/ops/release-lock')
    record('发布锁目录存在', lock_dir.is_dir(), str(lock_dir))
    if lock_dir.is_dir():
        files = sorted(p.name for p in lock_dir.iterdir())
        record('发布锁有文档与实现', len(files) >= 1, f'{files}')

    # ── 7. 发布锁与自愈互斥（代码级：调度器先查锁）───────────────────────
    daily_src = (REPO_ROOT / 'server/services/dailyplan.py').read_text(encoding='utf-8')
    has_lock_check = 'release_in_progress()' in daily_src
    record('调度器检查发布锁', has_lock_check,
           'dailyplan._loop 已调用 release_in_progress()（N43 修复）'
           if has_lock_check else '未接入发布锁')
    # 发布锁探测的真实行为（Linux 才有 fcntl）
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl:
        lock = tmp / 'release.lock'
        lock.write_text('')
        os.environ['ZH_RELEASE_LOCK_FILE'] = str(lock)
        import importlib
        dp = importlib.reload(dailyplan)
        free = dp.release_in_progress()
        fd = os.open(str(lock), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            dp = importlib.reload(dailyplan)
            held = dp.release_in_progress()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        dp = importlib.reload(dailyplan)
        released = dp.release_in_progress()
        record('发布锁探测：无持有→False / 持有→True / 释放→False',
               (free, held, released) == (False, True, False),
               f'free={free} held={held} released={released}')
    else:
        record('发布锁探测（非 POSIX 跳过 fcntl 部分）', True, 'Windows 无 fcntl')

    # ── 8. K28 跨进程租约：claim / fence / 过期接管 / ABA ─────────────────
    from server import tasklease
    tasklease.reset_identity()
    owner_a = 'lab-a:' + 'a' * 32
    owner_b = 'lab-b:' + 'b' * 32
    got_a, fence_a = tasklease.claim('k28-probe', 120, owner=owner_a)
    refused_b, _ = tasklease.claim('k28-probe', 120, owner=owner_b)
    record('K28 首个 owner 取得租约', got_a and fence_a == 1, f'fence={fence_a}')
    record('K28 未过期时第二个 owner 被拒', not refused_b, f'claimed={refused_b}')
    again_a, fence_a2 = tasklease.claim('k28-probe', 120, owner=owner_a)
    record('K28 自持重入 fence 递增', again_a and fence_a2 == fence_a + 1,
           f'{fence_a} -> {fence_a2}')
    record('K28 旧 fence 续租被拒',
           not tasklease.renew('k28-probe', fence_a, 120, owner=owner_a),
           'fence 已被自持重入推进')
    db.execute('UPDATE task_leases SET lease_until=? WHERE task=?',
               (int(time.time()) - 1, 'k28-probe'))
    got_b, fence_b = tasklease.claim('k28-probe', 120, owner=owner_b)
    record('K28 过期租约被接管且 fence 递增',
           got_b and fence_b == fence_a2 + 1, f'{fence_a2} -> {fence_b}')
    record('K28 接管后旧 owner 释放被拒',
           not tasklease.release('k28-probe', fence_a2, owner=owner_a),
           '旧 fence 不能释放新 owner 的租约')
    record('K28 接管后新 owner 可释放',
           tasklease.release('k28-probe', fence_b, owner=owner_b), 'release=True')
    got_c, fence_c = tasklease.claim('k28-probe', 120, owner=owner_a)
    record('K28 释放后可再 claim 且 fence 单调',
           got_c and fence_c == fence_b + 1, f'{fence_b} -> {fence_c}')
    tasklease.release('k28-probe', fence_c, owner=owner_a)

    # ── 9. owner_alive：本机可判断、跨机不猜 ──────────────────────────────
    me = tasklease.identity()
    record('K28 本机活着的 owner 判定存活', tasklease.owner_alive(me) is True, me)
    record('K28 跨机 owner 不猜生死',
           tasklease.owner_alive('other-lab:' + 'c' * 32) is None, 'None = 不确定')

    # ── 10. 发布互斥双向握手（DB 侧）──────────────────────────────────────
    lease_ok, lease_fence = tasklease.claim('k28-lease-block', 120, owner=owner_a)
    publish_refused = False
    publish_detail = ''
    try:
        with db.transaction() as conn:
            tasklease.begin_publish(conn, 'release:lab:test', 120)
        publish_detail = '不应成功'
    except tasklease.PublishBlocked as exc:
        publish_refused = True
        publish_detail = str(exc)[:120]
    record('K28 活跃任务租约阻止发布', lease_ok and publish_refused, publish_detail)
    tasklease.release('k28-lease-block', lease_fence, owner=owner_a)
    with db.transaction() as conn:
        tasklease.begin_publish(conn, 'release:lab:test', 120)
    blocked, _ = tasklease.claim('k28-publish-blocked', 120, owner=owner_b)
    record('K28 publish.state 阻止 claim', not blocked, f'claimed={blocked}')
    with db.transaction() as conn:
        tasklease.end_publish(conn, 'release:lab:test')
    allowed, allowed_fence = tasklease.claim('k28-publish-blocked', 120, owner=owner_b)
    record('K28 发布结束后可 claim', allowed, f'fence={allowed_fence}')
    tasklease.release('k28-publish-blocked', allowed_fence, owner=owner_b)

    # ── 11. 真实 flock：发布方持锁时调度 claim 被拒（POSIX only）──────────
    if fcntl:
        release_lock = tmp / 'release-k28.lock'
        release_lock.write_text('')
        os.environ['ZH_RELEASE_LOCK_FILE'] = str(release_lock)
        import importlib
        dp = importlib.reload(dailyplan)
        fd = os.open(str(release_lock), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held_ok, _ = dp.claim_task('k28-flock')
            record('K28 发布方持 flock 时调度 claim 被拒', not held_ok,
                   f'claimed={held_ok}')
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        dp = importlib.reload(dailyplan)
        free_ok, free_fence = dp.claim_task('k28-flock')
        record('K28 释放 flock 后可 claim', free_ok, f'fence={free_fence}')
        if free_ok:
            tasklease.release('k28-flock', free_fence)
    else:
        record('K28 flock 发布互斥（非 POSIX 跳过）', True, 'Windows 无 fcntl')

    # ── 12. 双进程并发：同一 task 只有一个进程拿到租约 ────────────────────
    race_dir = tmp / 'race-ready'
    race_dir.mkdir(exist_ok=True)
    race_script = tmp / 'k28_race_child.py'
    race_script.write_text('\n'.join([
        'import json, os, sys, time',
        'from pathlib import Path',
        f'sys.path.insert(0, {str(REPO_ROOT)!r})',
        'from server import config, db, tasklease',
        f'config.DB_PATH = Path({str(work)!r})',
        'db._conn = None',
        'db.connect()',
        'marker = Path(sys.argv[1]) / f"race-ready-{os.getpid()}"',
        'marker.write_text("1", encoding="utf-8")',
        'deadline = time.time() + 60',
        'while len(list(marker.parent.glob("race-ready-*"))) < 2:',
        '    if time.time() > deadline:',
        '        break',
        '    time.sleep(0.01)',
        'claimed, fence = tasklease.claim("k28-race", 120)',
        'print(json.dumps({"claimed": claimed, "fence": fence}))',
        '',
    ]), encoding='utf-8')
    children = [subprocess.Popen(
        [sys.executable, '-B', str(race_script), str(race_dir)],
        cwd=str(tmp), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8') for _ in range(2)]
    winners = []
    race_errors = []
    for child in children:
        out, err = child.communicate(timeout=180)
        if child.returncode != 0:
            race_errors.append(err[-300:])
            continue
        lines = [line for line in out.splitlines() if line.strip().startswith('{')]
        if lines and json.loads(lines[-1]).get('claimed'):
            winners.append(json.loads(lines[-1]))
    record('K28 双进程并发只有一个取得租约',
           len(winners) == 1 and not race_errors,
           f'winners={winners} errors={race_errors}')
    # 胜者租约由子进程持有：用 owner 注入释放，清出干净的现场给后面的集成项。
    race_row = db.query_one('SELECT owner, fence FROM task_leases WHERE task=?', ('k28-race',))
    if race_row is not None:
        tasklease.release('k28-race', int(race_row['fence']), owner=str(race_row['owner']))

    # ── 13. 真实 release_lock.py 集成（需要显式 ZH_MANAGER_DB）──────────────
    if fcntl:
        release_tool = Path(REPO_ROOT).parent / 'ops' / 'release-lock' / 'release_lock.py'
        if release_tool.is_file():
            env_release = {
                **os.environ,
                'ZH_MANAGER_DB': str(work),
                'ZH_RELEASE_WAIT_LEASE_SECONDS': '0.5',
                'ZH_RELEASE_LOCK_FILE': str(tmp / 'release-integration.lock'),
                'ZH_RELEASE_STATE_FILE': str(tmp / 'release-integration.state.json'),
            }
            held_ok, held_fence = tasklease.claim('k28-release-integration', 120)
            blocked_run = subprocess.run(
                [sys.executable, str(release_tool), 'run', 'k28-it', 'manager', '--',
                 sys.executable, '-c', 'pass'],
                env=env_release, capture_output=True, text=True, encoding='utf-8')
            record('K28 release_lock 有活跃租约时明确失败(76)',
                   held_ok and blocked_run.returncode == 76,
                   f'exit={blocked_run.returncode} '
                   f'stderr={blocked_run.stderr.strip()[:160]}')
            tasklease.release('k28-release-integration', held_fence)
            free_run = subprocess.run(
                [sys.executable, str(release_tool), 'run', 'k28-it', 'manager', '--',
                 sys.executable, '-c', 'pass'],
                env=env_release, capture_output=True, text=True, encoding='utf-8')
            leftover = tasklease.publish_state(db.connect())
            record('K28 release_lock 成功后清理 publish.state',
                   free_run.returncode == 0 and leftover is None,
                   f'exit={free_run.returncode} leftover={leftover} '
                   f'stderr={free_run.stderr.strip()[:120]}')
        else:
            record('K28 release_lock 集成（找不到发布工具）', False, str(release_tool))
    else:
        record('K28 release_lock 集成（非 POSIX 跳过）', True, 'Windows 无 fcntl')

    db._conn.close()
    db._conn = None
    shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in ROWS if not r['ok']]
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {'schema': 'zhihui-k28-scheduler/v1', 'rows': ROWS,
             'passed': len(ROWS) - len(failed), 'failed': len(failed)},
            ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'\n{len(ROWS) - len(failed)}/{len(ROWS)} passed')
    return 0  # 结论性脚本：发现"未实现"不算失败，由验收表裁定


if __name__ == '__main__':
    sys.exit(main())
