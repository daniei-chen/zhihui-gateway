"""管理端鉴权与会话。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, security
from ..iputil import client_ip
from ..services.asyncwork import joined_thread

logger = logging.getLogger('workbuddy.auth')

router = APIRouter(prefix='/api', tags=['auth'])
_login_active = 0
_LOGIN_MAX_ACTIVE = 4


@router.get('/healthz')
def healthz() -> dict:
    return {'ok': True, 'service': 'zhihui-gateway'}

@router.get('/readyz')
def readyz():
    from .. import operational
    ready = operational.public_readiness()
    # Public readiness intentionally returns one bit. Detailed health requires a user.
    return JSONResponse({'ok': ready}, status_code=200 if ready else 503)

@router.get('/operations/health')
def operations_health(user:dict=Depends(security.require_panel_reader)):
    from .. import operational
    return operational.status()


@router.post('/login')
async def login(request: Request) -> JSONResponse:
    # Login creates ambient browser authority. Protect the initial login too:
    # a cross-site form can submit valid JSON with a text/plain content type,
    # even when the victim has no existing session cookie.
    security.check_same_origin(request, require_cookie=False)
    ip = client_ip(request)

    # 先解析用户名，以便同时做「按 IP」与「按用户名」的锁定判断
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail='请求格式错误') from None
    # body 必须是 JSON 对象：`null` / `[]` / `"str"` / `123` 都是**合法 JSON**，
    # 上面的 try 不会拦住它们，随后 body.get(...) 会抛 AttributeError → 500。
    # 500 不泄露内容，但属于未处理异常：每次触发都在服务端留下错误日志
    # （可被用来刷日志），且暴露了输入校验不完整。这里显式拒绝。
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail='请求格式错误') from None
    username = str(body.get('username', '')).strip()[:64]
    password = str(body.get('password', ''))

    global _login_active
    if _login_active >= _LOGIN_MAX_ACTIVE:
        raise HTTPException(status_code=429, detail='登录繁忙，请稍后重试')
    _login_active += 1
    try:
        # PBKDF2 and atomic user-store/DB work must not stall inference streams.
        # joined_thread retains this admission slot until the worker has ended,
        # including repeated client cancellation. No unbounded work queue.
        return await joined_thread(_complete_login, request, ip, username, password)
    finally:
        _login_active -= 1


def _complete_login(request: Request, ip: str, username: str, password: str) -> JSONResponse:
    # 日志与审计只写清洗后的标签；认证、锁定键与用户查找继续用原值。
    label = security.log_text(username)
    # 来源 IP 维度是唯一还在真正限速的闸门，所以硬拦，且拦在 KDF 之前。
    # 账号维度**不能**在这里拦：锁定期内还要放行正确密码（审计 D-1）。
    if security.ip_login_blocked(ip):
        raise HTTPException(status_code=429, detail='失败次数过多，请 10 分钟后再试')

    cfg = security.load_users()
    user = next((u for u in cfg.get('users', []) if u.get('username') == username), None)
    if not user:
        # Avoid a no-KDF fast path that reveals whether the username exists.
        security.dummy_verify(password)
    if not user or not security.verify_pwd(password, user.get('pwd_hash', '')):
        # 判定顺序：先验密码，再看账号是否已在锁定期。
        # 错密码在锁定期内**继续拒绝并计数**；正确密码走到下面照常登录并清零。
        # 两次判定读的是同一次密码校验的结果，所以锁定与普通失败的耗时一致
        # （KDF 照跑），不额外制造时序旁路。
        locked = security.account_locked(username)
        # 同时记 IP 与用户名：前者防单机爆破，后者防换 IP 打同一账号
        fails = security.record_fail(ip, username)
        # 登录失败也要留痕：本次事故中攻击者拿到了会话，但服务端没有任何记录
        security.audit({'username': label or '(空)'}, 'login_failed', label,
                       f'来源 {ip}', ip=ip)
        if locked:
            raise HTTPException(status_code=429, detail='失败次数过多，请 10 分钟后再试')
        # 快到锁定时才说「还剩几次」。
        #
        # 2026-11-15：有用户输错几次后来问「密码是不是换了」—— 因为每次都是
        # 同一句「用户名或密码错误」，他不知道已经接近锁定。
        #
        # **只在剩余一两次时提示**：全程报进度等于把「还能试几次」直接送给爆破方，
        # 而临近锁定这条信息对正常用户最有用、对攻击者最没用（他马上就进不去了）。
        #
        # 对返回值做类型检查而不是直接用：这个函数在测试里会被替换成只记调用的桩，
        # 而登录失败这条路径**不能因为一个附加提示的取值失败就整条崩掉** ——
        # 那样连「用户名或密码错误」都不会返回，用户看到的是 500。
        detail = '用户名或密码错误'
        if isinstance(fails, int) and not isinstance(fails, bool):
            remaining = max(security.MAX_FAILS - fails, 0)
            if 0 < remaining <= 2:
                detail = f'用户名或密码错误，再错 {remaining} 次将暂时锁定'
        raise HTTPException(status_code=401, detail=detail)

    # Password verification is intentionally outside the user-store lock. Bind
    # signing to that exact snapshot so a concurrent reset cannot mint a fresh
    # session for an old password, deleted account or previous role.
    from .. import userstore
    with userstore.locked(config.USERS_FILE):
        current_cfg = security.load_users(allow_bootstrap=False)
        current = next((row for row in current_cfg['users'] if row.get('username') == username), None)
        if (current is None or security._authorization_fingerprint(cfg, user)
                != security._authorization_fingerprint(current_cfg, current)):
            raise HTTPException(status_code=401, detail='登录状态已失效，请重新登录')
        token = security.issue_token(username, user.get('role', 'viewer'),
                                     expected_sv=int(user.get('sv', 0)))

    security.clear_fail(ip, username)
    # URL 与 Host 都写进日志，便于在反馈里直接看出真实监听形态
    logger.info('登录成功: %s（来源 %s）', label, ip)
    # 反代存在但 cookie 拿不到 Secure 时明确告警 —— 这类「以为安全其实没有」
    # 靠用户自己去翻 DevTools 才能发现，不如在日志里点出来（每个登录一条，不刷屏）。
    warn = security.insecure_cookie_warning(request)
    if warn:
        logger.warning(warn)
    # orig 缺省 = 现在（首次登录）：这次是会话的**起点**，后续续期不会推后它。
    security.audit(user, 'login', label, f'来源 {ip}', ip=ip)
    resp = JSONResponse({'ok': True, 'username': username, 'role': user.get('role', 'viewer')})
    resp.set_cookie(
        config.COOKIE_NAME,
        token,
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        samesite='lax',
        secure=security.cookie_secure(request),
        path='/',
    )
    return resp


@router.post('/logout')
def logout(request: Request) -> JSONResponse:
    security.check_same_origin(request, require_cookie=False)
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(config.COOKIE_NAME, path='/')
    return resp


@router.get('/me')
def me(user: dict = Depends(security.current_user)) -> dict:
    return {'username': user.get('username'), 'role': user.get('role', 'viewer')}


@router.get('/authz/probe')
def identity_probe(request: Request, user: dict = Depends(security.current_user)) -> JSONResponse:
    """Authentication only; QTG authorizes each actual operation, not this GET.

    Never call a business settings endpoint or return a long-lived credential.
    Cache disabled so role changes/revocation are checked on every edge request.
    """
    from .. import edgeidentity
    return JSONResponse({'ok': True, 'probe': 'manager-identity-v1'}, headers={
        'X-QTG-Identity': edgeidentity.assertion(request, user),
        'Cache-Control': 'no-store', 'Vary': 'Cookie, Authorization, X-API-Key'})


@router.post('/sessions/revoke')
def revoke_own_sessions(request: Request,
                        user: dict = Depends(security.require_session_admin)) -> dict:
    """吊销**当前用户**的全部会话（含本机这次），并清除本机 cookie。

    用途：怀疑会话被盗用（在别人电脑上登录过、旧设备没退出、备份里有 cookie）时，
    一键让所有已签发的 cookie 立即失效，而不必去改密码（改密码会连带影响
    下游配置与其它用途）。

    为什么限定「当前用户」而不是提供「踢掉所有人」：本管理端通常只有一两个账号，
    真正的诉求是「我自己的会话失控了」。把别人的会话一起踢掉属于过度杀伤，
    而且那种操作在多人协作时容易误伤（对方正在操作）。
    若确实要踢掉某个用户，用「用户管理」里的改角色/改密码（同样走 sv 递增）。

    注意：吊销后**本会话也失效**（这正是它要的效果），前端需提示重新登录。
    """
    username = str(user.get('username') or '')
    if not username:
        raise HTTPException(status_code=401, detail='未登录')
    security.revoke_sessions(username)
    ip = client_ip(request)
    security.audit(user, 'revoke_sessions', username, f'来源 {ip}', ip=ip)
    resp = JSONResponse({'ok': True, 'relogin_required': True})
    resp.delete_cookie(config.COOKIE_NAME, path='/')
    return resp
