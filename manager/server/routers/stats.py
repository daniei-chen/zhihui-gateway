"""用量统计聚合。"""
from __future__ import annotations

import datetime
import json
import ssl
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from .. import config, db, observability, security
from ..services import wb2api

# 2026-10-27 统计归一（用户需求）：国内版 cn: 前缀是上游路由约定，统计展示一律剥掉、同名合并；global: 原样保留。
MODEL_BARE_SQL = "CASE WHEN model LIKE 'cn:%' THEN substr(model, 4) ELSE model END"

router = APIRouter(prefix='/api/stats', tags=['stats'])


# days 的取值范围。**必须有上限**：超大整数在 SQLite 绑定时溢出抛错
# （实测 /api/logs?days=999999999999999 返回 500）。
# 10 年足够覆盖任何正常查询，同时避免溢出与全表扫描。
_DAYS_MAX = 3650


def _norm_realm(realm: str | None) -> str | None:
    """归一 realm；拼错显式 400（U17 合同）。

    此前多处都是 `realm in ("cn", "global")` 才过滤，拼错（`glboal`/`eu`）
    会静默退化成「不过滤」，调用方拿到全量却以为是单版本数据。
    返回 None 表示「不按版本过滤」，返回值本身可直接作真值判断。
    """
    if realm is None or realm == "":
        return None
    if realm not in ("cn", "global"):
        raise HTTPException(status_code=400, detail="invalid_realm")
    return realm


def _clamp_days(days: int | None, default: int) -> int:
    """把 days 钳到 [1, _DAYS_MAX]；非法值退回默认。"""
    try:
        d = int(days) if days is not None else default
    except (TypeError, ValueError):
        return default
    return min(_DAYS_MAX, max(1, d))


def _since(days: int) -> str:
    """这段时间范围的**起始日期**（本地时区，含首日）。

    语义是「近 N 天」且**含今天**：`days=1` 正好就是「今天一天」（起始日 = 今天），
    `days=7` 是今天与前 6 天。所以界面上的「今日」传 1 就够，不必另开参数——
    趋势图、按模型、按密钥三处与时段选择器因此天然同一口径（issue #53）。
    """
    d = _clamp_days(days, 30)
    return time.strftime('%Y-%m-%d', time.localtime(time.time() - (d - 1) * 86400))


def _epoch_since(days: int) -> int:
    """与 `_since` 同一口径（本地时区、含今天）的起始时刻（epoch 秒）。"""
    return int(time.mktime(time.strptime(_since(days), '%Y-%m-%d')))


def _throughput_by_model(days: int, realm: str | None) -> dict[str, dict]:
    """按模型算**输出速率**（tokens/秒，issue #214）。

    数据源必须是 `request_logs`：`usage_daily` 只累计请求数与 token 数，没有耗时。

    口径（写清楚是因为「tok/s」很容易被算成好看但没意义的数）：

      · 生成耗时 = `latency_ms − first_token_ms`。流式请求的 first_token 是首字
        延迟，两者相减正好是「开始吐字到结束」；非流式没有中间过程，first_token
        为 NULL，用整个 latency（略低估，但不会凭空造数）；
      · 只统计成功（2xx）、有输出 token、耗时为正的记录 —— 失败请求的 latency
        含重试/排队，算进去会把速率压低成噪声；
      · 只覆盖**请求日志保留期**内的数据，返回 `throughput_samples` 让界面如实
        标注样本量（统计页的时间范围可能比日志保留期长）。
    """
    since = _epoch_since(days)
    rf = ' AND realm = ?' if _norm_realm(realm) else ''
    args: tuple = (since,) + ((realm,) if rf else ())
    rows = db.query(
        'SELECT ' + MODEL_BARE_SQL + ' AS name, COUNT(*) AS samples, '
        'SUM(completion_tokens) AS ct, '
        'SUM(CASE WHEN latency_ms > COALESCE(first_token_ms, 0) '
        '         THEN latency_ms - COALESCE(first_token_ms, 0) ELSE 0 END) AS gen_ms '
        'FROM request_logs '
        'WHERE ts >= ? AND status >= 200 AND status < 300 AND completion_tokens > 0 '
        # 生成耗时必须为正：latency=0 或 latency<=first_token 的记录算不出速率，
        # 留在分母外也不该把它们的 token 计进分子（否则速率会虚高一倍）。
        'AND latency_ms > COALESCE(first_token_ms, 0)'
        f'{rf} GROUP BY ' + MODEL_BARE_SQL,
        args,
    )
    out: dict[str, dict] = {}
    for r in rows:
        name = r['name'] or '未知'
        gen_ms = float(r['gen_ms'] or 0)
        ct = float(r['ct'] or 0)
        out[name] = {
            'tokens_per_sec': round(ct / (gen_ms / 1000.0), 2) if gen_ms > 0 and ct > 0 else None,
            'throughput_samples': int(r['samples'] or 0),
        }
    return out


