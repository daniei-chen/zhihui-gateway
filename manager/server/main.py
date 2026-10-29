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

from . import config, db, drain, redpacket, security
from .iputil import client_ip
from .stdio_utf8 import force_utf8_stdio
from .routers import (
    accounts, anthropic, auth, engine, gateway, keys, logs, models, playground,
    pgsync as pgsync_router, read_api, redpackets, responses,
    security as security_router, proxies, settings, stats, system, tokens, upstreams,
)
from .services import accountlog, cooling, dailyplan, pgsync, renew, tasklog, taskrun
from .routers import budgets
from .routers import qtg_panel

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail before creating directories, opening the ledger, or starting jobs.
    from .configcontract import validate_environment
    app.state.configuration_snapshot = validate_environment()
    config.ensure_dirs()
    db.connect()
    security.migrate_user_roles()
    # U01（2026-10-28）：启动时先消化「上次事务失败留下的完成回执」。失败不阻断
    # 启动——monitor 循环会继续重放，且就绪判定会因积压>0 保持失败。
    from . import ledgerreplay
    try:
        ledgerreplay.recover()
    except Exception:  # noqa: BLE001
        logger.exception('ledger receipt recovery failed at startup')
    from . import budget
    budget.recover()
    async def budget_heartbeat():
        while True:
            await asyncio.sleep(10)
            try:
                budget.heartbeat()
            except Exception:
                logger.exception('budget owner heartbeat failed')
    heartbeat_task = asyncio.create_task(budget_heartbeat())
    from . import operational
    monitor_task = asyncio.create_task(operational.monitor())
    # 弱口令巡检改后台线程（原 sitecustomize 猴子补丁收编进源码，2026-10-27）：
    # 每个管理员 × 常见弱口令表逐个跑 PBKDF2，实测 3 管理员要 13.7 秒，
    # 阻塞在 startup 里是当时 15 秒停机窗口的最大单项。不拒绝启动：用户需要
    # 先进面板改密码，日志必须足够醒目。
    def _weak_admin_check():
        try:
            weak_admins = security.weak_admin_passwords()
            if weak_admins:
                logger.error('管理员仍在使用常见弱口令：%s。请立即在「设置 → 管理用户」'
                             '中修改；本次生产事故的入口就是 admin123。',
                             '、'.join(weak_admins))
        except Exception:  # noqa: BLE001  巡检失败不影响服务
            logger.exception('weak admin password check failed')
    threading.Thread(target=_weak_admin_check, daemon=True,
                     name='wb-weak-pwd-check').start()
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
        from .qtgcore.config import load_settings as _qtg_load_settings
        from .qtgcore.runtime import Runtime as _QtgRuntime
        _qtg_rt = _QtgRuntime(_qtg_load_settings())
        await _qtg_rt.startup()
        app.state.rt = _qtg_rt
        logger.info('qoder/trae control plane merged in (qtgcore started)')
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
        from .instance_runtime import InstanceRuntime
        instance_runtime = InstanceRuntime()
        await instance_runtime.start()
        app.state.instance_runtime = instance_runtime
        yield
    finally:
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
        monitor_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
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
        dailyplan.stop_scheduler()
        accountlog.stop_collector()
        pgsync.stop_scheduler()
        # 出站客户端是共享的（issue #144：Windows 上每次新建要 ~1 秒），
        # 退出时统一关掉，别把连接池与 SSL 上下文丢在原地。
        await config.close_clients()


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
        limit = (MAX_PACKAGE_BODY_BYTES
                 if request.url.path in PACKAGE_UPLOAD_PATHS else MAX_API_BODY_BYTES)
        if declared > limit:
            return JSONResponse(
                {'detail': f'请求体过大（上限 {limit // 1024 // 1024} MB）'},
                status_code=413,
            )
    return await call_next(request)

