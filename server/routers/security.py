"""入站 IP 管控：全局开关/模式、白黑名单规则、访问审计。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import db, security
from ..iputil import client_ip
from ..iputil import ip_matches

router = APIRouter(prefix='/api/security', tags=['security'])

DEFAULT_CONFIG = {'enabled': False, 'mode': 'blacklist'}


class ConfigIn(BaseModel):
    enabled: bool
    mode: str = Field(pattern='^(whitelist|blacklist)$')
    #: 明示「我知道这会切断推理入站」。
    #:
    #: PB0108（2026-10-26）：开启白名单而当前操作者 IP 不在任何 allow 规则内，
    #: 保存后**所有不在放行规则内的来源 IP 都无法调用推理 API**（`/v1` 与
    #: `/v1/messages` 返回 403 `permission_error`），业务立即中断。
    #:
    #: 但它**不是**把你锁在控制台外面：`ip_rules` 只被 `gateway.py` 与
    #: `anthropic.py` 的入站闸门消费，`/api/*` 控制面**没有**这层检查
    #: （`grep ip_rules` 可复核）。所以管理员仍能登录本页把模式改回黑名单，
    #: 是**可自助恢复**的——这一点必须说准：说是「改数据库才能恢复」会把处置
    #: 方向指错，而说是「不影响业务」又会低估后果。
    #: 这里不硬拦（管理员可能就是要先在外部把规则准备好），但**必须显式确认**。
    confirm_lockout: bool = False


class RuleIn(BaseModel):
    kind: str = Field(pattern='^(allow|deny)$')
    cidr: str = Field(min_length=1, max_length=64)
    note: str = ''
    confirm_lockout: bool = False


def allow_rules_cover(ip: str) -> bool:
    """现有 allow 规则里是否有覆盖该 IP 的。

    PB0108 的判据：白名单模式下，没有覆盖自己的 allow 规则 = 保存即切断推理入站。
    """
    rows = db.query("SELECT cidr FROM ip_rules WHERE kind = 'allow'")
    return any(ip_matches(ip, row['cidr']) for row in rows)


def lockout_detail(ip: str, action: str) -> dict:
    """确认要求的 409 载荷。前端据 `code` 决定弹确认框，而不是展示一段泛化错误。

    `code` 里的 `self_lockout_risk` 是**历史命名**（沿用首批实现与前端契约），
    不要按字面理解成「管理员会被锁在控制台外」——见 `ConfigIn.confirm_lockout`
    的说明：受影响的只有 `/v1` 推理入站。
    """
    return {
        'code': 'self_lockout_risk',
        'action': action,
        'ip': ip,
        'message': (f'{action}：不在放行规则内的来源 IP（含 {ip}）将无法调用推理 API，'
                    '业务会中断。控制台页面不受 IP 规则限制，仍可登录本页把模式改回'
                    '黑名单或补一条放行规则恢复。确认后重试。'),
    }


def get_config() -> dict:
    return {**DEFAULT_CONFIG, **(db.get_setting('security', {}) or {})}


@router.get('/config')
def read_config(user: dict = Depends(security.current_user)) -> dict:
    return get_config()


@router.post('/config')
def write_config(body: ConfigIn, request: Request,
                 user: dict = Depends(security.require_session_admin)) -> dict:
    cfg = body.model_dump(exclude={'confirm_lockout'})
    ip = client_ip(request)
    # PB0108：开启白名单而自己不在任何 allow 规则里 → 推理入站立刻被切断，必须显式确认。
    if cfg.get('enabled') and cfg.get('mode') == 'whitelist' and not allow_rules_cover(ip):
        if not body.confirm_lockout:
            raise HTTPException(status_code=409, detail=lockout_detail(ip, '开启 IP 白名单'))
        security.audit(user, 'security_self_lockout_confirmed', '',
                       f'开启白名单但 ip={ip} 不在 allow 规则内，管理员已显式确认')
    db.set_setting('security', cfg)
    # 改 IP 管控开关/模式会直接影响对外放行策略，必须留痕
    security.audit(user, 'update_security', '',
                   f"enabled={cfg.get('enabled')} mode={cfg.get('mode')}；来源 {ip}")
    return cfg


@router.get('/rules')
def list_rules(user: dict = Depends(security.current_user)) -> list[dict]:
    rows = db.query('SELECT * FROM ip_rules ORDER BY id DESC')
    return [
        {'id': r['id'], 'kind': r['kind'], 'cidr': r['cidr'], 'note': r['note'], 'created_at': r['created_at']}
        for r in rows
    ]


@router.post('/rules')
def add_rule(body: RuleIn, request: Request,
             user: dict = Depends(security.require_session_admin)) -> dict:
    # 校验 CIDR 合法性（便于尽早发现错误输入）
    if not ip_matches('0.0.0.0', body.cidr) and not ip_matches('::', body.cidr):
        # ip_matches 对非法 CIDR 返回 False，这里额外做一次严格校验
        import ipaddress

        try:
            ipaddress.ip_network(body.cidr, strict=False)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f'非法的 IP / CIDR: {body.cidr}') from exc
    ip = client_ip(request)
    # PB0108：把自己加进 deny，等于自己此后所有推理调用都被拦（业务中断）。
    if body.kind == 'deny' and ip_matches(ip, body.cidr):
        if not body.confirm_lockout:
            raise HTTPException(status_code=409,
                                detail=lockout_detail(ip, f'添加 deny 规则 {body.cidr}'))
        security.audit(user, 'security_self_lockout_confirmed', '',
                       f'添加 deny {body.cidr} 覆盖了自身 ip={ip}，管理员已显式确认')
    rule_id = db.execute(
        'INSERT INTO ip_rules(kind, cidr, note, created_at) VALUES(?, ?, ?, ?)',
        (body.kind, body.cidr, body.note, int(time.time())),
    )
    row = db.query_one('SELECT * FROM ip_rules WHERE id = ?', (rule_id,))
    return {'id': row['id'], 'kind': row['kind'], 'cidr': row['cidr'], 'note': row['note'], 'created_at': row['created_at']}


@router.delete('/rules/{rule_id}')
def delete_rule(rule_id: int, request: Request, confirm_lockout: bool = False,
                user: dict = Depends(security.require_session_admin)) -> dict:
    row = db.query_one('SELECT * FROM ip_rules WHERE id = ?', (rule_id,))
    if not row:
        raise HTTPException(status_code=404, detail='规则不存在')
    cfg = get_config()
    ip = client_ip(request)
    # PB0108：白名单模式下删掉「唯一覆盖自己」的 allow 规则，同样会切断自己的推理入站。
    if row['kind'] == 'allow' and cfg.get('enabled') and cfg.get('mode') == 'whitelist':
        others = db.query("SELECT cidr FROM ip_rules WHERE kind = 'allow' AND id != ?", (rule_id,))
        covered = any(ip_matches(ip, r['cidr']) for r in others)
        if not covered and not confirm_lockout:
            raise HTTPException(status_code=409,
                                detail=lockout_detail(ip, f'删除 allow 规则 {row["cidr"]}'))
        if not covered:
            security.audit(user, 'security_self_lockout_confirmed', '',
                           f'删除 allow {row["cidr"]} 后 ip={ip} 失去覆盖，管理员已显式确认')
    db.execute('DELETE FROM ip_rules WHERE id = ?', (rule_id,))
    return {'ok': True}


@router.get('/logs')
def access_logs(limit: int = 200, user: dict = Depends(security.current_user)) -> list[dict]:
    rows = db.query(
        'SELECT * FROM ip_access_logs ORDER BY id DESC LIMIT ?',
        (min(1000, max(1, limit)),),
    )
    return [
        {
            'id': r['id'], 'ts': r['ts'], 'ip': r['ip'], 'path': r['path'],
            'blocked': bool(r['blocked']), 'ua': r['ua'],
            # 拦截原因短码（issue #33）。存量记录为 NULL——那时没记原因，
            # 界面按「未记录」展示，不编造。
            'reason': r['reason'] if 'reason' in r.keys() else None,
        }
        for r in rows
    ]


@router.post('/logs/clear')
def clear_logs(user: dict = Depends(security.require_session_admin)) -> dict:
    db.execute('DELETE FROM ip_access_logs')
    return {'ok': True}