@router.get('/credit-reconcile')
def credit_reconcile(user: dict = Depends(security.current_user)) -> dict:
    """C-001：计费三面口径对账（**只读诊断**，绝不写库）。

    背景（2026-10-27 审计 S1）：同一批用量在三个面上曾经各不相同 ——
    `usage_events`（账本，只覆盖账本上线后）17.6k、`usage_daily`（展示，全量聚合）
    55.6k、`api_keys.used_credit_units`（配额执法）45.4k，最大差 3.17 倍。

    口径（U03 / 2026-10-28 复核后确立）：
      * 明细 `request_logs` 为全量权威，但**受 90 天保留期裁剪**
        （`db._prune_request_logs`）——它只能当"近期明细"，不能当终身累计；
      * 展示 `usage_daily` = 明细的按日聚合，同样可被重建/裁剪；
      * 执法 `api_keys.used_credit_units` = 该密钥**终身**累计，**不可**用明细覆盖。

    ⚠️ U03：本端点**只读**。任何"用明细总和覆盖 used_credit_units"的 apply 动作
    都已在代码侧封死（`db.assert_credit_units_unchanged`）：裁剪发生后再覆盖，
    等于把已消耗的配额退回去。需要修漂移请用
    `POST /api/stats/credit-reconcile/adjust`（基于不可变事件的增量调整）。

    NULL 与 0 必须分开看：`credit IS NULL` = 上游没回（**未知**，不能当 0）；
    `credit = 0` = 确实扣了 0。把未知混成 0 会让"少扣"看起来像"免费"。
    """
    scale = 1_000_000.0
    rows = [dict(r) for r in db.query(
        'SELECT k.id, k.name, k.used_credit_units, '
        '  COALESCE((SELECT SUM(r.credit) FROM request_logs r '
        '            WHERE r.key_id=k.id AND r.credit IS NOT NULL),0) AS detail, '
        '  (SELECT COUNT(*) FROM request_logs r '
        '   WHERE r.key_id=k.id AND r.credit IS NOT NULL) AS known_rows, '
        '  (SELECT COUNT(*) FROM request_logs r '
        '   WHERE r.key_id=k.id AND r.credit IS NULL) AS unknown_rows '
        'FROM api_keys k ORDER BY k.id')]
    items = []
    drift = []
    for r in rows:
        cur = float(r.get('used_credit_units') or 0) / scale
        want = float(r.get('detail') or 0)
        gap = round(want - cur, 6)
        bad = abs(gap) >= 0.005
        if bad:
            drift.append(int(r['id']))
        items.append({'key_id': int(r['id']), 'name': r.get('name'),
                      'enforced_credit': round(cur, 6),
                      'detail_credit': round(want, 6),
                      'gap': gap, 'ok': not bad,
                      'detail_credit_known_rows': int(r.get('known_rows') or 0),
                      'detail_credit_unknown_rows': int(r.get('unknown_rows') or 0),
                      # gap<0 多半是"明细被裁剪"而非"多扣"，不可据此下调
                      'gap_explained_by_retention': gap < 0})
    totals = {}
    row = dict(db.query_one(
        'SELECT '
        ' (SELECT COALESCE(SUM(credit),0) FROM request_logs WHERE credit IS NOT NULL) AS detail, '
        ' (SELECT COUNT(*) FROM request_logs WHERE credit IS NOT NULL) AS detail_known_rows, '
        ' (SELECT COUNT(*) FROM request_logs WHERE credit IS NULL) AS detail_unknown_rows, '
        ' (SELECT COUNT(*) FROM request_logs WHERE credit IS NOT NULL AND credit = 0) AS detail_zero_rows, '
        ' (SELECT COALESCE(SUM(credit_units),0)/1000000.0 FROM usage_events) AS ledger, '
        ' (SELECT COALESCE(SUM(COALESCE(credit_units, CAST(credit*1000000 AS INTEGER))),0)/1000000.0 FROM usage_daily) AS daily, '
        ' (SELECT COALESCE(SUM(used_credit_units),0)/1000000.0 FROM api_keys) AS enforced, '
        ' (SELECT COALESCE(SUM(credit),0) FROM request_logs '
        '    WHERE credit IS NOT NULL AND (key_id IS NULL OR key_id NOT IN (SELECT id FROM api_keys))) AS orphan'
    ) or {})
    for k in ('detail', 'ledger', 'daily', 'enforced', 'orphan'):
        totals[k] = round(float(row.get(k) or 0), 6)
    for k in ('detail_known_rows', 'detail_unknown_rows', 'detail_zero_rows'):
        totals[k] = int(row.get(k) or 0)
    orow = dict(db.query_one(
        'SELECT COUNT(*) AS n FROM request_logs '
        'WHERE credit IS NOT NULL AND (key_id IS NULL OR key_id NOT IN (SELECT id FROM api_keys))') or {})
    return {
        'scale': 'credit',
        'totals': totals,
        'orphan_rows': int(orow.get('n') or 0),
        'drift_keys': drift,
        'ok': not drift,
        'items': items,
        'read_only': True,
        'writes_enforced_credit': False,
        'adjust_endpoint': '/api/stats/credit-reconcile/adjust',
        'note': 'ledger 仅覆盖账本上线后的逐请求事件；detail 为全量权威但受 90 天裁剪；'
                'enforced 应等于 detail 按密钥聚合；orphan 属已删除密钥、不计入任何存活密钥；'
                'detail_unknown_rows（credit IS NULL=上游未回）不得与 detail_zero_rows'
                '（credit=0=确实扣 0）混同；gap<0 通常由裁剪解释，禁止用 detail 覆盖 enforced',
    }


@router.get('/summary')
def summary(realm: str | None = None,
            user: dict = Depends(security.current_user)) -> dict:
    """总览。

    realm（cn / global）非空时只统计该版本——界面按版本切换时用。

    附带的 `usage_health` 用于暴露「统计根本没在累计」这种情况：曾经有个缺陷
    （issue #9）让存量库上一行都写不进 usage_daily，而表现是完全静默的
    （页面照常轮询、数字只是不动），拖了几个版本才被发现。判据是**今天的
    请求日志有内容但今天的统计为 0**——正常部署不该出现这种组合。

    另外附带 `failures`（失败请求数，按 4xx / 5xx 分档）。**它单独取自
    request_logs，不能从 usage_daily 算**——后者只累计有 token 或有扣费的请求，
    被拒绝的调用与全池不可用（503，零 token）在里面**根本不存在**。实测线上
    一次凌晨的全池中断：31 次请求全部失败，而趋势图上显示为「没有请求」，
    用户只能去翻日志才知道出过事。详见 `_failures`。
    """
    today = time.strftime('%Y-%m-%d')
    week = _since(7)
    # 过滤片段：realm 为空不过滤（保持既有行为），非空则按列匹配
    rf = ' AND realm = ?' if _norm_realm(realm) else ''
    rargs: tuple = (realm,) if rf else ()

    def agg(where: str, args: tuple) -> tuple[int, int, float]:
        row = db.query_one(
            f'SELECT COALESCE(SUM(requests),0) AS r, '
            f'COALESCE(SUM(prompt_tokens + completion_tokens),0) AS t, '
            f'COALESCE(SUM(credit),0) AS c FROM usage_daily WHERE {where}{rf}',
            args + rargs,
        )
        return int(row['r']), int(row['t']), float(row['c'] or 0)

    t_req, t_tok, t_credit = agg('day = ?', (today,))
    w_req, w_tok, w_credit = agg('day >= ?', (week,))
    a_req, a_tok, a_credit = agg('1=1', ())

    # 活跃密钥：密钥本身**不分版本**（同一把密钥可调两个版本，由模型名决定路由），
    # 所以按版本时应统计「**该版本有调用**的密钥数」，而不是「属于该版本的密钥数」
    # ——后者在架构上不存在。如实标注口径，避免让人以为密钥是隔离的。
    if _norm_realm(realm):
        active_keys = db.query_one(
            'SELECT COUNT(DISTINCT key_id) AS c FROM usage_daily WHERE realm = ?',
            (realm,),
        )['c']
    else:
        active_keys = db.query_one('SELECT COUNT(*) AS c FROM api_keys WHERE enabled = 1')['c']
    top = db.query_one(
        'SELECT ' + MODEL_BARE_SQL + ' AS model, SUM(requests + prompt_tokens + completion_tokens) AS score '
        f'FROM usage_daily WHERE 1=1{rf} GROUP BY ' + MODEL_BARE_SQL + ' ORDER BY score DESC LIMIT 1',
        rargs,
    )
    return {
        'today_requests': t_req,
        'today_tokens': t_tok,
        # 实际扣费（上游 usage.credit 合计）；上游未返回该字段时恒为 0
        'today_credit': t_credit,
        'week_credit': w_credit,
        'total_credit': a_credit,
        'week_requests': w_req,
        'week_tokens': w_tok,
        'total_requests': a_req,
        'total_tokens': a_tok,
        'active_keys': int(active_keys),
        'top_model': top['model'] if top else None,
        'usage_health': _usage_health(today, t_req, realm),
        'failures': _failures(realm),
    }


