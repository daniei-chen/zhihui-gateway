"""请求日志查询。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from .. import db, keysvc, observability, security

router = APIRouter(prefix='/api/logs', tags=['logs'])

#: U21：`terminal_state` 的已知取值。过滤参数必须是其中之一，否则显式 400 ——
#: 拼错的终态会静默返回空列表，看起来像"这段时间没有失败"，比报错更危险。
TERMINAL_STATES = ('completed', 'rejected', 'upstream_error', 'transport_error',
                   'protocol_incomplete', 'application_error', 'client_cancelled',
                   'interrupted')
#: U21：轨迹分类（真实推理 / 管理动作 / 探针 / 其它）。
TRAFFIC_CLASSES = ('inference', 'admin', 'probe', 'other')


@router.get('')
def list_logs(
    page: int = 1,
    size: int = 20,
    key_id: str | None = None,
    model: str | None = None,
    status: str | None = None,
    ip: str | None = None,
    days: int | None = None,
    realm: str | None = None,
    user: dict = Depends(security.current_user),
) -> dict:
    # 分页参数钳制（审计 2026-10-18）：`page` 无上限时，超大整数会在 SQLite
    # 绑定期溢出抛 500；`size` 无上限则可以一次拉走整张表（内存与响应体都不受控）。
    page = max(1, min(int(page), 100_000))
    size = max(1, min(int(size), 500))
    where: list[str] = []
    args: list[object] = []

    # 版本过滤（cn / global）。历史记录该列为 NULL —— 按 cn 归类，
    # 与 db.realm_of_model 的口径一致（无前缀即国内版）。
    if realm in ('cn', 'global'):
        if realm == 'cn':
            where.append('(l.realm = ? OR l.realm IS NULL)')
        else:
            where.append('l.realm = ?')
        args.append(realm)

    if days:
        # 钳到 [1, 3650]：超大值会在 SQLite 绑定时溢出（曾 500），
        # 负值则会把过滤条件变成「未来之后」，语义上无意义
        d = min(3650, max(1, int(days)))
        where.append('l.ts >= ?')
        args.append(int(time.time()) - d * 86400)
    if key_id and key_id not in ('all', ''):
        # 非数字会 int() 抛错 → 500。这里显式拦成 400，避免用非法输入探测
        try:
            kid = int(key_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='key_id 必须是数字或 all') from None
        where.append('l.key_id = ?')
        args.append(kid)
    if model:
        where.append('(l.model LIKE ? OR l.mapped_model LIKE ?)')
        args.extend([f'%{model}%', f'%{model}%'])
    if ip:
        where.append('l.ip LIKE ?')
        args.append(f'%{ip}%')
    if status == 'ok':
        where.append('l.status >= 200 AND l.status < 300')
    elif status == 'error':
        where.append('(l.status < 200 OR l.status >= 300)')

    clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    total = db.query_one(f'SELECT COUNT(*) AS c FROM request_logs l {clause}', args)['c']

    page = max(1, page)
    size = min(200, max(1, size))
    rows = db.query(
        f'SELECT l.*, k.name AS key_name FROM request_logs l '
        f'LEFT JOIN api_keys k ON k.id = l.key_id {clause} '
        f'ORDER BY l.id DESC LIMIT ? OFFSET ?',
        (*args, size, (page - 1) * size),
    )
    items = [
        {
            'id': r['id'],
            'ts': r['ts'],
            'key_id': r['key_id'],
            'key_name': r['key_name'],
            'ip': r['ip'],
            'model': r['model'],
            'mapped_model': r['mapped_model'],
            'status': r['status'],
            'realm': r['realm'] or 'cn',
            'prompt_tokens': r['prompt_tokens'],
            'completion_tokens': r['completion_tokens'],
            'latency_ms': r['latency_ms'],
            # 首字延迟：NULL 表示未采集（非流式请求或旧记录）
            'first_token_ms': r['first_token_ms'],
            # 提示词缓存三段（issue #69）：NULL = 上游没给这三个字段（老上游），
            # 与「给了 0」不同 —— 界面据此显示「—」而不是 0%。
            'cache_hit_tokens': r['cache_hit_tokens'],
            'cache_miss_tokens': r['cache_miss_tokens'],
            'cache_write_tokens': r['cache_write_tokens'],
            'ua': r['ua'],
            'error': r['error'],
            'stream': bool(r['stream']),
            'credit': r['credit'],
            # 本次实际用了哪个上游账号（issue #69）。NULL = 尚未回填 ——
            # 账号是采集上游日志后按时间对回来的，比请求本身晚几秒，
            # 所以刚打完的请求这一列可能是空的（界面显示「—」，刷新即可）。
            'account': r['account'],
            # 输入侧命中缓存的 token 数。NULL 与 0 含义不同：
            # NULL = 上游没给这个字段，0 = 这次确实没命中（见 db 的列说明）。
            'cache_hit_tokens': r['cache_hit_tokens'],
            # RV01：两个 ID 分开暴露，语义不同、不能互相冒充。
            #   request_id        = 服务端执行 ID（预算/账本/outcome 的键，
            #                       /api/logs/attempts 用的也是它）；
            #   client_request_id = 调用方自带的 X-Request-ID（校验通过才存，
            #                       NULL = 未提供或不合法）。外部系统拿自己的 ID
            #                       查日志靠这一列 —— 它**不是**幂等键。
            'request_id': r['request_id'],
            'client_request_id': r['client_request_id'],
        }
        for r in rows
    ]
    return {'total': total, 'items': items}


@router.get('/outcomes')
def list_outcomes(
    page: int = 1,
    size: int = 50,
    error_code: str | None = None,
    terminal_state: str | None = None,
    platform: str | None = None,
    upstream_platform: str | None = None,
    release_id: str | None = None,
    traffic_class: str | None = None,
    days: int | None = None,
    since: int | None = None,
    until: int | None = None,
    user: dict = Depends(security.current_user),
) -> dict:
    """请求轨迹检索（U21）：按稳定错误码 / 终态 / 平台 / 发布号 / 时间窗过滤。

    回答的是「**这次发布之后哪类错误变多了**」：`release_id` 切发布，
    `error_code` 切错因，`traffic_class` 把探针与管理动作摘出去。

    返回里刻意包含 `attempt_count`（该请求发生过几次 provider attempt）与
    `platform`（`request_logs.actual_platform`；NULL = 未记录路由结果，**不是**
    workbuddy），这两列是「同一个错因是网关自己的问题还是某个上游的问题」的
    最小充分信息。

    历史行（U21 上线前）的 `error_code` 是 NULL —— 语义是「未分类」，不是
    「没出错」；`error_code_coverage` 会把这个比例单独报出来。

    **平台过滤有两个参数名**（U21 实测的坑）：`main.py::_qtg_platform_route`
    会把 `?platform=qoder|trae` 的请求重写进 QTG 控制面，而 `/api/logs` 在它的
    路由白名单里 —— 于是 `?platform=qoder` 根本到不了本端点（返回 404 页面）。
    `upstream_platform` 是本端点的别名，两个平台值都能用；`platform` 只在
    workbuddy 这类值上有效。要按 qoder/trae 过滤请用 `upstream_platform`。
    """
    page = max(1, min(int(page), 100_000))
    size = max(1, min(int(size), 500))
    target_platform = (upstream_platform or platform or '').strip()
    where: list[str] = []
    args: list[object] = []

    if error_code:
        code = observability.sanitize_error_code(error_code)
        if code is None:
            raise HTTPException(status_code=400,
                                detail='error_code 只允许 [A-Za-z0-9._:-]{1,64}')
        where.append('o.error_code = ?')
        args.append(code)
    if terminal_state:
        if terminal_state not in TERMINAL_STATES:
            raise HTTPException(status_code=400,
                                detail='terminal_state 必须是 ' + ' / '.join(TERMINAL_STATES))
        where.append('o.terminal_state = ?')
        args.append(terminal_state)
    if target_platform:
        # 平台名是上游配置里的标识（workbuddy / qoder / trae …），不做白名单，
        # 只钳长度：这里只是等值过滤，参数化查询不会注入。
        where.append('l.actual_platform = ?')
        args.append(target_platform[:64])
    if release_id:
        where.append('o.release_id = ?')
        args.append(str(release_id)[:128])
    if since is not None:
        where.append('o.ts >= ?')
        args.append(int(since))
    if until is not None:
        where.append('o.ts <= ?')
        args.append(int(until))
    if days:
        d = min(3650, max(1, int(days)))
        where.append('o.ts >= ?')
        args.append(int(time.time()) - d * 86400)

    # 轨迹分类：探针/管理动作不该混进真实流量的失败统计里（SQL 侧就切开，
    # 避免把 20 万行拉进内存再过滤）。表达式与 `observability.traffic_class()`
    # 同源（`TRAFFIC_CLASS_SQL`），由测试锁定两者一致。
    classes: list[str] = []
    if traffic_class:
        wanted = [c.strip() for c in str(traffic_class).split(',') if c.strip()]
        for name in wanted:
            if name not in TRAFFIC_CLASSES:
                raise HTTPException(status_code=400,
                                    detail='traffic_class 必须是 ' + ' / '.join(TRAFFIC_CLASSES))
        classes = wanted
        placeholders = ','.join('?' * len(classes))
        where.append(f'({observability.traffic_class_sql("o.endpoint")}) IN ({placeholders})')
        args.extend(classes)

    has_code = observability.ensure_outcome_schema()
    code_expr = 'o.error_code' if has_code else 'NULL'
    source_expr = 'o.error_source' if has_code else 'NULL'
    clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    joined = 'FROM request_outcomes o LEFT JOIN request_logs l ON l.request_id = o.request_id'
    total = db.query_one(f'SELECT COUNT(*) AS c {joined} {clause}', args)['c']
    rows = db.query(
        f'SELECT o.request_id, o.ts, o.endpoint, o.method, o.protocol, o.http_status, '
        f'o.terminal_state, o.error_stage, {code_expr} AS error_code, '
        f'{source_expr} AS error_source, o.release_id, o.client_request_id, o.latency_ms, '
        f'l.actual_platform AS platform, l.account AS account, '
        f'(SELECT COUNT(DISTINCT a.attempt) FROM attempt_events a '
        f' WHERE a.request_id = o.request_id) AS attempt_count '
        f'{joined} {clause} ORDER BY o.ts DESC, o.request_id LIMIT ? OFFSET ?',
        (*args, size, (page - 1) * size),
    )
    items = [
        {
            'request_id': r['request_id'],
            'ts': r['ts'],
            'endpoint': r['endpoint'],
            'method': r['method'],
            'protocol': r['protocol'],
            'http_status': r['http_status'],
            'terminal_state': r['terminal_state'],
            'error_stage': r['error_stage'],
            # 稳定错误码 + 来源（upstream / gateway / client）；NULL = 未分类（历史行）
            'error_code': r['error_code'],
            'error_source': r['error_source'],
            'release_id': r['release_id'],
            'client_request_id': r['client_request_id'],
            'latency_ms': r['latency_ms'],
            # 实际路由到的平台；NULL = 未记录（例如鉴权阶段就被拒），不是 workbuddy
            'platform': r['platform'],
            'account': r['account'],
            'attempt_count': int(r['attempt_count'] or 0),
            'traffic_class': observability.traffic_class(r['endpoint'], r['method']),
        }
        for r in rows
    ]
    return {
        'total': int(total),
        'page': page,
        'size': size,
        'error_code_available': bool(has_code),
        'filters': {'error_code': error_code, 'terminal_state': terminal_state,
                    'platform': target_platform or None, 'release_id': release_id,
                    'traffic_class': classes or None, 'days': days,
                    'since': since, 'until': until},
        'items': items,
    }


@router.get('/attempts')
def attempts(request_id: str, user: dict = Depends(security.current_user)) -> list[dict]:
    """Exact gateway attempt events; fields contain no prompts or credentials."""
    if len(request_id)>128:return []
    # 2026-10-28 修复：直接返回 sqlite3.Row 列表时 FastAPI 按 `-> list[dict]` 序列化会抛
    # ResponseValidationError → 只要该请求有 attempt 行就 500（空结果看不出来，所以一直
    # 没被发现）。这里显式转 dict；行内容不变，只是可序列化。
    return [dict(row) for row in db.query(
        'SELECT * FROM attempt_events WHERE request_id=? ORDER BY attempt,ts,phase',
        (request_id,))]


@router.get('/attempts/billing')
def attempts_billing(request_id: str, user: dict = Depends(security.current_user)) -> dict:
    """这次请求有几个 attempt、各自有没有计费证据（U04）。

    只读；返回 `ledger.attempt_billing_report()`：每个 attempt 的三态
    （reported / unknown / not_applicable）+ 来源（provider_receipt / absent）+
    evidence，以及请求级 usage_events 事实。`per_attempt_billing` 恒为
    `not_derived` —— 缺少池侧回执前不做按 attempt 计费，也不编造金额。
    """
    from .. import ledger
    if not request_id or len(request_id) > 128:
        raise HTTPException(status_code=400, detail='request_id 必填且不超过 128 字符')
    return ledger.attempt_billing_report(request_id)


@router.get('/attempts/explain')
def attempts_explain(request_id: str, user: dict = Depends(security.current_user)) -> dict:
    """这次请求的 attempt 时间线 + **解释边界**（U22）。

    只读；返回 `ledger.attempt_timeline()`：每个 attempt 的 phase 序列、起止与
    耗时、是否到达上游、已落库的计费事实，外加请求级终态。

    **必须一起读的边界**：响应里 `explainable=false` +
    `reason=upstream_does_not_expose_selection_snapshot`。当前只能回答
    「选中了哪个账号、在哪个阶段结束/失败」，**不能**回答「为什么选它而不是别的」
    —— 上游 /v1 只回结果，不回候选集 / 排除原因 / 权重 / 冷却快照。缺的证据清单
    在 `boundary.missing_evidence` 里，别把"没查到"读成"没发生"。
    """
    from .. import ledger
    if not request_id or len(request_id) > 128:
        raise HTTPException(status_code=400, detail='request_id 必填且不超过 128 字符')
    return ledger.attempt_timeline(request_id)


@router.post('/clear')
def clear_logs(user: dict = Depends(security.require_session_admin)) -> dict:
    db.execute('DELETE FROM request_logs')
    return {'ok': True}
