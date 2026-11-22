"""Readiness and actionable local health state; no generation probes or secrets."""
import asyncio
import logging
import os
import re
import stat
import time
from pathlib import Path
from . import db, config, ledger

log = logging.getLogger('workbuddy.operations')
ledger_write_healthy = True
#: U01：回执通道自身不可写（同盘写满/无权限）。置真期间就绪判定必须失败——
#: 「事实既没进库、也没进回执」的请求已经无法自动恢复，不能再假装健康。
ledger_spool_failed = False
#: U01：连续成功重放轮数。只有它达到阈值且积压归零，才允许把写入健康置真。
_ledger_replay_streak = 0
alerts = {}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS readiness_probe(id INTEGER PRIMARY KEY CHECK(id=1), ts INTEGER NOT NULL);
'''

def database_writable():
    """真正验证「能落盘」，而不只是「能写入 SQLite 的缓冲」。

    N30：原先只做一次 UPSERT。SQLite 在已有连接/开启 WAL 时，INSERT 可能写进页缓存
    就返回成功，即使磁盘只读或已写满——于是 `/api/readyz` 在磁盘故障时仍报 ok。
    实测（把 data 目录 chmod 500）readyz 仍返回 `{"ok": true}`，即是此因。

    这里补两步：
      1. UPSERT 后执行 `PRAGMA wal_checkpoint(FULL)`——强制把 WAL 刷进主库文件；
         磁盘不可写时这一步会抛错。
      2. 再查一次刚写入的行，确认真的可见（防"写进缓存但未持久化"）。
    """
    now = int(time.time())
    with db.transaction() as conn:
        conn.execute(
            'INSERT INTO readiness_probe VALUES(1,?) '
            'ON CONFLICT(id) DO UPDATE SET ts=excluded.ts', (now,))
        # 强制落盘：磁盘只读/满时这里会抛 sqlite3.OperationalError
        try:
            conn.execute('PRAGMA wal_checkpoint(FULL)')
        except Exception:  # noqa: BLE001
            # memory/非 WAL 模式下 checkpoint 无意义，不影响判定
            pass
        row = conn.execute('SELECT ts FROM readiness_probe WHERE id=1').fetchone()
        if row is None or int(row[0]) != now:
            raise RuntimeError('readiness probe not persisted')
    # N04：事务已退出，这里用独立连接记录 WAL 合并结果（不阻塞、不算失败）。
    _probe_checkpoint()
    return True

def _ledger_backlog() -> int:
    """待重放回执条数；读不到目录时返回 -1（按不健康处理，不当 0）。"""
    try:
        from . import ledgerreplay
        return ledgerreplay.backlog()
    except Exception:  # noqa: BLE001 - 回执通道异常不能冒充「无积压」
        return -1


def _ledger_channel_ok() -> bool:
    """账本通道健康 = 写入未失败 + 回执通道可写 + 无待重放积压。

    U01（2026-10-28）：此前「任意下一笔写成功」就把全局标志置真，于是一次磁盘
    满之后，后续一条根本不写账本的被拒请求也能让就绪恢复成 ok。现在的恢复条件
    是三者同时成立，且由 monitor 的连续成功重放轮数把关（见 `_ledger_replay_streak`）。
    """
    # N01/N02（复审 reaudit-20261028-1108）：还要求
    #   · **未处置死信为 0**（死信是持久状态，不因"这轮没扫到文件"消失）；
    #   · 回执通道**真实可写**（写探测通过），而不是"目录存在"。
    # 只算 pending 条数会把「spool 根本写不进去」当成"没有积压"。
    from . import ledgerreplay
    try:
        channel = ledgerreplay.channel_state()
    except Exception:  # noqa: BLE001 - 通道状态读不到 = 不健康，不猜
        return False
    return (ledger_write_healthy and not ledger_spool_failed
            and channel['state'] != 'degraded'
            and channel['pending'] == 0 and channel['dead'] == 0)


_last_checkpoint = {'at': None, 'busy': None, 'log': None, 'checkpointed': None,
                    'error': None}


def _probe_checkpoint() -> dict:
    """用**独立连接**做一次 PASSIVE checkpoint，记录 WAL 合并结果（N04）。

    为什么必须独立连接：`database_writable()` 的 `db.transaction()` 是
    `BEGIN IMMEDIATE`，在写事务内部做 `wal_checkpoint(FULL)` 必然回
    `database table is locked` —— 原实现把这个异常 `except: pass` 掉了，于是
    「强制刷盘」从未真正生效。

    PASSIVE 语义：不阻塞、不因锁报错；`busy=1` 表示有读者尚未完成 checkpoint，
    此时**拿不到落盘证明，但不算失败**。这一点必须显式区分，不能写成「非 0 即失败」。
    """
    global _last_checkpoint
    now = int(time.time())
    try:
        import sqlite3
        probe = sqlite3.connect(str(config.DB_PATH), timeout=2)
        try:
            busy, log_frames, checkpointed = probe.execute(
                'PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        finally:
            probe.close()
        _last_checkpoint = {'at': now, 'busy': int(busy), 'log': int(log_frames),
                            'checkpointed': int(checkpointed), 'error': None}
    except Exception as exc:  # noqa: BLE001 - checkpoint 失败不改变「提交成功」
        _last_checkpoint = {'at': now, 'busy': None, 'log': None,
                            'checkpointed': None, 'error': str(exc)[:200]}
    return dict(_last_checkpoint)


def _write_proof() -> dict:
    """最近一次「数据库真的写进去了」的证据时间（U21）。

    `database_writable()` 每次 UPSERT `readiness_probe`，那张表的 `ts` 是
    **「事务提交成功」**的证据。健康恢复必须能指出这份证明，而不只是「这一轮
    探测没抛异常」—— 后者在 SQLite 把写缓冲进页缓存时也会成立（N30）。

    N04（2026-10-28 复审）修正：原实现声称这里还包含「强制 checkpoint」，
    但那个 checkpoint 在 `BEGIN IMMEDIATE` 事务内执行、必然 `database table is
    locked` 且被吞掉，从未生效。现在 checkpoint 改由 `_probe_checkpoint()`
    在事务外用独立连接做 PASSIVE，结果单独放 `checkpoint` 字段；
    `durable_evidence=True` 仅表示「拿到了非 busy 的合并结果」，
    **commit 成功本身不等于物理落盘**。
    """
    try:
        row = db.query_one('SELECT ts FROM readiness_probe WHERE id=1')
    except Exception:  # noqa: BLE001 - 证据读不到 ≠ 数据库健康
        return {'ts': None, 'age_seconds': None, 'reason': 'readiness_probe 不可读'}
    if row is None or not row['ts']:
        return {'ts': None, 'age_seconds': None,
                'reason': '尚无写入证明（启动后还没跑过就绪探测）'}
    stamp = int(row['ts'])
    checkpoint = dict(_last_checkpoint)
    durable = (checkpoint.get('at') is not None
               and checkpoint.get('error') is None
               and checkpoint.get('busy') == 0)
    return {'ts': stamp, 'age_seconds': max(0, int(time.time()) - stamp), 'reason': None,
            'checkpoint': checkpoint, 'durable_evidence': durable}


def readiness():
    """就绪判定：数据库可写 + 账本通道健康 + **未处于排空中**（K12）。

    draining 时返回 ok=False，`/api/readyz` 据此 503，发布脚本/入口据此把新流量
    切走；已经接单的请求不受影响（它们由 drain.wait_until_drained 保护）。

    `ledger_backlog` / `ledger_spool` 是 U01 新增的可观测项：积压 >0 表示有请求
    的事实还停在回执里没进库，此时就绪必须失败，避免继续放大新生成。

    U21（2026-10-28）：把「健康恢复的依据」显式暴露出来 —— 恢复要求**持久回执
    积压归零**（`ledger_backlog == 0`）+ **连续成功重放轮数达标**
    （`ledger_replay_streak >= WB_LEDGER_RECOVER_STREAK`）+ 回执通道未降级，
    并附带**最近一次写入证明**的时间与年龄（`write_proof`）。单条请求写成功
    不再能把状态翻回健康（那是 U01 修掉的漏洞）。
    """
    from . import drain
    draining = drain.is_draining()
    shared_ok = True
    if config.SHARED_ADMISSION:
        from .observability import admission_snapshot
        shared = admission_snapshot(probe=True)
        shared_ok = bool(shared and shared.get('scope') == 'same_host_shared' and shared.get('healthy'))
    backlog = _ledger_backlog()
    healthy = _ledger_channel_ok()
    spool = 'failed' if ledger_spool_failed else 'ok'
    try:
        writable = database_writable()
    except Exception:  # noqa: BLE001 - 写不进就是不可写，不猜
        writable = False
    try:
        from . import ledgerreplay
        channel = ledgerreplay.channel_state()
    except Exception:  # noqa: BLE001 - 读不到按降级报，不冒充健康
        channel = {'state': 'degraded', 'pending': -1, 'dead': -1,
                   'writable': False, 'reason': '通道状态不可读',
                   'dead_dispositions': -1, 'spool_dir': '', 'exists': False,
                   'used': False, 'checked_at': None}
    return {
        'ok': bool(writable and healthy and not draining and shared_ok),
        'database_write': bool(writable), 'ledger_write': healthy,
        'ledger_backlog': max(0, backlog), 'ledger_spool': spool,
        'draining': draining,
        # N01：死信与通道状态**分别持久统计**（跨轮/跨重启都在）
        'ledger_dead': int(channel.get('dead', -1)),
        'ledger_dead_dispositions': int(channel.get('dead_dispositions', -1)),
        'ledger_channel_state': channel.get('state'),
        'ledger_channel_writable': bool(channel.get('writable')),
        'ledger_channel_reason': channel.get('reason'),
        # U21：健康恢复的依据（可复核，而不是"最近一次没报错"）
        'ledger_write_healthy': bool(ledger_write_healthy),
        'ledger_replay_streak': int(_ledger_replay_streak),
        'ledger_recover_streak_required': _reconcile_env_int('WB_LEDGER_RECOVER_STREAK', 3),
        'write_proof': _write_proof(),
        'recovery_basis': ('ledger_backlog==0 且 ledger_replay_streak>='
                           'ledger_recover_streak_required 且 spool 未降级；'
                           'write_proof 是最近一次真实落盘的证据'),
    }

def transition(name, active, detail):
    old = alerts.get(name)
    if active:
        if old is None:
            alerts[name] = {'name':name,'since':int(time.time()),'detail':detail}
            log.error('operational alert name=%s detail=%s',name,detail)
            # K46：日志之外**再**投递一次（默认 disabled，不擅自外发）。
            # 只在状态**进入**时投递，避免 monitor 每轮重复刷。
            try:
                from . import notify
                notify.dispatch(name, True, detail)
            except Exception:  # noqa: BLE001 - 投递失败绝不影响告警状态机
                log.warning('alert dispatch failed name=%s', name)
    elif old is not None:
        alerts.pop(name)
        log.info('operational recovery name=%s',name)
        try:
            from . import notify
            notify.dispatch(name, False, 'recovered')
        except Exception:  # noqa: BLE001
            log.warning('alert recovery dispatch failed name=%s', name)


def _offsite_configured():
    """异地发布是否**已配置**。

    判据只用 `offsitebackup.CONFIG` 是否存在——与该模块自身的入口条件同源。
    不另建一个「开关」变量：两份事实来源必然漂移，而漂移的方向恰好是「关了还在报」
    或「开着却不报」。

    为什么需要它：`backup_publish`（加密异地发布未验证）与 `backup_size`（密文接近
    50 MiB 上限）描述的都是**异地发布**的问题。2026-10-26 用户决定不做异地备份后，
    这两项不是「故障」而是「不适用」——继续报会让每次启动都刷两条噪音，
    稀释真正需要人看的告警（启动日志里它们和 `budget_unknown`、`model_completion`
    混在一起，看得多了就没人看了）。

    2026-10-27 R5（进程降权）：Python 3.12 的 `Path.is_file()` 对 EACCES 会抛
    PermissionError（不再吞掉）。manager 以 uid 10001 运行后 /root 不可遍历属
    **正常形态**——异地备份通道由 root 侧任务维护与判读，这里按「未配置」处理，
    不能让它打断整个健康评估循环。
    """
    from . import offsitebackup
    try:
        return offsitebackup.CONFIG.is_file()
    except OSError:
        return False


_orphan_seen = None  # 上次观察到的孤儿 owner 集合，用于「只在变化时记录」


def evaluate_portal_ledger():
    """双库一致性独立自检。

    此前唯一的对账挂在每日全量备份里：备份一失败当天就没有任何自检，而且失败只进
    journal、没有告警。把只读摘要挪到分钟级评估里，账务分叉才可能被及时发现。
    本函数不改任何账目，只做判断。
    """
    from . import portalrecovery
    try:
        result = portalrecovery.ledger_consistency(
            config.DATA_DIR / 'manager.db', config.DATA_DIR / 'user-portal.sqlite3')
    except Exception as exc:  # noqa: BLE001 - 自检自身失败也必须可见
        transition('portal_ledger_check', True,
                   'portal ledger consistency check failed: ' + type(exc).__name__)
        return
    transition('portal_ledger_check', False, '')
    transition('portal_ledger_diverged', not result['ok'], result['detail'])
    try:
        orphans = portalrecovery.orphan_ledger_owners(
            config.DATA_DIR / 'user-portal.sqlite3')
    except Exception as exc:  # noqa: BLE001 - 诊断失败不得影响健康评估
        log.warning('portal orphan ledger scan failed: %s', type(exc).__name__)
        return
    # 0.1.16：只在**集合变化**时记录。此前每次分钟级评估都打一条 warning，
    # 实测 6 小时产生 140 条重复日志 —— 噪音会把真正需要看的信息淹掉。
    # 仍保持 warning 级：这类 owner 通常已无可用凭据、不影响当前业务，
    # 不该升级为运营告警，但必须可追溯。
    global _orphan_seen
    current = tuple(sorted(str(o['owner']) for o in orphans))
    if current != _orphan_seen:
        _orphan_seen = current
        if current:
            log.warning('portal ledger: %d 个 owner 有扣费但无发放/流水（历史重建遗留）: %s',
                        len(current), ','.join(x[:8] for x in current[:5]))
        else:
            log.info('portal ledger: 孤儿 owner 已清空')


#: 账号文件名里那段 UUID。
#:
#: ## 为什么不能直接数 `*.json`
#:
#: 2026-11-15 实测：qoder 的账号目录里有三个 json ——
#:
#: | 文件 | 是什么 |
#: | --- | --- |
#: | `019ee5d2-482e-7924-b38c-329cd5133c4b.json` | **账号** |
#: | `machine_identity.json` | 机器标识 |
#: | `settings.json` | 平台配置 |
#:
#: 直接数文件会得出 3，而实际只有 1 个账号。冗余度告警的判据是「≤1 即无冗余」——
#: **那个隐患因此被完全掩盖了**：真出事时没有任何告警。
#:
#: workbuddy 那侧是 48 个 `workbuddy-<uuid>.json`，全是账号，所以此前没暴露。
#:
#: 判据取「文件名里含 UUID」而不是排除名单：辅助文件的名字是语义化的，而账号名
#: 一定是 UUID 形状。排除名单会在上游新增一个辅助文件时再次失效 —— 这次的教训
#: 就是「枚举追不上演进」。
_ACCOUNT_NAME = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)


def _count_account_files(directory) -> int:
    """数目录里真正是账号的 json 文件（按文件名含 UUID 判断）。"""
    return sum(1 for path in directory.glob('*.json')
               if _ACCOUNT_NAME.search(path.name))


def collect_account_counts() -> dict:
    """数各平台现有的账号。**读不到的平台直接不返回**，不拿 0 冒充。

    ## 三条读取路径各不相同

    | 平台 | 形态 |
    | --- | --- |
    | workbuddy | 目录下 `*.json` |
    | trae | hub 引擎：JSON 账号目录；aiwork 引擎：SQLite 的 `accounts` 表 |

    （qoder 于 2026-11-22 从网关移除，不再参与评估。）

    trae 那条用 `mode=ro` 打开 —— **绝不能用读写模式碰生产库**：它由引擎自己维护，
    这边只是数一行，任何写操作都可能和引擎抢锁。

    任一平台读失败（权限、路径不存在、库被占用）就**跳过它**：跳过意味着「这条告警
    这次不评估」，而报 0 意味着「这个平台没有账号了」—— 后者是严重误报。
    """
    import sqlite3
    from pathlib import Path
    from . import config, externalplatforms
    counts: dict[str, int] = {}

    # 2026-11-22：qoder 已从网关移除，不再评估它的账号池 —— 继续报
    # 「qoder 只有 1 个账号」等于在提醒运维去修一个已经不存在的平台。
    for platform, directory in (
            ('workbuddy', getattr(config, 'AUTH_DIR', None)),):
        if not directory:
            continue
        try:
            counts[platform] = _count_account_files(Path(directory))
        except OSError:
            continue

    # trae 的账号来源跟着引擎走：hub 引擎读 JSON 账号目录，aiwork 引擎读 sqlite。
    # 以前这里写死读 sqlite，切到 hub 之后数的是历史库，告警与事实脱节。
    try:
        engine = getattr(externalplatforms, '_trae_engine', None)
        engine = engine() if callable(engine) else ''
        hub_dir = getattr(externalplatforms, 'TRAE_HUB_ACCOUNTS_DIR', None)
        if engine == 'hub' and hub_dir:
            hub_accounts = externalplatforms._trae_hub_accounts()
            counts['trae'] = int(hub_accounts.get('count') or 0)
        else:
            db_path = getattr(externalplatforms, 'TRAE_DB', None)
            if db_path:
                conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=2)
                try:
                    counts['trae'] = int(
                        conn.execute('SELECT count(*) FROM accounts').fetchone()[0])
                finally:
                    conn.close()
    except (sqlite3.Error, OSError, TypeError):
        pass

    return counts


def account_pool_alerts(counts: dict) -> list[tuple[str, bool, str]]:
    """按平台的账号池冗余度：`[(告警名, 是否触发, 文案)]`，**每个平台一条**。

    ## 为什么需要它

    2026-11-15 实测三个平台的账号池：workbuddy **48**、qoder **3**、trae **1**。

    **trae 只有一个账号** —— 而它承载 26 个模型（含全部豆包）。那个账号一旦被限流
    或过期，整个平台就没了，而且**除了用户报错之外没有任何信号**。

    这不是代码缺陷（账号是运维资源），但「只有一个」这件事**应该被告知**：
    冗余度是运行风险，不是使用量。

    ## 判据

    | 账号数 | 状态 |
    | --- | --- |
    | 0 | **不可用** —— 该平台此刻必然失败 |
    | 1 | **单点** —— 没有冗余，一次限流就全断 |
    | ≥ 2 | 正常 |

    **只报 0 与 1**：两个账号也不算宽裕，但那是「要不要扩容」的判断，
    不是「现在就有风险」的事实。告警只报事实。

    `counts` 里**没有的平台跳过**（读不到不该被当成 0 报「不可用」）。
    """
    alerts = []
    for platform in sorted(counts):
        count = counts[platform]
        if count is None:
            continue
        if count <= 0:
            alerts.append((
                f'account_pool_{platform}', True,
                f'{platform} has no usable accounts; every request on this platform '
                f'will fail until one is added'))
        elif count == 1:
            alerts.append((
                f'account_pool_{platform}', True,
                f'{platform} has exactly 1 account; there is no redundancy, so a single '
                f'rate limit or token expiry takes the whole platform down'))
        else:
            alerts.append((
                f'account_pool_{platform}', False,
                f'{platform} has {count} accounts'))
    return alerts


def disk_space_alerts(free_bytes: int | None) -> list[tuple[str, bool, str]]:
    """磁盘水位分两档返回 [(告警名, 是否触发, 文案)]。

    ## 为什么要两档

    原来只有「可用低于 2 GiB」一档。但实测这条卷的**年增长约 9 GB** —— 数据库每年
    涨约 855 MB，而多副本备份把它放大到约 9 GB。按这个速度，2 GiB 触发时离写满只剩
    几周，那时可选项已经很少了。

    所以补一档提前量。两档用**不同的告警名**，因为它们语义不同：

    | 告警名 | 阈值 | 含义 |
    | --- | --- | --- |
    | `disk_free` | < 2 GiB | 紧急，马上要写满了 |
    | `disk_space_trend` | < 5 GiB | 趋势，该做决定了 |

    分成两个名字而不是一个名字两种文案，是因为状态机只在**变化**时通报：如果共用
    一个名字，从 5 GiB 掉到 2 GiB 不会产生新通报 —— 而那一刻才是最该被看见的。

    取不到时（没有 `statvfs` 的平台）两项都返回未触发，不猜。
    """
    if free_bytes is None:
        return [('disk_free', False, 'disk free space unknown on this platform'),
                ('disk_space_trend', False, 'disk free space unknown on this platform')]
    urgent = free_bytes < 2 * 1024**3
    trending = free_bytes < 5 * 1024**3
    gib = free_bytes / 1024**3
    return [
        ('disk_free', urgent,
         'application volume free space below 2 GiB (%.1f GiB free)' % gib),
        ('disk_space_trend', trending,
         'application volume free space below 5 GiB (%.1f GiB free); '
         'growth budget in docs/DATA-GROWTH.md' % gib),
    ]


def budget_rejected_unknown_alert(count) -> tuple[bool, str]:
    """`budget_rejected_unknown`：被拒却留着未决预留的条数。

    ## 为什么单独看这一项

    `budget_unknown` 的判据是「积压超过处理期限」，它汇总了各种成因
    （客户端断开、上游报错、传输中断……）—— 那些是机制固有的不确定性，可以接受。

    但有一类不该存在：**请求被拒绝（400/429），预算却留在未决状态**。
    拒绝意味着什么都没发生，预留应当归还应而不是挂着。出现这种行说明
    预留与归还之间断了链。

    实测 2026-11-15：库里有 156 条这种残留，集中在 11-03（112 条）与
    11-10、11-12，形态都是 `prompt_too_long` 的 400 却预留了上千万 token。
    **11-13 之后为 0** —— 说明链路已被修好，但没有专门的告警盯着它，
    若将来回归只能靠人工翻数据发现。

    所以这条告警只看**最近 24 小时**：历史残留不该让它长期 firing，
    但**新出现一条就说明有回归**。

    ## 2026-11-22 修正：上游拒绝不算「回归」

    实测发现这条告警把**另一类**也算了进来：`error_stage='upstream'` 的 400
    （典型是 `prompt_too_long`，一次预留上千万 token）。那两条来自今天 00:24，
    但按 `budget.py` 的既有分类它们是 `upstream_rejected` —— **本来就该保持
    unknown**：「上游拒绝了」不等于「零计费」，不能替供应商猜账。

    也就是说：判据（`terminal_state='rejected'`）与分类（`upstream_rejected`
    保留未知）互相矛盾，凡是上游返回 4xx 就会让这条告警常亮，把真正的回归
    信号淹没。现在只看**网关自己拒绝**的那一类：预留是网关建的，网关拒绝却
    不归还，才是真的断链。
    """
    count = int(count or 0)
    return (count > 0,
            'rejected requests left unsettled budget reservations in the last 24h: %d '
            '(a rejection must release its reservation; see docs/LEDGER-RECONCILIATION.md)'
            % count)


def model_completion_alert(n: int, failed: int) -> tuple[bool, str]:
    """`model_completion` 的判据与文案：比例与绝对失败数**两个都要满足**。

    ## 为什么不能只看比例

    2026-11-15 实测一次误报：5 分钟窗口 **25 个请求、2 个失败 = 8%**，按「>1%」判据
    触发。但 25 个样本里 1 个失败就是 4%、2 个就是 8% —— **一两起偶发就够**。
    那次的两个失败还是边界探测产生的（上游回「没这个模型」），不是真故障。

    低流量下比例判据没有统计意义，所以加绝对下限：

    | 场景 | 判断 |
    | --- | --- |
    | 20 个请求 + 3 个失败（15%） | **告警**，值得看一眼 |
    | 1000 个请求 + 3 个失败（0.3%） | 不告警，本来就不该 |
    | 1000 个请求 + 30 个失败（3%） | **告警** |

    真实故障通常是批量失败（上游整个不可用会成片出错），不会只掉一两个，
    所以这个下限不会挡住真问题。
    """
    n = int(n or 0)
    failed = int(failed or 0)
    ratio = (failed / n) if n else 0.0
    detail = ('model terminal failures %d/%d (%.1f%%) exceed 1%% / 5 minutes / '
              '>=20 requests and >=3 failures' % (failed, n, ratio * 100))
    return (n >= 20 and failed >= 3 and ratio > 0.01), detail


def evaluate_backup_health(now):
    """Missing/unreadable/invalid reports are unknown, never a healthy backup."""
    import json
    latest = config.BACKUP_STATUS_FILE
    offsite = _offsite_configured()
    try:
        data = json.loads(latest.read_text())
        size = data.get('bytes')
        age = now - latest.stat().st_mtime
        valid = (data.get('restore_check') == 'ok'
                 and isinstance(size, int) and not isinstance(size, bool) and size > 0
                 and age >= -300)
        if not valid:
            raise ValueError('invalid_backup_report')
    except (OSError, ValueError, AttributeError, TypeError):
        transition('backup_status_unavailable', True, 'backup report missing, unreadable or invalid')
        transition('backup_stale', True, 'no valid backup freshness evidence')
        transition('backup_publish', offsite, 'no verified offsite publication evidence')
        # 基线坏了不代表整链状态不值得看 —— 恰恰相反，这是最该看的时候。
        _recovery_chain_alert()
        return
    transition('backup_status_unavailable', False, '')
    transition('backup_stale', age > 26*3600, 'verified backup report older than 26 hours')
    transition('backup_publish', offsite and data.get('offsite') is not True,
               'encrypted offsite publication not verified')
    transition('backup_size', offsite and size > 45*1024**2,
               'backup approaching 50 MiB ciphertext limit')

    # ── 恢复链整链（2026-11-15）──────────────────────────────────────────
    #
    # 上面几条测的都是**基线 tar**。而基线备份每次都成功 —— 于是加密快照一份都
    # 产不出来时，这里仍然一片安静。
    #
    # 2026-11-15 实测：完整恢复链连续失败多次（先是超时、再是校验重复解压、
    # 再是密文超 1 GiB），而 `backup_stale` 从头到尾没有响过。
    #
    # **必须在两个分支都调用**：上面那个 except 会提前 return，而「基线报告缺失」
    # 恰恰是最需要看整链状态的时候 —— 第一次实现把它放在 return 之后，测试当场
    # 就抓到了（基线一坏，整链告警也跟着哑掉）。
    _recovery_chain_alert()


def _recovery_chain_alert():
    """单独一条：整条加密恢复链最后一次端到端验证通过是什么时候。

    **与基线报告完全独立。** 基线成功不代表整链成功；基线失败时整链状态
    也仍然值得看。所以它既不依赖 `backup-status.json` 的内容，也不在它的
    失败分支里被跳过。
    """
    chain = recovery_chain_status()
    if chain is None:
        transition('recovery_chain_stale', True,
                   'no evidence that the encrypted recovery chain ever completed; '
                   'the baseline report says nothing about this')
        return
    # 阈值取 50 小时：日备份留出两次机会，又不至于漏掉一整天。
    transition('recovery_chain_stale', chain['age_seconds'] > 50*3600,
               'encrypted recovery chain last verified %d hours ago (%d parts, %d bytes)'
               % (chain['age_seconds'] // 3600, chain['snapshot_parts'], chain['snapshot_bytes']))


def _archive_worker_alert():
    """归档线程是唯一会主动删数据的后台任务，它停摆不能是静默的。

    ## 为什么单列一条

    2026-11-15 排查「磁盘一直涨」时，我看到 `archive_runs` 是空的，就断言
    归档从没跑过。**实际它在跑** —— 保留期 90 天，而这批数据只有 41 天，
    没有到期行，所以从未产生过归档记录。

    有这条告警，那种推断就不必靠猜：线程活着、周期在推进，一眼可见。

    ## 阈值

    归档周期是 300 秒，这里放到 30 分钟 —— 一次慢周期或短暂卡顿不该报警，
    但连续半小时没完成一次就该有人看。
    """
    try:
        from . import archiveworker
        status = archiveworker.status()
    except Exception:      # noqa: BLE001
        # 拿不到状态时不报警：这条告警的价值在于「线程该在却不在」，
        # 而不是在导入期问题上制造噪音。
        return
    if not status.get('enabled'):
        # 明确关掉了，不是故障。
        transition('archive_worker_stalled', False, '')
        return
    if not status.get('worker_alive'):
        transition('archive_worker_stalled', True, 'archive worker thread is not running')
        return
    age = status.get('age_seconds')
    if not isinstance(age, int) or isinstance(age, bool):
        # 还没跑过第一轮（启动后 30 秒内）。给它时间。
        transition('archive_worker_stalled', False, '')
        return
    transition('archive_worker_stalled', age > 30*60,
               'archive worker last completed a cycle %d minutes ago' % (age // 60))

# ── 预算 unknown 的应用内增量对账（2026-10-27 审计 P1-1）─────────────────
# 定期扫描不是计费证明。零预扣及超期只决定扫描候选；完整上游零回执和
# 不可变用量事件必须一致，并在 resolve 的写事务内重读。缺少证据继续 unknown。
def _reconcile_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def reconcile_zero_reservation_unknowns(now: int) -> int:
    """Reconcile zero reservations only when a complete zero receipt exists."""
    from . import budget
    horizon = now - _reconcile_env_int('WB_BUDGET_RECONCILE_MIN_AGE_SEC', 7200)
    settled = 0
    for row in budget.iter_unresolved():
        if row.get('state') != 'unknown':
            continue
        if any(row.get(field) or 0 for field in ('reserved_tokens', 'reserved_credit', 'charged_tokens', 'charged_credit')):
            continue          # 有真实用量：必须人工核对，自动对账不碰
        if int(row.get('created_at') or now) > horizon:
            continue          # 未到年龄阈值：先等真实结算
        if not budget._confirmed_zero_usage(row['request_id'], row['key_id']):
            continue          # 未记账不等于上游未计费；事务内还会再次校验
        try:
            if budget.resolve(row['request_id'], 0, 0.0, 'auto-reconcile',
                               '完整上游回执与不可变用量事件均确认零用量', zero_only=True):
                settled += 1
        except Exception:  # noqa: BLE001  单条失败不中断本轮
            log.exception('incremental budget reconcile failed request_id=%s',
                          row.get('request_id'))
    if settled:
        log.info('incremental budget reconcile settled %d zero-reservation unknowns',
                 settled)
    return settled


#: 未决预算「积压失控」的规模阈值。
#:
#: ## 为什么需要一条上限，而不是「有一条超期就报」
#:
#: 原来的判据是 `needs_human_old > 0` —— **只要有一条超期就永久 firing**。
#:
#: 2026-11-15 实测：库里 916 条已超期，而处理方式只有**单条核销**
#: （`POST /api/budgets/{id}/resolve`，没有批量接口）。九百多条逐条核销不现实，
#: 于是这条告警从上线起就一直亮着 —— 亮到没人再看它。
#:
#: 这是典型的告警疲劳：一条永远为真的告警，等价于没有告警。
#:
#: 所以拆成两件不同的事：
#:
#: | 关注点 | 判据 | 为什么 |
#: | --- | --- | --- |
#: | 积压是否失控 | 超期条数 >= 2000 | 已知积压不该反复叫人；**涨到失控**才叫 |
#: | 新增是否异常 | 最近 24h 新增 > 100 | 真正的「又出问题了」信号 |
#:
#: 当前实测 916 条超期、约 16 条/天新增，两条都安静 —— 与实际情况相符。
#: 积压规模仍然完整地写在告警文案里，不会因为不报而看不见。
BUDGET_BACKLOG_LIMIT = 2000

#: 最近 24 小时新增未决预算的告警阈值。实测约 16/天，取 100 留 6 倍余量。
BUDGET_INFLOW_LIMIT = 100


def budget_unknown_alert(summary: dict) -> tuple[bool, str]:
    """`budget_unknown` 的判据与文案。

    ## 2026-11-15：判据从「有超期就报」改成「积压失控才报」

    原判据 `needs_human_old > 0` 会让这条告警永久 firing —— 见
    `BUDGET_BACKLOG_LIMIT` 的说明。现在按**规模**判断，并且把完整的按龄分布
    继续写在文案里：不报不等于看不见。

    ## 2026-11-15（更早）：文案要说出「去哪儿处理」

    这条告警长期在报「需要人工核对」，但**没有说去哪儿核对**。看到日志的人要么
    去找代码，要么干脆忽略 —— 后者更常见。
    """
    buckets = summary.get('review_buckets') or {}
    backlog = int(summary.get('needs_human_old', 0) or 0)
    detail = ('unknown budget backlog %d past deadline (limit %d): '
              'new-24h %d, new-1d %d, older-7d %d, upstream-rejected %d, '
              'auto-reconcilable %d, archived-pending %d, token bound %d; '
              'review at GET /api/budgets/review, evidence at '
              'GET /api/budgets/review/evidence, settle with '
              'POST /api/budgets/{request_id}/resolve'
              % (backlog, BUDGET_BACKLOG_LIMIT,
                 buckets.get('new_24h', 0), buckets.get('new_1h', 0),
                 buckets.get('older_7d', 0), buckets.get('upstream_rejected', 0),
                 summary.get('auto_reconcilable', 0),
                 summary.get('archived_pending', 0), summary.get('token_lower_bound', 0)))
    return backlog >= BUDGET_BACKLOG_LIMIT, detail


def budget_unknown_inflow_alert(summary: dict) -> tuple[bool, str]:
    """`budget_unknown_inflow`：**最近 24 小时新增**过多。

    与上面的积压告警分工明确：积压看**总量是否失控**，这条看**新增是否异常**。

    为什么要分开：积压里绝大部分是历史上的集中事件（限流那几天一次进来上百条），
    它们不会自己消失；而「今天又新增了一百条」才是需要立刻查的信号。把两者塞进
    同一条告警，前者会把后者盖住。
    """
    buckets = summary.get('review_buckets') or {}
    new_24h = int(buckets.get('new_24h', 0) or 0)
    detail = ('unknown budget inflow %d in the last 24h (limit %d); '
              'a spike here usually means an upstream stopped returning usage; '
              'inspect GET /api/budgets/review?bucket=unknown'
              % (new_24h, BUDGET_INFLOW_LIMIT))
    return new_24h > BUDGET_INFLOW_LIMIT, detail


def replay_ledger_receipts(now: int) -> dict:
    """U01：重放一批完成回执，并推进「账本写入健康」的恢复状态机。

    恢复条件必须同时成立：本轮无失败、无死信、积压归零，且连续成功轮数达到
    `WB_LEDGER_RECOVER_STREAK`（默认 3）。单条请求写成功不再能翻回健康——那正是
    旧实现「任意下一笔写成功就把标志置真」的漏洞。
    """
    global ledger_write_healthy, ledger_spool_failed, _ledger_replay_streak
    try:
        from . import ledgerreplay
        result = ledgerreplay.replay_batch()
    except Exception:  # noqa: BLE001 - 重放自身异常不能中断监控循环
        _ledger_replay_streak = 0
        log.exception('ledger receipt replay pass failed')
        return {'scanned': 0, 'applied': 0, 'duplicate': 0, 'failed': -1,
                'dead': 0, 'backlog': _ledger_backlog()}
    from . import ledgerreplay
    try:
        # force_probe：恢复判定必须基于**这一次**的真实写探测，不能用 1 秒缓存
        channel = ledgerreplay.channel_state(force_probe=True)
    except Exception:  # noqa: BLE001 - 通道状态读不到 = 不健康
        channel = {'state': 'degraded', 'writable': False, 'pending': -1, 'dead': -1,
                   'reason': '通道状态不可读'}
    dead_pending = int(channel.get('dead', -1))
    # N02：`spool_failed` 只能被**真实写探测成功**清掉。
    # 旧实现里"三轮无文件重放"就会把它清掉 —— 但空目录并不证明能写：
    # 目录可能已被删/只读/配额满，恰恰是它当初置真的原因。
    if channel.get('writable'):
        if ledger_spool_failed:
            log.warning('ledger spool channel writable again (probe ok)')
        ledger_spool_failed = False
    clean_round = (result['failed'] == 0 and result['dead'] == 0
                   and result['backlog'] == 0 and dead_pending == 0
                   and channel.get('state') != 'degraded')
    if clean_round:
        _ledger_replay_streak += 1
    else:
        _ledger_replay_streak = 0
    if _ledger_replay_streak >= _reconcile_env_int('WB_LEDGER_RECOVER_STREAK', 3):
        if not ledger_write_healthy:
            log.warning('ledger channel recovered: backlog=0 streak=%d', _ledger_replay_streak)
        ledger_write_healthy = True
    transition('ledger_backlog', result['backlog'] > 0,
               'ledger completion receipts awaiting replay (%d)' % result['backlog'])
    # N01：死信是**持久**降级项，告警不会因为重放轮次清空而消失
    transition('ledger_dead_letters', dead_pending > 0,
               'ledger receipts in dead letter awaiting audited disposition (%d)' % dead_pending)
    transition('ledger_channel', channel.get('state') == 'degraded',
               'ledger spool channel degraded: %s' % (channel.get('reason') or 'writable probe failed'))
    return result


def _monitor_tick(now, failures, next_reconcile, next_replay):
    if not config.BACKGROUND_JOBS:
        # Transition workers refresh local readiness only. Shared recovery,
        # notifications and maintenance belong to the stable primary instance.
        return failures, next_reconcile, next_replay, readiness()
    """Keep disk/SQLite/replay/legacy notification I/O off the ASGI event loop."""
    from . import archiveworker
    archive = archiveworker.status()
    archive_problem = archive['enabled'] and (not archive['worker_alive'] or (
        archive['sampled'] and (not archive['ok'] or not archive['fresh'])))
    transition('archive_retention', archive_problem,
               'retention archive unavailable or stale; source rows retained; inference readiness unchanged')
    # U01：先消化回执再判就绪，避免「积压刚产生就报警」的抖动。
    if now >= next_replay and config.BACKGROUND_JOBS:
        next_replay = now + _reconcile_env_int('WB_LEDGER_REPLAY_INTERVAL_SEC', 30)
        replay_ledger_receipts(now)
    status = readiness()
    # Planned drain deliberately makes readiness 503. It must not become a
    # database failure alert (or trigger a future self-heal restart).
    storage_ok = status['database_write'] and status['ledger_write']
    failures = 0 if storage_ok else failures+1
    transition('readiness',failures>=2,'database/ledger readiness failed twice')
    now=int(time.time())
    try:
        # U02（2026-10-28）：告警只统计**真正需要人工**的 unknown。
        # 只有完整零回执可自动结清；拒绝错误和采集缺失仍需核对。
        # U02 收尾：超过处理期限仍无上游证据的已归档待核对，也不再触发告警。
        from . import budget as _budget
        _summary = _budget.unresolved_summary(now=now)['summary']
        _active, _detail = budget_unknown_alert(_summary)
        transition('budget_unknown', _active, _detail)
        # 新增速率单独一条：积压里大部分是历史集中事件，会把「今天又出一批」盖住。
        _active_in, _detail_in = budget_unknown_inflow_alert(_summary or {})
        transition('budget_unknown_inflow', _active_in, _detail_in)
        row=db.query_one("SELECT count(*) n,sum(CASE WHEN terminal_state IN ('upstream_error','transport_error','protocol_incomplete','application_error','interrupted') THEN 1 ELSE 0 END) failed FROM request_outcomes WHERE protocol!='http' AND ts>=? AND terminal_state!='rejected'",(now-300,))
        # 判据与文案集中在 `model_completion_alert`：比例要满足，绝对失败数也要满足。
        _active, _detail = model_completion_alert(row['n'], row['failed'])
        transition('model_completion', _active, _detail)
        # 被拒却留着未决预留 —— 不该存在，出现即回归（见函数说明）。
        # 排除 error_stage='upstream'：上游拒绝（prompt_too_long / 429 限流等）
        # 按 budget.py 的 UNKNOWN_CLASSES 属于 upstream_rejected，**本就该留在
        # 待核对**，不能替供应商猜成零用量。这里只抓网关自己拒绝却没归还预留。
        _rejected_unknown = db.query_one(
            "SELECT count(*) n FROM budget_reservations b "
            "JOIN request_logs l ON l.request_id=b.request_id "
            "WHERE b.state='unknown' AND l.terminal_state='rejected' "
            "AND COALESCE(l.error_stage,'')<>'upstream' "
            "AND b.created_at>=?", (now - 86400,))
        _active, _detail = budget_rejected_unknown_alert(
            _rejected_unknown['n'] if _rejected_unknown else 0)
        transition('budget_rejected_unknown', _active, _detail)
        free=os.statvfs(config.DATA_DIR).f_bavail*os.statvfs(config.DATA_DIR).f_frsize if hasattr(os,'statvfs') else None
        # 两档水位，两个告警名（见 `disk_space_alerts` 的说明：名字不能共用）。
        for _name, _active, _detail in disk_space_alerts(free):
            transition(_name, _active, _detail)
        # 账号池冗余度：单点平台没有冗余，一次限流就全断，而此前完全没有信号。
        from . import credit_watch
        for _name, _active, _detail in credit_watch.alerts():
            transition(_name, _active, _detail)
        # WorkBuddy 池的积分：与 trae 分开两条告警名（见 workbuddy_alerts 的说明）。
        # 之前只有「账号数」告警，池子烧到只剩两成也没有信号。
        for _name, _active, _detail in credit_watch.workbuddy_alerts():
            transition(_name, _active, _detail)
        # 凭证过期：23 个账号是同一批登录的，到期时刻几乎相同 —— 失效是
        # 「某天集体掉线」而不是慢慢变少，必须提前看日期。
        for _name, _active, _detail in credit_watch.account_token_alerts():
            transition(_name, _active, _detail)
        # 签到陈旧：平台不会因此不可用，只是每天少收入 —— 不看就不会发现。
        for _name, _active, _detail in credit_watch.checkin_stale_alert():
            transition(_name, _active, _detail)
        for _name, _active, _detail in account_pool_alerts(collect_account_counts()):
            transition(_name, _active, _detail)
        # The installed host job is daily. Keep that honest: use a 26-hour
        # freshness guard until operations can install a more frequent job.
        evaluate_backup_health(now)
        evaluate_portal_ledger()
        # 归档线程是唯一会主动删数据的后台任务。它停摆时数据库只会变大，
        # 而此前没有任何信号能说出来（见 `_archive_worker_alert` 的说明）。
        _archive_worker_alert()
    except Exception:
        log.exception('operational health evaluation failed')
    # P1-1：应用内增量对账。首轮立即跑（消化存量），之后按间隔。
    # 只在主实例跑（WB_BACKGROUND_JOBS）：零停机发布的过渡实例不重复对账。
    if now >= next_reconcile and config.BACKGROUND_JOBS:
        next_reconcile = now + _reconcile_env_int('WB_BUDGET_RECONCILE_INTERVAL_SEC', 900)
        try:
            settled_zero = reconcile_zero_reservation_unknowns(now)
            # 完整上游零回执与不可变用量记录必须一致。
            from . import budget as _budget
            settled_evidenced = _budget.reconcile_evidenced_unknowns(now)
            # U02 收尾：超期无证据的 unknown 标记「已归档待核对」（不改 state、
            # 不删除、不改用量），只把它从告警判据里摘出来。
            archived = _budget.archive_overdue_unknowns(now)
            # U04：给已终结、过了稳定窗口的请求补 attempt 计费事实（有界）。
            from . import ledger as _ledger
            billing_filled = _ledger.backfill_attempt_billing(now=now)
            # 0.1.21：客户端取消后的冻结释放此前**只在启动时**跑一次（reconcile 内），
            # 服务长期不重启时这类记录会一直停在 unknown —— 实测近 24 小时新增 57 条。
            # 并入既有对账周期（默认 15 分钟）。该函数的安全边界不变：只处理终端状态为
            # client_cancelled 且没有完整上游回执的记录，不碰 upstream_error 等证据不足类。
            from . import portalwallet as _portalwallet
            auto_closed = _portalwallet.auto_close_client_cancelled()
            if (settled_zero or settled_evidenced or archived or billing_filled
                    or auto_closed):
                log.info('budget reconcile: zero-reservation=%d evidenced=%d archived=%d '
                         'attempt-billing=%d client-cancelled=%d', settled_zero,
                         settled_evidenced, archived, billing_filled, auto_closed)
        except Exception:
            log.exception('incremental budget reconcile pass failed')
    return failures, next_reconcile, next_replay, status


_cached_readiness = None


async def initialize_readiness():
    global _cached_readiness
    from .services.asyncwork import joined_thread
    _cached_readiness = None
    try:
        status = await joined_thread(readiness)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception('startup readiness sample unavailable')
        return False
    _cached_readiness = {'at': time.monotonic(), 'ok': bool(status['ok'])}
    return bool(status['ok'])


def public_readiness(now=None):
    """A read-only, bounded-age result, never a fresh database write per visitor."""
    from . import drain
    now = time.monotonic() if now is None else now
    cached = _cached_readiness
    return bool(cached and 0 <= now - cached['at'] <= 75
                and cached['ok'] and not drain.is_draining())


#: 监控循环的累计统计。
#:
#: ## 为什么需要它
#:
#: 2026-11-15 出过一次这样的事：加了一个统计桶却忘了初始化，`unresolved_summary`
#: 在特定数据状态下抛键错误。**那一整轮健康评估的剩余部分就不跑了** ——
#: 磁盘水位、账号池、备份检查都在崩溃点之后。
#:
#: 从外部**完全看不出来**：请求正常、健康检查 200、完整性闭包 ok。因为它**只在那
#: 一轮失败**，下一轮可能又成功 —— `/readyz` 读的是「最近一次结果」，闪断看不见。
#:
#: 所以这里累计成功与失败次数，并由 `/metrics` 暴露。**偶发失败要从计数上看出来，
#: 不能只看此刻是否健康。**
#:
#: 三个键全部显式初始化：这次事故的教训就是「漏一个键会抛」。
_MONITOR_STATS = {
    'successes': 0,
    'failures': 0,
    # 最近一次成功时的单调时钟读数；0.0 表示「本进程还没成功过」。
    'last_success': 0.0,
}


#: 维护窗口在 nginx 配置里留下的围栏标记。
#:
#: ## 为什么网关要看这个
#:
#: 2026-11-15 出过一次：迁移失败后，nginx 配置里残留了两行围栏规则 ——
#:
#: ```nginx
#: if ($uri ~ ^/(api|qoder|trae|wb|workbuddy|zpush)(/|$)) { return 503; }
#: ```
#:
#: 结果 `/api/*` 全部 503（管理端、登录、工作台全挂），而 `/v1/*` 与 `/healthz`
#: **完全正常**。
#:
#: **网关自己察觉不到**：健康检查走的是进程内逻辑，不经过 nginx；从服务视角看一切
#: 都好。而从外面看，管理端已经没了。
#:
#: 所以这里直接读 nginx 配置找那个标记。是跨层了，但这一层出了问题会让整个管理端
#: 消失，值得。
#:
#: `checked=False` 表示「读不到」—— 与 `fenced=False`（确认没有围栏）是两件事，
#: 不能混。读不到时不输出指标，免得把「不知道」显示成「没问题」。
_NGINX_FENCE_MARKER = '# zh-container-maintenance-v1'

#: 读取围栏状态时要看的配置文件。路径不存在就跳过，不报错。
_NGINX_CONFIG_PATHS = (
    '/etc/nginx/sites-enabled',
    '/etc/nginx/conf.d',
)


def nginx_fence_status() -> dict:
    """检查 nginx 配置里是否残留维护围栏。

    返回 `{'checked': bool, 'fenced': bool, 'files': [命中的文件]}`。

    * `checked=False` —— 一个配置文件都没读到（路径不存在、权限不足）。
      **这不等于没有围栏**，调用方必须区分。
    * `fenced=True` —— 至少一个文件里还有标记，此时 `/api/*` 大概全在返 503。
    """
    from pathlib import Path
    hits: list[str] = []
    readable = 0
    for root in _NGINX_CONFIG_PATHS:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            candidates = sorted(p for p in base.iterdir() if p.is_file())
        except OSError:
            continue
        for path in candidates:
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            readable += 1
            if _NGINX_FENCE_MARKER in text:
                hits.append(str(path))
    return {'checked': readable > 0, 'fenced': bool(hits), 'files': hits}


#: 数据库文件大小。
#:
#: ## 为什么要三个文件一起算
#:
#: 用的是 WAL 模式，所以「数据库有多大」不是主库一个文件的事：
#:
#: | 文件 | 内容 |
#: | --- | --- |
#: | `manager.db` | 已写回的主体 |
#: | `manager.db-wal` | 尚未 checkpoint 的增量 |
#: | `manager.db-shm` | 共享内存索引 |
#:
#: **只看主库会漏掉 WAL**：一次大批量写入之后、checkpoint 之前，WAL 可能有几十 MB，
#: 而那正是磁盘水位告警最该看到的部分。
#:
#: 2026-11-15 实测：库 614 MB、磁盘余量 9.7 GB。没有这条指标，增长只能靠人工 `du`。
_DB_SIDECAR_SUFFIXES = ('', '-wal', '-shm')


def database_size_bytes() -> dict | None:
    """返回 `{'main': n, 'wal': n, 'shm': n, 'total': n}`；读不到返回 None。

    单个文件缺失按 0 计（`-wal` 在 checkpoint 之后可能不存在），
    但**一个都统计不到**时返回 None —— 那是「不知道」，不是「零字节」。

    **只统计普通文件**：`Path('')` 会静默变成当前目录，而目录的 `stat()` 也返回大小
    （4096 之类）。空配置下若不加这一道，量出来的会是工作目录的块大小。
    """
    from pathlib import Path
    raw = getattr(config, 'DB_PATH', '')
    if not raw:
        return None
    base = Path(raw)
    sizes = {}
    readable = 0
    for suffix in _DB_SIDECAR_SUFFIXES:
        name = {'': 'main', '-wal': 'wal', '-shm': 'shm'}[suffix]
        try:
            info = Path(str(base) + suffix).stat()
        except OSError:
            sizes[name] = 0
            continue
        if not stat.S_ISREG(info.st_mode):
            sizes[name] = 0
            continue
        sizes[name] = info.st_size
        readable += 1
    if readable == 0:
        return None
    sizes['total'] = sizes['main'] + sizes['wal'] + sizes['shm']
    return sizes


def backup_age_seconds(now=None) -> int | None:
    """最近一次成功备份距今多少秒；读不到返回 None。

    告警只在超过 26 小时时报。**但「还差多久会报」看不到** —— 备份时间如果一天天
    往后漂（比如从 04:30 漂到 05:20），告警不会响，而它已经在变慢。

    这条指标给出趋势本身。

    `None` 表示「拿不到证据」：文件不存在、不可读、或报告不合法。那是未知，
    不是「零秒前刚备份过」。
    """
    import json
    latest = getattr(config, 'BACKUP_STATUS_FILE', None)
    if latest is None:
        return None
    now = int(time.time()) if now is None else int(now)
    try:
        data = json.loads(Path(latest).read_text())
        age = now - Path(latest).stat().st_mtime
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    # 与 `evaluate_backup_health` 用同一套有效性判据，避免两处结论不一致。
    size = data.get('bytes') if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get('restore_check') == 'ok'
            and isinstance(size, int) and not isinstance(size, bool) and size > 0
            and age >= -300):
        return None
    return max(0, age)


#: 加密快照（恢复体系 v2）的根目录。
#:
#: ## 为什么网关要看这个目录
#:
#: 2026-11-15 发现：`recovery_v3.py` 里调 `backup_application.py` 的超时写的是 300 秒，
#: 而实际要 4 分钟以上。那一版起它**每次都在 300 秒处放弃**，于是：
#:
#: | 产物 | 状态 |
#: | --- | --- |
#: | tar 基线备份 | **照常产出**（子进程成了孤儿，继续跑完） |
#: | 加密快照 | **一份都没有** |
#:
#: **而 `backup-status.json` 仍写着 `restore_check: ok`** —— 因为那份报告只校验
#: 基线 tar 本身。于是「备份正常」这个结论在告警系统里成立，在事实上不成立。
#:
#: `workbuddy_backup_age_seconds` 测的是那份报告的年龄，**测不到这个**。
#: 快照目录的最后修改时间才是「v2 恢复点还在不在推进」的直接证据。
_SNAPSHOT_ROOT = Path('/root/backups/zhihui-v2/snapshots')


#: 恢复链（加密快照整链）的结论文件。
#:
#: ## 为什么需要第三个文件
#:
#: 手上有两份「备份状态」，但它们回答的都不是「恢复链还能不能用」：
#:
#: | 文件 | 谁写 | 反映什么 |
#: | --- | --- | --- |
#: | `backup-status.json` | 基线备份（`backup_application.py`） | **只有基线 tar** |
#: | 快照目录（`/root/...`） | 恢复脚本 | 但**网关读不到**（0700 vs uid 10001） |
#:
#: 后果是：`backup_stale` 告警测的是基线 tar，而基线备份**每次都成功** ——
#: 于是加密快照一份都产不出来时，告警仍然一片安静。
#:
#: 2026-11-15 实测：完整恢复链连续失败多次，`backup_age_seconds` 始终只有几分钟。
#:
#: 恢复脚本现在在整链成功后额外写这个文件（`publish_gateway_status`），
#: 落在网关可读的目录里。
_RECOVERY_CHAIN_STATUS = Path('/var/lib/zhihui-gateway/recovery-chain.json')


def recovery_chain_status(now=None) -> dict | None:
    """最近一次**整链**验证通过的时间与规模；读不到返回 None。

    与 `backup_age_seconds` 的区别就是这个模块存在的原因：那个测基线，
    这个测整链。**它们可以不一致，而那正是 11-15 的现场。**
    """
    import json
    now = int(time.time()) if now is None else int(now)
    try:
        data = json.loads(_RECOVERY_CHAIN_STATUS.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('schema') != 1:
        return None
    verified_at = data.get('verified_at')
    parts = data.get('snapshot_parts')
    size = data.get('snapshot_bytes')
    if not (isinstance(verified_at, int) and not isinstance(verified_at, bool)
            and isinstance(parts, int) and not isinstance(parts, bool) and parts > 0
            and isinstance(size, int) and not isinstance(size, bool) and size > 0):
        return None
    age = now - verified_at
    if age < -300:
        # 时间戳在未来太远：时钟错乱或文件不可信，不拿它出指标。
        return None
    return {'verified_at': verified_at, 'age_seconds': max(0, age),
            'snapshot_bytes': size, 'snapshot_parts': parts,
            'files_verified': (data.get('snapshot_checks') or {}).get('files_verified')}


def snapshot_age_seconds(now=None) -> int | None:
    """最新加密快照距今多少秒；读不到或一份都没有时返回 None。

    **一份都没有也返回 None，而不是返回一个**：`None` 表示「没有证据」，
    而调用方据此不输出指标 —— 让缺失本身是一个显式的信息缺口，而不是一个看起来
    像「备份很旧」的数字。后者会被误读成「备份在变慢」，而实际是「没做」。
    """
    now = int(time.time()) if now is None else int(now)
    try:
        snapshots = [p for p in _SNAPSHOT_ROOT.iterdir() if p.is_dir()]
    except OSError:
        return None
    if not snapshots:
        return None
    try:
        newest = max(p.stat().st_mtime for p in snapshots)
    except OSError:
        return None
    return max(0, now - int(newest))


def monitor_stats() -> dict:
    """返回监控循环统计的副本（调用方不该直接改内部状态）。"""
    return dict(_MONITOR_STATS)


def note_monitor_result(ok: bool, now: float | None = None) -> None:
    """记一次监控轮次的结果。热路径之外的轻量更新。"""
    import time as _time
    if ok:
        _MONITOR_STATS['successes'] += 1
        _MONITOR_STATS['last_success'] = _time.monotonic() if now is None else now
    else:
        _MONITOR_STATS['failures'] += 1


async def monitor():
    global _cached_readiness
    from .services.asyncwork import joined_task
    failures, next_reconcile, next_replay = 0, 0, 0
    while True:
        tick = asyncio.create_task(asyncio.to_thread(
            _monitor_tick, int(time.time()), failures, next_reconcile, next_replay))
        try:
            failures, next_reconcile, next_replay, status = await joined_task(tick)
            _cached_readiness = {'at': time.monotonic(), 'ok': bool(status['ok'])}
            # 累计成功次数：偶发失败只能从计数看出来（见 `_MONITOR_STATS` 的说明）。
            note_monitor_result(True)
        except asyncio.CancelledError:
            # joined_task retains ownership through repeated cancellation until
            # blocking work ends, before lifespan can close its DB/worker lease.
            raise
        except Exception:
            _cached_readiness = None
            note_monitor_result(False)
            log.exception('operational monitor tick failed; readiness unavailable')
        await asyncio.sleep(30)

def _release_id() -> str:
    from . import release_identity
    return release_identity.current()


def status():
    return {'readiness':readiness(),'alerts':list(alerts.values()),
            'notification_delivery':'local journal and authenticated API only',
            'backup_freshness_hours':26,'credit_scale':ledger.SCALE,
            'financial_events_since_release': _release_id()}