# ── U21（2026-10-28）：请求轨迹的分类统计 ───────────────────────────────────
#
# 与 `_failures()` 的分工：那边只回答「今天/本周有多少 4xx/5xx」，取自
# `request_logs`（因为 `usage_daily` 根本不记被拒请求）；这里回答「失败**是什么
# 原因**、这次发布后哪类变多了」，取自 `request_outcomes` + U21 的稳定
# `error_code`，并且**显式区分 unknown 与 0**（读不到的证据一律 null + 原因，
# 不写 0 冒充"没有"）。
#
# `request_outcomes` 没有 realm 列，按版本过滤走 `request_logs` 联表
# （历史行 realm 为 NULL → 按 cn 归类，与 `/api/logs` 同一口径）。

_OUTCOME_CLASS_SQL = (
    "CASE WHEN {col} = 'completed' THEN 'completed' "
    "WHEN {col} = 'client_cancelled' THEN 'cancelled' "
    "WHEN {col} = 'protocol_incomplete' THEN 'protocol_incomplete' "
    "WHEN {col} IN ('rejected','upstream_error','transport_error',"
    "'application_error','interrupted') THEN 'failed' "
    "ELSE 'unknown' END"
)
OUTCOME_CLASSES = ('completed', 'failed', 'cancelled', 'protocol_incomplete', 'unknown')

#: 499 = 客户端主动断开（nginx 记为 `client closed request`）。
#:
#: **它不是网关失败**：长流式请求被客户端取消、被客户端超时切断，都会落成 499；
#: 上游把这些请求记成 `client_cancelled`（`_OUTCOME_CLASS_SQL` 里就是 'cancelled'）。
#: 2026-10-28 实测：近 7 天 `status >= 400` 共 390 条，其中 129 条是 499 ——
#: 把它们算进「失败」会让总览页的失败数虚高近 50%，把真正的上游故障埋掉
#: （真实失败是 261 条）。所以下面两处「失败数」的 SQL 都显式排除 499，
#: 并把 499 单独以 `*_client_closed` 报出来，口径不丢但要分清性质。
CLIENT_CLOSED = 499

#: 备份"新鲜"的判据（与 operational.evaluate_backup_health 同口径：26 小时）。
_BACKUP_FRESH_S = 26 * 3600
#: 证书剩余天数低于它就报 expiring（Let's Encrypt 常规续期窗口是 30 天）。
_CERT_WARN_DAYS = 21
#: 证书到期证据的可能位置（**只读**）。读不到就显式 unknown，不猜。
CERT_EVIDENCE_FILES = (Path('/var/lib/zhihui-gateway/cert-status.json'),)
CERT_LIVE_DIRS = (Path('/etc/letsencrypt/live'),)


