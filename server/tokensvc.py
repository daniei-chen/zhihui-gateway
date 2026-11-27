"""管理面作用域化 API Token：生成、解析与校验（见 docs/api-tokens.md）。

与 `keysvc`（数据面网关密钥）**刻意分开**：那套只授权模型调用，与后台权限无关；
本模块的 token 授权的是管理面 `/api/*`，因此安全要求更高——

  * 明文只在创建时返回一次，库中只存 SHA-256 哈希；
  * 按 prefix 定位 + 常量时间比较（`secrets.compare_digest`），不全表扫描；
  * scope 决定角色（readonly → viewer，admin → admin），非法值降级为最小权限；
  * 绑定签发人的会话版本（sv）：改密码 / 吊销会话 / 删号后立即失效（DAN-11）；
  * 失败时**不区分**「不存在 / 已停用 / 已过期 / 版本不符」，不给探测者任何信号。

历史（2026-09-14）：管理端曾接受 users.json 里 `api_keys` 数组中的 `X-API-Key`
并直接授予 admin，那条提权后门已彻底移除。本模块的形态与它刻意不同，理由见
设计文档的「必须避免重蹈的覆辙」一节。
"""
from __future__ import annotations

import hashlib
import secrets
import time

from . import db

# 与网关密钥 `wbk_` 刻意区分，避免两类凭据在同一处被混用。
TOKEN_PREFIX = 'wbt_'
# 'keys' 是 K01-S2 引入的服务身份：只允许代理密钥管理（创建/更新/删除/重置用量），
# 不能访问账号、设置、用户、认证等高危端点。它不是 admin 的别名。
# scope 语义（K26/N32）：
#   readonly —— 面板里给人用的只读令牌（可读全部只读面，等同 viewer）
#   admin    —— 面板里给人用的全权令牌
#   keys     —— 内部服务：**只能**管密钥（K01-S2 的收敛，不能读统计）
#   read     —— 内部服务：**只能读**既有数据（K26 http 读通道），任何写方法一律拒绝，
#               且不得读凭据面（/api/tokens）与用户面（/api/users）
SCOPES = ('readonly', 'admin', 'keys', 'read')

# last_used 写库节流（秒）。管理面在总览页会被 30 秒轮询，逐请求写会把库打热。
LAST_USED_THROTTLE = 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_scope(value: object) -> str:
    """归一化 scope；**非法值一律降级为最小权限** readonly。

    宁可给少了权限（用户发现功能不可用会来问），也不要因为一个拼写错误
    意外给出管理员权限。
    """
    v = str(value or '').strip().lower()
    return v if v in SCOPES else 'readonly'


def scope_to_role(scope: object) -> str:
    """把 scope 映射为管理端角色（供 `security.current_user` 使用）。

    'keys' 在 role 体系里等同 **viewer**（只读），不引入第三套角色模型；
    密钥写权限由 `security.require_keys_writer` 依据 token 的原始 scope 单独放行。
    这样该令牌不可能因为"新角色"被某个只查 role 的端点意外接受。
    """
    v = normalize_scope(scope)
    return 'admin' if v == 'admin' else 'viewer'


def _parse(row) -> dict:
    return {
        'id': row['id'],
        'name': row['name'],
        'prefix': row['prefix'],
        'scope': row['scope'],
        'enabled': bool(row['enabled']),
        'expires_at': row['expires_at'],
        'created_at': row['created_at'],
        'created_by': row['created_by'],
        'last_used_at': row['last_used_at'],
        'last_used_ip': row['last_used_ip'],
    }


def list_tokens() -> list[dict]:
    """列出全部令牌。**永不回传明文或哈希。**"""
    rows = db.query('SELECT * FROM api_tokens ORDER BY id DESC')
    return [_parse(r) for r in rows]


def _issuer_session_version(username: str) -> int:
    """签发人此刻的会话版本；签发人不存在或用户名为空时报错。

    在这里统一盖章，而不是让每个调用方自己传：任何一条签发路径都拿不到「没有
    版本的令牌」，也就不会有人忘了传参而留下永久凭据。

    惰性导入 `security`：它在模块层导入本模块，反向导入会成环；调用发生在两者
    都加载完之后，安全。
    """
    from . import security
    if not username:
        raise ValueError('create_token 必须指定 created_by：令牌要绑定签发人的会话版本')
    version = security.issuer_session_version(security.load_users(), username)
    if version is None:
        raise ValueError(f'签发人不存在，拒绝签发无法回收的令牌：{username}')
    return version


