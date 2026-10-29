"""K27 / K47：request_id 全链路对账 + 日志脱敏实测（RV01 新契约口径）。

## 为什么合成一个脚本

K27（请求归因、用量与预算对账）和 K47（跨层追踪与隐私）在同一批表上验证：
`request_logs` / `request_outcomes` / `usage_events` / `forwarding_events` /
`budget_reservations`。分开做会重复同一套请求驱动，且容易只测一半。

## 本轮口径变更（RV01，2026-10）

旧脚本把调用方自带的 `X-Request-ID` 当成 `request_id` 去查四张表，因此在新候选上
9/14 失败——不是产品回归，是**脚本停在旧契约**。新契约（`observability.py`）：

* `request_id` = **服务端生成**的 32 位 uuid hex 执行 ID，调用方**无法指定**；
  它是预算保留、`request_logs`、`request_outcomes`、usage/attempt 事件的主键。
* 外部 `X-Request-ID` 只作**关联 ID**（`client_request_id`）：
  通过 `[A-Za-z0-9_.:-]{1,128}` 校验才落库/回显，非法值一律丢弃（不是回退成执行 ID）；
  响应用 **`X-Client-Request-ID`** 回显，`X-Request-ID` 始终是服务端执行 ID。
* 外部 ID **不是幂等键**：同一调用方重发同一个 ID 也只是两笔独立执行。

所以本脚本的流程改为：**先从响应头拿执行 ID 与关联 ID**，再用执行 ID 查库，
并逐条核对「两套 ID 分离」。断言只收紧不放宽：

1. 执行 ID 必须是 32 位 hex，且不等于外部 ID；
2. 关联 ID 必须回显为 `X-Client-Request-ID`；未提供/非法时**不得**出现该响应头；
3. `request_logs.client_request_id` 等于外部 ID，`request_id` 等于执行 ID，两者不互相冒充；
4. 恶意外部 ID（超长/换行/控制字符/空格）不得成为执行 ID、不得进响应头、不得落库；
5. 同一外部 ID 并发/重发不得共用执行 ID 或预算主键（RV01 原始缺陷）；
6. 原有检查全部保留：无 Bearer/prompt 泄露、UA 限长、`actual_platform` 有值、
   错误路径终态、不完整 usage 的 provenance、error 字段无 query 凭据、request_id 索引。

## 运行

    python3 k27_k47_trace.py --manager http://127.0.0.1:33313 \
        --token-file <api-key-file> --db <manager.db> [--json out.json]

对真实 fixture Manager 发请求，然后直接读它的 SQLite 判定闭环。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROWS: list[dict] = []

#: 合入契约的服务端执行 ID 形态：uuid4().hex（小写 32 位 hex）。
EXECUTION_ID = re.compile(r'[0-9a-f]{32}\Z')

#: 响应头：调用方关联 ID 的回显（只在外部 ID 通过校验时出现）。
CLIENT_REQUEST_ID_RESPONSE_HEADER = 'x-client-request-id'


def record(case, ok, detail, **extra):
    ROWS.append({'case': case, 'ok': bool(ok), 'detail': detail, **extra})
    print(('PASS ' if ok else 'FAIL ') + f'{case}: {detail}')


def query(db_path, sql, params=()):
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def table_names(db_path):
    return {r['name'] for r in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}


def wait_rows(db_path, sql, params=(), *, timeout=10.0, minimum=1):
    """轮询只读库直到行数达到下限（异步审计落库有延迟，固定 sleep 容易偶发失败）。"""
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while True:
        try:
            rows = query(db_path, sql, params)
        except sqlite3.Error:
            rows = []
        if len(rows) >= minimum or time.monotonic() >= deadline:
            return rows
        time.sleep(0.15)


# ─────────── 纯逻辑（可在 Windows 单测，不需要 Manager） ───────────
def is_execution_id(value) -> bool:
    """服务端执行 ID：32 位小写 hex。None/空串/外部可控值一律 False。"""
    return isinstance(value, str) and EXECUTION_ID.fullmatch(value) is not None


def header_contract_verdict(external_id, execution_id, echoed_client_id) -> dict:
    """响应头契约：执行 ID 服务端生成，关联 ID 单独回显且不得与执行 ID 混同。"""
    reasons = []
    if not is_execution_id(execution_id):
        reasons.append('execution_id_not_server_hex32')
    if execution_id == external_id:
        reasons.append('execution_id_equals_external_id')
    if echoed_client_id != external_id:
        reasons.append('client_request_id_not_echoed')
    return {'ok': not reasons, 'reasons': reasons}


def no_external_id_verdict(execution_id, echoed_client_id) -> dict:
    """未提供外部 ID：不得回显关联 ID，执行 ID 仍然必须是服务端生成。"""
    reasons = []
    if echoed_client_id is not None:
        reasons.append('client_request_id_echoed_without_external_id')
    if not is_execution_id(execution_id):
        reasons.append('execution_id_not_server_hex32')
    return {'ok': not reasons, 'reasons': reasons}


def malicious_id_verdict(*, status, execution_id, executed_client_id, bad_value) -> dict:
    """恶意 request-id：可以明确拒绝（4xx），但 2xx 时不得污染 ID 空间/响应头。

    判定不接受“客户端异常”以外的宽松解释：只要服务端回了 2xx，
    执行 ID 就必须是服务端 hex32、必须不等于恶意值、且不得回显未校验的关联 ID。
    """
    reasons = []
    if status is not None and status >= 400:
        return {'ok': True, 'reasons': [], 'note': 'explicit_rejection'}
    if not is_execution_id(execution_id):
        reasons.append('execution_id_not_server_hex32')
    if execution_id == bad_value:
        reasons.append('external_value_became_execution_id')
    if executed_client_id:
        reasons.append('unvalidated_client_request_id_echoed')
    return {'ok': not reasons, 'reasons': reasons}


def identity_verdict(row, *, external_id, execution_id) -> dict:
    """`request_logs` 行：执行 ID 与关联 ID 分开落库，不能互相冒充。"""
    reasons = []
    if not row:
        reasons.append('request_logs_row_missing')
        return {'ok': False, 'reasons': reasons}
    if row.get('request_id') != execution_id:
        reasons.append('request_id_mismatch')
    if row.get('client_request_id') != external_id:
        reasons.append('client_request_id_mismatch')
    if row.get('client_request_id') and row.get('client_request_id') == row.get('request_id'):
        reasons.append('ids_not_independent')
    return {'ok': not reasons, 'reasons': reasons}


def distinct_execution_verdict(external_id, executions) -> dict:
    """同一外部 ID 的多笔执行：执行 ID 两两不同，且都不等于外部 ID（RV01 原始缺陷）。"""
    reasons = []
    ids = [item.get('execution_id') for item in executions]
    statuses = [item.get('status') for item in executions]
    if len(executions) < 2:
        reasons.append('fewer_than_two_executions')
    if any(status != 200 for status in statuses):
        reasons.append('duplicate_external_id_request_failed')
    if any(not is_execution_id(value) for value in ids):
        reasons.append('execution_id_not_server_hex32')
    if len(set(ids)) != len(ids):
        reasons.append('execution_ids_shared')
    if external_id in ids:
        reasons.append('external_id_reused_as_execution_id')
    return {'ok': not reasons, 'reasons': reasons}


def provenance_verdict(row) -> dict:
    """不完整 usage 的 provenance 必须可辨识，不能冒充“计费成功”。"""
    reasons = []
    if row is None:
        reasons.append('usage_event_missing')
    elif row.get('provenance') not in ('unknown-credit', 'provider-reported'):
        reasons.append('provenance_unrecognized')
    return {'ok': not reasons, 'reasons': reasons}


# ─────────── 主流程 ───────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--manager', required=True)
    ap.add_argument('--token-file', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--json', type=Path)
    args = ap.parse_args(argv)

    import httpx
    token = Path(args.token_file).read_text(encoding='utf-8').strip()
    client = httpx.Client(base_url=args.manager, trust_env=False, timeout=60)
    auth = {'Authorization': f'Bearer {token}'}
    tables = table_names(args.db)

    # ── RV01-1：响应头必须暴露两套 ID，且执行 ID 不是调用方可控值 ────────
    rid = 'k47' + str(int(time.time()))[-8:]
    r = client.post('/v1/chat/completions',
                    headers={**auth, 'X-Request-ID': rid},
                    json={'model': 'fake-model:normal',
                          'messages': [{'role': 'user', 'content': 'k47 trace probe'}]})
    execution_id = r.headers.get('x-request-id')
    echoed_client_id = r.headers.get(CLIENT_REQUEST_ID_RESPONSE_HEADER)
    verdict = header_contract_verdict(rid, execution_id, echoed_client_id)
    record('RV01 执行 ID 由服务端生成且与外部 ID 分离',
           r.status_code == 200 and verdict['ok'],
           f'status={r.status_code} execution_id={execution_id} '
           f'client_request_id={echoed_client_id} external_id={rid}',
           status=r.status_code, reasons=verdict['reasons'])

    # ── K27-1：request_logs 里有该执行 ID ──────────────────────────────
    if 'request_logs' not in tables:
        record('request_logs 表存在', False, '表不存在')
        return finish(args)
    rows = wait_rows(args.db, 'SELECT * FROM request_logs WHERE request_id=?',
                     (execution_id,))
    record('request_logs 记录闭环（按服务端执行 ID）', len(rows) == 1,
           f'命中 {len(rows)} 行 (execution_id={execution_id})', rows=len(rows))
    row = rows[0] if rows else {}
    identity = identity_verdict(row, external_id=rid, execution_id=execution_id)
    record('RV01 request_logs 两套 ID 分离落库', identity['ok'],
           f"request_id={row.get('request_id')!r} "
           f"client_request_id={row.get('client_request_id')!r} "
           f"reasons={identity['reasons']}")

    # ── K27-2：usage_events 同一执行 ID 有终态 ─────────────────────────
    if 'usage_events' in tables:
        ue = wait_rows(args.db, 'SELECT * FROM usage_events WHERE request_id=?',
                       (execution_id,))
        record('usage_events 关联（按服务端执行 ID）', len(ue) >= 1, f'{len(ue)} 条',
               terminal=[u.get('terminal_state') for u in ue])
        if ue:
            u = ue[0]
            record('usage_events 有 terminal_state', bool(u.get('terminal_state')),
                   f"terminal={u.get('terminal_state')} counted={u.get('counted')}")
            record('usage_events 不落 prompt', 'k47 trace probe' not in json.dumps(ue),
                   'usage_events 内无 prompt 文本')
    else:
        record('usage_events 表存在', False, '表不存在')

    # ── K27-3：forwarding_events 记录实际路由 ──────────────────────────
    if 'forwarding_events' in tables:
        fe = wait_rows(args.db, 'SELECT * FROM forwarding_events WHERE request_id=?',
                       (execution_id,))
        record('forwarding_events 关联（按服务端执行 ID）', len(fe) >= 1, f'{len(fe)} 条')
    else:
        record('forwarding_events 表存在', False, '表不存在')

    # ── K27-4：实际 platform 有记录（不是 NULL 冒充 workbuddy）───────────
    record('request_logs.actual_platform 有值',
           bool(row.get('actual_platform')),
           f"actual_platform={row.get('actual_platform')!r} platform={row.get('platform')!r}")

    # ── K47-2：日志里不得出现 Authorization / 密钥 / prompt ──────────────
    blob = json.dumps(row, ensure_ascii=False)
    record('日志不含 Bearer 令牌', 'Bearer ' not in blob and token not in blob,
           'request_logs 行内无令牌')
    record('日志不含 prompt 原文', 'k47 trace probe' not in blob,
           'request_logs 行内无 prompt')
    if row.get('ua'):
        record('UA 长度受限', len(str(row['ua'])) <= 300, f'ua len={len(str(row["ua"]))}')

    # ── K47-2b：request_outcomes 同样按执行 ID / 关联 ID 分开落库 ────────
    if 'request_outcomes' in tables:
        oc = wait_rows(args.db, 'SELECT * FROM request_outcomes WHERE request_id=?',
                       (execution_id,))
        record('request_outcomes 记录闭环', len(oc) == 1, f'{len(oc)} 行')
        if oc:
            record('request_outcomes.client_request_id 对齐',
                   oc[0].get('client_request_id') == rid,
                   f"client_request_id={oc[0].get('client_request_id')!r}")

    # ── RV01-2：未提供外部 ID 时不回显关联 ID，执行 ID 仍服务端生成 ───────
    plain = client.post('/v1/chat/completions', headers=auth,
                        json={'model': 'fake-model:normal',
                              'messages': [{'role': 'user', 'content': 'k47 plain probe'}]})
    plain_exec = plain.headers.get('x-request-id')
    plain_client = plain.headers.get(CLIENT_REQUEST_ID_RESPONSE_HEADER)
    plain_verdict = no_external_id_verdict(plain_exec, plain_client)
    record('RV01 未提供外部 ID 时不回显 X-Client-Request-ID',
           plain.status_code == 200 and plain_verdict['ok'],
           f'status={plain.status_code} execution_id={plain_exec} '
           f'client_request_id={plain_client} reasons={plain_verdict["reasons"]}')

    # ── K47-3：恶意 request-id（超长 / 换行 / 控制字符 / 空格）────────────
    # 新契约下这三种结果都可接受：① 客户端拒绝发送（httpx 对换行头直接抛错）；
    # ② 服务端 4xx 明确拒绝；③ 服务端忽略非法关联 ID 正常服务。不可接受的是：
    # 恶意值变成执行 ID、被回显进响应头、或落库成主键。
    survived_bad_values: list[str] = []
    for label, bad in (('超长 300 字符', 'A' * 300),
                       ('含换行', 'abc\r\nX-Injected: 1'),
                       ('含控制字符', 'abc\x01def'),
                       ('含空格', 'abc def')):
        try:
            rr = client.post('/v1/chat/completions',
                             headers={**auth, 'X-Request-ID': bad},
                             json={'model': 'fake-model:normal',
                                   'messages': [{'role': 'user', 'content': 'probe'}]})
        except Exception as exc:  # noqa: BLE001
            # 客户端在发送前就拒绝（如换行头）——比“服务端正常处理”更严格，可接受。
            record(f'恶意 request-id（{label}）', True,
                   f'客户端拒绝发送：{type(exc).__name__}')
            continue
        bad_exec = rr.headers.get('x-request-id')
        bad_client = rr.headers.get(CLIENT_REQUEST_ID_RESPONSE_HEADER)
        bad_verdict = malicious_id_verdict(status=rr.status_code, execution_id=bad_exec,
                                           executed_client_id=bad_client, bad_value=bad)
        if rr.status_code < 400:
            survived_bad_values.append(bad)
        record(f'恶意 request-id（{label}）不产生头注入/主键污染',
               bad_verdict['ok'],
               f'status={rr.status_code} execution_id={bad_exec} '
               f'client_request_id={bad_client} reasons={bad_verdict["reasons"]}',
               status=rr.status_code)

    # 落库复核：通过 2xx 的恶意值一个都不许出现在两套 ID 列里（含 client_request_id）。
    if survived_bad_values:
        placeholders = ','.join('?' * len(survived_bad_values))
        dirty = query(args.db,
                      'SELECT request_id, client_request_id FROM request_logs '
                      f'WHERE request_id IN ({placeholders}) '
                      f'OR client_request_id IN ({placeholders})',
                      (*survived_bad_values, *survived_bad_values))
        record('恶意 ID 未写入 request_id / client_request_id',
               len(dirty) == 0,
               f'{len(dirty)} 行命中：{[(r.get("request_id"), r.get("client_request_id")) for r in dirty][:3]}')
    else:
        record('恶意 ID 未写入 request_id / client_request_id', True,
               '全部恶意值在发送/服务端即被拒绝，无落库机会')

    # ── RV01-3：同一外部 ID 两笔执行不得共用执行 ID / 预算主键 ────────────
    dup_rid = 'k47dup' + str(int(time.time()))[-6:]
    dup_runs = []
    for _ in range(2):
        dr = client.post('/v1/chat/completions',
                         headers={**auth, 'X-Request-ID': dup_rid},
                         json={'model': 'fake-model:normal',
                               'messages': [{'role': 'user', 'content': 'k47 dup probe'}]})
        dup_runs.append({'status': dr.status_code,
                         'execution_id': dr.headers.get('x-request-id')})
    dup_verdict = distinct_execution_verdict(dup_rid, dup_runs)
    record('RV01 同一外部 ID 两笔执行各自独立',
           dup_verdict['ok'],
           f'statuses={[d["status"] for d in dup_runs]} '
           f'ids={[d["execution_id"] for d in dup_runs]} reasons={dup_verdict["reasons"]}')
    if dup_verdict['ok']:
        # 精确核对「这两笔执行各自一行、关联 ID 都是外部值、执行 ID 不重复」，
        # 不按外部 ID 数总行数（外部 ID 本来就可跨轮次重复，数总行会误判）。
        dup_ids = [d['execution_id'] for d in dup_runs]
        dup_rows = wait_rows(
            args.db,
            'SELECT request_id, client_request_id FROM request_logs '
            'WHERE request_id IN (?,?)', tuple(dup_ids), minimum=2)
        stored = sorted(r['request_id'] for r in dup_rows)
        record('RV01 同一外部 ID 两笔各一行、主键不重复',
               stored == sorted(dup_ids)
               and all(r['client_request_id'] == dup_rid for r in dup_rows),
               f'期望执行 ID={sorted(dup_ids)} 实际={stored}；'
               f'client_request_id={[r["client_request_id"] for r in dup_rows]}')

    # ── K27-5：错误路径也要有终态（provider 500）─────────────────────────
    err_rid = 'k27err' + str(int(time.time()))[-8:]
    err_response = client.post('/v1/chat/completions',
                               headers={**auth, 'X-Request-ID': err_rid},
                               json={'model': 'fake-model:error_500',
                                     'messages': [{'role': 'user', 'content': 'probe error'}]})
    err_exec = err_response.headers.get('x-request-id')
    err_client = err_response.headers.get(CLIENT_REQUEST_ID_RESPONSE_HEADER)
    err_header_verdict = header_contract_verdict(err_rid, err_exec, err_client)
    record('RV01 错误路径同样两套 ID 分离',
           err_header_verdict['ok'],
           f'status={err_response.status_code} execution_id={err_exec} '
           f'client_request_id={err_client} reasons={err_header_verdict["reasons"]}')
    erows = wait_rows(args.db, 'SELECT * FROM request_logs WHERE request_id=?', (err_exec,))
    record('provider 错误也落 request_logs', len(erows) == 1, f'{len(erows)} 行')
    if erows:
        e = erows[0]
        record('provider 错误有 error_stage 或 terminal_state',
               bool(e.get('error_stage') or e.get('terminal_state')),
               f"stage={e.get('error_stage')!r} terminal={e.get('terminal_state')!r}")
        err_identity = identity_verdict(e, external_id=err_rid, execution_id=err_exec)
        record('RV01 错误路径关联 ID 正确落库', err_identity['ok'],
               f"client_request_id={e.get('client_request_id')!r} "
               f"reasons={err_identity['reasons']}")

    # ── K27-6：usage 不完整时不能被当成"计费成功"─────────────────────────
    if 'usage_events' in tables:
        prov = query(args.db, 'SELECT provenance, counted FROM usage_events WHERE request_id=?',
                     (err_exec,))
        p_verdict = provenance_verdict(prov[0] if prov else None)
        if prov:
            p = prov[0]
            record('不完整 usage 的 provenance 可辨识',
                   p_verdict['ok'],
                   f"provenance={p['provenance']} counted={p['counted']} "
                   f"reasons={p_verdict['reasons']}")

    # ── K47-4：查询里不得出现 query 串泄漏（例如 ?key=）──────────────────
    leak = query(args.db,
                 "SELECT request_id FROM request_logs WHERE coalesce(error,'') LIKE '%key=%' "
                 "OR coalesce(error,'') LIKE '%token=%' LIMIT 5")
    record('error 字段不含 query 里的凭据', len(leak) == 0, f'{len(leak)} 条可疑')

    # ── K47-5：request_id 索引存在（否则按 id 查会全表扫）─────────────────
    idx = query(args.db, "SELECT name FROM sqlite_master WHERE type='index' "
                         "AND tbl_name='request_logs' AND sql LIKE '%request_id%'")
    record('request_logs 有 request_id 索引', len(idx) >= 1,
           f'{len(idx)} 个：{[i["name"] for i in idx]}')

    return finish(args)


def finish(args) -> int:
    failed = [r for r in ROWS if not r['ok']]
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {'schema': 'zhihui-k27-k47/v1',
             'contract': 'RV01: server-execution-id + client-request-id（X-Client-Request-ID 回显）',
             'rows': ROWS,
             'passed': len(ROWS) - len(failed), 'failed': len(failed)},
            ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'\n{len(ROWS) - len(failed)}/{len(ROWS)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