def _backup_health(now: int) -> dict:
    """备份证据的年龄。缺失/不可读 = `unknown`（**不是** 0 秒，也不是健康）。"""
    path = Path(config.BACKUP_STATUS_FILE)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        stat = path.stat()
    except (OSError, ValueError):
        return {'status': 'unknown', 'age_seconds': None, 'source': str(path),
                'reason': '备份证据文件缺失或不可读'}
    created = data.get('created_at') if isinstance(data, dict) else None
    stamp = int(created) if isinstance(created, int) and not isinstance(created, bool) else None
    age = int(now - (stamp if stamp is not None else stat.st_mtime))
    restore = data.get('restore_check') if isinstance(data, dict) else None
    size = data.get('bytes') if isinstance(data, dict) else None
    verified = restore == 'ok' and isinstance(size, int) and not isinstance(size, bool) and size > 0
    if not verified:
        status, reason = 'unknown', '备份证据未通过校验（restore_check != ok 或 size 非法）'
    elif age > _BACKUP_FRESH_S:
        status, reason = 'stale', '最近一次已校验备份超过 %d 小时' % (_BACKUP_FRESH_S // 3600)
    else:
        status, reason = 'ok', None
    return {'status': status, 'age_seconds': age, 'restore_check': restore,
            'bytes': size, 'created_at': stamp, 'source': str(path), 'reason': reason}


def _certificate_health(now: int) -> dict:
    """证书到期时间。读不到就 `unknown` —— 绝不返回 0 天或伪造日期。"""
    for path in CERT_EVIDENCE_FILES:
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        stamp = None
        if isinstance(data, dict):
            for key in ('not_after', 'expires_at', 'notAfter'):
                value = data.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    stamp = int(value)
                    break
        if stamp is not None:
            return _cert_result(stamp, now, str(path), data.get('domain')
                                if isinstance(data, dict) else None)
    for directory in CERT_LIVE_DIRS:
        try:
            candidates = sorted(Path(directory).glob('*/cert.pem'))
        except OSError:
            continue
        for cert in candidates:
            try:
                decoded = ssl._ssl._test_decode_cert(str(cert))  # noqa: SLF001 - 只解析不校验证书链
                not_after = decoded.get('notAfter')
                stamp = int(ssl.cert_time_to_seconds(not_after))
            except (OSError, ValueError, AttributeError, TypeError):
                continue
            return _cert_result(stamp, now, str(cert), cert.parent.name)
    return {'status': 'unknown', 'not_after': None, 'days_left': None, 'domain': None,
            'source': None,
            'reason': '证书到期证据不可得（/etc/letsencrypt 对服务进程不可读，'
                      '且没有已发布的 cert-status.json）'}


def _cert_result(stamp: int, now: int, source: str, domain) -> dict:
    days_left = int((stamp - now) // 86400)
    if days_left < 0:
        status = 'expired'
    elif days_left <= _CERT_WARN_DAYS:
        status = 'expiring'
    else:
        status = 'ok'
    return {'status': status, 'not_after': stamp, 'days_left': days_left,
            'domain': domain if isinstance(domain, str) else None,
            'source': source, 'reason': None}


@router.get('/outcomes')
def outcomes(days: int = 7, realm: str | None = None, top: int = 10,
             user: dict = Depends(security.current_user)) -> dict:
    """失败分类计数 + error_code Top-N + 备份年龄 + 证书期限（U21）。

    **unknown 不冒充 0**：读不到的证据返回 `null` + `reason`（备份、证书），
    历史行没有 `error_code` 时单独报 `error_code_coverage`，而不是把它算成
    「没有错误」。

    `traffic_class` 把探针（`/healthz`、`/api/readyz`、`/metrics`）与管理动作
    （`/api/*`）从真实推理流量里切出来 —— 否则一次面板轮询就能把失败率抬高。

    `error_code_top` **不含 `none`**（那是"没有错误"，会把真正的错因挤下去）；
    成功量看 `classes.completed`。按平台过滤请走
    `/api/logs/outcomes?upstream_platform=`：`?platform=qoder|trae` 会被
    `main.py::_qtg_platform_route` 重写进 QTG 控制面（`/api/stats/` 在其白名单里），
    本端点根本收不到。
    """
    d = _clamp_days(days, 7)
    limit = min(50, max(1, int(top)))
    since = _epoch_since(d)
    now = int(time.time())
    rf = " AND COALESCE(l.realm, 'cn') = ?" if _norm_realm(realm) else ''
    rargs: tuple = (realm,) if rf else ()
    has_code = observability.ensure_outcome_schema()
    class_expr = _OUTCOME_CLASS_SQL.format(col='o.terminal_state')
    traffic_expr = observability.traffic_class_sql('o.endpoint')
    joined = 'FROM request_outcomes o LEFT JOIN request_logs l ON l.request_id = o.request_id'
    base = f'{joined} WHERE o.ts >= ?{rf}'
    base_args: tuple = (since,) + rargs

    def counts(expr: str, extra: str = '', extra_args: tuple = ()) -> dict:
        rows = db.query(f'SELECT {expr} AS k, COUNT(*) AS n {base}{extra} GROUP BY k',
                        base_args + extra_args)
        return {str(row['k']): int(row['n']) for row in rows}

    classes = {name: 0 for name in OUTCOME_CLASSES}
    classes.update(counts(class_expr))
    by_terminal = counts('COALESCE(o.terminal_state, \'(null)\')')
    by_traffic = counts(traffic_expr)
    total = sum(classes.values())

    top_rows = []
    coverage = {'with_code': None, 'without_code': None, 'total': None, 'ratio': None,
                'note': 'error_code 列不可用（补列失败或旧库），覆盖率未知'}
    if has_code:
        top_rows = [
            {'error_code': row['code'], 'error_source': row['source'], 'count': int(row['n'])}
            for row in db.query(
                f'SELECT o.error_code AS code, o.error_source AS source, COUNT(*) AS n '
                f'{base} AND o.error_code IS NOT NULL AND o.error_code <> ? '
                f'GROUP BY o.error_code, o.error_source ORDER BY n DESC LIMIT ?',
                base_args + (observability.ERROR_CODE_NONE, limit))
        ]
        row = db.query_one(
            f'SELECT SUM(CASE WHEN o.error_code IS NULL THEN 1 ELSE 0 END) AS missing, '
            f'COUNT(*) AS total {base}', base_args)
        with_code = int(row['total'] or 0) - int(row['missing'] or 0)
        coverage = {
            'with_code': with_code,
            'without_code': int(row['missing'] or 0),
            'total': int(row['total'] or 0),
            'ratio': (round(with_code / int(row['total']), 4) if row['total'] else None),
            'note': 'NULL = 未分类（U21 上线前的历史行，或该路径没有稳定码）；不是"没出错"',
        }

    releases = [
        {'release_id': row['release_id'], 'requests': int(row['n']),
         'failed': int(row['failed'] or 0), 'cancelled': int(row['cancelled'] or 0)}
        for row in db.query(
            f"SELECT o.release_id AS release_id, COUNT(*) AS n, "
            f"SUM(CASE WHEN {class_expr} = 'failed' THEN 1 ELSE 0 END) AS failed, "
            f"SUM(CASE WHEN {class_expr} = 'cancelled' THEN 1 ELSE 0 END) AS cancelled "
            f"{base} GROUP BY o.release_id ORDER BY n DESC LIMIT 5", base_args)
    ]

    return {
        'ok': True,
        'window': {'days': d, 'since': since, 'until': now, 'realm': realm},
        'requests': total,
        'classes': classes,
        'by_terminal_state': by_terminal,
        'by_traffic_class': by_traffic,
        'error_code_top': top_rows,
        'error_code_coverage': coverage,
        'by_release': releases,
        'backup': _backup_health(now),
        'certificate': _certificate_health(now),
        'notes': [
            'classes.unknown 是"终态不在已知集合里"的行数，不是 0 失败的等价物',
            'backup/certificate 读不到证据时 status=unknown 且 age/days_left 为 null，不写 0',
            'traffic_class 把探针与管理动作从真实推理流量里切开（同一口径见 /api/logs/outcomes）',
        ],
    }


def _failures(realm: str | None = None) -> dict:
    """今天 / 近 7 天的失败请求数，按 4xx 与 5xx 分档。

    为什么必须单独算：`usage_daily` 只累计**有 token 或有扣费**的请求
    （`bump_usage` 的调用条件是 `if total or credit`，刻意如此——否则「只有被
    拒绝的调用」的部署会被 `_usage_health` 误报成「统计没在累计」）。代价是
    失败请求在用量表里**完全不存在**：

      · 403 / 429（密钥被拒、配额用尽、限流）→ 零 token，不累计；
      · 503（池里没有可用账号）→ 零 token，不累计；
      · 502（上游不可用）→ 同样。

    于是界面上「请求数」只反映成功的量，趋势图里失败的那段是**空白**——用户
    看不到"出过事"，只能去翻日志。实测线上凌晨一次全池中断：31 次请求全部
    失败，趋势图上却显示为没有请求。

    `request_logs` 是**无条件**记录每一笔的（见 `gateway._record`），所以它是
    失败数的唯一可靠来源。这里不做任何「是否该累计」的过滤：凡是被拒或被
    上游打回的都是失败，正是用户想看到的。

    口径说明：只统计**带了密钥**的请求（`key_id IS NOT NULL`）。没有密钥的
    401 是扫描器/配置错误打进来的噪声，混进来会让失败数失去信号价值。
    """
    out: dict = {
        'today_4xx': 0, 'today_5xx': 0,
        'week_4xx': 0, 'week_5xx': 0,
    }
    try:
        today = time.strftime('%Y-%m-%d')
        week = _since(7)
        # realm 过滤：日志表的 realm 列可能是历史 NULL（该列是后加的），按 cn 归类
        # ——**必须写成 `COALESCE(realm,'cn') = ?`，不能写成 `COALESCE(realm,?) = ?`**：
        # 后者会把 NULL 兜成「当前查询的那个版本」，于是看国际版时历史 NULL 行
        # 也被算进来（实测：只看 global 却统计到 cn 的历史记录）。
        rf = " AND COALESCE(realm, 'cn') = ?" if _norm_realm(realm) else ''
        for label, since in (('today', today), ('week', week)):
            # 用 `ts >= 当地零点` 而不是 `day_sql(ts) >= 日期串`：两者逐行等价
            # （见 test_day_filter_equivalence.py 的边界样本），但后者把 ts 包在
            # 函数里，查询计划退化成 SCAN（扫整个索引），前者是 SEARCH（按范围
            # 定位）。本接口在总览页被 30 秒轮询，是热路径。
            args = (db.day_start_ts(since),) + ((realm,) if rf else ())
            row = db.query_one(
                f"SELECT SUM(CASE WHEN status >= 400 AND status < 500 "
                f"AND status <> {CLIENT_CLOSED} THEN 1 ELSE 0 END) AS c4, "
                f"SUM(CASE WHEN status >= 500 THEN 1 ELSE 0 END) AS c5, "
                f"SUM(CASE WHEN status = {CLIENT_CLOSED} THEN 1 ELSE 0 END) AS cc "
                f"FROM request_logs WHERE ts >= ? AND key_id IS NOT NULL{rf}",
                args,
            )
            if row:
                out[f'{label}_4xx'] = int(row['c4'] or 0)
                out[f'{label}_5xx'] = int(row['c5'] or 0)
                # 499 单独报，不计入 4xx/5xx：它是客户端断开，不是网关失败
                out[f'{label}_client_closed'] = int(row['cc'] or 0)
    except Exception:  # noqa: BLE001
        # 统计属旁路：任何一个数算不出来都不该让总览接口失败（失败计数只是
        # 锦上添花，总览本身有更多关键数字）。返回全 0，前端按「无失败」显示。
        pass
    return out


def _usage_health(today: str, today_requests: int, realm: str | None = None) -> dict:
    """检测「用量统计没有在累计」——本该累计的请求有日志、但统计为 0。

    为什么值得单独做：统计写入是旁路（失败只记 warning，不影响转发），
    所以一旦写入路径坏了，**用户侧完全看不出异常**：页面照常刷新、数字只是
    停着不动。issue #9 就是这个形态，拖了几个版本才有人发现。

    **判据必须与「什么请求才会累计用量」完全对齐**，否则会误报。这是被两次
    真实误报教会的：

      1. **版本口径**：日志按全版本数、用量按当前版本过滤 → 只有国内版流量时
         切到国际版必然触发（实测报告过 605 次那个数字）；
      2. **请求类型**：`_record` 里 `request_logs` 是**无条件**写的，而
         `bump_usage` 只在 `key 且 (tokens 或 credit)` 时调用。于是「今天只有
         被拒绝的调用」（403 / 429 / 无 key）或「只调了 /v1/models」（0 token）
         会留下日志却没有用量行 —— 那种部署**完全健康**，却被报「统计可能没有
         正常写入」（实测复现过）。

    所以这里只数**本该产生用量行**的那部分日志：有 key、且 token 或扣费非 0。
    这与 gateway._record 的判断条件是同一份口径；那边改了这里也要跟着改。

    返回里带上结构化数字，**不要让前端去正则解析这句中文**（曾经这么做，
    改文案时前端静默失效、译文形同虚设）。
    """
    try:
        # 与用量同一口径：看某版本时只看该版本（COALESCE 把历史 NULL 归 cn）
        rgt = ' AND COALESCE(realm, ?) = ?' if _norm_realm(realm) else ''
        # 只数「本该累计」的：与 _record 的 `if key:` 且 `if total or credit:` 对齐
        should = (
            'key_id IS NOT NULL'
            " AND (COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0) > 0"
            '      OR COALESCE(credit,0) > 0)'
        )
        # 用 ts 的半开区间 [当地零点, 次日零点) 代替 `day_sql(ts) = 日期串`：
        # 逐行等价（见 test_day_filter_equivalence.py），但前者走索引。
        #
        # **等值必须给上下界**：只写 `ts >= 零点` 会把次日、下月、明年的记录
        # 全算成今天 —— 那比慢更糟（数字直接错）。所以这里明确写 `< 次日零点`。
        logs_today = db.query_one(
            f'SELECT COUNT(*) AS c FROM request_logs '
            f'WHERE ts >= ? AND ts < ? AND {should}{rgt}',
            (db.day_start_ts(today), db.day_start_ts(today) + 86400)
            + (('cn', realm) if rgt else ()),
        )
        n = int(logs_today['c']) if logs_today else 0
    except Exception:  # noqa: BLE001
        return {'ok': True, 'detail': '', 'logs_today': 0}
    if n > 0 and today_requests == 0:
        scope = {'cn': '国内版', 'global': '国际版'}.get(str(realm or ''), '')
        return {
            'ok': False,
            'logs_today': n,
            'detail': (f'今天{scope}已有 {n} 次调用记录，但用量统计为 0——统计可能没有 '
                       f'正常写入。可尝试「修复统计」；若仍为 0，请查看服务端日志。'),
        }
    return {'ok': True, 'detail': '', 'logs_today': 0}


@router.post('/repair-usage')
def repair_usage(user: dict = Depends(security.require_admin)) -> dict:
    """按请求日志回填用量统计的缺口（幂等，可重复执行）。

    用于修复历史缺陷导致的部分调用未计入统计。
    """
    try:
        return db.backfill_usage_from_logs()
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(409, str(exc)) from exc


@router.post('/rebuild-usage')
def rebuild_usage(user: dict = Depends(security.require_admin)) -> dict:
    """以请求日志为准重建用量统计（清理时区口径不一致造成的重复计数）。

    与 /repair-usage 的区别：repair 只补缺口（增量、幂等），
    本接口是**重建**——会替换 usage_daily 的内容，能删除此前多出来的行。
    """
    try:
        return db.rebuild_usage_from_logs()
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(409, str(exc)) from exc


@router.get('/daily')
def daily(days: int = 30, realm: str | None = None,
          user: dict = Depends(security.current_user)) -> list[dict]:
    """按天聚合。realm 非空时只统计该版本。

    `failed` 是当天的失败请求数（4xx / 5xx 合计），**来自 request_logs 而不是
    usage_daily**：后者不含被拒绝与零 token 的失败请求，失败数在其中恒为 0
    （详见 `_failures`）。趋势图用它把「失败」画出来——否则全池中断那天的
    曲线是空的，看不出出过事。
    """
    rf = ' AND realm = ?' if _norm_realm(realm) else ''
    args = (_since(max(1, days)),) + ((realm,) if rf else ())
    rows = db.query(
        'SELECT day, SUM(requests) AS requests, '
        'SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, '
        'COALESCE(SUM(credit),0) AS credit, '
        'COALESCE(SUM(cache_hit_tokens),0) AS cache_hit_tokens, '
        'COALESCE(SUM(cache_miss_tokens),0) AS cache_miss_tokens '
        f'FROM usage_daily WHERE day >= ?{rf} GROUP BY day ORDER BY day ASC',
        args,
    )
    failures = _daily_failures(days, realm)
    days_seen = {r['day'] for r in rows}
    out = [
        {
            'day': r['day'],
            'requests': int(r['requests'] or 0),
            'prompt_tokens': int(r['prompt_tokens'] or 0),
            'completion_tokens': int(r['completion_tokens'] or 0),
            'credit': float(r['credit'] or 0),
            'failed': failures.get(r['day'], 0),
            'cache_hit_tokens': int(r['cache_hit_tokens'] or 0),
            'cache_miss_tokens': int(r['cache_miss_tokens'] or 0),
        }
        for r in rows
    ]
    # **只有失败、没有成功**的日子也要出现：那一天正是最该被看到的。
    # 只按 usage_daily 出行的话，全池中断日会整条缺失（它没有任何成功请求），
    # 趋势图上等于那天不存在——比画成 0 更糟。
    for day, n in sorted(failures.items()):
        if day not in days_seen and n > 0:
            out.append({
                'day': day, 'requests': 0, 'prompt_tokens': 0,
                'completion_tokens': 0, 'credit': 0.0, 'failed': n,
            })
    out.sort(key=lambda d: d['day'])
    return out


def _daily_failures(days: int, realm: str | None = None) -> dict[str, int]:
    """按天统计**网关失败**请求数（4xx + 5xx，**不含 499 客户端断开**）。

    口径与 `_failures` 一致：499 是客户端取消/超时断开，账不记在网关头上。
    """
    try:
        # 同 `_failures`：NULL 固定归 cn，不能把 NULL 兜成查询目标版本
        rf = " AND COALESCE(realm, 'cn') = ?" if _norm_realm(realm) else ''
        # 过滤用 ts 范围（走索引），**分组**仍必须用日期表达式（要按天聚合，
        # 这个没法避免）。两者分开：过滤是热路径上的大头，分组只作用于过滤后的行。
        args = (db.day_start_ts(_since(max(1, days))),) + ((realm,) if rf else ())
        rows = db.query(
            f"SELECT {db.day_sql('ts')} AS day, COUNT(*) AS n FROM request_logs "
            f"WHERE ts >= ? AND status >= 400 AND status <> {CLIENT_CLOSED} "
            f"AND key_id IS NOT NULL{rf} "
            f'GROUP BY day',
            args,
        )
        return {str(r['day']): int(r['n']) for r in rows if r['day']}
    except Exception:  # noqa: BLE001
        return {}


@router.get('/hourly')
def hourly(day: str | None = None, realm: str | None = None,
           user: dict = Depends(security.current_user)) -> list[dict]:
    """某天**按小时**聚合（默认今天）。「今日」趋势图用它 —— 范围只有一天时
    按天聚合只会得到一根柱子，看不出「今天什么时候忙」。

    与 `/daily` 同一套口径：
      · 用量取自 `usage_hourly`，它与 `usage_daily` 由**同一处写入**累计，
        所以这里的合计与页头卡片天然一致（清日志、日志保留期裁剪都不影响）；
      · `failed` 仍来自 `request_logs`（`usage_*` 只累计有 token 或有扣费的请求，
        被拒绝与零 token 的失败在里面根本不存在）。

    **固定返回 24 个桶（补零）**：X 轴长度不随「今天走到几点」变化，图上不会
    因为还没到的时段而抖动；没数据的时段就是 0，读起来也清楚。
    """
    d = (day or '').strip() or time.strftime('%Y-%m-%d')
    # 只接受 YYYY-MM-DD：这是个查询参数，写错形态就回落到今天，而不是抛错
    # （趋势图不该因为一个手改的 URL 变成红色错误态）。
    if len(d) != 10 or d[4] != '-' or d[7] != '-':
        d = time.strftime('%Y-%m-%d')
    rf = ' AND realm = ?' if _norm_realm(realm) else ''
    rargs: tuple = (realm,) if rf else ()
    rows = db.query(
        'SELECT hour, SUM(requests) AS requests, '
        'SUM(prompt_tokens) AS prompt_tokens, '
        'SUM(completion_tokens) AS completion_tokens, '
        'COALESCE(SUM(credit),0) AS credit '
        f'FROM usage_hourly WHERE day = ?{rf} GROUP BY hour ORDER BY hour ASC',
        (d,) + rargs,
    )
    by_hour = {int(r['hour']): r for r in rows}
    failures = _hourly_failures(d, realm)
    out: list[dict] = []
    for h in range(24):
        r = by_hour.get(h)
        out.append({
            'day': d,
            'hour': h,
            'requests': int(r['requests'] or 0) if r else 0,
            'prompt_tokens': int(r['prompt_tokens'] or 0) if r else 0,
            'completion_tokens': int(r['completion_tokens'] or 0) if r else 0,
            'credit': float(r['credit'] or 0) if r else 0.0,
            'failed': failures.get(h, 0),
        })
    return out


def _hourly_failures(day: str, realm: str | None = None) -> dict[int, int]:
    """某天**按小时**的网关失败请求数（4xx + 5xx，不含 499）。口径同 `_failures`。"""
    try:
        rf = " AND COALESCE(realm, 'cn') = ?" if _norm_realm(realm) else ''
        # 过滤用 ts 范围（走索引），分组用小时表达式（要聚合，躲不开）
        args = (db.day_start_ts(day), db.day_start_ts(day) + 86400) + ((realm,) if rf else ())
        rows = db.query(
            f"SELECT {db.hour_sql('ts')} AS bucket, COUNT(*) AS n FROM request_logs "
            f"WHERE ts >= ? AND ts < ? AND status >= 400 AND status <> {CLIENT_CLOSED} "
            f"AND key_id IS NOT NULL{rf} "
            f'GROUP BY bucket',
            args,
        )
        out: dict[int, int] = {}
        for r in rows:
            bucket = str(r['bucket'] or '')
            if len(bucket) >= 13:
                out[int(bucket[11:13])] = int(r['n'])
        return out
    except Exception:  # noqa: BLE001
        return {}


# ── 积分收支（收入 / 支出）───────────────────────────────
#
# 两个数字来自**两套不同口径**：
#   · 收入 = task_logs 里 kind='credit' 的余额增加流水；
#   · 支出 = usage_daily.credit（每次调用的真实扣费合计）。
#
# 为什么收入不把 task_logs 的全部 kind 加起来：那条流里混着「令牌保活成功」
# 这类把 credits 当布尔标记写的行（renew.py 的 `_record(acct, 1, ...)`），
# 直接求和会把续期次数当成积分收入。只用 kind='credit' 才是真正的余额增加。


def _realm_uids(realm: str | None) -> set[str] | None:
    """该版本（cn/global）的完整 uid 集合；realm 非法返回 None（= 不过滤）。

    task_logs 没有 realm 列，收入要按版本筛只能把 uid 映射过去。`kind='credit'`
    的 uid 由 credits.record_balance 写入，是**完整 uid**，所以可以精确匹配，
    不需要像上游日志那样兼容 8 位前缀。
    """
    if realm not in ('cn', 'global'):
        return None
    try:
        accounts = wb2api.list_auth_accounts()
    except Exception:  # noqa: BLE001
        return set()
    return {str(a.get('uid')) for a in accounts
            if a.get('uid') and (a.get('realm') or 'cn') == realm}


def _credit_income_by_day(since: str, realm: str | None) -> dict[str, float]:
    """收入：`kind='credit'` 流水按本地日聚合（见本段顶部说明）。"""
    uids = _realm_uids(realm)
    if uids is not None and not uids:
        return {}
    sql = (f"SELECT {db.day_sql('ts')} AS day, COALESCE(SUM(credits),0) AS income "
           "FROM task_logs WHERE kind = 'credit' AND ts >= ?")
    args: list = [db.day_start_ts(since)]
    if uids is not None:
        sql += f' AND uid IN ({",".join("?" * len(uids))})'
        args.extend(sorted(uids))
    sql += ' GROUP BY day'
    rows = db.query(sql, tuple(args))
    return {str(r['day']): float(r['income'] or 0) for r in rows if r['day']}


@router.get('/credits')
def credits_flow(days: int = 30, realm: str | None = None,
                 user: dict = Depends(security.current_user)) -> dict:
    """积分收支：按天给出收入与支出，并给出今日 / 近 7 天 / 区间合计。

    ## 收入口径（近似值，界面须如实说明，不要假装是精确账本）

    收入是 `credits.record_balance` 在**真的向腾讯查了一次余额、且比上次查询
    增加**时写下的流水。它覆盖签到 / 活跃 / 旅行等所有渠道（这些渠道大多不在
    上游日志里打数额），但有两个已知近似：

      · 两次查询之间「先增后耗」、净额不为正时，那笔增加不会被记下；
      · 长时间无人打开面板 / 无人刷新积分时，期间的多次增加会**合并**记到
        下一次查询那一刻所在的日期。

    ## 支出口径

    `usage_daily.credit` 是每次调用上游返回的真实扣费（`usage.credit`）当日合计，
    精确到每次请求。两个口径精度不同，所以不把「净额」当作唯一结论，而是收入 /
    支出 / 净额三项都列。
    """
    d = _clamp_days(days, 30)
    since = _since(d)
    today = time.strftime('%Y-%m-%d')

    # 支出：usage_daily 带 realm 列，可直接筛
    rf = ' AND realm = ?' if _norm_realm(realm) else ''
    rargs: tuple = (realm,) if rf else ()
    expense = {
        str(r['day']): float(r['credit'] or 0)
        for r in db.query(
            'SELECT day, COALESCE(SUM(credit),0) AS credit FROM usage_daily '
            f'WHERE day >= ?{rf} GROUP BY day',
            (since,) + rargs,
        )
    }
    income = _credit_income_by_day(since, realm)

    # 补齐每一天（含无数据的日子），折线才不会断成两截
    start = datetime.date(*time.localtime(db.day_start_ts(since))[:3])
    daily: list[dict] = []
    for i in range(d):
        day = (start + datetime.timedelta(days=i)).isoformat()
        inc = income.get(day, 0.0)
        exp = expense.get(day, 0.0)
        daily.append({'day': day, 'income': inc, 'expense': exp, 'net': inc - exp})

    week_from = _since(7)

    def _sum(rows: list[dict]) -> dict:
        inc = sum(r['income'] for r in rows)
        exp = sum(r['expense'] for r in rows)
        return {'income': inc, 'expense': exp, 'net': inc - exp}

    return {
        'days': d,
        'from': daily[0]['day'] if daily else since,
        'to': today,
        'today': _sum([r for r in daily if r['day'] == today]),
        'week': _sum([r for r in daily if r['day'] >= week_from]),
        'total': _sum(daily),
        'daily': daily,
    }


@router.get('/by-model')
def by_model(days: int = 30, realm: str | None = None,
             user: dict = Depends(security.current_user)) -> list[dict]:
    """按模型聚合。realm 非空时只统计该版本。"""
    rf = ' AND realm = ?' if _norm_realm(realm) else ''
    args = (_since(max(1, days)),) + ((realm,) if rf else ())
    rows = db.query(
        'SELECT ' + MODEL_BARE_SQL + ' AS name, SUM(requests) AS requests, '
        'SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, '
        'COALESCE(SUM(credit),0) AS credit, '
        'COALESCE(SUM(cache_hit_tokens),0) AS cache_hit_tokens, '
        'COALESCE(SUM(cache_miss_tokens),0) AS cache_miss_tokens '
        f'FROM usage_daily WHERE day >= ?{rf} GROUP BY ' + MODEL_BARE_SQL + ' ORDER BY SUM(prompt_tokens + completion_tokens) DESC',
        args,
    )
    # 输出速率来自请求日志（issue #214）：与上面的用量表**同区间不同源**，
    # 所以只给「两边都有」的模型补字段；日志里完全没有的模型给 null（显示 —）。
    tmap = _throughput_by_model(days, realm)
    out = []
    for r in rows:
        name = r['name'] or '未知'
        out.append({
            'name': name,
            'requests': int(r['requests'] or 0),
            'prompt_tokens': int(r['prompt_tokens'] or 0),
            'completion_tokens': int(r['completion_tokens'] or 0),
            'credit': float(r['credit'] or 0),
            'cache_hit_tokens': int(r['cache_hit_tokens'] or 0),
            'cache_miss_tokens': int(r['cache_miss_tokens'] or 0),
            **tmap.get(name, {'tokens_per_sec': None, 'throughput_samples': 0}),
        })
    return out


@router.get('/by-key')
def by_key(days: int = 30, realm: str | None = None,
           user: dict = Depends(security.current_user)) -> list[dict]:
    """按密钥聚合。realm 非空时只统计该版本。

    密钥本身不分版本（同一把密钥可调两个版本），这里筛的是「该密钥在该版本上
    的调用量」——所以同一把密钥在两个版本下都会出现，数字各是各的。这是如实
    反映架构，不是重复计数。
    """
    rf = ' AND u.realm = ?' if _norm_realm(realm) else ''
    args = (_since(max(1, days)),) + ((realm,) if rf else ())
    rows = db.query(
        'SELECT COALESCE(k.name, u.key_id || "") AS name, SUM(u.requests) AS requests, '
        'SUM(u.prompt_tokens) AS prompt_tokens, SUM(u.completion_tokens) AS completion_tokens, '
        'COALESCE(SUM(u.credit),0) AS credit, '
        'COALESCE(SUM(u.cache_hit_tokens),0) AS cache_hit_tokens, '
        'COALESCE(SUM(u.cache_miss_tokens),0) AS cache_miss_tokens '
        'FROM usage_daily u LEFT JOIN api_keys k ON k.id = u.key_id '
        f'WHERE u.day >= ?{rf} GROUP BY u.key_id ORDER BY SUM(u.prompt_tokens + u.completion_tokens) DESC',
        args,
    )
    return [
        {
            'name': r['name'] or '未知',
            'requests': int(r['requests'] or 0),
            'prompt_tokens': int(r['prompt_tokens'] or 0),
            'completion_tokens': int(r['completion_tokens'] or 0),
            'credit': float(r['credit'] or 0),
            'cache_hit_tokens': int(r['cache_hit_tokens'] or 0),
            'cache_miss_tokens': int(r['cache_miss_tokens'] or 0),
        }
        for r in rows
    ]


@router.get('/upstream')
async def upstream_stats(user: dict = Depends(security.current_user)) -> dict:
    """上游自己那份统计（`/v1/stats`，issue #59）。

    **口径与这一页其它数字不同，界面上必须分开摆**：本页的汇总 / 趋势 / 按模型 /
    按密钥都来自本网关的库、只含经过本网关的调用、且跟时段选择器走；这里返回的是
    上游进程自己的累计（含**直连 7863** 的调用），并且是从上游启动算起，没有时段
    可言。用户要看「原有那把密钥用了多少」只能从这里看——那把密钥直连上游，
    本网关看不见它。

    取不到时返回 `available=False` 与原因（上游没起来 / 版本太旧没这个端点 /
    api_key 不一致），界面据此说明，而不是显示一片空白让人以为「用量是 0」。
    """
    return await wb2api.get_upstream_stats()


@router.post('/credit-reconcile/adjust')
def credit_reconcile_adjust(payload: dict,
                            user: dict = Depends(security.require_admin)) -> dict:
    """基于**不可变事件**修正 `used_credit_units` 漂移（管理员；增量、只上调）。

    为什么不提供"按明细覆盖"（U03，P0）：`request_logs` 有 90 天保留期
    （`db._prune_request_logs`），而 `used_credit_units` 是终身累计。用可裁剪的
    明细覆盖执法值 = 把已消耗的配额退回去，而且越老越严重。历史上
    `deploy/reconcile_credits.py --apply` 正是这么做的（第 47/109/117 行），
    该脚本不在本服务运行路径内，但必须被视为**禁用**。

    本接口因此：
      * 基线取 `usage_events`（不可变、永不裁剪）；修正落 `adjustment_events`
        （同样不可变，作审计凭据，不计入基线以免自我叠加）；
      * 只做**增量上调**（补记漏记）；`enforced > expected` 只报告不写库；
      * 必须带 reason；actor 取自会话身份；每次写入落 `adjustment_events`
        （actor/reason，不可变）+ `audit_logs`；
      * `apply=false`（默认）为预演；重复 apply 幂等（第二次 delta=0）。

    请求体：`{"reason": "...", "apply": false}`
    """
    reason = str(payload.get('reason') or '').strip()
    apply = bool(payload.get('apply'))
    actor = str(user.get('username') or 'admin')
    try:
        return db.reconcile_credit_from_events(actor, reason, apply=apply)
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(400, str(exc)) from exc