def create_token(name: str, scope: object = 'readonly',
                 expires_at: int | None = None, created_by: str = '') -> dict:
    """创建一个令牌。返回体里的 `token` 是**明文，仅此一次**。

    令牌绑在签发人的**当前会话版本**上：签发人改密码 / 改角色 / 吊销会话（都是
    sv+1）或被删号之后，这枚令牌立即失效。
    """
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    exp = int(expires_at) if expires_at else None
    creator = str(created_by or '')[:64]
    token_id = db.execute(
        'INSERT INTO api_tokens(name, token_hash, prefix, scope, enabled, '
        'expires_at, session_version, created_at, created_by) '
        'VALUES(?, ?, ?, ?, 1, ?, ?, ?, ?)',
        (str(name)[:64], _hash(token), token[:12], normalize_scope(scope),
         exp, _issuer_session_version(creator), int(time.time()), creator),
    )
    out = _parse(db.query_one('SELECT * FROM api_tokens WHERE id = ?', (token_id,)))
    out['token'] = token  # 仅此一次
    return out


def update_token(token_id: int, patch: dict) -> dict | None:
    """部分更新（PATCH 语义：只改真的提交了的字段）。不存在返回 None。"""
    if not db.query_one('SELECT id FROM api_tokens WHERE id = ?', (token_id,)):
        return None
    fields: dict[str, object] = {}
    if 'name' in patch and patch['name']:
        fields['name'] = str(patch['name'])[:64]
    if 'enabled' in patch:
        fields['enabled'] = 1 if patch['enabled'] else 0
    if 'scope' in patch:
        fields['scope'] = normalize_scope(patch['scope'])
    if 'expires_at' in patch:
        # 显式传 null/0 表示「改为永不过期」——不能用真值判断。
        v = patch['expires_at']
        fields['expires_at'] = int(v) if v else None
    if fields:
        assignments = ', '.join(f'{k} = ?' for k in fields)
        db.execute(f'UPDATE api_tokens SET {assignments} WHERE id = ?',
                   (*fields.values(), token_id))
    return _parse(db.query_one('SELECT * FROM api_tokens WHERE id = ?', (token_id,)))


def delete_token(token_id: int) -> bool:
    if not db.query_one('SELECT id FROM api_tokens WHERE id = ?', (token_id,)):
        return False
    db.execute('DELETE FROM api_tokens WHERE id = ?', (token_id,))
    return True


def resolve(token: object) -> dict | None:
    """校验明文令牌：先按 prefix 定位候选行，再对哈希做常量时间比较。

    返回行字典（含 token_hash，仅内部使用）；不匹配返回 None。
    """
    if not token or not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        return None
    digest = _hash(token)
    for row in db.query('SELECT * FROM api_tokens WHERE prefix = ?', (token[:12],)):
        if secrets.compare_digest(row['token_hash'], digest):
            return dict(row)
    return None


def usable(row: dict) -> bool:
    """令牌是否可用：已启用，且未过期（无 expires_at = 不按时间过期）。

    这只是**令牌自身**的状态；能否通行还要求 `session_bound()` —— 两者在
    `security._token_identity` 里合起来判定。逐次校验、不做缓存，保证吊销与
    过期**即时生效**。
    """
    if not row.get('enabled'):
        return False
    exp = row.get('expires_at')
    return not exp or int(exp) > int(time.time())


def session_bound(row: dict, issuer_sv: int | None) -> bool:
    """令牌的会话版本是否仍与签发人一致。

    为什么必须有这一条：吊销会话（`security.revoke_sessions`）、改密码、改角色
    都只递增 users.json 里的 sv，对 SQLite 里这枚令牌本来毫无影响 —— 一次会话
    泄露能被洗成永久 admin 凭据（DAN-11）。把令牌钉在签发时的 sv 上之后，
    sv 一变令牌就废，吊销因此天然覆盖了令牌。

    判据只有一条等式：签发时的版本 == 签发人当前的版本。因此：
      * 改密码 / 改角色 / 吊销会话（都是 sv+1）→ 不等 → 失效；
      * 签发人被删号 → `issuer_sv` 为 None → 失效；
      * 升级前签发的令牌没有记录版本（NULL）→ fail-closed，必须重新签发。

    版本必须是**非负整数**：`None`（没有版本）与任何负数（被改坏的行）都不参与
    等式比较，所以这两种行永远不可能「恰好对上」某个签发人。真正的守卫在签发侧
    （`create_token` 拒绝没有签发人的令牌），这里是纵深防御。
    """
    bound = row.get('session_version')
    if bound is None or int(bound) < 0:
        return False
    if issuer_sv is None or int(issuer_sv) < 0:
        return False
    return int(bound) == int(issuer_sv)


def touch(token_id: int, ip: str) -> None:
    """记录最近使用（**节流**：距上次记录不足 LAST_USED_THROTTLE 秒则跳过）。

    记账属于旁路，任何异常都不能影响请求本身（与请求日志同一原则）。
    """
    try:
        now = int(time.time())
        row = db.query_one('SELECT last_used_at FROM api_tokens WHERE id = ?', (token_id,))
        if row and row['last_used_at'] and now - int(row['last_used_at']) < LAST_USED_THROTTLE:
            return
        db.execute('UPDATE api_tokens SET last_used_at = ?, last_used_ip = ? WHERE id = ?',
                   (now, db._clean(ip, 64), token_id))
    except Exception:  # noqa: BLE001
        pass