# ── 路由注册顺序很重要：先 API / 网关，最后挂静态文件 ──
app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(keys.router)
# 红包：批量发放带额度的密钥（与密钥同属「分发」这件事，所以挨着放）
app.include_router(redpackets.router)
# 抽奖：**公开端点**（收到链接的人不需要账号），单独挂便于区分边界
app.include_router(redpackets.claim_router)
app.include_router(logs.router)
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
# proxy.py **未删**：路由同样不可达，但 compat.py 依赖其中的
# _base / _catalog / _upstream_key（函数级引用）。
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
    if platform in ("qoder", "trae") and _qtg_should_route(path):
        try:
            _user = security.current_user(request)
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
            cfg = security.load_users()
            obj = security._unsign(token, cfg['secret']) or {}
            username = str(obj.get('username') or '')
            if username:
                # orig 原样延续 —— 续期只推后**空闲截止**，不延长**总寿命**。
                # role 传空：鉴权一律以用户表为准（载荷里的 role 仅作展示参考）。
                fresh = security.issue_token(
                    username, '', orig=int(obj.get('orig') or 0) or None,
                    expected_sv=int(obj.get('sv') or 0),
                )
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

    path = request.url.path
    if path.startswith('/_next/static/'):
        # 文件名含内容哈希，内容变了文件名就变，可长期强缓存
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif path == '/metrics' or path.startswith(('/api/', '/v1/', '/v2/', '/healthz')):
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
        return JSONResponse({'error': 'not found'}, status_code=404)


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
    path_limits={path: MAX_PACKAGE_BODY_BYTES for path in PACKAGE_UPLOAD_PATHS},
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
@app.get('/metrics')
def prometheus_metrics(request: Request):
    from .metricsauth import require_metrics
    require_metrics(request)
    from fastapi.responses import PlainTextResponse

    lines: list[str] = []

    def _m(name: str, mtype: str, help_: str, value) -> None:
        if not any(x.startswith('# HELP ' + name + ' ') for x in lines):
            lines.append('# HELP %s %s' % (name, help_))
            lines.append('# TYPE %s %s' % (name, mtype))
        lines.append('%s %s' % (name, value))

    try:
        row = db.query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errs,"
            " SUM(CASE WHEN ts >= strftime('%s','now','-1 hour') THEN 1 ELSE 0 END) AS last_hour,"
            " SUM(CASE WHEN ts >= strftime('%s','now','-1 hour') AND status >= 400 THEN 1 ELSE 0 END) AS err_hour"
            " FROM request_logs")
        d = dict(row) if row is not None else {}
        _m('workbuddy_requests_total', 'counter', 'Total gateway requests recorded.', int(d.get('total') or 0))
        _m('workbuddy_errors_total', 'counter', 'Total requests with status >= 400.', int(d.get('errs') or 0))
        _m('workbuddy_requests_last_hour', 'gauge', 'Requests recorded in the last hour.', int(d.get('last_hour') or 0))
        _m('workbuddy_errors_last_hour', 'gauge', 'Errors in the last hour.', int(d.get('err_hour') or 0))
    except Exception as exc:                                    # noqa: BLE001
        logger.warning('Metrics aggregate unavailable (%s)', type(exc).__name__)
        lines.append('# metrics aggregate unavailable')

    try:
        rows = db.query(
            "SELECT COALESCE(model,'') AS model, COUNT(*) AS c FROM request_logs"
            " WHERE ts >= strftime('%s','now','-24 hour') GROUP BY model ORDER BY c DESC LIMIT 40")
        block: list[str] = []
        for r in rows:
            rr = dict(r)
            model = str(rr.get('model') or '').replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')
            if not model:
                continue
            block.append('workbuddy_requests_24h_by_model{model="%s"} %d' % (model, int(rr.get('c') or 0)))
        if block:
            lines.append('# HELP workbuddy_requests_24h_by_model requests in last 24h by model')
            lines.append('# TYPE workbuddy_requests_24h_by_model gauge')
            lines.extend(block)
    except Exception as exc:                                    # noqa: BLE001
        logger.warning('Metrics model aggregate unavailable (%s)', type(exc).__name__)
        lines.append('# metrics model aggregate unavailable')

    _m('workbuddy_up', 'gauge', '1 when the manager is serving.', 1)
    _m('workbuddy_uptime_seconds', 'gauge', 'Process uptime in seconds.',
       int(time.time() - _PROCESS_STARTED_AT))

    # ── F3（2026-10-27）：延迟分位数 + 准入水位 + 拒绝面计数 ──────────────
    try:
        since = int(_time_now()) - 3600
        rows = db.query(
            'SELECT latency_ms FROM request_logs WHERE ts >= ? AND latency_ms > 0 '
            'ORDER BY ts DESC LIMIT 20000', (since,))
        lat = sorted(int(r['latency_ms']) for r in rows)
        if lat:
            def _pct(p):
                return lat[min(len(lat) - 1, int(len(lat) * p))]
            _m('workbuddy_latency_ms_p50_1h', 'gauge', 'P50 end-to-end latency, last 1h (ms).', _pct(0.50))
            _m('workbuddy_latency_ms_p95_1h', 'gauge', 'P95 end-to-end latency, last 1h (ms).', _pct(0.95))
            _m('workbuddy_latency_ms_p99_1h', 'gauge', 'P99 end-to-end latency, last 1h (ms).', _pct(0.99))
            _m('workbuddy_latency_samples_1h', 'gauge', 'Latency samples in last 1h.', len(lat))
    except Exception as exc:                                    # noqa: BLE001
        logger.warning('Metrics latency percentiles unavailable (%s)', type(exc).__name__)

    try:
        from . import observability as _obs
        snap = _obs.admission_snapshot()
        if snap is not None:
            _m('workbuddy_gateway_inflight', 'gauge', 'Requests currently inside the gateway.', snap['active'])
            _m('workbuddy_gateway_inflight_large', 'gauge', 'Large-body (>1MB) requests in flight.', snap['large'])
            _m('workbuddy_gateway_inflight_max', 'gauge', 'Admission ceiling (WB_GATEWAY_MAX_CONCURRENCY).', snap['maximum'])
            _m('workbuddy_gateway_inflight_body_bytes', 'gauge', 'Reserved raw request-body bytes until response completion.', snap['inflight_body_bytes'])
            _m('workbuddy_gateway_inflight_body_bytes_max', 'gauge', 'Per-worker raw body reservation ceiling.', snap['maximum_body_bytes'])
            _m('workbuddy_gateway_capacity_rejections_total', 'counter', 'Requests rejected by count or byte admission.', snap['capacity_rejections'])
            _m('workbuddy_gateway_key_slots', 'gauge', 'Keys currently holding a per-key slot.', snap['key_slots'])
    except Exception as exc:                                    # noqa: BLE001
        logger.warning('Metrics admission snapshot unavailable (%s)', type(exc).__name__)

    try:
        row = db.query_one(
            "SELECT SUM(CASE WHEN status=429 THEN 1 ELSE 0 END) c429,"
            " SUM(CASE WHEN status=503 THEN 1 ELSE 0 END) c503,"
            " SUM(CASE WHEN status>=500 AND status<600 THEN 1 ELSE 0 END) c5xx"
            " FROM request_logs WHERE ts >= ?", (int(_time_now()) - 86400,))
        d24 = dict(row) if row is not None else {}
        _m('workbuddy_rejections_429_24h', 'counter', 'HTTP 429 responses in last 24h (rate/key/budget gates).', int(d24.get('c429') or 0))
        _m('workbuddy_rejections_503_24h', 'counter', 'HTTP 503 responses in last 24h (capacity/upstream).', int(d24.get('c503') or 0))
        _m('workbuddy_errors_5xx_24h', 'counter', 'All 5xx responses in last 24h.', int(d24.get('c5xx') or 0))
    except Exception as exc:                                    # noqa: BLE001
        logger.warning('Metrics rejection counters unavailable (%s)', type(exc).__name__)

    return PlainTextResponse('\n'.join(lines) + '\n', media_type='text/plain; version=0.0.4')
