"""智汇网关（zhihui-gateway）入口：管理 API + 对外反代网关 + 静态前端托管。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import logging
import os
import asyncio
import threading
import time

from . import archiveworker, config, db, drain, redpacket, security, traffic
from .iputil import client_ip
from .stdio_utf8 import force_utf8_stdio
from .routers import (
    accounts, anthropic, auth, engine, gateway, keys, logs, models, playground,
    pgsync as pgsync_router, read_api, redpackets, responses,
    security as security_router, proxies, settings, stats, system, tokens,
    traffic as traffic_router, upstreams, steward,
)
from .services import accountlog, cooling, dailyplan, pgsync, renew, tasklog, taskrun
from .services.asyncwork import joined_thread
from .services.startupchecks import WeakAdminCheck
from .routers import budgets
from .routers import qtg_panel
from .routers import portal

# 最早时刻把标准输出/错误钉成 UTF-8：Windows 中文区域默认按 cp936 编码，日志里
# 一旦出现 emoji（账号昵称就可能带）就会抛 UnicodeEncodeError —— 与「成长任务」
# 那次崩溃同源（issue #147），只是触发条件更少。
force_utf8_stdio()

def _configure_app_logging() -> None:
    """给应用 logger 装 handler（2026-10-27 审计：此前一个都没有）。

    为什么必须做：root logger 默认 WARNING，而全仓有 38 处 `logger.info(...)`
    —— 包括启动事件、签到调度、令牌刷新、保留期清理。没有 handler 时它们**全部
    被丢弃**，出问题时只能翻 uvicorn 的 access log 猜。实测：签到调度确实在跑
    （random_checkin.json 里有 done 记录），日志里却一条都没有。

    为什么只压这几个库：httpx / httpcore / urllib3 / docker / asyncio 每请求都打
    INFO，不压会把应用日志淹掉。应用自身的 INFO 只有 38 处且无每请求级调用。

    可用 `WB_LOG_LEVEL` 覆盖（DEBUG/INFO/WARNING/ERROR）。
    """
    if logging.getLogger().handlers:
        return          # 已被 pytest / uvicorn --log-config 等配置过，不重复加
    level_name = (os.environ.get('WB_LOG_LEVEL') or 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    for _noisy in ('httpx', 'httpcore', 'urllib3', 'docker', 'asyncio', 'watchfiles'):
        logging.getLogger(_noisy).setLevel(logging.WARNING)


_configure_app_logging()

logger = logging.getLogger(__name__)

# 进程启动时刻（墙上钟）：/metrics 的 uptime 用它与当前时间求差。
# 此前误用 time.monotonic()（单调钟只保证递增，不是运行时长），数值无意义。
_PROCESS_STARTED_AT = time.time()


def _time_now() -> float:
    return time.time()


def _recover_primary_state():
    from . import ledgerreplay, budget, userroles
    if not config.BACKGROUND_JOBS:
        if config.SHARED_ADMISSION and not config.USERS_FILE.is_file():
            raise RuntimeError('transition_requires_initialized_users')
        source = security.load_users()
        if userroles.migration(source)[0] is not source:
            raise RuntimeError('transition_requires_primary_role_migration')
        return
    security.migrate_user_roles()
    try:
        ledgerreplay.recover()
    except Exception:
        logger.exception('ledger receipt recovery failed at startup')
    budget.recover()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 在第一次 `to_thread` 之前替换默认执行器。CPython 在 2 vCPU 上把它定成
    # `min(32, cpu+4) = 6` 个线程，而推理主路径一个请求要占 6 次线程跳转 ——
    # 队列一积压，排在后面的实例心跳就会超时，租约失守后进程只能重启。
    # 分池的理由见 `threadpools` 的模块文档。
    from . import threadpools
    asyncio.get_running_loop().set_default_executor(threadpools.blocking_pool())
    # Fail before creating directories, opening the ledger, or starting jobs.
    from .configcontract import validate_environment
    app.state.configuration_snapshot = validate_environment()
    config.ensure_dirs()
    db.connect()
    # U01（2026-10-28）：启动时先消化「上次事务失败留下的完成回执」。失败不阻断
    # 启动——monitor 循环会继续重放，且就绪判定会因积压>0 保持失败。
    _recover_primary_state()
    from . import budget

    async def _traffic_housekeeping(interval_s: int = 3600):
        """抓包目录的保留期清理。

        与转发路径无关：请求只往队列里放，写盘与删除都在别的线程。这里失败也
        只是「过期记录多留一会儿」，不影响服务 —— 所以只记警告，不重试风暴。
        """
        while True:
            try:
                result = await asyncio.to_thread(traffic.sweep)
                if result.get('removed'):
                    logger.info('traffic sweep removed=%s kept=%s',
                                result['removed'], result['kept'])
            except Exception:  # noqa: BLE001 - 清理失败不该影响任何请求
                logger.warning('traffic sweep failed', exc_info=True)
            await asyncio.sleep(interval_s)

    async def budget_heartbeat():
        while True:
            await asyncio.sleep(10)
            try:
                # 心跳走专用池：它的失败代价是"停止准入且不自愈"，不能排在任何
                # 业务请求后面。
                await asyncio.get_running_loop().run_in_executor(
                    threadpools.heartbeat_pool(), budget.heartbeat)
            except Exception:
                logger.exception('budget owner heartbeat failed')
    heartbeat_task = asyncio.create_task(budget_heartbeat())
    from . import operational
    # Take the first readiness sample before the monitor can publish a
    # concurrent, partially-initialized result.  Starting the monitor first
    # made the startup contract timing-dependent: /readyz could briefly report
    # 503 even though initialization had completed successfully.
    monitor_task = None
    # Portal accounts use a separate store; the persisted opt-in is off by default.
    from . import portalservice
    portal_checkins = None
    credit_watch_task = None
    # 弱口令巡检改后台线程（原 sitecustomize 猴子补丁收编进源码，2026-10-27）：
    # 每个管理员 × 常见弱口令表逐个跑 PBKDF2，实测 3 管理员要 13.7 秒，
    # 阻塞在 startup 里是当时 15 秒停机窗口的最大单项。不拒绝启动：用户需要
    # 先进面板改密码，日志必须足够醒目。
    weak_admin_check = WeakAdminCheck()
    _warn_if_exposed()
    if config.BACKGROUND_JOBS:
        # 后台作业只在「主实例」（systemd 管理的那个）运行。零停机发布的过渡
        # 实例以 WB_BACKGROUND_JOBS=0 启动：只服务请求，不跑调度器/采集器，
        # 避免过渡窗口内双实例重复签到、重复回填、重复对账（预算心跳除外——
        # 它只更新自己名下的预留，必须随请求路径存在）。
        # 给「抽奖码」这一列上线之前建的红包补码（幂等）：没有码就拼不出抽奖链接，
        # 等于那些红包只能自己发 key、没法让大家抽。
        filled = redpacket.backfill_codes()
        if filled:
            logger.info('为 %d 个旧红包补上了抽奖码', filled)
        # 后台采集上游自动任务日志（旅行/活跃/签到/保活），容器日志会被重建清掉，
        # 这里解析后落库长期保留，界面才能看到「这趟旅行领了多少积分」
        tasklog.start_collector()
        # 定时领奖（成长任务里幂等的那一半）：只把已完成任务的奖励领回来，不伪造
        # 任何活跃上报，因此可以安全地到点自动跑。点亮那半只允许手动（见 taskrun）
        taskrun.start_scheduler()
        # token 自动续期（issue #40）：上游只在「保活时刻」与「有流量时」刷新，
        # 长期闲置的账号会一路走到过期。这里按剩余寿命巡检补齐那个空档。
        renew.start_scheduler()
        # 强制冷却到期清理：manual_disabled 不会随 until 自动消失，不清理会让
        # 定时冷却变成永久停用（见 services/cooling.py）。
        cooling.start_scheduler()
        # 每日任务计划（签到 / 领奖 / 做任务）：管理端自己的调度器，把「一键」动作
        # 做成每天到点自动跑。默认全部关闭，由用户在账号页显式开启（full 有风控风险）。
        dailyplan.start_scheduler()
        # 请求日志的「账号」回填（issue #69）：账号是上游选的、不在响应里回传，
        # 只能从它的容器日志里读出来再按时间对回去（见 accountlog 的说明）。
        accountlog.start_collector()
        archiveworker.start()
        # 抓包目录的过期清理。启动跑一次，之后每小时一次；放在线程里执行，
        # 因为它要遍历并删除文件，不该占住事件循环。
        traffic_housekeeping = asyncio.create_task(_traffic_housekeeping())
        # PostgreSQL 定时备份（用户反馈：怕服务器暴雷丢数据）。默认关闭 ——
        # 只有配好连接、打开开关并设了间隔才会真的推数据出去。
        pgsync.start_scheduler()
    else:
        logger.info('WB_BACKGROUND_JOBS=0：后台调度器/采集器已停用（过渡实例形态）')
    instance_runtime = None
    # Qoder/Trae control plane (folded in from the standalone qtg service).
    # Routers read request.app.state.rt, so start the runtime before yield;
    # failures here only affect the qoder/trae branches.
    _qtg_rt = None
    try:
        if not (config.SHARED_ADMISSION and not config.BACKGROUND_JOBS):
            from .qtgcore.config import load_settings as _qtg_load_settings
            from .qtgcore.runtime import Runtime as _QtgRuntime
            _qtg_rt = _QtgRuntime(_qtg_load_settings())
            await _qtg_rt.startup()
        app.state.rt = _qtg_rt
        if _qtg_rt is not None:
            logger.info('qoder/trae control plane merged in (qtgcore started)')

        # 身份签名密钥的启动自检。
        #
        # ## 为什么需要
        #
        # 2026-11-15：管理员密码正确、登录接口返回 200，然后 nginx 的鉴权子请求
        # 读不到密钥返回 503，于是**登录成功也进不去面板**，三小时 19 次。
        #
        # 根因是那条路径的来源被换成了一个**部署里从未设置过的配置项**。
        # 这件事从代码上看完全正常，从测试上看也全绿 ——
        # **它只在真实登录时以「进不去」的形式暴露。**
        #
        # 现在路径解析已经有兜底（配置为空时回落默认位置），所以默认情况下
        # 不会再把这条链路弄断。但**兜底也读不到**的情形仍然存在（文件没部署、
        # 权限被改），而那种情况下依然是同一句「登录进不去」。
        #
        # 所以启动时读一次，把结论写进启动日志：
        # 正常情况下只有一行 INFO，异常时是一行 WARNING 并点名后果。
        #
        # 只记日志、不阻断启动：密钥读不到时面板仍可浏览，只是登录流程走不完，
        # 而把它变成启动失败会让一次配置错误升级成一次宕机。
        try:
            from . import edgeidentity as _edge
            _secret_path = _edge.identity_secret_path()
            if _edge._secret():
                logger.info('identity signing key ready (%s)', _secret_path)
            else:
                logger.warning(
                    'identity signing key UNREADABLE at %s — admin login will succeed '
                    'but the shell cannot pass nginx auth_request (503). '
                    'Check the file exists, is a regular file, mode 0600, '
                    'size <= 4096, and contains no whitespace.',
                    _secret_path)
        except Exception:      # noqa: BLE001
            logger.warning('identity signing key check failed', exc_info=True)

        # D-008 修复（2026-10-27 审计 S1）：qtg 合并时**漏了这一步接线**。
        # `checkin_scheduler.start()` 的 docstring 明确写着「在 lifespan 里调用」，
        # 但全仓没有任何调用方，于是 qoder/trae 的每日随机签到静默停摆：
        # compat-plans.json 里 daily.checkin.enabled 一直是 true，last_run 却停在
        # 2026-10-22-18，random_checkin.json 的 done/attempts/last 全空。
        # 是否真的签到仍由两道既有开关控制（checkin_scheduler_enabled 与
        # compat-plans 的 daily.checkin.enabled），这里只恢复代码路径。
        try:
            from .qtgcore import checkin_scheduler as _qcheckin
            # 过渡实例（WB_BACKGROUND_JOBS=0）不跑签到调度，其余 qtg 运行时照常。
            _qtg_checkin_task = _qcheckin.start(_qtg_rt) if config.BACKGROUND_JOBS else None
            app.state.qtg_checkin_task = _qtg_checkin_task
            if _qtg_checkin_task is not None:
                logger.info('qtgcore 每日随机签到调度已接线并在运行')
        except Exception:
            app.state.qtg_checkin_task = None
            logger.exception('qtgcore 签到调度启动失败（不影响其它功能）')
    except Exception:
        logger.exception('qoder/trae control plane failed; workbuddy proxy unaffected')
    try:
        if config.BACKGROUND_JOBS:
            weak_admin_check.start()
        from .instance_runtime import InstanceRuntime
        instance_runtime = InstanceRuntime()
        await instance_runtime.start()
        app.state.instance_runtime = instance_runtime
        from .steward_runtime import StewardRuntime
        steward_instance = StewardRuntime(_qtg_rt)
        app.state.steward = steward_instance
        try:
            await steward_instance.start()
        except Exception:
            await steward_instance.close()
            logger.error('steward startup failed; status is unavailable, gateway remains serving')
        if config.BACKGROUND_JOBS:
            portal_checkins = asyncio.create_task(portalservice.automatic_loop())
        # 积分余额快照：Trae 按积分结算，但上游不返回单次消耗（周期批量结算），
        # 所以先把余额按小时存下来 —— 有了「用量」与「余额减少」两条序列，
        # 才能反推换算关系。只记录，不参与任何扣费判定。
        if config.BACKGROUND_JOBS:
            try:
                from . import credit_watch as _credit_watch
                credit_watch_task = _credit_watch.start()
                logger.info('积分余额快照采集已接线并在运行')
            except Exception:
                credit_watch_task = None
                logger.exception('积分快照采集启动失败（不影响其它功能）')
        await operational.initialize_readiness()
        monitor_task = asyncio.create_task(operational.monitor())
        yield
    finally:
        # A completed application lifetime must not leave expensive hash workers
        # running against another lifetime's configuration or competing for CPU.
        pending_shutdown_cancel = None
        try:
            await weak_admin_check.close()
        except asyncio.CancelledError as error:
            # joined_thread has already joined the hash worker. Defer its
            # cancellation until the remaining leases/clients are also closed.
            pending_shutdown_cancel = error
        if portal_checkins is not None:
            portal_checkins.cancel()
            try:
                await portal_checkins
            except asyncio.CancelledError:
                pass
        if credit_watch_task is not None:
            credit_watch_task.cancel()
            try:
                await credit_watch_task
            except asyncio.CancelledError:
                pass
        if getattr(app.state, 'steward', None) is not None:
            await app.state.steward.close()
        # D-008：先停签到调度，再关 runtime —— 顺序反了会出现"runtime 已关、
        # 调度还在签到"的窗口。
        _qct = getattr(app.state, 'qtg_checkin_task', None)
        if _qct is not None:
            _qct.cancel()
            try:
                await _qct
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception('qtgcore 签到调度关闭异常')
        if _qtg_rt is not None:
            try:
                await _qtg_rt.shutdown()
            except Exception:
                logger.exception('qtgcore shutdown failed')
        # A managed launcher/pre-stop controller marks drain BEFORE uvicorn
        # starts shutdown. Lifespan shutdown is only the final cleanup stage.
        _drain = await drain.wait_until_drained(config.DRAIN_TIMEOUT_SEC)
        if not _drain['drained']:
            logger.error('Shutdown reached cleanup with requests outstanding: %s',
                         _drain['remaining'])
        if instance_runtime is not None:
            await instance_runtime.close()
        heartbeat_task.cancel()
        if monitor_task is not None:
            monitor_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        if monitor_task is not None:
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
        from . import budgetowner
        budgetowner.close()
        tasklog.stop_collector()
        taskrun.stop_scheduler()
        renew.stop_scheduler()
        cooling.stop_scheduler()
        # Plans retain their task leases until owned work has been cancelled
        # and joined; finish that cleanup before closing shared HTTP clients.
        await dailyplan.close_scheduler()
        accountlog.stop_collector()
        await asyncio.to_thread(archiveworker.stop)
        pgsync.stop_scheduler()
        # 出站客户端是共享的（issue #144：Windows 上每次新建要 ~1 秒），
        # 退出时统一关掉，别把连接池与 SSL 上下文丢在原地。
        await config.close_clients()
        # 在清理末尾装一个**可用的**新执行器，让收尾阶段仍有地方提交阻塞工作。
        #
        # ## 为什么必须这么做
        #
        # asyncio 的默认执行器是**懒创建**的。如果整个 lifespan 从没排过阻塞任务，
        # 它就从未被创建，于是「循环关闭时关闭默认执行器」这一步是空操作 —— 之后
        # 收尾再提交时会新建一个，一切正常。这就是引入自定义池之前的行为。
        #
        # 从引入自定义池那一版（1.2.44）起，lifespan 一开始就把默认执行器换成了我们的池：
        # 它必然存在，于是收尾时被关闭；之后任何提交都抛「已关闭后仍要提交」。
        # 那个失败又打断归档线程的收尾，让后续检查连带报「归档线程已在运行」。
        #
        # 2026-11-14 审计：按版本二分确认，同一组检查在引入自定义池之前全部通过、
        # 之后固定七处失败；去掉那一行即恢复正常。
        #
        # ## 为什么是小池、且不显式关闭
        #
        # 这个阶段之后进程很快就退出了，线程由操作系统回收。它存在的窗口只有毫秒
        # 级，用途仅仅是让收尾提交有去处 —— 那里的阻塞工作本来就是一次性的。
        #
        # 不用 `set_default_executor(None)`：CPython 要求参数是 ThreadPoolExecutor
        # 实例，传 None 会抛 TypeError，反而让收尾多一处异常。
        try:
            from concurrent.futures import ThreadPoolExecutor
            asyncio.get_running_loop().set_default_executor(
                ThreadPoolExecutor(max_workers=2, thread_name_prefix='wb-final'))
        except (RuntimeError, ImportError):
            pass
        if pending_shutdown_cancel is not None:
            raise pending_shutdown_cancel


def _warn_if_exposed() -> None:
    """监听所有网卡时提醒：确保前面有反代，不要把 7864 直接暴露到公网。

    为什么只警告不自动改：标准部署（1Panel 反代到本机端口）与"直连公网"用的是
    同一个 0.0.0.0，自动改成 127.0.0.1 会把前者一起改坏。IP 伪造的问题已在
    `iputil.client_ip` 从源头修掉（只在 TCP 对端来自可信网段时才采信转发头），
    这里的提示是纵深防御——少一层暴露就少一类风险。
    """
    if config.HOST not in ('0.0.0.0', '::'):
        return
    logger.warning(
        '服务监听在 %s（所有网卡）：请确认 7864 端口没有直接暴露到公网，'
        '仅在反向代理后使用；直连会让来源 IP 类管控与登录锁定失去意义。',
        config.HOST,
    )


def _release_version() -> str:
    """Product version is independent of the inherited Manager baseline."""
    from .product import identity
    return 'v' + identity()['version']


app = FastAPI(
    title='智汇网关',
    version=_release_version(),
    lifespan=lifespan,
    # 生产环境默认关闭交互式文档与 OpenAPI 描述：
    # 它们会把管理接口全貌（路径、参数、结构）暴露给任何未认证访问者，
    # 便于攻击者摸清面。需要时设 WB_ENABLE_DOCS=1 打开。
    docs_url='/docs' if config.ENABLE_DOCS else None,
    redoc_url='/redoc' if config.ENABLE_DOCS else None,
    openapi_url='/openapi.json' if config.ENABLE_DOCS else None,
)

# qtgcore 的错误翻译器。
#
# `qtgcore/errors.py` 定义了 `QtgError` 及五个子类（BadRequest/Unauthorized/
# Forbidden/NotFound/UpstreamError），每个都带自己的 `status_code`，同一文件里
# 也写好了 `install_handlers(app)` 把它们挂到 FastAPI。
#
# **但这个函数从来没有被调用过。**全仓只有两处命中：定义处与一句 docstring。
# 结果是这些受控错误一路冒到 Starlette，被兜成 500 并打印 traceback ——
# 「缓存已更新」「上游不可用」这类本可识别的失败，客户端只看到「服务器内部错误」。
#
# 实测证据：`request_outcomes` 里 133 条 `gateway.application.error`，`error_stage`
# 全为 null；按端点集中在 `/api/stats/*`（控制台轮询下载缓存失效时抛的
# `UpstreamError(..., code='cache_invalidated')`）与 `/api/system/internal-session-verify`。
#
# 注意 `to_body()` 现在同时给 `detail` 与 `error` 两套字段：只给 `error` 会让
# 按 `detail` 取值的控制台与测试读不到文案（曾经试过，四个测试当场失败）。
from .qtgcore.errors import install_handlers as _install_qtg_error_handlers
_install_qtg_error_handlers(app)

app.include_router(budgets.router)

if config.CORS_ORIGINS:
    # 只允许**明确列出的**来源。绝不要把 WB_CORS_ORIGINS 设成 `*`：
    # 本应用用 Cookie 认证管理端，而 Starlette 在 allow_credentials=True 时
    # 会把 `*` 回显成请求方 Origin（不是字面 `*`），于是**任意网站**都能带着
    # 管理员的 Cookie 调 /api/* 并读到响应 —— 等于把控制台交给任何网页。
    # 留空（默认）即同源部署，安全。
    if any(o.strip() == '*' for o in config.CORS_ORIGINS):
        # v2.10.2：误配 `*` 等于把控制台交给任意网站，直接拒绝启动。
        raise RuntimeError(
            'WB_CORS_ORIGINS 含 `*` 且已启用 Cookie 凭据：请改为列出具体来源，'
            '或留空（同源部署，默认）。'
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )

# 管理端请求体上限。网关那条路有自己的逐块校验（gateway._read_json_body），
# 但 `/api/*` 此前**没有任何上限** —— 未鉴权的 `/api/login` 就能用大 body
# 把内存占住（实测：12MB 请求体被完整缓冲并解析，没有 413）。这里统一兜住。
# 取得比网关默认（8MB）宽松些：管理端有「保存上游配置」这类正常的大请求。
MAX_API_BODY_BYTES = 16 * 1024 * 1024

# 唯一需要更大请求体的管理端路径：上传离线升级包（issue #153）。发布包现在约
# 11 MB，将来会变大，所以这里给到 64 MB —— 仍然是个**硬上限**，不是没有上限：
# 上传本身还要过会话管理员鉴权，且落盘后立刻做 tar.gz 结构与 .version 校验。
# 与 `updater.PACKAGE_MAX_BYTES` 必须一致（有测试盯着，别只改一边）。
MAX_PACKAGE_BODY_BYTES = 64 * 1024 * 1024
PACKAGE_UPLOAD_PATHS = ('/api/system/update-package',)
from .portalregistration import BODY_LIMIT as REGISTRATION_BODY_LIMIT
# 登录只需要 `{username, password}`（几百字节）。在**未鉴权**的路径上沿用 16 MiB
# 上限，等于允许匿名请求把 16 MiB × APIBodyGuard 的并发名额（默认 32）≈ 512 MiB
# 缓冲打满，把整个管理面拖成 503。给登录一个独立的小上限：正常客户端远低于它，
# 恶意大 body 在声明长度阶段就被 413 拒掉，连缓冲都不进。
LOGIN_BODY_LIMIT = 64 * 1024
API_PATH_LIMITS={**{path:MAX_PACKAGE_BODY_BYTES for path in PACKAGE_UPLOAD_PATHS},
                 '/api/portal/register':REGISTRATION_BODY_LIMIT,
                 '/api/login':LOGIN_BODY_LIMIT}


app.add_middleware(drain.DrainMiddleware)


@app.middleware('http')
async def limit_api_body(request: Request, call_next):
    """给 `/api/*` 的请求体加上限（上传升级包那条路放宽，见上面的常量）。

    声明长度的快速拒绝；实际分块累计、总读取超时和缓冲容量由
    APIBodyGuard 在读取 JSON 前统一校验。
    """
    if request.url.path.startswith('/api/'):
        try:
            declared = int(request.headers.get('content-length') or 0)
        except ValueError:
            declared = 0
        limit = API_PATH_LIMITS.get(request.url.path.rstrip('/'),MAX_API_BODY_BYTES)
        if declared > limit:
            label=f'{limit//1024} KiB' if limit<1024*1024 else f'{limit//1024//1024} MiB'
            return JSONResponse(
                {'detail': f'请求体过大（上限 {label}）'},
                status_code=413,
            )
    return await call_next(request)

# ── 路由注册顺序很重要：先 API / 网关，最后挂静态文件 ──
app.include_router(auth.router)
app.include_router(portal.router)
app.include_router(portal.admin_router)
app.include_router(accounts.router)
app.include_router(keys.router)
# 红包：批量发放带额度的密钥（与密钥同属「分发」这件事，所以挨着放）
app.include_router(redpackets.router)
# 抽奖：**公开端点**（收到链接的人不需要账号），单独挂便于区分边界
app.include_router(redpackets.claim_router)
app.include_router(logs.router)
# 调用轨迹（抓包）。默认关闭，开启后正文落本地独立目录，不进数据库。
app.include_router(traffic_router.router)
app.include_router(stats.router)
app.include_router(security_router.router)
# 管理面作用域化 API Token（见 docs/api-tokens.md）；接口本身只接受会话鉴权
app.include_router(tokens.router)
app.include_router(settings.router)
# 代理线路与全局出口代理（设置 → 代理）
app.include_router(proxies.router)
# PostgreSQL 异地备份：把本地数据镜像出去、需要时拉回来（见 services/pgsync）
app.include_router(pgsync_router.router)
# 多上游（账号池分组）：密钥绑定上游 = 请求走那个池，见 upstreamsvc
app.include_router(upstreams.router)
app.include_router(system.router)
app.include_router(models.router)
# 内部只读 API 的版本/能力探测（K26）：QTG 先探测契约版本再决定能否读，
# 不可用时必须报错而不是回退直读 Manager SQLite。
app.include_router(read_api.router)
# 引擎侧数据（U3 阶段 2）：Manager 读 QTG 的 `/api/v1/adapter/*` 契约。
# 与上面 K26 的方向相反 —— 两者合起来才是双向通道：QTG 读 Manager 的账本，
# Manager 读 QTG 的引擎运行态；缺口在任一侧都必须是显式错误，不许回退直读对端库。
app.include_router(engine.router)
app.include_router(playground.router)
app.include_router(steward.router)


@app.get('/readyz', include_in_schema=False)
def public_ready():
    from . import operational
    ready = operational.public_readiness()
    return JSONResponse({'ok': ready}, status_code=200 if ready else 503)


@app.get('/api/release', include_in_schema=False)
def release_info():
    """Expose only the immutable release identity used by host cutover checks."""
    from . import product, release_identity
    metadata = product.metadata(config.ROOT, managed=True)
    return JSONResponse({
        'service': 'zhihui-gateway',
        'release_id': release_identity.current(),
        'version': metadata['version'],
        'source_commit': metadata['source_commit'],
    }, headers={'Cache-Control': 'no-store'})


@app.get('/api/health/alerts', include_in_schema=False)
def health_alerts(request: Request):
    """本机巡检用的告警快照（只读）。

    为什么单独开一个端点：应用内 18 项告警（备份、归档、预算对账、账本回执、
    门户账本……）只存在 `operational.alerts` 这个内存字典里，`transition()` 进入
    时写一条日志就不再重复，于是**持续 active 的告警在日志里只有开头一行**。
    人工跑巡检脚本时因此看不到它们，脚本报「全部正常」而实际有告警挂着。

    ## 边界

    **只允许回环地址**：巡检脚本在宿主机上 `curl 127.0.0.1`，外部拿不到。
    既有的 `/api/release` 无鉴权是因为它只暴露不可变的发布身份；告警名虽然
    也是内部术语，但没有必要对外开放，所以这里额外限定来源。

    **不返回 detail**：`detail` 里可能带账号名、上游错误原文。巡检只需要知道
    「哪一项挂着、挂了多久」，排查时再按名字去日志里找原文。
    """
    client = request.client.host if request.client else ''
    if client not in ('127.0.0.1', '::1', 'localhost'):
        return JSONResponse({'error': 'loopback only'}, status_code=403)
    from . import operational
    now = int(time.time())
    active = [{'name': name, 'age_seconds': max(0, now - int(item.get('since') or now))}
              for name, item in sorted(operational.alerts.items())]
    return JSONResponse({
        'service': 'zhihui-gateway',
        'count': len(active),
        'active': active,
    }, headers={'Cache-Control': 'no-store'})


@app.get('/api/readyz/detail', include_in_schema=False)
def readiness_detail(user: dict = Depends(security.require_panel_reader)) -> dict:
    """完整就绪诊断（需面板读者身份）。

    ## 为什么 `/readyz` 之外还要一个

    `/readyz` 只回一个布尔值，那是对的 —— 探针可能每秒打很多次，不能每次都去
    碰数据库（见 `public_readiness` 的说明，它读的是监控循环每 30 秒刷新一次的缓存）。

    但**不就绪时只看到一个 false，排障无从下手**：到底是数据库不可写、账本通道
    降级、回执有积压、还是正在排空？这四种情况的处置完全不同。

    这里跑一次完整的判定，把每一项判据都带出来。

    **只该在需要排障时调用**：它会真的去探数据库可写性、读回执通道状态，
    不是读缓存 —— 放在探测探针里会把健康检查变成写压力。
    """
    from . import operational
    return operational.readiness()


app.include_router(gateway.router)
# Anthropic Messages API 兼容层（/v1/messages）——给只认该协议的客户端用
app.include_router(anthropic.router)
# OpenAI Responses API 兼容层（/v1/responses、/responses）——给 Codex /
# DeepSeek Harness 的 openai-responses 协议用
app.include_router(responses.router)
# Qoder / Trae control plane (folded in from the standalone qtg service):
# same login as every other manager endpoint, no second credential set.
app.include_router(qtg_panel.router)

# Qoder/Trae compat control plane (was qtg:8795), mounted under an internal
# prefix so its /api/* handlers cannot collide with manager own endpoints.
# 2026-10-27 审计 H-001（第一步）：以下 11 个 router 文件的**全部端点**都不在
# _QTGP_ROUTE_PREFIXES 白名单内（67 条路由、命中 0 次），已整文件删除：
#   actions adapter anthropic auth authz capabilities data_admin health ip_guard read system
# proxy.py **未删**，但原因不是"还有人用"：它当年被 compat.py 函数级引用
# （_base / _catalog / _upstream_key），**该引用现已不存在** —— compat.py 里
# 没有任何 `_base(`/`_catalog(`/`_upstream_key`，全仓也没有 `from .proxy import`。
# 它现在是一份纯死代码，保留只因为删除属于一次独立的清理。
# 注意它内部 `_base`/`_upstream_key` 的 if/elif 只有 qoder/trae 两个分支，
# doubaowork 会落到 trae（拿 Trae 的地址与密钥）——真有人接线前必须先补分支。
from .qtgcore.routers import (compat as _qc,
    compat_accounts_ext as _qace, compat_fixes as _qcf, compat_settings as _qcs,
    compat_stats as _qst)
_QTG_INTERNAL = "/__qtg"
# Only PLATFORM DATA endpoints are routed to the merged qtg control plane.
# Everything else (login/session/users/tokens/system/settings/keys/security)
# MUST stay on manager: nginx auth_request calls /api/system/internal-session-verify
# with the same X-QTG-Platform header, and rewriting it yields a 501 there which
# nginx turns into a hard 500 for the real request.
_QTGP_ROUTE_PREFIXES = ("/api/accounts", "/api/stats/", "/api/models",
                        "/api/model-catalog", "/api/upstreams", "/api/platforms",
                        "/api/status", "/api/checkin-logs", "/api/logs",
                        "/api/playground")

def _qtg_should_route(path):
    if not path.startswith("/api/"):
        return False
    for _p in _QTGP_ROUTE_PREFIXES:
        # H-010：必须按路径段边界匹配。/api/logs 这类无尾斜杠的前缀此前用
        # startswith 会让 /api/logsXXX 也命中，使"哪些路径走 qtgcore"不可静态推断。
        _base = _p.rstrip("/")
        if path == _base or path.startswith(_base + "/"):
            return True
    return False
# Order matters: qtg registers compat LAST (catch-all fallback, see
# qtg/routeaudit.py REGISTRATION_ORDER); real handlers must come first,
# otherwise /api/accounts hits compat not_implemented stub.
for _qr in (_qcf, _qst, _qcs, _qace, _qc):
    app.include_router(_qr.router, prefix=_QTG_INTERNAL)

@app.middleware('http')
async def _qtg_platform_route(request, call_next):
    from fastapi.responses import JSONResponse
    path = request.url.path
    if path.startswith(_QTG_INTERNAL) and not getattr(request.state, "qtg_rewritten", False):
        return JSONResponse({'detail': 'not found'}, status_code=404)
    platform = (request.headers.get("x-qtg-platform") or "").strip().lower()
    if not platform:
        # H-001 / B1（2026-10-27 审计）：平台选择同时接受 `?platform=` 查询参数。
        # 迁移理由：用请求头承载业务语义，URL 里看不出来、缓存与日志无法区分两种
        # 响应、任何中间层都可能丢掉它，前端只能靠全局拦截器注入。
        # 兼容策略：**header 优先**，缺省时才看 query —— 旧前端零影响。
        try:
            platform = (request.query_params.get("platform") or "").strip().lower()
        except Exception:  # noqa: BLE001  取不到就当没有
            platform = ""
    # 这里列出「数据面由 qtgcore 托管」的平台。workbuddy 不在其中——它走
    # manager 自己的原生实现（accounts.py / status 等），这是历史分工。
    # doubaowork 必须在这里：它的账号/模型/统计只在 qtgcore 的 compat 层实现过，
    # 漏掉这一项会让豆包工作的请求落到 manager 的 WorkBuddy handler 上
    # （显示 WorkBuddy 的账号池，写操作也会写进 WorkBuddy 的目录）。
    #
    # 2026-11-19：qoder 已摘除（官方不再送积分），从白名单移除。它现在属于
    # 「可识别但不可用」（`platformpolicy._ACCEPTED`），请求会落到下游由
    # `runtime.qoder is None` 给出明确的「平台已下线」，而不是被这里静默
    # 转发到 WorkBuddy 的 handler 上。
    # `/api/platforms` 是**唯一**与「当前选中平台」无关的 qtg 端点：它的语义
    # 就是「把所有平台的能力列出来」，调用方（前端 `platformApi.list()`）**从不**
    # 带 platform 参数。
    #
    # 2026-11-20 修：此前它被下面那句 `platform in ("doubaowork", "trae")` 挡住 ——
    # 没有 platform 头 → 不进 qtgcore → 落到 manager 的兜底 404。
    # 实测 `GET /api/platforms` 返回 `{"detail":"not found"}`，而这个接口是
    # 「平台能力矩阵」的唯一来源，前端拿不到就只能把三个平台的按钮全按默认渲染。
    # 带参数时反而是好的（`?platform=trae` → 200），说明实现没问题，只是进不来。
    #
    # 这里只放行这一个精确路径，且**不改变**其它任何路径的分流结果 ——
    # manager 侧没有 `/api/platforms` 的 handler（已 grep 确认），所以放行它
    # 不会抢走任何既有实现，只是把一个原本 404 的端点接通。
    _is_platform_matrix = path.rstrip("/") == "/api/platforms"
    if (platform in ("doubaowork", "trae") or _is_platform_matrix) and _qtg_should_route(path):
        try:
            # Identity resolution may wait on a file/SQLite lock. Keep that
            # blocking work off the loop that also delivers inference streams.
            _user = await joined_thread(security.current_user, request)
        except Exception as _exc:
            # H-011：区分「鉴权失败」与「内部错误」。此前一律 401，导致
            # DB 抖动 / 用户文件读失败 / 磁盘满都表现为"未登录"——用户被莫名
            # 踢到登录页，监控上只见大量 401，真实故障（5xx 级）被完全掩盖。
            if getattr(_exc, 'status_code', 500) == 401:
                return JSONResponse({'detail': 'auth required'}, status_code=401,
                                    headers={'WWW-Authenticate': 'Bearer'})
            if getattr(_exc, 'status_code', 500) == 403:
                return JSONResponse({'detail': getattr(_exc, 'detail', 'Forbidden')},
                                    status_code=403, headers=getattr(_exc, 'headers', None))
            logger.exception('qtg 平台路由：身份解析出现内部错误')
            return JSONResponse({'detail': 'internal error'}, status_code=500)
        import time as _t
        from .qtgcore.identity import Identity as _I
        _role = 'admin' if _user.get('role') == 'admin' else 'viewer'
        # ── S0 授权闸门（H-000 / B-001 / D-001，2026-10-27 审计修复）────────
        # qtgcore 的 handler 签名里没有任何 Depends 授权依赖，而 manager 原生
        # 同路径 handler 全部有 require_admin。若不在此拦，已登录的 viewer 与
        # scope=read/keys 服务令牌都能通过 X-QTG-Platform 头执行写操作
        # （实测 viewer 会话成功对真实账号完成签到）。
        #
        # 判据：写方法必须同时满足「cookie 身份 role=admin」且「不是服务令牌」
        # （_token_identity 会设 request.state.token_id；cookie 路径不设）。
        # 读方法（GET/HEAD/OPTIONS）行为不变，面板查看不受影响。
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            _is_privileged = (_user.get('role') == 'admin'
                              and not getattr(request.state, 'token_id', None))
            if not _is_privileged:
                return JSONResponse({'detail': '需要管理员权限'}, status_code=403)
        _now = int(_t.time())
        request.state.identity = _I(sub=str(_user.get("username") or "manager"),
            role=_role, scopes=frozenset({"*"} if _role == "admin" else {"read"}),
            auth_method="manager_session", aud="qtg", exp=_now + 60,
            iat=_now, iss="manager", jti="")
        # H-001/B3（2026-10-27 审计）：平台解析结果落到请求上下文，
        # qtgcore 统一读 state（identity.platform_of），不再各自解析请求头。
        request.state.qtg_platform = platform
        request.state.qtg_rewritten = True
        request.scope["path"] = _QTG_INTERNAL + path
        request.scope["raw_path"] = (_QTG_INTERNAL + path).encode()
        # H-001/B1（2026-10-27 审计）：平台若来自 `?platform=`（客户端没发请求头），
        # 这里把它补进 scope 的 headers —— qtgcore 内部有 10+ 处独立读
        # `x-qtg-platform`（compat.py 8 处、authz.py、proxy.py），不补的话它们
        # 读到空值就按「不过滤」处理，返回全部平台数据（实测 total 从 1 变 34）。
        # 注意：这只是让两种入口行为一致；真正统一语义要改那 10+ 处为显式参数。
        if not (request.headers.get("x-qtg-platform") or "").strip():
            _h = [(k, v) for k, v in request.scope.get("headers") or []
                  if k.lower() != b"x-qtg-platform"]
            _h.append((b"x-qtg-platform", platform.encode("latin-1")))
            request.scope["headers"] = _h
    return await call_next(request)


@app.middleware('http')
async def cache_headers(request: Request, call_next):
    """按内容性质设置缓存策略与安全响应头。

    - /_next/static/**：文件名含内容哈希，可长期强缓存（immutable）
    - /api/**、/v1/**：动态数据，禁止任何缓存（含浏览器与中间代理）
    - 其余（HTML 文档）：no-cache，即每次回源校验 ETag，避免拿到旧页面

    安全头说明：
    - X-Content-Type-Options: 阻止浏览器嗅探类型（防内容被当作脚本执行）
    - X-Frame-Options / frame-ancestors: 禁止被其他站点内嵌（防点击劫持）
    - Referrer-Policy: 跨站请求不带完整 URL（避免泄露路径）
    - CSP: 只允许同源资源与内联样式（前端使用内联样式属性）；
      限制外联目标，降低 XSS 得手后的影响面
    """
    original_path = request.url.path
    response = await call_next(request)

    # 会话滑动续期：current_user 判定「该续了」时在这里重签 cookie。
    #
    # 为什么放中间件而不是 current_user 里：那个函数是 FastAPI 依赖，只负责
    # **解析身份**，在它里面写响应会耦合出不必要的层，而且它被大量接口调用、
    # 有的还是只读的。中间件是唯一能看到「响应对象 + 请求状态」的地方。
    cookie_already_set = any(
        value.split('=', 1)[0].strip() == config.COOKIE_NAME
        for value in response.headers.getlist('set-cookie')
    )
    if (getattr(request.state, 'session_renew', False)
            and response.status_code < 400 and not cookie_already_set):
        try:
            token = request.cookies.get(config.COOKIE_NAME) or ''
            def renew_cookie():
                cfg = security.load_users()
                obj = security._unsign(token, cfg['secret']) or {}
                username = str(obj.get('username') or '')
                if not username:
                    return None
                # orig 原样延续 —— 续期只推后**空闲截止**，不延长**总寿命**。
                # role 传空：鉴权一律以用户表为准（载荷里的 role 仅作展示参考）。
                return security.issue_token(
                    username, '', orig=int(obj.get('orig') or 0) or None,
                    expected_sv=int(obj.get('sv') or 0),
                )
            # File/authorization locks can wait behind an administrator update.
            # Other streams must keep progressing while this cookie is renewed.
            fresh = await joined_thread(renew_cookie)
            if fresh:
                response.set_cookie(
                    config.COOKIE_NAME, fresh,
                    max_age=config.SESSION_DAYS * 86400, httponly=True,
                    samesite='lax', secure=security.cookie_secure(request), path='/',
                )
        except security.HTTPException:
            # Revoked/deleted while the request was running: do not renew it.
            pass
        except Exception as exc:  # noqa: BLE001
            # 续期失败不能让请求失败 —— 用户这次照常，只是下次要重登
            logger.warning('会话续期失败（不影响本次请求）: %s', exc)

    path = original_path
    if path.startswith('/_next/static/'):
        # 文件名含内容哈希，内容变了文件名就变，可长期强缓存
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif path in {'/metrics', '/readyz', '/responses', '/responses/'} or path.startswith(('/api/', '/v1/', '/v2/', '/healthz', _QTG_INTERNAL + '/api/')):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
    elif path.startswith('/favicon/') or path.endswith(('.png', '.ico', '.svg', '.woff2', '.webmanifest')):
        # 图标 / 字体等静态资源，内容基本不变，缓存一天
        response.headers['Cache-Control'] = 'public, max-age=86400'
    else:
        response.headers['Cache-Control'] = 'no-cache'

    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        # object-src 必须显式写 'none'：只靠 default-src 'self' 回退时，
        # 同源的 <object>/<embed> 仍可加载插件内容（CSP3 §6.1 推荐显式声明）。
        # 本应用没有任何插件内容，收紧到 none 无兼容成本。
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'",
    )
    return response


@app.get('/api/sysinfo')
def sysinfo(user: dict = Depends(security.current_user)) -> dict:
    """服务信息。需要登录 —— 路径类信息不应对未认证访问者暴露。"""
    return {
        'service': 'zhihui-gateway',
        'version': app.version,
    }


# ── 静态前端（Next.js 静态导出）────────────────────────
def _looks_like_traversal(full_path: str) -> bool:
    """判断请求路径是否像目录穿越尝试（用于记日志，不参与拦截决策）。

    拦截一律由 `_safe_static_path` 的包含性校验负责，这里只是让安全事件
    留下痕迹：静态路径原本不记录任何访问日志，万一被利用了也无从发现。
    """
    p = (full_path or '').replace(chr(92), '/')
    return '..' in p.split('/') or p.startswith('/') or ':' in p


def _safe_static_path(full_path: str) -> Path | None:
    """把请求路径解析为静态目录下的真实文件；越界或非法一律返回 None。

    安全（重要）：这里曾直接把请求路径拼到 STATIC_DIR 上，未做任何越界校验。
    由于 ASGI 会先对 %2f 解码，`/..%2f..%2fdata%2fusers.json` 这类请求
    在 `Path / str` 拼接后指向了部署目录**之外**，导致任意文件读取——
    实测可读到 `users.json`（内含签发会话的 secret，可据此伪造 admin 会话）、
    `.env`、上游 `config.json`（api_key）与 `auths/*.json`（账号 accessToken）。

    修法：用 `resolve()` 归一化后，强制要求结果仍位于 STATIC_DIR 之内。
    这是防目录穿越的标准做法，能同时覆盖 `..`、编码斜杠、绝对路径与
    符号链接等各变体；不合规的直接当作 404，不泄露任何信息。
    """
    if not full_path:
        return None
    try:
        root = config.STATIC_DIR.resolve()
        # 绝对路径（如 full_path 以 / 开头或形如 C:\...）会被 Path 当作新根，
        # 这里先剥掉前导分隔符，再统一在后面做包含性校验。
        candidate = (root / full_path.lstrip('/\\')).resolve()
    except (OSError, ValueError, RuntimeError):
        # resolve() 在符号链接成环等情况下可能抛错：一律视为不可访问
        return None
    if candidate != root and root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def _is_document_request(request: Request) -> bool:
    """这次请求是不是「把结果当页面显示」的文档型导航。

    浏览器的地址栏导航会带 `Accept: text/html,...`；而客户端路由抓 RSC 数据
    用的是 `fetch`（`Accept: */*` 或 `text/x-component`）并带 `RSC: 1` 头。
    两者必须分开：前者拿到的若是 flight 文本，用户看到的就是满屏原始数据。
    """
    if request.headers.get('rsc'):
        return False
    return 'text/html' in (request.headers.get('accept') or '').lower()


def _rsc_page_for(full_path: str) -> str | None:
    """文档型请求命中 Next 静态导出的 RSC 数据文件时，返回它对应的页面路径。

    `output: 'export'` 会把每个页面的 RSC（flight）数据写成
    `<页>/index.txt`（实测：`dashboard/index.txt`、`settings/index.txt`…）。
    正常客户端拿它做客户端路由的数据源；但 router 的兜底分支
    （`Failed to fetch RSC payload … Falling back to browser navigation`）
    会把浏览器**整页导航**到这个地址，于是浏览器按 `text/plain` 渲染那份
    flight 数据、地址栏也变成 `.txt`，刷新只会继续显示它（issue #48）。

    **只有对应页面确实存在时才返回**：`.txt` 也可能是真实静态文件
    （如 `robots.txt`），那种没有同名页面，不能一并重定向。

    两种导出形态都要认：`<页>/index.html`（绝大多数页面，实测 dashboard 等）
    与 `<页>.html`（根页面就是这种：`index.html` + `index.txt`）。少认一种，
    对应形态的页面在真机上就会继续显示原始数据。
    """
    p = (full_path or '').strip('/')
    if not p.endswith('.txt'):
        return None
    stem = p[:-4]
    if stem.endswith('/index'):
        stem = stem[: -len('/index')]
    elif stem == 'index':
        stem = ''
    candidates = (f'{stem}/index.html' if stem else 'index.html',
                  f'{stem}.html' if stem else 'index.html')
    if not any(_safe_static_path(c) is not None for c in candidates):
        return None          # 没有同名页面 → 是真实文件，按普通静态资源处理
    return f'/{stem}' if stem else '/'


if config.STATIC_DIR.is_dir():
    app.mount('/_next', StaticFiles(directory=str(config.STATIC_DIR / '_next')), name='next-assets')
    if (config.STATIC_DIR / 'favicon.ico').exists():
        @app.get('/favicon.ico', include_in_schema=False)
        def favicon() -> FileResponse:
            return FileResponse(config.STATIC_DIR / 'favicon.ico')

    @app.get('/', include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(config.STATIC_DIR / 'index.html')

    @app.get('/{full_path:path}', include_in_schema=False)
    def spa(full_path: str, request: Request):
        if full_path == "metrics":
            return prometheus_metrics(request)
        # H-004：/api/* 走到这里意味着「没有任何业务路由匹配」（路径不存在，
        # 或路径存在但方法不对被 Starlette 跳过后落到本兜底）。此前会返回
        # 11777 字节的 404.html，前端 res.json() 抛 SyntaxError 被伪装成
        # "数据加载失败"，真实原因完全被掩盖。这里明确回 JSON，
        # 并给出 method 已注册时正确的 405 语义。
        if full_path == "api" or full_path.startswith("api/"):
            _path = "/" + full_path
            _allow = sorted({
                m for r in app.routes
                for m in (getattr(r, "methods", None) or ())
                if getattr(r, "path", None) == _path and m not in ("HEAD", "OPTIONS")
            })
            if _allow:
                return JSONResponse(
                    {"detail": "method not allowed", "allow": _allow},
                    status_code=405, headers={"Allow": ", ".join(_allow)})
            return JSONResponse({"detail": "not found"}, status_code=404)
        # 优先命中导出的静态页面 / 资源，否则回退到 404 页面。
        # 所有路径都必须先通过 _safe_static_path（越界即 None → 404）。
        #
        # 越界尝试会记一条 WARN 日志：这类请求 100% 是攻击或扫描行为，
        # 之前不记录任何痕迹，出事后无从追溯。日志只写路径，不含内容。
        if _looks_like_traversal(full_path):
            logger.warning('拦截疑似路径穿越请求: %r', full_path[:300])
        # ① 文档型请求命中 RSC 数据文件 → 送回对应页面（issue #48）。
        # 见 `_rsc_page_for` 的说明：客户端路由的兜底分支会把浏览器整页导航到
        # `<页>/index.txt`，那边返回的是 flight 文本，用户看到满屏原始数据、
        # 地址栏也变成了 .txt，只能手动改回地址。
        if _is_document_request(request):
            page = _rsc_page_for(full_path)
            if page is not None:
                # 补上子路径前缀：反代剥掉前缀后才到这里，但 Location 是发回浏览器
                # 的绝对地址，不带前缀就会跳到域名根（另一个站点）上。
                return RedirectResponse(f'{config.BASE_PATH}{page}', status_code=302)
        target = _safe_static_path(full_path)
        if target is not None:
            return FileResponse(target)
        index_candidate = _safe_static_path(f'{full_path}/index.html')
        if index_candidate is not None:
            return FileResponse(index_candidate)
        not_found = config.STATIC_DIR / '404.html'
        if not_found.is_file():
            return FileResponse(not_found, status_code=404)
        # 静态目录里连 404.html 都没有时的最后兜底。
        #
        # 2026-11-25：这里原来是 `{'error': 'not found'}` —— 全仓唯一一处把
        # `error` 写成裸字符串的地方。其余所有错误响应（含 401/403/404/405/422）
        # 的 `error` 都是 `{code, message, type}` 对象，客户端按 `error.code`
        # 取值时，走到这条路径会拿到 `undefined`，进而把它当成"未知错误"。
        #
        # `detail` 保持原样，因为前端与既有断言按它取值；补充 `error` 对象
        # 让两条读取路径都能工作。
        return JSONResponse(
            {
                'detail': 'not found',
                'error': {
                    'code': 'not_found',
                    'message': 'not found',
                    'type': 'api_error',
                },
            },
            status_code=404,
        )


# ── 子路径部署（反向代理前缀）────────────────────────────────────────
#
# 反向代理把本站挂在 /workbuddy-manager 这类前缀下、且**不剥离**前缀时，后端
# 收到的路径仍带着前缀 —— 路由、静态挂载与 SPA 兜底全都匹配不上，页面会 404。
# 这里在最外层统一剥掉，让内部逻辑只看到「前缀之后的路径」。
#
# 反向代理若已经剥离了前缀（proxy_pass 带 URI 的常见写法），本中间件找不到
# 前缀、原样放行 —— 两种反代配置都能工作，nginx 侧不必改。
#
# 响应侧同步处理：SPA 的 RSC 兜底等场景会 302 到站内绝对路径（`/xxx`），
# 不补回前缀就会把用户带出子路径、落到站点根目录。
#
# 与 `config.BASE_PATH` 的分工：那个值被用来**主动构造**带前缀的绝对地址
# （见上面 SPA 兜底的 RedirectResponse）；本中间件负责「进来的路径带前缀」与
# 「出去的 Location 漏前缀」这两件事。两者都指同一个 WB_BASE_PATH。
class StripBasePathMiddleware:
    """纯 ASGI 中间件：剥离请求前缀，并把响应里的站内绝对位置补回前缀。"""

    def __init__(self, app, prefix: str) -> None:
        self.app = app
        self.prefix = prefix
        self._prefix_slash = prefix + '/'

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        path = scope.get('path') or '/'
        if path == self.prefix or path.startswith(self._prefix_slash):
            stripped = path[len(self.prefix):] or '/'
            scope = dict(scope)
            scope['path'] = stripped
            scope['raw_path'] = stripped.encode('utf-8')

        async def send_with_prefix(message):
            if message.get('type') == 'http.response.start' and message.get('headers'):
                message = dict(message)
                message['headers'] = [
                    (k, self._rewrite_location(v) if k.lower() == b'location' else v)
                    for k, v in message['headers']
                ]
            await send(message)

        await self.app(scope, receive, send_with_prefix)

    def _rewrite_location(self, value: bytes) -> bytes:
        try:
            text = value.decode('latin-1')
        except Exception:  # noqa: BLE001
            return value
        # 只处理站内绝对路径（/ 开头，且不是 //host 这种协议相对写法）
        if not text.startswith('/') or text.startswith('//'):
            return value
        # 已经带前缀的不重复叠加（SPA 兜底那处已自己补过）
        if text == self.prefix or text.startswith(self._prefix_slash):
            return value
        return (self.prefix + text).encode('latin-1')


from .bodyguard import APIBodyGuard
# N39：bodyguard 的实际上限必须与上面的声明长度预检一致，否则会出现
# "声明 64 MiB 放行、读取到 16 MiB 被 413"（实测 17 MiB 升级包传不上去）。
app.add_middleware(
    APIBodyGuard,
    maximum=MAX_API_BODY_BYTES,
    path_limits=API_PATH_LIMITS,
    path_timeouts={'/api/portal/register':5},
)

if config.BASE_PATH:
    # add_middleware 后注册的在外层，所以这行放在文件末尾：请求进来先过它，
    # 后面的限流 / 缓存头 / 路由看到的都是剥离后的路径。
    app.add_middleware(StripBasePathMiddleware, prefix=config.BASE_PATH)
    logger.info('子路径部署：已启用前缀 %s（反代可保留或自行剥离，两者都可用）', config.BASE_PATH)

from .observability import RequestAuditMiddleware
app.add_middleware(RequestAuditMiddleware)


# ── 三平台 URL 前缀（2026-10-23）───────────────────────────────────────────
# /qoder/v1/* 与 /trae/v1/* 各自锁定一个上游：路径重写成 /v1/* 后复用同一条
# 鉴权 / 限流 / 预算 / 记账链路，模型名保持各平台原生（Auto、glm-5.3-flash）。
# 注册在文件末尾 = 包在最外层，路由看到的就是重写后的路径。
from .routers import gateway as _threeway_gateway


from .platformpolicy import PlatformRoutingMiddleware
app.add_middleware(PlatformRoutingMiddleware, base_path=config.BASE_PATH)



# ── Prometheus 指标（2026-10-23 审计补充：manager 之前完全没有 /metrics）──────
# Application authentication is mandatory even behind a proxy or static fallback.
# Collectors use an independent credential; normal readonly/keys tokens do not qualify.
def _cgroup_memory_peak_bytes() -> int | None:
    """本服务 cgroup 的内存峰值字节数；取不到返回 None。

    ## 为什么峰值比当前值更有用

    `workbuddy_process_resident_bytes` 是**此刻**的常驻内存，它会来回波动 ——
    一次大请求把内存顶上去、请求结束又落回来，曲线看不出异常。

    **峰值只会涨，不会落。** 所以：

    | 观察 | 含义 |
    | --- | --- |
    | 峰值与当前值同步缓慢上涨 | 可能是泄漏 |
    | 峰值远高于当前值且不再变化 | 曾经有过一个大尖峰（大 body / 长上下文） |
    | 两者一起回落 | 不可能 —— 峰值不回落，回落的是当前值 |

    有了这两条曲线，「有没有在慢慢胀」才成为一个能看图回答的问题，而不是靠几天后
    发现 OOM。

    ## 为什么读 cgroup 而不是自己记账

    `memory.peak` 由内核维护，包含进程的匿名页、页缓存和内核开销 —— 那正是
    OOM killer 看的口径。自己记账只会漏掉一部分。

    需要 cgroup v2 与内核 5.19+；取不到（老内核、容器内没有该文件）时返回 None，
    指标不输出，不报假的 0。
    """
    try:
        import os
        with open(f'/sys/fs/cgroup{_cgroup_scope()}/memory.peak', encoding='ascii') as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _cgroup_scope() -> str:
    """当前进程所在 cgroup 的相对路径（形如 `/system.slice/xxx.service`）。

    取不到就返回空串，让调用方拼出 `/sys/fs/cgroup/memory.peak` —— 那在容器里
    往往是对的（cgroup 命名空间已经把根指向本容器）。
    """
    try:
        with open('/proc/self/cgroup', encoding='ascii') as handle:
            for line in handle:
                parts = line.strip().split(':', 2)
                if len(parts) == 3 and parts[0] == '0':
                    return parts[2].rstrip('/')
    except OSError:
        pass
    return ''


def _process_memory_bytes() -> tuple[int, int] | None:
    """(常驻内存, 虚拟内存) 字节数；取不到返回 None。

    ## 为什么需要它

    网关是常驻进程，跑几天会不会慢慢胀、有没有泄漏，**没有任何指标能看出来**。
    `/metrics` 此前只有「在不在服务」和「跑了多久」两个进程级指标 —— 也就是说
    只有崩了才发现，中间那段缓升完全不可见。

    加趋势指标比加告警更有用：先能看见曲线，才谈得上判断哪段斜率不正常。

    ## 为什么读 /proc 而不是引入依赖

    只需要两个数字。`/proc/self/statm` 是每页计数，乘页面大小即可；不引 `psutil`
    这类依赖，避免为两行数据增加一个包。取不到（非 Linux、或 /proc 不可读）时返回
    None，指标干脆不输出 —— 总比报一个假的 0 强。
    """
    try:
        with open('/proc/self/statm', encoding='ascii') as handle:
            fields = handle.read().split()
        page_size = os.sysconf('SC_PAGE_SIZE')
        virtual_pages = int(fields[0])
        resident_pages = int(fields[1])
        return resident_pages * page_size, virtual_pages * page_size
    except (OSError, ValueError, IndexError, AttributeError):
        return None


#: 分位数查询的缓存槽（按列名分开）。
#:
#: 查询本身很便宜（时间字段有索引，实测个位数毫秒），
#: 缓存不是为了省查询，是**避免并发抓取时把同一份数据算很多遍** ——
#: 监控系统常见行为是多个实例同时抓同一个端点。
_COLUMN_CACHES: dict[str, dict] = {}


def _column_quantiles(column: str, window_seconds: int = 3600) -> dict | None:
    """某个数值列在窗口内的分位数，带 30 秒缓存。

    ## 为什么抽成一个函数

    首字延迟和上下文长度需要**完全一样**的查询形状（窗口内排序取分位、
    样本不足不报、失败返回空）。复制一份等于以后要改两处，
    而这两处一定会有人只改一处。

    ## 列名必须白名单

    `column` 会被**拼进 SQL**（列名不能用占位符参数化），
    所以它只能是下面这几个内部常量，**绝不接受任何外部输入**。
    不在名单里就直接返回空 —— 拼错的列名会让整条指标消失，
    而不是让端点报错，那样更难发现。

    ## 样本不足时不报分位值

    少于 20 个样本时只报样本数，**分位值留空**。

    10 个样本算出来的 p99 是那 10 个里最大的一个，把它当分位数展示
    会让人以为看到了长尾，实际上只是看了最小值集。
    **宁可空着，也不要给一个看起来像结论的数字。**
    """
    #: 列名 → 缓存槽。新增列必须登记在这里，否则查不了。
    allowed = {'first_token_ms': 'ttft', 'prompt_tokens': 'context'}
    slot = allowed.get(column)
    if slot is None:
        return None
    cache = _COLUMN_CACHES.setdefault(slot, {'at': 0.0, 'value': None})
    now = time.monotonic()
    cached = cache['value']
    if cached is not None and 0 <= now - cache['at'] <= 30:
        return cached
    try:
        rows = db.query(
            'SELECT %s AS v FROM request_logs WHERE ts > ? AND %s IS NOT NULL '
            'ORDER BY %s' % (column, column, column),
            (int(time.time()) - window_seconds,))
        values = [int(row['v']) for row in rows]
    except Exception:      # noqa: BLE001
        # 读不到就报不了，不能让指标端点因此 500。
        cache.update(at=now, value=None)
        return None
    result = {'samples': len(values), 'p50': None, 'p95': None, 'p99': None, 'max': None}
    if len(values) >= 20:
        def _pick(q: float) -> int:
            return values[min(len(values) - 1, int(len(values) * q))]
        result.update(p50=_pick(0.50), p95=_pick(0.95), p99=_pick(0.99),
                      max=values[-1])
    cache.update(at=now, value=result)
    return result


def _ttft_quantiles(window_seconds: int = 3600) -> dict | None:
    """最近窗口内的首字延迟分位数。

    ## 为什么需要

    2026-11-15 查性能时发现：平均首字延迟 7.3 秒、p99 57 秒，
    而**这些数字当时只存在于数据库里** —— 看板看不见，也就没有人会盯。

    两个账号的首字延迟接近 40 秒（其他 6–13 秒），差距六倍，
    **直接把 p99 拉高**。这类「少数账号拖垮长尾」的问题，
    不上趋势图就只能等用户来投诉。

    实现在 `_column_quantiles`，这里只固定列名。
    """
    return _column_quantiles('first_token_ms', window_seconds)


def _context_quantiles(window_seconds: int = 3600) -> dict | None:
    """最近窗口内的上下文长度（prompt tokens）分位数。

    ## 为什么需要

    2026-11-15 有反馈说长对话跑到后面会开始重复同一段内容。
    查下去网关侧三项都排除了（不裁剪历史、不注入系统消息、不丢推理内容），
    但数据面看到一个强信号：

        近三小时平均 prompt_tokens 31 万，**最大 50 万**，
        还有一个请求输出了 2 万 token / 93 秒。

    **正常回答不会有两万字。** 那几乎可以确定是在重复。

    问题在于：**这个数字当时没有任何地方能看到**。要发现「对话在膨胀」，
    只能去翻数据库。等到它表现为循环，已经晚了。

    这个指标的作用是让膨胀**先于故障可见** —— 上下文一路上涨时，
    趋势图会先抬头，而不是等对话崩掉。

    ## 为什么不设「太大」的阈值

    不同模型的上下文上限不同，而且上游随时会调。
    写死一个阈值，要么太松没意义、要么太紧天天误报。
    **只报分位数和最大值，让看的人按自己的模型判断。**
    """
    return _column_quantiles('prompt_tokens', window_seconds)


#: 按账号的 TTFT 缓存槽。键存在这里而不是复用 `_COLUMN_CACHES`，是因为后者的
#: 形状是「一条分位数」，而这里是「每个账号一条」。
_ACCOUNT_TTFT_CACHE: dict = {'at': 0.0, 'value': None}


def _prom_label(value: str) -> str:
    """Prometheus 标签值转义。

    账号名来自数据库、可能带引号或反斜杠；不转义会**直接生成一行坏掉的暴露文本**
    —— 要么整条序列被 Prometheus 丢弃，要么把后面的行也带坏。
    """
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _account_ttft(window_seconds: int = 3600, min_samples: int = 20,
                  max_accounts: int = 32) -> list[dict] | None:
    """最近窗口内**按账号**的首字延迟分位数。

    ## 为什么需要（不是"再做一个分位数"）

    2026-11-15 排性能时看到一个数字：平均首字延迟 7.3 秒、p99 57 秒，而当时
    **只有全局分位数**。进一步查下去才发现，两个账号的首字延迟接近 40 秒，
    其他账号是 6–13 秒 —— 少数账号把 p99 整个拉高了。

    全局 p99 只能告诉你"慢"，不能告诉你"谁慢"。而账号池的调度、权重、下线决策
    全是按账号做的：不知道是哪个账号在拖，就只能靠用户投诉来定位。

    ## 边界

    * **样本不足的账号不出现**（< `min_samples`）。一个只有 3 次调用的账号算出来的
      p99 就是那 3 次里最大的一个，把它画到图上等于制造结论。
    * **最多导出 `max_accounts` 个**（按 p99 从慢到快），防止账号数量增长时
      序列基数失控 —— 指标是用来盯趋势的，不是用来替代数据库查询的。
    * 读失败返回 `None`（指标端点不能因此 500），并且**不缓存失败**：下一次抓取重试。
    """
    now_mono = time.monotonic()
    cached = _ACCOUNT_TTFT_CACHE['value']
    if cached is not None and 0 <= now_mono - _ACCOUNT_TTFT_CACHE['at'] <= 60:
        return cached
    try:
        rows = db.query(
            'SELECT account, first_token_ms AS v FROM request_logs '
            'WHERE ts > ? AND first_token_ms IS NOT NULL '
            "AND account IS NOT NULL AND account <> ''",
            (int(time.time()) - window_seconds,))
    except Exception:      # noqa: BLE001
        _ACCOUNT_TTFT_CACHE.update(at=now_mono, value=None)
        return None
    buckets: dict[str, list[int]] = {}
    for row in rows:
        try:
            buckets.setdefault(str(row['account']), []).append(int(row['v']))
        except (TypeError, ValueError):
            continue
    out: list[dict] = []
    for name, values in buckets.items():
        if len(values) < min_samples:
            continue
        values.sort()
        last = len(values) - 1
        pick = lambda q, v=values, last=last: v[min(last, int(len(v) * q))]  # noqa: E731
        out.append({'account': name, 'samples': len(values),
                    'p50': pick(0.50), 'p95': pick(0.95), 'p99': pick(0.99),
                    'max': values[-1]})
    # 慢的排前面：看板第一眼看到的就是要处理的账号。
    out.sort(key=lambda r: (-r['p99'], r['account']))
    if len(out) > max_accounts:
        out = out[:max_accounts]
    _ACCOUNT_TTFT_CACHE.update(at=now_mono, value=out)
    return out


@app.get('/metrics')
def prometheus_metrics(request: Request):
    from .metricsauth import require_metrics
    require_metrics(request)
    from fastapi.responses import PlainTextResponse

    from .runtimemetrics import METRICS
    lines: list[str] = METRICS.render()

    def _m(name: str, mtype: str, help_: str, value) -> None:
        if not any(x.startswith('# HELP ' + name + ' ') for x in lines):
            lines.append('# HELP %s %s' % (name, help_))
            lines.append('# TYPE %s %s' % (name, mtype))
        lines.append('%s %s' % (name, value))

    _m('workbuddy_up', 'gauge', '1 when the manager is serving.', 1)
    # 首字延迟分位数。
    #
    # 必须带标签（Prometheus 的分位数惯例），而 `_m` 不支持标签，
    # 所以这三条手工写，HELP/TYPE 只写一次。
    _ttft = _ttft_quantiles()
    if _ttft is not None:
        _m('workbuddy_ttft_samples', 'gauge',
           'Requests with a first-token measurement in the last hour. Below 20 the '
           'quantiles are withheld on purpose: a p99 over that few samples is just '
           'the largest of them, not a tail.', _ttft['samples'])
        if _ttft['p50'] is not None:
            if not any(x.startswith('# HELP workbuddy_ttft_ms ') for x in lines):
                lines.append('# HELP workbuddy_ttft_ms Time to first token in milliseconds '
                             'over the last hour, by quantile. This is what a user waits '
                             'through before any text appears; end-to-end latency hides '
                             'it behind the generation time.')
                lines.append('# TYPE workbuddy_ttft_ms gauge')
            for _q, _key in (('0.50', 'p50'), ('0.95', 'p95'), ('0.99', 'p99')):
                lines.append('workbuddy_ttft_ms{quantile="%s"} %d' % (_q, _ttft[_key]))
    # 上下文长度（prompt tokens）分位数。
    #
    # 同一套查询，换一个列。**不设「太大」的阈值** ——
    # 各模型上限不同且上游随时会调，写死阈值要么没用要么天天误报。
    # 只报分位数和最大值，让看的人按自己的模型判断。
    _ctx = _context_quantiles()
    if _ctx is not None:
        _m('workbuddy_context_samples', 'gauge',
           'Requests with a prompt-token measurement in the last hour. Below 20 '
           'the quantiles are withheld, for the same reason as the first-token '
           'metric: a p99 over a handful of samples is just the largest of them.',
           _ctx['samples'])
        if _ctx['p50'] is not None:
            if not any(x.startswith('# HELP workbuddy_context_tokens ') for x in lines):
                lines.append('# HELP workbuddy_context_tokens Prompt tokens per request '
                             'over the last hour, by quantile, plus the largest seen. '
                             'No threshold is applied: model limits differ and change, '
                             'so the number is for whoever knows their own limit. A '
                             'dialogue that keeps growing shows here before it starts '
                             'repeating itself.')
                lines.append('# TYPE workbuddy_context_tokens gauge')
            for _q, _key in (('0.50', 'p50'), ('0.95', 'p95'), ('0.99', 'p99')):
                lines.append('workbuddy_context_tokens{quantile="%s"} %d'
                             % (_q, _ctx[_key]))
            _m('workbuddy_context_max_tokens', 'gauge',
               'Largest prompt-token count in the last hour. Grows steadily in a '
               'long dialogue; that is the signal worth watching.', _ctx['max'])
    # 按账号的首字延迟。
    #
    # 全局 p99 只能说"慢"，说不了"谁慢"。2026-11-15 那次排障里，两个账号的首字延迟
    # 接近 40 秒、其他账号 6–13 秒 —— 少数账号把整体长尾整个拉高，而调度、权重、
    # 下线全是按账号做的决策。没有这条序列，就只能等用户投诉来定位。
    #
    # 只在样本足够的账号上出数（见 `_account_ttft` 的边界说明），并且账号名单变化时
    # 序列会自然消失 —— 这是 gauge，不是计数器，不需要清零逻辑。
    _acct = _account_ttft()
    if _acct:
        if not any(x.startswith('# HELP workbuddy_account_ttft_ms ') for x in lines):
            lines.append('# HELP workbuddy_account_ttft_ms Time to first token in '
                         'milliseconds over the last hour, per account. Accounts with '
                         'too few samples are omitted on purpose: they would only '
                         'produce a "quantile" that is really just their largest value. '
                         'A few slow accounts raise the global p99 without being '
                         'visible in it.')
            lines.append('# TYPE workbuddy_account_ttft_ms gauge')
        for _row in _acct:
            _label = _prom_label(_row['account'])
            lines.append('workbuddy_account_ttft_samples{account="%s"} %d'
                         % (_label, _row['samples']))
            for _q, _key in (('0.50', 'p50'), ('0.95', 'p95'), ('0.99', 'p99')):
                lines.append('workbuddy_account_ttft_ms{account="%s",quantile="%s"} %d'
                             % (_label, _q, _row[_key]))
    # 上游等待时间的转发次数。
    #
    # 这个能力依赖外部条件 —— 上游得先限流。实现当天查了两次日志都是零，
    # 那不是坏了，是上游一直没限流。只靠翻日志答不出「它跑过几次」，
    # 计数器能直接回答。进程重启归零，语义是「本次运行里发生过吗」。
    from .routers import gateway as _gateway_router
    _m('workbuddy_retry_after_forwarded', 'counter',
       'Times the gateway forwarded a Retry-After taken from upstream. Zero means '
       'the capability has not had a chance to run, not that it is broken: it only '
       'fires when upstream itself returns a wait time.',
       _gateway_router._RETRY_AFTER_FORWARDED['count'])
    # 当前实际选中的数据面引擎。
    #
    # ## 为什么值得单独暴露
    #
    # qoder 和 trae 各有两代适配器，由 `QTG_QODER_ENGINE` / `QTG_TRAE_ENGINE`
    # 或 `/opt/workbuddy-manager/engine/var/` 下的一个文件决定。
    #
    # **文件缺失时会回落到 `bff`** —— 而那个引擎的源码已经丢失，只用于回滚。
    # 也就是说：一次误删、一次恢复备份没带上那个文件，服务会**静默**切到一个
    # 再也升不了级的实现上。启动日志里有一行，但那需要有人去翻日志。
    #
    # 2026-11-15 查引擎时确认：日志有记录（`qtg 启动：qoder=qoder2(...)`），
    # 但 `/metrics` 里一个引擎相关的序列都没有。**监控看不到，告警自然也配不了。**
    #
    # 三个平台各一条，取值是「当前引擎名」，用 `engine` 标签区分。
    _qtg_rt = getattr(getattr(request.app, 'state', None), 'rt', None)
    if _qtg_rt is not None:
        # HELP/TYPE 只在循环外写一次。写在循环里会给每个平台各来一份，
        # 而 Prometheus 对同一个名字重复的 HELP 是明确报错的
        # （`duplicate HELP line`），采集直接失败 —— 而这条指标本来就是为了
        # 让监控看得见，采集挂掉等于白做。
        _engine_lines = []
        for _platform, _adapter in (('qoder', 'qoder'), ('trae', 'trae')):
            _obj = getattr(_qtg_rt, _adapter, None)
            _name = type(_obj).__name__ if _obj is not None else 'absent'
            # 适配器类名形如 Qoder2Adapter / QoderAdapter —— 归一成引擎名，
            # 与配置文件里能写的值（qoder2 / bff、aiwork / bff）对得上。
            _engine = {'Qoder2Adapter': 'qoder2', 'QoderAdapter': 'bff',
                       'Trae2Adapter': 'aiwork', 'TraeAdapter': 'bff'}.get(_name, _name)
            if _engine not in ('qoder2', 'aiwork', 'bff', 'absent'):
                _engine = 'unknown'
            _engine_lines.append(
                'workbuddy_engine_active{platform="%s",engine="%s"} 1' % (_platform, _engine))
        _wb = getattr(_qtg_rt, 'wb', None)
        _wb_engine = 'workbuddy' if _wb is not None else 'off'
        _engine_lines.append(
            'workbuddy_engine_active{platform="workbuddy",engine="%s"} 1' % _wb_engine)
        lines.append('# HELP workbuddy_engine_active The data-plane engine currently '
                     'selected per platform. On bff the upstream source is no longer '
                     'available, so a silent fallback there means the service can never '
                     'be upgraded.')
        lines.append('# TYPE workbuddy_engine_active gauge')
        lines.extend(_engine_lines)
    _m('workbuddy_uptime_seconds', 'gauge', 'Process uptime in seconds.',
       int(time.time() - _PROCESS_STARTED_AT))
    # 进程自身占用：长期跑下去会不会胀，只能靠趋势看（见 `_process_memory_bytes`）。
    _memory = _process_memory_bytes()
    if _memory is not None:
        _resident, _virtual = _memory
        # 说明里点明口径差异：这个数是进程级 RSS，**不要**和下面那个 cgroup 数值直接相减。
        # 2026-11-15 实测（同一时刻）：anon 两侧完全一致（124.8 MiB），差的全在 file ——
        # 进程 RSS 含共享库映射页，cgroup 的 file 只算实际驻留的页缓存，于是进程侧
        # 143.8 MiB 对 cgroup 侧 138.4 MiB。相差约 5 MiB 属正常，不是泄漏。
        _m('workbuddy_process_resident_bytes', 'gauge',
           'Resident set size of the gateway process. Process-level accounting: it counts '
           'shared library mappings that the cgroup figure below does not, so the two will '
           'differ by a few MiB and must not be subtracted from each other.',
           _resident)
        _m('workbuddy_process_virtual_bytes', 'gauge',
           'Virtual memory size of the gateway process.', _virtual)
    _m('workbuddy_process_threads', 'gauge',
       'Live OS threads in the gateway process.', threading.active_count())
    # cgroup 的内存峰值：它只涨不落，和常驻内存两条线一起看才知道有没有在慢慢胀。
    # 见 `_cgroup_memory_peak_bytes` 的说明。
    _peak = _cgroup_memory_peak_bytes()
    if _peak is not None:
        _m('workbuddy_cgroup_memory_peak_bytes', 'gauge',
           'Peak memory of the service cgroup since boot; never decreases, so a rising '
           'line alongside a flat resident gauge means a real leak rather than churn. '
           'Cgroup-level accounting: excludes shared library mappings the process RSS '
           'counts, so it reads a few MiB lower than the resident gauge by design.',
           _peak)

    # 监控循环的累计成败。偶发失败只能从计数看出来 —— 出过的那次事故里，
    # 只有部分轮次崩，`/readyz` 读的是「最近一次结果」，闪断完全看不见。
    # 见 `operational._MONITOR_STATS` 的说明。
    try:
        from . import operational as _operational
        _monitor = _operational.monitor_stats()
        _m('workbuddy_monitor_successes_total', 'counter',
           'Successful monitor ticks since process start.', _monitor['successes'])
        _m('workbuddy_monitor_failures_total', 'counter',
           'Failed monitor ticks. Non-zero means a whole health-evaluation round was '
           'skipped: every alert after the failure point went un-evaluated.',
           _monitor['failures'])
        if _monitor['last_success']:
            _m('workbuddy_monitor_last_success_age_seconds', 'gauge',
               'Seconds since the last successful monitor tick. Grows without bound if '
               'the loop is stalled, which readiness alone cannot tell you.',
               max(0.0, time.monotonic() - _monitor['last_success']))
    except Exception:      # noqa: BLE001 - 指标取不到不该让整个 /metrics 失败
        pass

    # nginx 配置里是否残留维护围栏。**读不到时不输出** —— 「不知道」不能显示成「没问题」。
    # 见 `operational.nginx_fence_status` 的说明：围栏生效时管理端全 503，而服务自身
    # 察觉不到（健康检查不经过 nginx）。
    try:
        _fence = _operational.nginx_fence_status()
        if _fence['checked']:
            _m('workbuddy_nginx_fence_active', 'gauge',
               'Whether a maintenance fence marker is still present in nginx config. '
               'When 1, /api/* is almost certainly answering 503 while /v1/* and '
               '/healthz stay healthy -- the failure mode that hides itself.',
               1 if _fence['fenced'] else 0)
    except Exception:      # noqa: BLE001
        pass

    # 数据库体积。**主库 + WAL + SHM 一起算** —— WAL 未 checkpoint 时可能有好几十 MB，
    # 只看主库会低估「还要多少磁盘」。见 operational.database_size_bytes。
    try:
        _dbsize = _operational.database_size_bytes()
        if _dbsize:
            _m('workbuddy_db_size_bytes', 'gauge',
               'Total on-disk size of the manager database, including the WAL and SHM '
               'sidecars. The WAL is where recent bulk writes pile up before checkpoint, '
               'so the main file alone understates what the disk still has to hold.',
               _dbsize['total'])
            _m('workbuddy_db_wal_size_bytes', 'gauge',
               'Size of the write-ahead log alone. Persistently large means checkpoints are '
               'falling behind the write rate, and read performance will start to pay for it.',
               _dbsize['wal'])
    except Exception:      # noqa: BLE001
        pass

    # 最近一次成功备份距今多久。告警只在超过 26 小时时报，这条能看到「还差多久」，
    # 也就能看出备份时间是否在一天天往后漂。见 operational.backup_age_seconds。
    #
    # 2026-11-15 补充：与下面那条快照年龄一样，改成「总是输出」。
    # 原来读不到年龄时整条序列消失 —— 而「从来没备份过」正是最该报警的状态。
    try:
        _backup_age = _operational.backup_age_seconds()
        _m('workbuddy_backup_age_seconds', 'gauge',
           'Seconds since the newest verified backup report; -1 means no report exists, '
           'which is itself the state worth alerting on. The stale alert only fires past '
           '26 hours; this shows the drift before it does, so a backup creeping later '
           'each day is visible while it is still healthy.',
           -1 if _backup_age is None else _backup_age)
    except Exception:      # noqa: BLE001
        pass

    # 加密快照（恢复体系 v2）的年龄。**与上面那条备份年龄不是一回事**：
    # 那个测的是基线报告，这个测的是 v2 恢复点本身。
    # 2026-11-15 的故障里，前者一直 ok，后者断了整整一天。
    #
    # 2026-11-15 补充：原来只有「拿到年龄」时才输出这一条，
    # 于是「从未产出过快照」和「这个指标坏了」在监控上长得一模一样 ——
    # 都是「这条序列不存在」。**而前者恰恰是最该报警的状态。**
    #
    # 现在总是输出：没有快照时报 -1。
    # 这样告警可以直接写「值 > 86400 或值 == -1」，不用再赌序列是否存在。
    try:
        _snapshot_age = _operational.snapshot_age_seconds()
        _m('workbuddy_snapshot_age_seconds', 'gauge',
           'Seconds since the newest encrypted recovery snapshot; -1 means unreadable, '
           'not absent. The snapshot root lives under /root, which the service user cannot '
           'traverse, so -1 is the expected value in normal operation -- cross-check '
           'recovery_chain_age_seconds, which reads a state file the service can reach, '
           'for the real reading. Distinct from backup_age_seconds, which covers the '
           'baseline report: on 2026-11-15 that report stayed ok while no snapshot was '
           'produced at all, and the two only diverged because both were being looked at.',
           -1 if _snapshot_age is None else _snapshot_age)
    except Exception:      # noqa: BLE001
        pass

    # 恢复链（加密快照整链）的结论。**第三条指标，回答的是另外两个都没答的问题**：
    # backup_age_seconds 测基线报告，snapshot_age_seconds 读不到 /root。
    try:
        _chain = _operational.recovery_chain_status()
        # 同样是「总是输出」：链断了不能被伪装成「没有数据」。
        # 2026-11-15 那天这条链反复失败而基线一直成功，所以相关告警全程沉默 ——
        # 那种沉默不该再靠人眼发现。
        _m('workbuddy_recovery_chain_age_seconds', 'gauge',
           'Seconds since the encrypted recovery chain last verified end to end; '
           '-1 means the chain state is unavailable, which is itself worth alerting on. '
           'Distinct from backup_age_seconds, which only covers the baseline tar: '
           'on 2026-11-15 the chain failed repeatedly while the baseline kept '
           'succeeding, so that alert stayed silent.',
           -1 if _chain is None else _chain['age_seconds'])
        # 这一条同样不能条件性输出 —— 缺失也报 0，配合上一条的 -1 一起看。
        _m('workbuddy_recovery_chain_bytes', 'gauge',
           'Size of the verified encrypted snapshot from the last successful chain run; '
           '0 when the chain state is unavailable (cross-check the age gauge, which '
           'reports -1 in that same situation).',
           0 if _chain is None else _chain['snapshot_bytes'])
    except Exception:      # noqa: BLE001
        pass

    # 归档线程的健康度。
    #
    # ## 为什么需要
    #
    # 归档是唯一会主动**删数据**的后台任务，它的状态此前只能通过管理端 API
    # 人工查看 —— 没有指标、没有告警。也就是说：它停了、卡了、或者开始出错，
    # 监控侧一片安静，只是数据库悄悄地一直变大。
    #
    # 2026-11-15 排查「磁盘一直涨」时，我在这一点上判断错过一次：看到
    # `archive_runs` 是空的就断言归档从没跑过。实际它在跑，只是保留期 90 天
    # 而数据只有 41 天，没有到期行 —— **有指标就不必靠猜**。
    try:
        from . import archiveworker as _arch
        _arch_status = _arch.status()
        _m('workbuddy_archive_worker_alive', 'gauge',
           'Whether the archive worker thread is running. This is the only background '
           'task that deletes rows, so its silence is not benign.',
           1 if _arch_status.get('worker_alive') else 0)
        _m('workbuddy_archive_enabled', 'gauge',
           'Archive worker enabled (config switch and background jobs both on).',
           1 if _arch_status.get('enabled') else 0)
        _arch_age = _arch_status.get('age_seconds')
        if isinstance(_arch_age, int) and not isinstance(_arch_age, bool):
            _m('workbuddy_archive_last_cycle_age_seconds', 'gauge',
               'Seconds since the archive worker last completed a cycle. It cycles every '
               'few minutes even with nothing to archive, so a large value means the '
               'thread is stuck rather than idle.',
               _arch_age)
        _arch_pending = _arch_status.get('pending')
        if isinstance(_arch_pending, dict):
            _m('workbuddy_archive_pending_datasets', 'gauge',
               'Datasets with rows past their retention window waiting to be archived.',
               sum(1 for value in _arch_pending.values() if value))
        _m('workbuddy_archive_last_cycle_failed', 'gauge',
           'Last archive cycle raised. Rows stay protected; nothing is deleted on failure.',
           0 if _arch_status.get('ok') is not False else 1)
        # 审计链完整性。
        #
        # `ledger.verify_chain()` 一直存在，但**生产里从来没被调用过** ——
        # 它只挂在归档流程上，而归档按 90 天阈值触发，库里最老的数据还没到那个年龄。
        # 也就是说「审计链有没有被篡改」这件事，上线至今一次都没验证过。
        #
        # 现在归档周期（默认 300 秒）顺手校验一次，成本实测 0.02 秒 / 25993 行。
        #
        # 三态要分清：**没跑过**（`-1`）、**链是好的**（`1`）、**链断了**（`0`）。
        # 把「没跑过」也报成 0 会和「真的断了」混在一起，那是最糟的一种歧义 ——
        # 一个从没验证过的系统看起来和刚验过且完好的系统一模一样。
        _chain_intact = _arch_status.get('chain_intact')
        _m('workbuddy_audit_chain_intact', 'gauge',
           '1 when the admin_events hash chain verifies end to end, 0 when it is '
           'broken, -1 before the first check has run. A broken chain means audit '
           'history was modified; -1 means nobody has looked yet, which is a '
           'different kind of problem.',
           1 if _chain_intact is True else (0 if _chain_intact is False else -1))
        _chain_events = _arch_status.get('chain_events')
        if isinstance(_chain_events, int) and not isinstance(_chain_events, bool):
            _m('workbuddy_audit_chain_events', 'gauge',
               'Rows in admin_events at the last chain verification.', _chain_events)
        _chain_error = _arch_status.get('chain_error')
        _m('workbuddy_audit_chain_check_failed', 'gauge',
           '1 when the last chain verification could not run at all (database '
           'unreadable, permissions), which is not the same as a broken chain and '
           'must not be read as one.',
           0 if not _chain_error else 1)
    except Exception:      # noqa: BLE001
        pass

    # 应用卷的剩余空间。
    #
    # ## 为什么需要
    #
    # 磁盘水位只在**告警日志**里出现过（`disk_space_trend` 每轮监控打一行），
    # 没有指标。后果是：想画一条「空间还剩多少」的趋势，只能去 grep 日志。
    #
    # 2026-11-15 实测就是这种情况：告警连着触发了 19 次，而从 3.8 GiB 降到
    # 3.4 GiB 这个过程，只有把日志按时间排序才看得出来。指标本身就能画出来。
    #
    # 阈值口径与 `operational.disk_space_alerts` 保持一致：
    # 低于 5 GiB 是趋势线，低于 2 GiB 是紧急线。
    try:
        import os as _os
        if hasattr(_os, 'statvfs'):
            _stat = _os.statvfs(config.DATA_DIR)
            _free = _stat.f_bavail * _stat.f_frsize
            _m('workbuddy_disk_free_bytes', 'gauge',
               'Free bytes on the volume holding the application data directory. '
               'Below 5 GiB raises disk_space_trend; below 2 GiB raises disk_free.',
               _free)
            _total = _stat.f_blocks * _stat.f_frsize
            if _total > 0:
                _m('workbuddy_disk_used_ratio', 'gauge',
                   'Fraction of the application volume in use (0-1).',
                   round(1 - _free / _total, 4))
    except Exception:      # noqa: BLE001
        pass

    try:
        from . import observability as _obs
        snap = _obs.admission_snapshot()
        if snap is not None:
            # 逐项取，缺一项不打掉整段。
            #
            # 2026-11-15 审计：这里的下标访问配合外层一个宽泛的 `except`，让「快照少
            # 一个键」变成了「后面所有指标一起消失」—— 共享准入开启时恰好少了三个
            # 键，于是排队超时、排队溢出、键槽占用三个指标长期没有输出，日志里只有
            # 一行容易被忽略的警告。
            #
            # 数据源已补齐；这里再用 `.get()` 兜一层：以后任何一侧再漂移，最多是
            # 某一个指标缺，而不是整段静默。
            def _g(name):
                return snap.get(name, 0) if isinstance(snap, dict) else 0
            _m('workbuddy_gateway_inflight', 'gauge', 'Requests currently inside the gateway.', _g('active'))
            _m('workbuddy_gateway_inflight_large', 'gauge', 'Large-body (>1MB) requests in flight.', _g('large'))
            _m('workbuddy_gateway_inflight_max', 'gauge', 'Admission ceiling (WB_GATEWAY_MAX_CONCURRENCY).', _g('maximum'))
            _m('workbuddy_gateway_inflight_body_bytes', 'gauge', 'Reserved raw request-body bytes until response completion.', _g('inflight_body_bytes'))
            _m('workbuddy_gateway_inflight_body_bytes_max', 'gauge', 'Per-worker raw body reservation ceiling.', _g('maximum_body_bytes'))
            _m('workbuddy_gateway_capacity_rejections_total', 'counter', 'Requests rejected by count or byte admission.', _g('capacity_rejections'))
            _m('workbuddy_gateway_key_slots', 'gauge', 'Keys currently holding a per-key slot.', _g('key_slots'))
            _m('workbuddy_gateway_queued', 'gauge', 'Waiting tickets before request-body allocation.', _g('queued'))
            _m('workbuddy_gateway_queue_timeouts_total', 'counter', 'Tickets exceeding the bounded capacity wait.', _g('queue_timeouts'))
            _m('workbuddy_gateway_queue_overflows_total', 'counter', 'Tickets rejected by global or per-key queue bounds.', _g('queue_overflows'))
            # 上游生成超过请求上限的次数（见 observability.note_output_over_limit）。
            _m('workbuddy_output_over_limit_total', 'counter',
               'Responses whose completion_tokens exceeded the requested cap. '
               'Non-zero here means the upstream ignores max_tokens; '
               'see docs/UPSTREAM-CAPABILITIES.md.',
               _obs.output_over_limit_count())
            # 请求体读取超时按类型分开数：两者的处置完全不同
            # （空闲 → 查上传链路；累计 → 精简上下文）。见 observability.note_body_timeout。
            _body_timeouts = _obs.body_timeout_counts()
            _m('workbuddy_body_timeout_idle_total', 'counter',
               'Uploads dropped after a stretch with no bytes at all. The link stalled; '
               'ask the caller to check connectivity, not to shrink the request.',
               _body_timeouts['idle'])
            _m('workbuddy_body_timeout_total_total', 'counter',
               'Uploads that were still trickling past the cumulative time limit. The link '
               'works but is too slow for this payload; shrinking the request helps.',
               _body_timeouts['total'])

        # ── 账号池集中度 ────────────────────────────────────────────────
        #
        # ## 为什么加
        #
        # 2026-11-15 的审计发现：96 个账号文件里只有 48 个在产生流量，
        # 用量最高与最低相差 **228 倍**，而其中一个余额比第一名还多的账号
        # 只被调用了 3 次。
        #
        # 上游（另一个仓库）的配置写着「按空闲时间加权，上限 2 倍」，
        # 二进制里也确实有加权选择和最小选择间隔两个机制 ——
        # **但实测行为与配置完全不符**。
        #
        # 那件事是上游的，不在这个仓库能修的范围内。**但「它不均衡」这件事
        # 本身应该是一个能被监控看到的数字，而不是一次人工排查才发现的结论。**
        #
        # 所以这里把集中度暴露成指标：以后它继续恶化，`/metrics` 会先报警，
        # 而不是等到有人注意到一半账号闲置。
        #
        # ## 口径
        #
        # 窗口取**最近一小时**而不是 24 小时 —— 这是一个 gauge，每次抓取都算，
        # 长窗口会让它对「刚刚开始倾斜」无感。
        #
        # 只统计带账号标记的对话请求：模型目录、拒绝类请求不落到具体账号上，
        # 把它们算进来会稀释比例。
        #
        # 指标含义：
        #
        # - `accounts`   窗口内有流量的账号数
        # - `requests`   窗口内这些账号上的请求数
        # - `top_share`  最忙那个账号占的比例（0~1）。1.0 表示全压在一个账号上
        #
        # 单独包一层 try：这一个取数源出问题，不该连带把上面那些指标一起弄没。
        try:
            from . import db as _db
            import time as _time
            _since = int(_time.time()) - 3600
            _row = _db.query_one(
                'WITH c AS ('
                '  SELECT account, count(*) AS n FROM request_logs'
                "  WHERE ts > ? AND account != '' AND endpoint LIKE '%chat%'"
                '  GROUP BY account)'
                ' SELECT count(*) AS accounts,'
                '        COALESCE(sum(n), 0) AS total,'
                '        COALESCE(max(n), 0) AS top_n FROM c',
                (_since,))
            _accounts = int(_row['accounts'] or 0) if _row is not None else 0
            _total = int(_row['total'] or 0) if _row is not None else 0
            _top = int(_row['top_n'] or 0) if _row is not None else 0
            _share = (_top / _total) if _total else 0.0
            _m('workbuddy_account_pool_accounts', 'gauge',
               'Accounts that served at least one chat request in the last hour.',
               _accounts)
            _m('workbuddy_account_pool_requests', 'gauge',
               'Chat requests attributed to an account in the last hour.', _total)
            _m('workbuddy_account_pool_top_share', 'gauge',
               'Share of the last hour handled by the busiest single account (0..1). '
               'A value near 1.0 means the pool is not spreading load; '
               'see docs/ACCOUNT-SCHEDULER-FINDINGS-2026-11-15.md.',
               round(_share, 4))
        except Exception:       # noqa: BLE001
            # 这个取数源坏了只影响这三个指标，不影响上面那一整段。
            logger.warning('account pool metrics unavailable', exc_info=True)
    except Exception as exc:                                    # noqa: BLE001
        # 这里刻意保留「不中断 /metrics」：一个取数源坏掉，不该让整个端点 500。
        #
        # 但**必须留下能诊断的痕迹**。原来只打异常类型：
        #
        #     logger.warning('... unavailable (%s)', type(exc).__name__)
        #
        # 2026-11-15 的实测说明了这个代价：日志里连着出现
        # `Metrics admission snapshot unavailable (KeyError)`，
        # **只知道是 KeyError，不知道哪个键、哪一行** —— 而同一个函数的文档里
        # 正写着「少一个键 → 整段静默，日志一行警告容易被忽略」。
        #
        # 结论就是：把异常类型换成了 `exc_info=True`，把栈打出来。
        # 类型那一行仍然在，只是后面多了栈 —— 习惯看简短警告的人不会觉得变吵，
        # 真要查的人第一次就能看到位置。
        logger.warning('Metrics admission snapshot unavailable (%s)',
                       type(exc).__name__, exc_info=True)

    return PlainTextResponse('\n'.join(lines) + '\n', media_type='text/plain; version=0.0.4')
