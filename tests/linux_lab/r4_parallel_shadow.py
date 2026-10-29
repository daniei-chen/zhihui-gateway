"""R4 批次G：新旧网关**并行（影子）**运行验证 + 串行切换（仅 disposable local VM）。

## 为什么不能直接 A/B 双写

项目已确认：`instance_leases` 只提供每实例身份，不做全局写者选举；A/B 同时写
同一业务 DB 需要另行完成共享状态所有权与事务内 fencing 协议及其反例。
SQLite `busy_timeout`、nginx 双 upstream、实验成功都不能冒充这个协议。

所以本实验**不双写**：

* **A（旧候选）**继续权威服务，独占主 DB（`WB_DB_A`）；
* **B（新候选）**用**独立 `IsolatedEnv`**（独立 `WB_DB_B` / `WB_DATA_DIR` / auths /
  端口 / 入口）启动，父进程把每个探针/推理请求**逐字节复制**一份发给 B
  （真实 HTTP/TLS/SSE，经 B 自己的 nginx 入口）；
* 比对两侧状态码 / SSE 帧数 / 终止信号 / 错误体形状；B 的影子账本单独对账，
  不进入 A 的账本，也不跨库相加。

## 阶段

1. **边界/前置**（纯函数 `guard_violations`）：hostname=`zhihui-local-lab`、
   home=`/home/gateway-lab`、`repo`/`--a-repo` 都在 `lab/snapshots/`、`--a-repo`
   必须显式给出且 ≠ repo（禁止拿同一份代码自欺）、out/qa 在 lab 内、out 不存在、
   qa.production_connected=false 且 workspace==repo、A/B 的 DB 路径不同、
   全部地址 loopback。违规 → exit 2，不跑实验。
2. **影子窗口**（默认 60 对、1s 间隔、≥60s）：先起 fake provider 与 A 主入口
   （nginx TLS，upstream=[A]）、B 影子实例与 B 影子入口（独立 prefix，upstream=[B]）。
   逐对并发发送同一请求体；A/B 各用自己的合成 key（独立库无法共用同一明文，
   报告只留 sha256 指纹）。
   判定（Lead 2026-10-26 修正 #1）：
   * **正常完成**用例：状态码 + SSE 帧数 + 终止信号；
   * **取消**用例：客户端首帧后断开，两侧帧数天然不等 —— **不比帧数**，只要求
     两侧都记录到客户端取消（A 侧不得出现未预期 5xx/连接错误，B 同理）；
   * **错误**用例：只比状态码类别（4xx/5xx）与错误体形状。
   阈值：`min_pairs≥40`、覆盖≥3 协议+错误+取消、A 未预期失败=0、B 差异=0
   （`--allow-divergence` 默认 0；放宽只用于非切换证据，**不参与 allow_switch 判定**）。
   A 主库零重复/零丢失；B 影子库完整性；A/B 的 request_id 互不越界。
3. **切换**（复用既有串行协议 `r3l2_handoff` 的 Instance/drain/readyz，配合
   `switch_upstream` 的真实 nginx 改写/reload）：停 B 影子 → A `drain begin` →
   readyz 503 → 在途归零 → A 退出并观测真实 release → B 权威启动（B 代码 + A 的
   主 env/DB）→ 主库迁移真实建出 `instance_leases`/`task_leases` → B 自己的租约行
   role=serving → 保存原 nginx 字节 → upstream 切 B → reload → 前端 200 →
   经入口探针 + 主库对账。失败/回退失败如实记录，回退自身失败标
   `manual_intervention_required`。
   旧快照 A（无 `instance_leases`）的 release 观测记 `n/a`，**不因此判失败**；
   若 A 有租约表则仍按 role=stopped / owner 空 / lease_until=0 / fence 不变严格判定。
4. **退出码**：0=影子通过且切换通过；1=影子失败，或切换已提交后失败/回退失败；
   2=边界违规；3=影子通过但切换被拒绝（且服务已恢复）。

## 报告（影子与切换分开记录，不混算）

`parallel-shadow-report.json` + `shadow-timeline.ndjson` / `switch-timeline.ndjson`
+ `shadow-ledger.json` / `switch-ledger.json`。报告含 `parallel_window`、
`shadow_ledger`、`switch_phase`、`verdict`。

## 边界

仅 disposable local VM；生产禁用；不联网（provider/入口全部 127.0.0.1）；不 freeze、
不安装、不改业务代码与既有脚本（既有脚本只读 import）。不写“零宕机”，只给观测。

## 用法

    python3 tests/linux_lab/r4_parallel_shadow.py \
        --qa-file /home/gateway-lab/lab/profiles/<batch>.json \
        --repo /home/gateway-lab/lab/snapshots/<new>/workspace \
        --a-repo /home/gateway-lab/lab/snapshots/ee89f51e2b6cd39a/workspace \
        --out /home/gateway-lab/lab/evidence/<run_id>/r4-parallel-shadow \
        --pairs 60 --interval 1.0
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import posixpath
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import urlsplit

from r3l2_handoff import (HandoffExperiment, Instance, Timeline, fingerprint_keys,
                          public_instance_info)
import switch_upstream
from rollback_guard import compare_business_data, probe_old_reader
from r3l2_env import SECURITY_HEADERS

LAB = Path('/home/gateway-lab/lab')
LAB_HOST, LAB_HOME = 'zhihui-local-lab', '/home/gateway-lab'
EXIT_PASS, EXIT_FAIL, EXIT_BOUNDARY, EXIT_SWITCH_REFUSED = 0, 1, 2, 3
READY_PATHS = ('/api/readyz', '/api/healthz')
REQUIRED_LEASE_TABLES = ('instance_leases', 'task_leases')

#: 影子窗口用例矩阵。`kind` 决定比对规则（Lead 修正 #1）。
CASES = (
    {'name': 'chat-stream', 'kind': 'normal', 'protocol': 'chat', 'platform': 'workbuddy',
     'path': '/v1/chat/completions', 'stream': True, 'model': 'fake-model:normal'},
    {'name': 'responses-stream', 'kind': 'normal', 'protocol': 'responses',
     'platform': 'workbuddy', 'path': '/v1/responses', 'stream': True,
     'model': 'fake-model:normal'},
    {'name': 'messages-stream', 'kind': 'normal', 'protocol': 'messages',
     'platform': 'qoder', 'path': '/qoder/v1/messages', 'stream': True,
     'model': 'fake-model:normal'},
    {'name': 'chat-nonstream', 'kind': 'normal', 'protocol': 'chat',
     'platform': 'workbuddy', 'path': '/v1/chat/completions', 'stream': False,
     'model': 'fake-model:normal'},
    {'name': 'error-500', 'kind': 'error', 'protocol': 'chat', 'platform': 'workbuddy',
     'path': '/v1/chat/completions', 'stream': False, 'model': 'fake-model:error_500',
     'expected_class': 5},
    {'name': 'error-400', 'kind': 'error', 'protocol': 'chat', 'platform': 'workbuddy',
     'path': '/v1/chat/completions', 'stream': False, 'model': 'fake-model:error_400',
     'expected_class': 4},
    {'name': 'client-cancel', 'kind': 'cancel', 'protocol': 'chat',
     'platform': 'workbuddy', 'path': '/v1/chat/completions', 'stream': True,
     'model': 'fake-model:normal'},
)
CASE_BY_NAME = {case['name']: case for case in CASES}
INFLIGHT_CASE = {'name': 'switch-inflight', 'kind': 'normal', 'protocol': 'chat',
                 'platform': 'workbuddy', 'path': '/v1/chat/completions',
                 'stream': True, 'model': 'fake-model:normal'}
SWITCH_PROBE_CASES = tuple(case for case in CASES if case['kind'] != 'cancel')


# ─────────────────────────── 纯逻辑（Windows 可单测） ───────────────────────────
def _norm(value) -> str:
    return posixpath.normpath(str(value)) if value not in (None, '') else ''


def _inside(child, parent) -> bool:
    """POSIX 路径包含判断（host 上是 Linux；Windows 单测传 POSIX 字符串）。"""
    if not child or not parent:
        return False
    try:
        return PurePosixPath(_norm(child)).is_relative_to(PurePosixPath(_norm(parent)))
    except (ValueError, TypeError):
        return False


def _is_loopback_url(value) -> bool:
    """loopback 判定（接受带 scheme 的 URL；FakeProvider.base_url 形如 http://127.0.0.1:<port>）。"""
    if value in (None, ''):
        return True
    text = str(value)
    host = urlsplit(text).hostname if '://' in text else text.split(':')[0].strip('[]')
    return (host or '').strip('[]').lower() in {'127.0.0.1', 'localhost', '::1'}


def guard_violations(facts: dict) -> list[str]:
    """边界/前置事实 → 违规列表；IO 由调用方采集，本函数纯逻辑（exit 2 的依据）。"""
    bad: list[str] = []
    lab = facts.get('lab') or str(LAB)
    if facts.get('hostname') != LAB_HOST:
        bad.append('hostname_not_lab')
    if facts.get('home') != LAB_HOME:
        bad.append('home_not_gateway_lab')
    if not _inside(facts.get('repo') or '', lab + '/snapshots'):
        bad.append('repo_outside_snapshots')
    if facts.get('repo_exists') is not True or facts.get('repo_has_manager') is not True:
        bad.append('repo_unusable')
    a_repo = facts.get('a_repo') or ''
    if not a_repo:
        bad.append('a_repo_required')
    else:
        if not _inside(a_repo, lab + '/snapshots'):
            bad.append('a_repo_outside_snapshots')
        if facts.get('a_repo_exists') is not True or facts.get('a_repo_has_manager') is not True:
            bad.append('a_repo_unusable')
        if _norm(a_repo) == _norm(facts.get('repo') or ''):
            bad.append('a_repo_same_as_repo')
        # 同一份代码换目录 = 自欺：两份关键文件指纹都相同直接拒绝。
        if (facts.get('a_main_sha256') and facts.get('repo_main_sha256')
                and facts['a_main_sha256'] == facts['repo_main_sha256']
                and facts.get('a_migrations_sha256')
                and facts['a_migrations_sha256'] == facts.get('repo_migrations_sha256')):
            bad.append('a_repo_same_content')
    if not _inside(facts.get('out') or '', lab):
        bad.append('out_outside_lab')
    if facts.get('out_exists') is not False:
        bad.append('out_exists_refuse_overwrite')
    if not _inside(facts.get('qa_file') or '', lab):
        bad.append('qa_outside_lab')
    if facts.get('qa_exists') is not True:
        bad.append('qa_file_missing')
    if facts.get('qa_production_connected') is not False:
        bad.append('production_connected_not_false')
    if _norm(facts.get('qa_workspace') or '') != _norm(facts.get('repo') or ''):
        bad.append('qa_workspace_mismatch')
    db_a, db_b = str(facts.get('db_a') or ''), str(facts.get('db_b') or '')
    if not db_a or not db_b or _norm(db_a) == _norm(db_b):
        bad.append('shadow_db_not_isolated')
    if facts.get('loopback_ok') is not True:
        bad.append('non_loopback_endpoint')
    return bad


def case_body_bytes(case: dict) -> bytes:
    """同一请求体逐字节复制到两侧（A/B 只有鉴权头不同）。"""
    if case['protocol'] == 'chat':
        body = {'model': case['model'],
                'messages': [{'role': 'user', 'content': 'r4 parallel shadow probe'}],
                'max_tokens': 32}
    elif case['protocol'] == 'responses':
        body = {'model': case['model'], 'input': 'r4 parallel shadow probe',
                'max_output_tokens': 32}
    else:
        body = {'model': case['model'], 'max_tokens': 32,
                'messages': [{'role': 'user', 'content': 'r4 parallel shadow probe'}]}
    if case['stream']:
        body['stream'] = True
    return json.dumps(body, ensure_ascii=False).encode('utf-8')


def classify_terminal(record: dict) -> str:
    """把一次请求归类成可比较的终止语义（不依赖具体协议）。

    非流式 200 没有 SSE 终止帧，不能因此判成 `stream_cut`（那会把正常
    non-stream 用例误算成 A 侧未预期失败）。
    """
    if record.get('cancelled'):
        return 'client_cancelled'
    if record.get('status') is None:
        return 'connect_error'
    if record.get('status') != 200:
        return 'http_error'
    if record.get('stream') is False or record.get('stream') is None:
        return 'completed'
    terminals = record.get('terminals') or []
    if 'error' in terminals:
        return 'upstream_error'
    if (record.get('saw_done')
            or any(t in terminals for t in ('[DONE]', 'message_stop', 'response.completed',
                                            'finish_reason', 'stop_reason'))):
        return 'completed'
    return 'stream_cut'


def _status_class(status) -> str:
    if not isinstance(status, int):
        return 'none'
    return f'{status // 100}xx'


def error_shape(record: dict) -> dict:
    """错误体形状：只保留类型/码/是否有 message/长度档，不含正文与 URL。"""
    text = record.get('body_head') or ''
    shape = {'json': False, 'top_keys': [], 'error_keys': [], 'type': None,
             'code': None, 'message_present': False, 'message_len': 0}
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return shape
    if not isinstance(obj, dict):
        return shape
    error = obj.get('error') if isinstance(obj.get('error'), dict) else {}
    message = str(error.get('message') or '')
    length = len(message)
    shape.update({
        'json': True,
        'top_keys': sorted(str(k) for k in obj.keys())[:10],
        'error_keys': sorted(str(k) for k in error.keys())[:10],
        'type': error.get('type'),
        'code': error.get('code'),
        'message_present': bool(message),
        'message_len': 0 if length == 0 else 1 if length <= 80 else 2 if length <= 200 else 3,
    })
    return shape


def success_shape(record: dict) -> dict:
    """非流式成功体形状（去掉 id/created 等易变字段）。"""
    shape = {'json': False, 'object': None, 'model_present': False,
             'choices': 0, 'finish_reasons': [], 'usage_keys': []}
    try:
        obj = json.loads(record.get('body_head') or '')
    except (ValueError, TypeError):
        return shape
    if not isinstance(obj, dict):
        return shape
    choices = obj.get('choices') if isinstance(obj.get('choices'), list) else []
    shape.update({
        'json': True,
        'object': obj.get('object'),
        'model_present': bool(obj.get('model')),
        'choices': len(choices),
        'finish_reasons': sorted({str((c or {}).get('finish_reason'))
                                  for c in choices if isinstance(c, dict)}),
        'usage_keys': sorted(str(k) for k in (obj.get('usage') or {}).keys())
        if isinstance(obj.get('usage'), dict) else [],
    })
    return shape


def _unexpected(record: dict, case: dict) -> bool:
    terminal = record.get('terminal') or classify_terminal(record)
    status = record.get('status')
    if case['kind'] == 'normal':
        return status != 200 or terminal != 'completed'
    if case['kind'] == 'error':
        return _status_class(status) != f"{case['expected_class']}xx"
    return status != 200 or terminal != 'client_cancelled'


def compare_pair(case: dict, a: dict, b: dict, frame_tolerance: int = 0) -> dict:
    """一对影子请求的一致性判定（按 Lead 修正 #1 分 kind 处理）。"""
    kind = case['kind']
    reasons: list[str] = []
    compared = {'status': False, 'frames': False, 'terminal': False,
                'error_shape': False, 'success_shape': False}
    if kind == 'cancel':
        # 取消：两侧帧数天然不同，**不比较帧数**；只要求都记录到客户端取消。
        for side, record in (('a', a), ('b', b)):
            if record.get('status') != 200:
                reasons.append(f'{side}_cancel_not_accepted')
            if not record.get('cancelled'):
                reasons.append(f'{side}_cancel_not_observed')
            if int(record.get('frames') or 0) < 1:
                reasons.append(f'{side}_cancel_without_first_frame')
            if record.get('error') and not record.get('cancelled'):
                reasons.append(f'{side}_cancel_connect_error')
    elif kind == 'error':
        compared['status'] = True
        compared['error_shape'] = True
        if _status_class(a.get('status')) != _status_class(b.get('status')):
            reasons.append('error_status_class_mismatch')
        if _status_class(a.get('status')) != f"{case['expected_class']}xx":
            reasons.append('a_error_status_class_unexpected')
        if _status_class(b.get('status')) != f"{case['expected_class']}xx":
            reasons.append('b_error_status_class_unexpected')
        a_shape, b_shape = error_shape(a), error_shape(b)
        if a_shape != b_shape:
            reasons.append('error_body_shape_mismatch')
    else:
        compared['status'] = True
        if a.get('status') != b.get('status'):
            reasons.append('status_mismatch')
        if a.get('status') != 200:
            reasons.append('normal_status_not_200')
        if case['stream']:
            compared['terminal'] = True
            compared['frames'] = True
            if abs(int(a.get('frames') or 0) - int(b.get('frames') or 0)) > frame_tolerance:
                reasons.append('sse_frame_count_mismatch')
            if (a.get('terminal') or classify_terminal(a)) != (
                    b.get('terminal') or classify_terminal(b)):
                reasons.append('terminal_mismatch')
            if (a.get('terminal') or classify_terminal(a)) != 'completed':
                reasons.append('normal_stream_not_completed')
        else:
            compared['success_shape'] = True
            if success_shape(a) != success_shape(b):
                reasons.append('success_body_shape_mismatch')
    a_unexpected = _unexpected(a, case)
    b_unexpected = _unexpected(b, case)
    return {
        'case': case['name'], 'kind': kind, 'protocol': case['protocol'],
        'equal': not reasons, 'reasons': reasons, 'compared': compared,
        'a_unexpected': a_unexpected, 'b_unexpected': b_unexpected,
        'a': _project(a), 'b': _project(b),
    }


def _project(record: dict) -> dict:
    return {key: record.get(key) for key in
            ('status', 'frames', 'saw_done', 'terminals', 'terminal', 'cancelled',
             'execution_id', 'error', 'first_frame_ms', 't_start_ms', 't_end_ms')}


def coverage_facts(pairs: list[dict]) -> dict:
    protocols = {pair['protocol'] for pair in pairs
                 if pair['kind'] == 'normal' and pair['case'] != 'chat-nonstream'}
    return {
        'normal_protocols': sorted(protocols),
        'normal_protocol_count': len(protocols),
        'error_pairs': sum(1 for pair in pairs if pair['kind'] == 'error'),
        'cancel_pairs': sum(1 for pair in pairs if pair['kind'] == 'cancel'),
    }


def shadow_gate(window: dict, *, min_pairs: int = 40, divergence_allowance: int = 0) -> dict:
    """影子窗口门槛（不满足 → 拒绝切换）。`divergence_allowance` 只用于证据放宽。"""
    pairs = int(window.get('request_pairs') or 0)
    coverage = window.get('coverage') or {}
    checks = {
        'sample_count': pairs >= int(min_pairs),
        'protocol_coverage': int(coverage.get('normal_protocol_count') or 0) >= 3,
        'error_coverage': int(coverage.get('error_pairs') or 0) >= 1,
        'cancel_coverage': int(coverage.get('cancel_pairs') or 0) >= 1,
        'a_unexpected_failures_zero': int(window.get('a_unexpected_failures') or 0) == 0,
        'b_unexpected_failures_zero': int(window.get('b_unexpected_failures') or 0) == 0,
        'status_all_equal': window.get('status_checked', 0) > 0
                            and window.get('status_equal') == window.get('status_checked'),
        'sse_frames_all_equal': window.get('sse_compared', 0) > 0
                                and window.get('sse_mismatches') == 0,
        'terminals_all_equal': window.get('terminal_compared', 0) > 0
                               and window.get('terminal_mismatches') == 0,
        'error_shapes_all_equal': window.get('error_shape_compared', 0) > 0
                                  and window.get('error_shape_mismatches') == 0,
        'divergences_within_allowance': int(window.get('divergences') or 0)
                                        <= int(divergence_allowance),
    }
    reasons = [name for name, ok in checks.items() if not ok]
    return {'pass': not reasons, 'checks': checks, 'reasons': reasons,
            'divergence_allowance': int(divergence_allowance)}


def ledger_gate(ledger: dict) -> dict:
    """A 主库零重复/零丢失 + B 影子库完整性 + 两库互不污染。"""
    a = ledger.get('a') or {}
    b = ledger.get('b') or {}
    cross = ledger.get('cross_contamination') or {}
    checks = {
        'db_isolated': ledger.get('db_isolated') is True,
        'a_execution_ids_available': ledger.get('a_execution_ids_available') is True,
        'b_execution_ids_available': ledger.get('b_execution_ids_available') is True,
        'a_request_logs_available': a.get('request_logs_available') is True,
        'b_request_logs_available': b.get('request_logs_available') is True,
        'a_no_duplicate_request_ids': int(a.get('duplicate_request_ids') or 0) == 0,
        'a_no_missing_logs': len(a.get('missing') or []) == 0,
        'a_no_duplicate_logs': len(a.get('duplicate') or []) == 0,
        'a_tokens_consistent': a.get('tokens_applicable') is False
                               or (a.get('request_logs_tokens') == a.get('usage_daily_tokens')),
        'b_no_duplicate_request_ids': int(b.get('duplicate_request_ids') or 0) == 0,
        'b_no_missing_logs': len(b.get('missing') or []) == 0,
        'b_no_duplicate_logs': len(b.get('duplicate') or []) == 0,
        'b_tokens_consistent': b.get('tokens_applicable') is True
                               and int(b.get('usage_daily_tokens') or 0) > 0
                               and b.get('request_logs_tokens') == b.get('usage_daily_tokens'),
        # B 影子库是新候选自己的完整性：不能有“A 基线残值”来源，所以这里仍按 0
        # 严格判定（候选7 实测为 0）。A 侧同项在切换账本里按已知集合归因。
        'b_no_unexpected_orphans': int(b.get('unexpected_outcome_without_log') or 0) == 0,
        # 未结算预留：cancel/error 用例天然会留 reserved/unknown；只允许“可归因”的残值，
        # 且 A/B 必须对称（候选6 的 24/24 对称残值不得因只查 B 而误杀）。
        'unsettled_symmetric': int(a.get('open_or_unknown_reservations') or 0)
                               == int(b.get('open_or_unknown_reservations') or 0),
        'a_no_unexpected_unsettled': not (a.get('unexpected_unsettled_ids') or []),
        'b_no_unexpected_unsettled': not (b.get('unexpected_unsettled_ids') or []),
        'b_cancel_terminals_recorded': len(b.get('cancel_terminals_missing') or []) == 0,
        'a_cancel_terminals_recorded': len(a.get('cancel_terminals_missing') or []) == 0,
        'a_ids_not_in_b': int(cross.get('a_ids_in_b') or 0) == 0,
        'b_ids_not_in_a': int(cross.get('b_ids_in_a') or 0) == 0,
    }
    reasons = [name for name, ok in checks.items() if not ok]
    return {'pass': not reasons, 'checks': checks, 'reasons': reasons}


def release_applicability(has_lease_table: bool, claimed: bool) -> dict:
    """A 的 release 观测是否适用：只有「有租约表且真的持有租约」才严格判定。"""
    if has_lease_table and claimed:
        return {'applicable': True, 'reason': 'instance_lease_held'}
    if has_lease_table:
        return {'applicable': False,
                'reason': 'lease_table_present_but_instance_did_not_claim'}
    return {'applicable': False, 'reason': 'legacy_schema_without_instance_leases'}


def release_observed(row: dict | None, fence) -> dict:
    """A 真实 release 的逐字段判定（有租约表时使用；缺一项都算未观察）。"""
    reasons: list[str] = []
    row = row or {}
    if not row:
        reasons.append('release_row_missing')
    if row.get('role') != 'stopped':
        reasons.append('role_not_stopped')
    if row.get('owner'):
        reasons.append('owner_not_empty')
    if int(row.get('lease_until') or 0) != 0:
        reasons.append('lease_until_not_zero')
    if fence is not None and int(row.get('fence') or 0) != int(fence):
        reasons.append('fence_changed')
    return {'observed': not reasons, 'reasons': reasons}


def attribute_residual(observed_ids, shadow_ids, probe_ids) -> dict:
    """把“未结算预留”按来源归因（候选7 的切换账本误判修复）。

    允许集合 = 影子窗口 A 侧已归因残值（cancel/error 用例）+ 切换期 error/cancel
    探针的 execution_id。只有**不在**允许集合里的未结算才算失败——不能只查
    “open_or_unknown_reservations == 0”，否则 A 主库天然带着的影子期残值会误杀。
    """
    observed = [value for value in (observed_ids or []) if value]
    from_shadow = sorted({value for value in (shadow_ids or []) if value})
    from_probes = sorted({value for value in (probe_ids or []) if value})
    allowed = set(from_shadow) | set(from_probes)
    unexpected = [value for value in observed if value not in allowed]
    return {
        'observed_total': len(observed),
        'allowed_from_shadow': from_shadow,
        'allowed_from_switch_probes': from_probes,
        'symmetric_residual': [value for value in observed if value in allowed],
        'unexpected': unexpected,
        'pass': not unexpected,
    }


def attribute_outcome_residual(observed_ids, known_ids) -> dict:
    """同类修正：outcome 无日志的“只查 0”也改成按已知执行集合归因。"""
    observed = [value for value in (observed_ids or []) if value]
    known = {value for value in (known_ids or []) if value}
    unexpected = [value for value in observed if value not in known]
    return {'observed_total': len(observed),
            'known_residual': [value for value in observed if value in known],
            'unexpected': unexpected, 'pass': not unexpected}


_SECRET_PATTERNS = (
    re.compile(r'(?i)(bearer\s+)[^\s"\',]+'),
    re.compile(r'wbk_[A-Za-z0-9_-]+'),
    re.compile(r'(?i)((?:password|secret|token)\s*[=:]\s*)[^\s,"\']+'),
)


def scrub_log(text, limit: int = 2000) -> str:
    """回退日志尾：只留判定需要的信息，不把 key/password 明文带进报告。"""
    value = str(text or '')[-max(1, int(limit)):]
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda match: (match.group(1) if match.groups() else '') + '***',
                            value)
    return value


def classify_launcher_failure(*, returncode, identity_present: bool,
                              port_in_use: bool, log_tail: str, ready_error=None) -> dict:
    """解释 A2 为什么没 ready（launcher/身份文件/端口/DB 读版本），写进回退报告。"""
    tail = str(log_tail or '')
    low = tail.lower()
    if '库需要至少 v' in tail or 'readertooold' in low or 'reader_too_old' in low:
        return {'reason': 'db_reader_too_old',
                'detail': '旧代码不能读已被 B 迁移过的新库；需先恢复切换前主库快照'}
    if 'modulenotfounderror' in low or 'importerror' in low:
        return {'reason': 'launcher_import_failed',
                'detail': tail[-400:] or 'launcher import error'}
    if port_in_use or 'address already in use' in low or 'errno 98' in low:
        return {'reason': 'port_in_use',
                'detail': 'A 旧端口在回退时仍被占用，A2 无法 bind'}
    if identity_present:
        return {'reason': 'identity_written_but_not_ready',
                'detail': str(ready_error or 'process alive but HTTP not ready')[:300]}
    if returncode is not None:
        return {'reason': f'process_exit_code_{returncode}',
                'detail': tail[-400:] or 'launcher exited before writing identity'}
    return {'reason': 'identity_file_missing',
            'detail': '等待期内未写出身份文件且进程仍存活（启动过慢或卡住）'}


def switch_verdict(switch: dict) -> dict:
    """切换阶段的逐项判定（A release 为 n/a 时不判失败）。"""
    drain = switch.get('a_drain') or {}
    release = switch.get('a_release') or {}
    b_auth = switch.get('b_authoritative') or {}
    entry = switch.get('entry') or {}
    ledger = switch.get('ledger') or {}
    probes = switch.get('probes') or []
    rollback = switch.get('rollback') or {}

    release_reasons: list[str] = []
    if release.get('applicable') is True:
        release_reasons = list(release_observed(release.get('row'),
                                                release.get('fence_before')).get('reasons') or [])
    b_reasons: list[str] = []
    if b_auth.get('started') is not True:
        b_reasons.append('b_not_started')
    if b_auth.get('readyz_200') is not True:
        b_reasons.append('b_readyz_not_200')
    lease = b_auth.get('lease_row') or {}
    if lease.get('role') != 'serving':
        b_reasons.append('b_lease_role_not_serving')
    if not lease.get('owner'):
        b_reasons.append('b_lease_owner_empty')
    if int(lease.get('lease_until') or 0) <= int(b_auth.get('checked_at') or 0):
        b_reasons.append('b_lease_not_held')
    migration = b_auth.get('migration') or {}
    missing_tables = [name for name in REQUIRED_LEASE_TABLES
                      if name not in (migration.get('tables_after') or [])]
    if missing_tables:
        b_reasons.append('migration_missing_tables:' + ','.join(missing_tables))
    probe_reasons = []
    for probe in probes:
        if probe.get('expected') == 200:
            if probe.get('status') != 200:
                probe_reasons.append(f"probe_status:{probe.get('case')}")
                continue
            if probe.get('terminal') != 'completed':
                probe_reasons.append(f"probe_terminal:{probe.get('case')}")
        else:
            want = str(probe.get('expected_class') or '')
            got = _status_class(probe.get('status'))
            if not want or got != want:
                probe_reasons.append(f"probe_status:{probe.get('case')}")
    ledger_reasons = []
    if ledger.get('request_logs_available') is not True:
        ledger_reasons.append('request_logs_unavailable')
    if int(ledger.get('duplicate_request_ids') or 0) != 0:
        ledger_reasons.append('duplicate_request_ids')
    if ledger.get('missing'):
        ledger_reasons.append('missing_logs')
    if ledger.get('tokens_applicable') is not False and (
            ledger.get('request_logs_tokens') != ledger.get('usage_daily_tokens')):
        ledger_reasons.append('tokens_mismatch')
    # 候选7 修复：未结算预留/无日志 outcome 都不能只查 0 —— A 主库天然带着影子期
    # cancel/error 的可归因残值（影子账本已判 pass）。新证据按“允许集合”归因；
    # 没有归因明细的旧证据才退回旧的 0 检查（保持对历史产物的可读性）。
    unexpected_outcome = ledger.get('unexpected_outcome_without_log_ids')
    if unexpected_outcome is None:
        if int(ledger.get('unexpected_outcome_without_log') or 0) != 0:
            ledger_reasons.append('unexpected_outcome_without_log')
    elif unexpected_outcome:
        ledger_reasons.append('unexpected_outcome_without_log_ids')
    unexpected_unsettled = ledger.get('unexpected_unsettled_ids')
    if unexpected_unsettled is None:
        if int(ledger.get('open_or_unknown_reservations') or 0) != 0:
            ledger_reasons.append('open_or_unknown_reservations')
    elif unexpected_unsettled:
        ledger_reasons.append('unexpected_unsettled_reservations')
    if ledger.get('a_shadow_rows_missing'):
        ledger_reasons.append('a_shadow_rows_missing')
    checks = {
        'a_drain_ok': (drain.get('begin_status') == 200 and drain.get('readyz_503') is True
                       and drain.get('in_flight_accepted') is True
                       and drain.get('in_flight_terminal') == 'completed'
                       and (drain.get('rejected_after_drain') or {}).get('rejected') is True),
        'a_release_ok': not release_reasons,
        'b_authoritative_ok': not b_reasons,
        'entry_switched_ok': (entry.get('nginx_t_ok') is True and entry.get('reload_ok') is True
                              and entry.get('front_200') is True),
        'switch_probes_ok': bool(probes) and not probe_reasons,
        'switch_ledger_ok': not ledger_reasons and int(ledger.get('expected_ids') or 0) > 0,
        'rollback_ok': (rollback.get('attempted') is not True) or rollback.get('pass') is True,
    }
    reasons = (['a_drain:' + name for name in _drain_reasons(drain)]
               + ['a_release:' + name for name in release_reasons]
               + ['b_authoritative:' + name for name in b_reasons]
               + ([] if checks['entry_switched_ok'] else ['entry_not_switched'])
               + ['switch_probe:' + name for name in probe_reasons]
               + ['switch_ledger:' + name for name in ledger_reasons]
               + ([] if checks['rollback_ok'] else ['rollback_failed']))
    return {'pass': all(checks.values()), 'checks': checks, 'reasons': reasons,
            'release_applicable': release.get('applicable') is True,
            'manual_intervention_required': (rollback.get('manual_intervention_required') is True)}


def _drain_reasons(drain: dict) -> list[str]:
    reasons = []
    if drain.get('begin_status') != 200:
        reasons.append('begin_not_200')
    if drain.get('readyz_503') is not True:
        reasons.append('readyz_not_503')
    if drain.get('in_flight_accepted') is not True:
        reasons.append('in_flight_not_accepted')
    if drain.get('in_flight_terminal') != 'completed':
        reasons.append('in_flight_not_completed')
    if (drain.get('rejected_after_drain') or {}).get('rejected') is not True:
        reasons.append('new_admission_not_rejected')
    return reasons


def is_drain_rejection(status, body_text: str) -> bool:
    """drain 后新请求必须被 503 gateway_draining 拒绝（经入口）。"""
    if status != 503:
        return False
    try:
        payload = json.loads(body_text or '')
    except (ValueError, TypeError):
        return 'gateway_draining' in (body_text or '')
    error = payload.get('error') if isinstance(payload, dict) else {}
    return isinstance(error, dict) and error.get('code') == 'gateway_draining'


def entry_facts_verdict(entries: dict) -> dict:
    """两个入口都必须是真实 TLS + 独立 nginx prefix（Lead 修正 #3）。"""
    reasons: list[str] = []
    for name in ('a', 'b'):
        row = entries.get(name) or {}
        if not row:
            reasons.append(f'{name}_entry_missing')
            continue
        if row.get('tls_ok') is not True:
            reasons.append(f'{name}_entry_not_tls')
        if not str(row.get('url') or '').startswith('https://'):
            reasons.append(f'{name}_entry_not_https')
        if not row.get('cert_sha256'):
            reasons.append(f'{name}_cert_fingerprint_missing')
        if not row.get('conf_sha256'):
            reasons.append(f'{name}_conf_sha_missing')
        if row.get('prefix_isolated') is not True:
            reasons.append(f'{name}_prefix_not_isolated')
        if row.get('front_ready') is not True:
            reasons.append(f'{name}_entry_not_reachable')
    a, b = entries.get('a') or {}, entries.get('b') or {}
    if a and b:
        if a.get('port') == b.get('port'):
            reasons.append('entry_ports_identical')
        if a.get('prefix') == b.get('prefix'):
            reasons.append('entry_prefixes_identical')
    return {'ok': not reasons, 'reasons': reasons}


def overall_verdict(evidence: dict) -> dict:
    """总判定 + 退出码（0/1/2/3）。边界违规在更早的阶段直接 exit 2。"""
    shadow = evidence.get('shadow') or {}
    switch = evidence.get('switch') or {}
    outcome = switch.get('outcome') or 'not_attempted'
    reasons: list[str] = list(shadow.get('reasons') or [])
    if shadow.get('pass') is not True:
        return {'decision': 'refuse_switch', 'exit_code': EXIT_FAIL,
                'reasons': ['shadow_failed:' + name for name in reasons],
                'outcome': outcome, 'policy': 'shadow_gate'}
    if outcome in ('refused_precondition', 'refused_precondition_restore_failed'):
        restored = switch.get('service_restored') is True
        exit_code = EXIT_SWITCH_REFUSED if restored else EXIT_FAIL
        return {'decision': 'refuse_switch', 'exit_code': exit_code,
                'reasons': ['switch_refused:' + name
                            for name in (switch.get('verdict') or {}).get('reasons') or []]
                           + ([] if restored else ['service_not_restored']),
                'outcome': outcome,
                'policy': 'allow_switch_requires_shadow_gate_and_serial_protocol'}
    verdict = switch.get('verdict') or {}
    if outcome == 'switched' and verdict.get('pass') is True:
        return {'decision': 'allow_switch', 'exit_code': EXIT_PASS, 'reasons': [],
                'outcome': outcome, 'policy': 'switched'}
    reasons_out = ['switch_failed:' + name for name in verdict.get('reasons') or []]
    if outcome == 'switch_failed_rollback_failed' or verdict.get('manual_intervention_required'):
        reasons_out.append('manual_intervention_required')
    return {'decision': 'refuse_switch', 'exit_code': EXIT_FAIL,
            'reasons': reasons_out or ['switch_not_completed'],
            'outcome': outcome, 'policy': 'switch_failed'}


# ─────────────────────────── IO 辅助（仅 VM） ───────────────────────────
def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return ''


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def port_closed(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(('127.0.0.1', int(port))) != 0


def query_rows(db_path, sql: str, params=()) -> list[dict]:
    import sqlite3
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def table_names(db_path) -> list[str]:
    try:
        return sorted(row['name'] for row in query_rows(
            db_path, "SELECT name FROM sqlite_master WHERE type='table'"))
    except Exception:  # noqa: BLE001
        return []


def _chunks(items: list, size: int = 400):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def ledger_completeness(db_path, expected_ids: list[str], *, table: str = 'request_logs') -> dict:
    """每个期望的 request_id 必须恰一行；返回 missing / duplicate 列表。"""
    ids = [value for value in expected_ids if value]
    found: dict[str, int] = {}
    for chunk in _chunks(ids):
        placeholders = ','.join('?' * len(chunk))
        rows = query_rows(db_path,
                          f'SELECT request_id, COUNT(*) AS c FROM {table} '
                          f'WHERE request_id IN ({placeholders}) GROUP BY request_id',
                          tuple(chunk))
        for row in rows:
            found[str(row['request_id'])] = int(row['c'])
    missing = [value for value in ids if value not in found]
    duplicate = [value for value, count in found.items() if count > 1]
    return {'expected': len(ids), 'found': len(found), 'missing': missing,
            'duplicate': duplicate}


def ledger_snapshot(db_path) -> dict:
    """账本快照：缺失的表按 n/a 处理，查询异常记进 errors（不静默改口径）。"""
    snapshot = {'tables': table_names(db_path), 'errors': [],
                'request_logs_rows': 0, 'duplicate_request_ids': 0,
                'request_logs_available': False,
                'inference_outcomes_rows': None, 'unexpected_outcome_without_log': None,
                'budget_reservations_rows': None, 'settled_reservation_without_log': None,
                'open_or_unknown_reservations': None,
                'request_logs_tokens': 0, 'usage_daily_tokens': 0}
    tables = set(snapshot['tables'])

    def count(label: str, sql: str, params=()) -> int | None:
        try:
            rows = query_rows(db_path, sql, params)
            return int((rows[0] or {}).get('c') or 0) if rows else 0
        except Exception as exc:  # noqa: BLE001
            snapshot['errors'].append(f'{label}:{type(exc).__name__}')
            return None

    if 'request_logs' in tables:
        snapshot['request_logs_rows'] = count(
            'request_logs_rows',
            "SELECT COUNT(*) AS c FROM request_logs WHERE request_id IS NOT NULL "
            "AND endpoint LIKE '/v1/%'") or 0
        snapshot['duplicate_request_ids'] = count(
            'duplicate_request_ids',
            'SELECT COUNT(*) AS c FROM (SELECT request_id FROM request_logs '
            'WHERE request_id IS NOT NULL GROUP BY request_id HAVING COUNT(*) > 1)') or 0
        snapshot['request_logs_tokens'] = count(
            'request_logs_tokens',
            'SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS c '
            'FROM request_logs WHERE status=200') or 0
        # 查询失败（旧库缺列等）时不能把 duplicate=0 当成“干净”：标记不可用。
        snapshot['request_logs_available'] = not any(
            str(error).startswith(('request_logs', 'duplicate_request_ids'))
            for error in snapshot['errors'])
    if 'usage_daily' in tables:
        snapshot['usage_daily_tokens'] = count(
            'usage_daily_tokens',
            'SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS c FROM usage_daily') or 0
    if 'request_outcomes' in tables:
        snapshot['inference_outcomes_rows'] = count(
            'inference_outcomes_rows',
            "SELECT COUNT(*) AS c FROM request_outcomes WHERE protocol != 'http'")
        snapshot['unexpected_outcome_without_log'] = count(
            'unexpected_outcome_without_log',
            "SELECT COUNT(*) AS c FROM request_outcomes o WHERE o.protocol != 'http' "
            "AND o.http_status NOT IN (503,429) AND NOT EXISTS ("
            "SELECT 1 FROM request_logs l WHERE l.request_id = o.request_id)")
    if 'budget_reservations' in tables:
        snapshot['budget_reservations_rows'] = count(
            'budget_reservations_rows', 'SELECT COUNT(*) AS c FROM budget_reservations')
        snapshot['settled_reservation_without_log'] = count(
            'settled_reservation_without_log',
            "SELECT COUNT(*) AS c FROM budget_reservations b WHERE b.state='settled' "
            "AND NOT EXISTS (SELECT 1 FROM request_logs l WHERE l.request_id=b.request_id)")
        snapshot['open_or_unknown_reservations'] = count(
            'open_or_unknown_reservations',
            "SELECT COUNT(*) AS c FROM budget_reservations "
            "WHERE state IN ('unknown','reserved','forwarded')")
        snapshot['unsettled_request_ids'] = []
        try:
            snapshot['unsettled_request_ids'] = sorted(
                str(row['request_id']) for row in query_rows(
                    db_path,
                    "SELECT request_id FROM budget_reservations "
                    "WHERE state IN ('unknown','reserved','forwarded') "
                    "AND request_id IS NOT NULL") if row.get('request_id'))
        except Exception as exc:  # noqa: BLE001 旧库缺列时不能误判为“无残值”
            snapshot['errors'].append(f'unsettled_request_ids:{type(exc).__name__}')
    return snapshot


def ids_present_in_db(db_path, ids: list[str], *, table: str = 'request_logs') -> int:
    total = 0
    for chunk in _chunks([value for value in ids if value]):
        placeholders = ','.join('?' * len(chunk))
        rows = query_rows(db_path,
                          f'SELECT COUNT(DISTINCT request_id) AS c FROM {table} '
                          f'WHERE request_id IN ({placeholders})', tuple(chunk))
        total += int((rows[0] or {}).get('c') or 0) if rows else 0
    return total


def cancel_terminals(db_path, ids: list[str]) -> dict:
    """取消用例的终态：必须记录成 client_cancelled。"""
    rows: dict[str, str | None] = {}
    for chunk in _chunks([value for value in ids if value]):
        placeholders = ','.join('?' * len(chunk))
        for row in query_rows(db_path,
                              'SELECT request_id, terminal_state FROM request_logs '
                              f'WHERE request_id IN ({placeholders})', tuple(chunk)):
            rows[str(row['request_id'])] = row.get('terminal_state')
    missing = [value for value in ids if value and rows.get(value) != 'client_cancelled']
    return {'missing': missing,
            'recorded': sum(1 for value in ids if rows.get(value) == 'client_cancelled')}


def outcome_without_log_ids(db_path) -> list[str]:
    """推理类 outcome 但 request_logs 缺失的 request_id（用于按已知集合归因）。"""
    try:
        tables = set(table_names(db_path))
        if 'request_outcomes' not in tables or 'request_logs' not in tables:
            return []
        rows = query_rows(
            db_path,
            "SELECT o.request_id FROM request_outcomes o WHERE o.protocol != 'http' "
            "AND o.http_status NOT IN (503,429) AND NOT EXISTS ("
            "SELECT 1 FROM request_logs l WHERE l.request_id = o.request_id) "
            'ORDER BY o.request_id')
        return [str(row['request_id']) for row in rows if row.get('request_id')]
    except Exception:  # noqa: BLE001 旧库形状差异不在这里判失败（另有 available 标记）
        return []


def backup_main_db(db_path, snapshot_path) -> dict:
    """切换前主库快照（SQLite backup API，WAL 下也能拿到一致副本）。

    回退时必须恢复成“旧代码能读的库”：B 的迁移会把 required_reader 提到 v8，
    旧代码再开这个库会被 schemaver.ReaderTooOld 拒绝（候选7 回退失败的根因）。
    """
    import sqlite3
    target = Path(snapshot_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        try:
            destination = sqlite3.connect(str(target))
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        return {'ok': True, 'path': str(target), 'bytes': target.stat().st_size,
                'sha256': sha256_file(target), 'error': None}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'path': str(target), 'bytes': 0, 'sha256': '',
                'error': type(exc).__name__ + ': ' + str(exc)[:300]}


def restore_main_db(db_path, snapshot_path, *, expected_sha256: str = '') -> dict:
    """Stopped-writer local lab only; refuse overwriting any new business data."""
    source = Path(snapshot_path)
    target = Path(db_path)
    record = {'ok': False, 'snapshot': str(source), 'db': str(target),
              'snapshot_sha256': '', 'removed_wal': [], 'error': None}
    try:
        if not source.is_file():
            record['error'] = 'snapshot_missing'
            return record
        record['snapshot_sha256'] = sha256_file(source)
        if expected_sha256 and record['snapshot_sha256'] != expected_sha256:
            record['error'] = 'snapshot_sha256_mismatch'
            return record
        record['business_data_guard'] = compare_business_data(target, source)
        if not record['business_data_guard']['safe_to_restore']:
            record['error'] = 'business_data_changed_since_snapshot'
            return record
        for suffix in ('-wal', '-shm'):
            side = Path(str(target) + suffix)
            if side.exists():
                side.unlink()
                record['removed_wal'].append(side.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        record['ok'] = target.is_file() and target.stat().st_size > 0
        record['bytes'] = target.stat().st_size if target.exists() else 0
        record['tables'] = len(table_names(target))
    except Exception as exc:  # noqa: BLE001
        record['error'] = type(exc).__name__ + ': ' + str(exc)[:300]
    return record


def port_bindable(port: int) -> bool:
    """该端口现在能否被 A2 绑定（与 uvicorn 一样带 SO_REUSEADDR）。

    只做 connect 探测会把“已 bind 但未 listen”的占用误判为空闲；回退前必须按
    真实 bind 结果判断，否则 A2 会在 bind 阶段失败而身份文件已经写出。
    """
    listener = socket.socket()
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', int(port)))
        return True
    except OSError:
        return False
    finally:
        listener.close()


def wait_port_free(port: int, timeout: float = 15.0, poll: float = 0.2) -> dict:
    """回退前等旧端口释放；A2 要 bind 同一个端口（入口配置不用改就能恢复）。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    in_use = True
    while time.monotonic() < deadline:
        in_use = not port_bindable(port)
        if not in_use:
            break
        time.sleep(poll)
    return {'port': int(port), 'in_use': in_use, 'free': not in_use,
            'waited_s': round(max(0.0, float(timeout) - (deadline - time.monotonic())), 3)}


def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def run_case(url: str, key: str, case: dict, *, timeout: float,
             progress: dict | None = None, rel_ms=None) -> dict:
    """向一个入口发一次真实 HTTP/TLS/SSE 请求并记录可比较字段。"""
    import httpx
    body = case_body_bytes(case)
    headers = {'Authorization': 'Bearer ' + key,
               'Content-Type': 'application/json',
               'Accept': 'text/event-stream' if case['stream'] else 'application/json'}
    clock = rel_ms or (lambda moment=None: round(time.monotonic(), 3))
    record = {'case': case['name'], 'kind': case['kind'], 'status': None, 'frames': 0,
              'stream': bool(case['stream']), 'saw_done': False, 'terminals': [],
              'cancelled': False,
              'execution_id': None, 'error': None, 'body_head': '',
              't_start_ms': clock(), 't_end_ms': None, 'first_frame_ms': None}
    try:
        with httpx.Client(trust_env=False, timeout=timeout, verify=_ssl_context()) as client:
            if case['stream']:
                with client.stream('POST', url + case['path'], content=body,
                                   headers=headers) as response:
                    record['status'] = response.status_code
                    record['execution_id'] = response.headers.get('x-request-id')
                    if response.status_code == 200:
                        for line in response.iter_lines():
                            if not line.startswith('data:'):
                                continue
                            payload = line[5:].strip()
                            if not payload:
                                continue
                            if payload == '[DONE]':
                                record['saw_done'] = True
                                record['terminals'].append('[DONE]')
                                break
                            record['frames'] += 1
                            if record['first_frame_ms'] is None:
                                record['first_frame_ms'] = clock()
                            if progress is not None:
                                progress['frames'] = record['frames']
                                progress['status'] = record['status']
                            try:
                                obj = json.loads(payload)
                            except ValueError:
                                continue
                            if not isinstance(obj, dict):
                                continue
                            if isinstance(obj.get('error'), dict):
                                record['terminals'].append('error')
                                record['error'] = json.dumps(
                                    obj['error'], ensure_ascii=False)[:200]
                                continue
                            kind = obj.get('type')
                            if kind in ('response.completed', 'response.failed',
                                        'response.incomplete', 'message_stop'):
                                record['terminals'].append(kind)
                            if kind == 'message_delta':
                                delta = obj.get('delta')
                                if isinstance(delta, dict) and delta.get('stop_reason'):
                                    record['terminals'].append('stop_reason')
                            for choice in obj.get('choices') or []:
                                if isinstance(choice, dict) and choice.get('finish_reason'):
                                    record['terminals'].append('finish_reason')
                            # 取消用例：收到首帧后主动断开客户端（模拟真实取消）。
                            if case['kind'] == 'cancel':
                                record['cancelled'] = True
                                break
                    else:
                        record['body_head'] = response.read(800).decode('utf-8', 'replace')
            else:
                response = client.post(url + case['path'], content=body, headers=headers)
                record['status'] = response.status_code
                record['execution_id'] = response.headers.get('x-request-id')
                record['body_head'] = response.text[:800]
    except Exception as exc:  # noqa: BLE001
        record['error'] = type(exc).__name__ + ': ' + str(exc)[:200]
    record['t_end_ms'] = clock()
    record['terminal'] = classify_terminal(record)
    return record


def spawn_entry_stream(url: str, key: str, case: dict, timeout: float,
                       rel_ms=None) -> dict:
    holder: dict = {'progress': {}}

    def run() -> None:
        holder['record'] = run_case(url, key, case, timeout=timeout,
                                    progress=holder['progress'], rel_ms=rel_ms)

    thread = threading.Thread(target=run, daemon=True)
    holder['thread'] = thread
    thread.start()
    return holder


def wait_frames(holder: dict, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if int((holder.get('progress') or {}).get('frames') or 0) >= 1:
            return True
        time.sleep(0.02)
    return False


def wait_ready(instance: Instance, timeout: float) -> dict:
    """实例就绪：接受 /api/readyz 或 /api/healthz 的 200（旧快照可能只有后者）。"""
    import httpx
    deadline = time.monotonic() + timeout
    last = {'ready': False, 'status': None, 'path': None, 'error': 'timeout'}
    with httpx.Client(base_url=instance.url, timeout=3.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            for path in READY_PATHS:
                try:
                    response = client.get(path)
                    if response.status_code == 200:
                        return {'ready': True, 'status': 200, 'path': path}
                    last = {'ready': False, 'status': response.status_code,
                            'path': path, 'error': 'not_200'}
                    if path == '/api/readyz' and response.status_code not in (404, 405):
                        break
                except Exception as exc:  # noqa: BLE001
                    last = {'ready': False, 'status': None, 'path': path,
                            'error': type(exc).__name__}
            if instance.proc is not None and instance.proc.poll() is not None:
                return {'ready': False, 'status': None, 'path': None,
                        'error': f'process_exited:{instance.proc.returncode}'}
            time.sleep(0.1)
    return last


def attach_session(instance: Instance) -> int | None:
    import httpx
    instance.session = httpx.Client(base_url=instance.url, timeout=20.0, trust_env=False)
    response = instance.session.post('/api/login', json={
        'username': 'r3l2-admin', 'password': 'r3l2-synthetic-password'})
    return response.status_code


class InstanceView:
    """给 `r3l2_handoff.Instance` 用的视图：只换 env/source/root，不复制其实现。"""

    def __init__(self, experiment: HandoffExperiment, *, source: Path, env: dict,
                 root: Path, label: str) -> None:
        self.experiment = experiment
        self.source = Path(source)
        self.env = dict(env)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.label = label
        self.timeline = experiment.timeline
        self.threeway_spec = experiment.threeway_spec
        self.source_sha = experiment.source_sha
        self.build_id = experiment.build_id

    def wait_ready(self, instance: Instance, timeout: float = 120) -> bool:
        return bool(wait_ready(instance, timeout).get('ready'))


OLD_LAUNCHER = r'''
import json, os, sys
from pathlib import Path

REPO = Path(os.environ['R3L2_MANAGER_ROOT'])
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

from server.routers import gateway
try:
    gateway._THREEWAY_SPEC = json.loads(os.environ['R3L2_THREEWAY_SPEC'])
except Exception:
    pass

from server import config, db, keysvc, security
config.ensure_dirs()
security.save_users({'secret': 'r3l2-signing-' * 8, 'api_keys': [], 'users': [
    {'username': 'r3l2-admin', 'role': 'admin',
     'pwd_hash': security.make_hash('r3l2-synthetic-password')}]})
db.connect()

keys_file = Path(os.environ['R3L2_KEYS_FILE'])
if not keys_file.is_file():
    keys = {}
    for platform in ('workbuddy', 'qoder', 'trae'):
        keys[platform] = keysvc.create_key('r3l2-' + platform,
                                           platforms=[platform])['key']
    tmp = keys_file.with_suffix('.tmp')
    tmp.write_text(json.dumps(keys), encoding='utf-8')
    tmp.replace(keys_file)
keys = json.loads(keys_file.read_text(encoding='utf-8'))

# 旧快照没有 instance_leases / 实例运行时：不 claim、不心跳，只把 key 交给父进程。
target = Path(os.environ['R3L2_INSTANCE_FILE'])
temporary = target.with_suffix('.tmp')
temporary.write_text(json.dumps({
    'instance_id': None, 'owner': None, 'fence': None, 'pid': os.getpid(),
    'claimed': False, 'claim_source': 'legacy_snapshot_without_instance_leases',
    'keys': keys}, ensure_ascii=False), encoding='utf-8')
temporary.replace(target)

from server.serve import main
try:
    main(['--host', '127.0.0.1', '--port', os.environ['R3L2_PORT'],
          '--timeout-graceful-shutdown', '45'])
except SystemExit as exc:
    # 更老的快照没有 --timeout-graceful-shutdown：argparse 以 2 退出时退回最小参数。
    if exc.code == 2:
        main(['--host', '127.0.0.1', '--port', os.environ['R3L2_PORT']])
    else:
        raise
'''


def start_instance(view: InstanceView, tag: str, *, ready_timeout: float,
                   launcher_text: str | None = None,
                   port_override: int | None = None, cleanup: list | None = None) -> dict:
    """启动一个真实 Manager 进程；旧快照用兼容 launcher，其余复用既有 launcher。

    ``port_override`` 用于回退：A2 必须 bind 回 A 的旧端口，入口配置不用改。
    """
    if launcher_text is not None:
        launcher = view.root / 'launch_manager.py'
        launcher.write_text(launcher_text, encoding='utf-8')
    instance = Instance(view, tag)
    if cleanup is not None:
        cleanup.append(instance)
    if port_override is not None:
        instance.port = int(port_override)
        instance.url = f'http://127.0.0.1:{int(port_override)}'
    instance.start(wait_ready=False)
    info = getattr(instance, 'info', None) or {}
    identity_path = view.root / f'instance-{tag}.json'
    exists = identity_path.is_file()
    ready = wait_ready(instance, ready_timeout) if info else {
        'ready': False, 'error': 'identity_file_missing'}
    login = attach_session(instance) if ready.get('ready') else None
    return {'instance': instance, 'info': info, 'identity_file': exists,
            'identity_path': str(identity_path), 'ready': ready,
            'login_status': login}


def stop_instance(instance: Instance | None, *, reason: str = '') -> int | None:
    if instance is None:
        return None
    try:
        code = instance.stop(timeout=60)
        if instance.proc is not None and instance.proc.poll() is None:
            code = instance.proc.wait(timeout=15)
    except Exception:  # noqa: BLE001
        code = None
    return code


# ─────────────────────────── 入口（真实 nginx TLS） ───────────────────────────
def build_entry_conf(*, root: Path, cert: Path, key: Path, listen_port: int,
                     upstream_port: int) -> str:
    """单实例 upstream 的 TLS 入口；标记块与 switch_upstream 的改写原语兼容。"""
    servers = f'\n    server 127.0.0.1:{int(upstream_port)};'
    return f'''user gateway-lab;
worker_processes 1;
pid {root}/nginx.pid;
error_log {root}/nginx-error.log warn;
events {{ worker_connections 128; }}
http {{
 access_log {root}/access.log;
 default_type application/octet-stream;
 {switch_upstream.MARK_BEGIN}
upstream manager_backend {{{servers}
}}
 {switch_upstream.MARK_END}
 server {{
  listen 127.0.0.1:{int(listen_port)} ssl;
  server_name zhihui.lab;
  ssl_certificate {cert};
  ssl_certificate_key {key};
  {SECURITY_HEADERS}
  location / {{
   proxy_pass http://manager_backend;
   proxy_http_version 1.1;
   proxy_set_header Host $http_host;
   proxy_set_header X-Real-IP $remote_addr;
   proxy_set_header X-Forwarded-For $remote_addr;
   proxy_set_header X-Forwarded-Proto $scheme;
   proxy_set_header Authorization $http_authorization;
   proxy_buffering off;
   proxy_read_timeout 600s;
  }}
 }}
}}
'''


class Entry:
    """一个真实的 nginx TLS 入口（独立 prefix、独立证书、独立端口）。"""

    def __init__(self, experiment: HandoffExperiment, name: str, upstream_port: int) -> None:
        self.name = name
        self.experiment = experiment
        self.root = experiment.root / f'entry-{name}'
        self.root.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.url = f'https://127.0.0.1:{self.port}'
        self.cert = self.root / 'tls.crt'
        self.key = self.root / 'tls.key'
        self.conf = self.root / 'nginx.conf'
        self.state = {'root': str(self.root), 'nginx_conf': str(self.conf),
                      'host_port': self.port, 'production_connected': False}
        self.upstream_port = int(upstream_port)

    def _make_cert(self) -> None:
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                        'ec_paramgen_curve:P-256', '-nodes', '-days', '1',
                        '-subj', '/CN=zhihui.lab', '-addext',
                        'subjectAltName=DNS:zhihui.lab,IP:127.0.0.1',
                        '-keyout', str(self.key), '-out', str(self.cert)],
                       check=True, capture_output=True, text=True)

    def start(self, timeout: float = 90) -> dict:
        self._make_cert()
        self.conf.write_text(build_entry_conf(
            root=self.root, cert=self.cert, key=self.key,
            listen_port=self.port, upstream_port=self.upstream_port), encoding='utf-8')
        check = subprocess.run(switch_upstream.nginx_command(self.state, '-t'),
                               capture_output=True, text=True)
        if check.returncode != 0:
            raise RuntimeError('entry nginx -t failed: '
                               + (check.stderr or check.stdout)[-1500:])
        started = subprocess.run(switch_upstream.nginx_command(self.state),
                                 capture_output=True, text=True)
        if started.returncode != 0:
            raise RuntimeError('entry nginx start failed: '
                               + (started.stderr or started.stdout)[-1500:])
        front = self.wait_front(timeout)
        return {'started': True, 'front': front}

    def stop(self) -> None:
        if not (self.root / 'nginx.pid').is_file():
            return
        subprocess.run(switch_upstream.nginx_command(self.state, '-s', 'quit'),
                       capture_output=True, text=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and (self.root / 'nginx.pid').is_file():
            time.sleep(0.1)

    def front_probe(self, path: str = '/api/readyz', timeout: float = 15.0) -> dict:
        return switch_upstream.front_probe(self.state, path, timeout)

    def wait_front(self, timeout: float = 60, poll: float = 0.2) -> dict:
        deadline = time.monotonic() + timeout
        last = {'status': None, 'path': None}
        while time.monotonic() < deadline:
            for path in READY_PATHS:
                probe = self.front_probe(path)
                last = {'status': probe.get('status'), 'path': path,
                        'error': probe.get('error')}
                if probe.get('status') == 200:
                    return {'front_200': True, 'path': path, 'status': 200}
                if path == '/api/readyz' and probe.get('status') not in (404, 405):
                    break
            time.sleep(poll)
        return {'front_200': False, **last}

    def facts(self, experiment: HandoffExperiment) -> dict:
        conf_bytes = self.conf.read_bytes() if self.conf.is_file() else b''
        cert_bytes = self.cert.read_bytes() if self.cert.is_file() else b''
        return {'name': self.name, 'port': self.port, 'url': self.url,
                'prefix': str(self.root),
                'prefix_isolated': _inside(self.root, experiment.root)
                                   and str(self.root) != str(experiment.root),
                'tls_ok': bool(cert_bytes) and self.conf.is_file()
                          and 'ssl_certificate' in conf_bytes.decode('utf-8', 'replace'),
                'cert_sha256': sha256_bytes(cert_bytes) if cert_bytes else '',
                'conf_sha256': sha256_bytes(conf_bytes) if conf_bytes else '',
                'upstream_port': self.upstream_port}


# ─────────────────────────── 环境与视图 ───────────────────────────
SHADOW_ENV_KEYS = ('WB2API_BASE', 'WB2API_KEY', 'WB2API_MODE', 'WB_STATIC_DIR',
                   'WB_UPSTREAM_TIMEOUT', 'WB_TOTAL_TIMEOUT_S', 'WB_FIRST_BYTE_TIMEOUT_S',
                   'WB_DRAIN_TIMEOUT_SEC', 'ZH_INSTANCE_LEASE_TTL_SEC',
                   'ZH_INSTANCE_HEARTBEAT_SEC')


def prepare_environment(experiment: HandoffExperiment, repo: Path,
                        args: argparse.Namespace) -> dict:
    """复用既有 prepare（fake provider + 主隔离 env），再建 B 影子的独立隔离 env。"""
    sys.path.insert(0, str(repo / 'tests'))
    from zhihui_harness.isolation import IsolatedEnv
    experiment.prepare()
    experiment.source_sha = args.source_sha256 or sha256_file(
        repo / 'manager/server/main.py')
    experiment.build_id = (args.build_id or experiment.source_sha)[:12]
    shadow_iso = IsolatedEnv.create(repo, keep=True,
                                    base_dir=str(experiment.root / 'shadow-iso'))
    shadow_env = shadow_iso.build_env(experiment.provider)
    for key in SHADOW_ENV_KEYS:
        if key in experiment.env:
            shadow_env[key] = experiment.env[key]
    main_db = str(experiment.env['WB_DB'])
    shadow_db = str(shadow_env['WB_DB'])
    return {'shadow_iso': shadow_iso, 'shadow_env': shadow_env,
            'main_db': main_db, 'shadow_db': shadow_db}


def collect_source_facts(root: Path) -> dict:
    return {'main_sha256': sha256_file(root / 'manager/server/main.py'),
            'migrations_sha256': sha256_file(root / 'manager/server/migrations.py'),
            'has_manager': (root / 'manager/server/main.py').is_file()}


def static_guard_facts(args, repo: Path, a_repo: Path) -> dict:
    import socket as _socket
    qa = {}
    if Path(args.qa_file).is_file():
        try:
            qa = json.loads(Path(args.qa_file).read_text(encoding='utf-8'))
        except ValueError:
            qa = {}
    b_facts = collect_source_facts(repo)
    a_facts = collect_source_facts(a_repo)
    return {
        'hostname': _socket.gethostname(), 'home': str(Path.home()),
        'lab': str(LAB), 'repo': str(repo), 'repo_exists': repo.is_dir(),
        'repo_has_manager': b_facts['has_manager'],
        'repo_main_sha256': b_facts['main_sha256'],
        'repo_migrations_sha256': b_facts['migrations_sha256'],
        'a_repo': str(a_repo) if str(a_repo) not in ('', '.') else '',
        'a_repo_exists': a_repo.is_dir(), 'a_repo_has_manager': a_facts['has_manager'],
        'a_main_sha256': a_facts['main_sha256'],
        'a_migrations_sha256': a_facts['migrations_sha256'],
        'out': str(Path(args.out).resolve()),
        'out_exists': Path(args.out).resolve().exists(),
        'qa_file': str(Path(args.qa_file).resolve()),
        'qa_exists': Path(args.qa_file).is_file(),
        'qa_production_connected': qa.get('production_connected'),
        'qa_workspace': qa.get('workspace'),
        'db_a': args._db_a or '<pending-main-db>',
        'db_b': args._db_b or '<pending-shadow-db>',
        'loopback_ok': _is_loopback_url(args._provider_base),
    }


# ─────────────────────────── 影子窗口 ───────────────────────────
def run_pair(entry_a: Entry, key_a: str, entry_b: Entry, key_b: str, case: dict,
             *, timeout: float, rel_ms) -> tuple[dict, dict]:
    results: dict = {}

    def fire(side: str, url: str, key: str) -> None:
        results[side] = run_case(url, key, case, timeout=timeout, rel_ms=rel_ms)

    left = threading.Thread(target=fire, args=('a', entry_a.url, key_a), daemon=True)
    right = threading.Thread(target=fire, args=('b', entry_b.url, key_b), daemon=True)
    left.start()
    right.start()
    left.join(timeout + 30)
    right.join(timeout + 30)
    return (results.get('a') or {'status': None, 'error': 'no_result'},
            results.get('b') or {'status': None, 'error': 'no_result'})


def pair_summary(pair: dict) -> dict:
    return {'case': pair['case'], 'kind': pair['kind'], 'equal': pair['equal'],
            'reasons': pair['reasons'],
            'a': {'status': pair['a'].get('status'), 'frames': pair['a'].get('frames'),
                  'terminal': pair['a'].get('terminal'),
                  'execution_id': pair['a'].get('execution_id'),
                  'error': pair['a'].get('error')},
            'b': {'status': pair['b'].get('status'), 'frames': pair['b'].get('frames'),
                  'terminal': pair['b'].get('terminal'),
                  'execution_id': pair['b'].get('execution_id'),
                  'error': pair['b'].get('error')}}


def window_metrics(pairs: list[dict]) -> dict:
    total = len(pairs)
    a_unexpected = sum(1 for pair in pairs if pair['a_unexpected'])
    b_unexpected = sum(1 for pair in pairs if pair['b_unexpected'])
    status_checked = sum(1 for pair in pairs if pair['compared']['status'])
    status_equal = sum(1 for pair in pairs if pair['compared']['status']
                       and pair['equal'])
    sse_compared = sum(1 for pair in pairs if pair['compared']['frames'])
    sse_mismatches = sum(1 for pair in pairs
                         if pair['compared']['frames'] and not pair['equal'])
    terminal_compared = sum(1 for pair in pairs if pair['compared']['terminal'])
    terminal_mismatches = sum(1 for pair in pairs
                              if pair['compared']['terminal'] and not pair['equal'])
    error_shape_compared = sum(1 for pair in pairs if pair['compared']['error_shape'])
    error_shape_mismatches = sum(1 for pair in pairs
                                 if pair['compared']['error_shape']
                                 and not pair['equal'])
    window = {
        'request_pairs': total,
        'a': {'requests': total, 'success': total - a_unexpected,
              'failed': a_unexpected,
              'error_rate': 0.0 if not total else round(a_unexpected / total, 4)},
        'b': {'requests': total, 'success': total - b_unexpected,
              'failed': b_unexpected,
              'error_rate': 0.0 if not total else round(b_unexpected / total, 4)},
        'coverage': coverage_facts(pairs),
        'a_unexpected_failures': a_unexpected,
        'b_unexpected_failures': b_unexpected,
        'divergences': sum(1 for pair in pairs if not pair['equal']),
        'status_checked': status_checked,
        'status_equal': status_equal,
        'sse_compared': sse_compared,
        'sse_mismatches': sse_mismatches,
        'terminal_compared': terminal_compared,
        'terminal_mismatches': terminal_mismatches,
        'error_shape_compared': error_shape_compared,
        'error_shape_mismatches': error_shape_mismatches,
        # 报告字段与设计一一对应（consistency / sse），内部计数仍在上面。
        'consistency': {
            'status': {'checked': status_checked, 'equal': status_equal},
            'terminal': {'checked': terminal_compared,
                         'equal': terminal_compared - terminal_mismatches},
            'sse_frames': {'checked': sse_compared,
                           'equal': sse_compared - sse_mismatches},
            'error_body': {'checked': error_shape_compared,
                           'equal': error_shape_compared - error_shape_mismatches},
        },
        'sse': {'pairs_compared': sse_compared, 'mismatches': sse_mismatches},
        'cases': {},
        'mismatches': [pair_summary(pair) for pair in pairs if not pair['equal']][:20],
    }
    for pair in pairs:
        row = window['cases'].setdefault(
            pair['case'], {'pairs': 0, 'divergences': 0, 'a_unexpected': 0,
                           'b_unexpected': 0})
        row['pairs'] += 1
        row['divergences'] += 0 if pair['equal'] else 1
        row['a_unexpected'] += 1 if pair['a_unexpected'] else 0
        row['b_unexpected'] += 1 if pair['b_unexpected'] else 0
    return window


def shadow_window_verdict(window: dict, *, min_pairs: int,
                          allow_divergence: int) -> dict:
    """严格门（allow_switch 用）与配置门（仅证据用，放宽不参与切换判定）。"""
    strict = shadow_gate(window, min_pairs=min_pairs, divergence_allowance=0)
    configured = shadow_gate(window, min_pairs=min_pairs,
                             divergence_allowance=max(0, allow_divergence))
    note = ('allow-divergence 只用于非切换证据；allow_switch 判定恒用严格门（0 差异）。'
            if allow_divergence > 0 else '零差异严格门。')
    return {'strict': strict, 'configured': configured,
            'pass': strict['pass'], 'reasons': strict['reasons'],
            'policy_note': note}


# ─────────────────────────── 影子账本 ───────────────────────────
def settle_shadow_ledger(main_db: str, shadow_db: str, a_ids: list[str], b_ids: list[str],
                         a_cancel_ids: list[str], b_cancel_ids: list[str],
                         *, wait: float,
                         a_expected_unsettled_ids: list[str] | None = None,
                         b_expected_unsettled_ids: list[str] | None = None) -> dict:
    started = time.monotonic()
    ledger: dict = {}
    while True:
        ledger = build_shadow_ledger(main_db, shadow_db, a_ids, b_ids,
                                    a_cancel_ids, b_cancel_ids,
                                    a_expected_unsettled_ids,
                                    b_expected_unsettled_ids)
        if ledger_gate(ledger)['pass'] or time.monotonic() - started >= wait:
            break
        time.sleep(0.5)
    ledger['settle_ms'] = round((time.monotonic() - started) * 1000, 3)
    return ledger


def build_shadow_ledger(main_db: str, shadow_db: str, a_ids: list[str],
                        b_ids: list[str], a_cancel_ids: list[str],
                        b_cancel_ids: list[str],
                        a_expected_unsettled_ids: list[str] | None = None,
                        b_expected_unsettled_ids: list[str] | None = None) -> dict:
    a_snapshot = ledger_snapshot(main_db)
    b_snapshot = ledger_snapshot(shadow_db)
    a_complete = ledger_completeness(main_db, a_ids)
    b_complete = ledger_completeness(shadow_db, b_ids)
    a_cancel = cancel_terminals(main_db, a_cancel_ids)
    b_cancel = cancel_terminals(shadow_db, b_cancel_ids)
    a_side = {**{key: a_snapshot.get(key) for key in a_snapshot},
              'missing': a_complete['missing'], 'duplicate': a_complete['duplicate'],
              'expected': a_complete['expected'],
              'cancel_terminals_missing': a_cancel['missing'],
              'tokens_applicable': int(a_snapshot.get('usage_daily_tokens') or 0) > 0}
    b_side = {**{key: b_snapshot.get(key) for key in b_snapshot},
              'missing': b_complete['missing'], 'duplicate': b_complete['duplicate'],
              'expected': b_complete['expected'],
              'cancel_terminals_missing': b_cancel['missing'],
              'tokens_applicable': int(b_snapshot.get('usage_daily_tokens') or 0) > 0}
    a_allowed = {value for value in (a_expected_unsettled_ids or []) if value}
    b_allowed = {value for value in (b_expected_unsettled_ids or []) if value}
    a_side['unexpected_unsettled_ids'] = [
        value for value in (a_snapshot.get('unsettled_request_ids') or [])
        if value not in a_allowed]
    b_side['unexpected_unsettled_ids'] = [
        value for value in (b_snapshot.get('unsettled_request_ids') or [])
        if value not in b_allowed]
    ledger = {'db_isolated': _norm(main_db) != _norm(shadow_db),
              'a_execution_ids_available': len([value for value in a_ids if value]) > 0,
              'b_execution_ids_available': len([value for value in b_ids if value]) > 0,
              'a_execution_ids_seen': len([value for value in a_ids if value]),
              'b_execution_ids_seen': len([value for value in b_ids if value]),
              'a': a_side, 'b': b_side,
              'cross_contamination': {
                  'a_ids_in_b': ids_present_in_db(shadow_db, a_ids),
                  'b_ids_in_a': ids_present_in_db(main_db, b_ids)},
              }
    ledger['verdict'] = ledger_gate(ledger)
    ledger['pass'] = ledger['verdict']['pass']
    return ledger


# ─────────────────────────── 切换阶段 ───────────────────────────
def observe_release(experiment: HandoffExperiment, instance: Instance,
                    *, applicable: bool) -> dict:
    """A 退出后观测真实 release；旧快照无 instance_leases 时记 n/a（不判失败）。"""
    info = instance.info or {}
    instance_id = info.get('instance_id')
    fence = info.get('fence')
    record = {'applicable': applicable, 'instance_id': instance_id,
              'fence_before': fence, 'row': None, 'observed': None,
              'reasons': [], 'exit_returncode': instance.proc.returncode
              if instance.proc is not None else None}
    if not applicable:
        record['reasons'] = ['legacy_schema_without_instance_leases']
        return record
    row = experiment.instance_row(instance_id) if instance_id else None
    record['row'] = {key: (row or {}).get(key) for key in
                     ('instance_id', 'owner', 'role', 'fence', 'lease_until')}
    verdict = release_observed(row, fence)
    record['observed'] = verdict['observed']
    record['reasons'] = verdict['reasons']
    return record


def probe_switch_cases(entry: Entry, keys: dict, *, timeout: float, rel_ms) -> list[dict]:
    probes = []
    for case in SWITCH_PROBE_CASES:
        record = run_case(entry.url, keys[case['platform']], case, timeout=timeout,
                          rel_ms=rel_ms)
        probes.append({'case': case['name'], 'kind': case['kind'],
                       'status': record.get('status'),
                       'expected': 200 if case['kind'] == 'normal' else None,
                       'expected_class': '2xx' if case['kind'] == 'normal'
                       else f"{case['expected_class']}xx",
                       'terminal': record.get('terminal'),
                       'frames': record.get('frames'),
                       'execution_id': record.get('execution_id'),
                       'error': record.get('error')})
    return probes


def switch_ledger_snapshot(main_db: str, expected_ids: list[str],
                           a_shadow_ids: list[str], *,
                           shadow_unsettled_ids: list[str] | None = None,
                           probe_unsettled_ids: list[str] | None = None) -> dict:
    """切换后主库对账：A 影子期/切换期可归因残值不算失败，不可归因的才算。"""
    snapshot = ledger_snapshot(main_db)
    complete = ledger_completeness(main_db, expected_ids)
    shadow_complete = ledger_completeness(main_db, a_shadow_ids)
    attribution = attribute_residual(snapshot.get('unsettled_request_ids') or [],
                                     shadow_unsettled_ids or [],
                                     probe_unsettled_ids or [])
    known_ids = (list(a_shadow_ids) + list(shadow_unsettled_ids or [])
                 + list(probe_unsettled_ids or []))
    outcome_attribution = attribute_outcome_residual(
        outcome_without_log_ids(main_db), known_ids)
    snapshot.update({
        'expected': complete['expected'], 'missing': complete['missing'],
        'duplicate': complete['duplicate'],
        'a_shadow_rows_missing': shadow_complete['missing'],
        'tokens_applicable': int(snapshot.get('usage_daily_tokens') or 0) > 0,
        # 兼容旧 reason 名：仍保留原始计数，判定改用归因后的 unexpected 列表。
        'unsettled_request_ids': snapshot.get('unsettled_request_ids') or [],
        'unsettled_attribution': attribution,
        'unexpected_unsettled_ids': attribution['unexpected'],
        'outcome_without_log_attribution': outcome_attribution,
        'unexpected_outcome_without_log_ids': outcome_attribution['unexpected'],
    })
    return snapshot


def restore_a_service(experiment: HandoffExperiment, view_a: InstanceView,
                      entry_main: Entry | None, *, old_port: int,
                      original_conf: bytes | None, timeout: float,
                      rel_ms, cleanup: list | None = None,
                      db_snapshot: str | None = None,
                      db_snapshot_sha256: str = '',
                      db_path: str | None = None,
                      launcher_text: str | None = None,
                      db_recovery_mode: str = 'preserve',
                      live_instance: Instance | None = None,
                      writer_processes: list | None = None) -> dict:
    """Recover the old service against the CURRENT DB after B has stopped.

    The default never restores a pre-switch snapshot. A failed reader preflight
    stops recovery before entry changes. snapshot_if_unchanged is an explicit
    lab-only option guarded by full business-data comparison.
    """
    report: dict = {'attempted': True, 'steps': [], 'errors': [],
                    'a2_started': False, 'entry_restored': False,
                    'front_200': False, 'manual_intervention_required': False,
                    'db_snapshot': db_snapshot, 'db_recovery_mode': db_recovery_mode}

    def step(name: str, ok: bool, **detail) -> dict:
        row = {'step': name, 'ok': bool(ok), **detail}
        report['steps'].append(row)
        return row

    # Resume A or start A2 only after every other controlled writer has exited.
    active = [getattr(item, 'tag', 'unknown') for item in (writer_processes or [])
              if item is not None and item is not live_instance
              and item.proc is not None and item.proc.poll() is None]
    if writer_processes is None or active:
        report.update({'pass': False, 'manual_intervention_required': True})
        report['errors'].append('writer_exit_proof_missing' if writer_processes is None else 'writer_still_running')
        step('all_other_writers_exited', False, active=active)
        return report
    step('all_other_writers_exited', True)
    if live_instance is not None and live_instance.proc is not None and live_instance.proc.poll() is None:
        report['branch'] = 'resume_live_a'
        try:
            resumed = experiment.drain_action(live_instance, 'resume')
            ready = wait_ready(live_instance, timeout)
            report['a_resumed'] = resumed.get('status') == 200 and ready.get('ready') is True
            step('resume_live_a', report['a_resumed'])
            if not report['a_resumed']:
                raise RuntimeError('live A could not leave drain safely')
            if entry_main is not None:
                if original_conf is not None:
                    entry_main.conf.write_bytes(original_conf)
                    switch_upstream.reload_nginx(entry_main.state)
                report['front_200'] = bool(entry_main.wait_front(timeout).get('front_200'))
            else:
                report['front_200'] = True
            report['pass'] = report['front_200']
        except Exception as exc:
            report['errors'].append('resume_live_a:' + type(exc).__name__)
            report['pass'] = False
        report['manual_intervention_required'] = not report['pass']
        return report
    report['branch'] = 'restart_old_a2_with_current_db'

    if db_recovery_mode not in ('preserve', 'snapshot_if_unchanged'):
        raise ValueError('Unknown database recovery mode')
    # 1) Current DB is authoritative; restoring a snapshot requires an explicit mode.
    if db_recovery_mode == 'snapshot_if_unchanged' and db_snapshot:
        if not db_path:
            report['db_restored'] = False
            step('restore_main_db_snapshot', False, error='db_path_missing')
        else:
            restored_db = restore_main_db(db_path, db_snapshot,
                                          expected_sha256=db_snapshot_sha256)
            report['db_restored'] = bool(restored_db.get('ok'))
            report['db_restore'] = restored_db
            step('restore_main_db_snapshot', report['db_restored'],
                 snapshot=restored_db.get('snapshot'),
                 sha256=restored_db.get('snapshot_sha256'),
                 removed_wal=restored_db.get('removed_wal'),
                 tables=restored_db.get('tables'), error=restored_db.get('error'))
            if not report['db_restored']:
                report['errors'].append('db_restore_failed')
        if not report['db_restored']:
            report['pass'] = False
            report['manual_intervention_required'] = True
            return report
    else:
        report['db_restored'] = False
        step('restore_main_db_snapshot', True, skipped='current_database_preserved')

    if db_path:
        reader = probe_old_reader(db_path, view_a.source)
        report['reader_preflight'] = reader
        step('current_db_old_reader_preflight', reader['readable'], **reader)
        if not reader['readable']:
            report['pass'] = False
            report['manual_intervention_required'] = True
            report['errors'].append('current_db_not_readable_by_old_release')
            return report
    elif db_recovery_mode == 'preserve':
        report.update({'pass': False, 'manual_intervention_required': True})
        report['errors'].append('db_path_required_for_preserving_rollback')
        return report

    # 2) 入口配置先写回原字节（reload 等 A2 起来后再做，避免指到死端口）
    entry_written = False
    if entry_main is not None and original_conf is not None:
        try:
            entry_main.conf.write_bytes(original_conf)
            entry_written = True
            report['entry_restored'] = True
        except Exception as exc:  # noqa: BLE001
            report['errors'].append('entry_write:' + type(exc).__name__)
        step('restore_entry_config_bytes', entry_written,
             sha256=sha256_bytes(original_conf),
             error=None if entry_written else report['errors'][-1])
    else:
        step('restore_entry_config_bytes', True, skipped='entry_not_modified')

    # 3) 等旧端口释放（A2 要 bind 同一端口，入口配置才能不改就恢复）
    port_state = wait_port_free(old_port, timeout=min(15.0, max(1.0, timeout)))
    report['port'] = port_state
    step('wait_old_port_free', port_state['free'], **port_state)
    if not port_state['free']:
        report.update({'pass': False, 'manual_intervention_required': True})
        report['errors'].append('old_port_still_owned')
        return report

    # 4) 用影子期同一路径启动 A2（legacy launcher + 身份文件等待 + 就绪探测）
    started: dict = {}
    instance = None
    # 默认复用影子期 A 已写好的 launcher（同一启动路径）；万一缺失才回落到 legacy。
    if launcher_text is None and not (view_a.root / 'launch_manager.py').is_file():
        launcher_text = OLD_LAUNCHER
    try:
        started = start_instance(view_a, 'A2-recover', ready_timeout=timeout,
                                 launcher_text=launcher_text, port_override=old_port, cleanup=cleanup)
        instance = started['instance']
        report['a2_started'] = bool(started['ready'].get('ready'))
        report['a2_ready'] = started['ready']
        report['login_status'] = started.get('login_status')
    except BaseException as exc:  # noqa: BLE001
        report['errors'].append('a2_start:' + type(exc).__name__ + ':' + str(exc)[:200])
        report['a2_ready'] = {'ready': False, 'error': type(exc).__name__}

    launcher_path = view_a.root / 'launch_manager.py'
    identity_path = view_a.root / 'instance-A2-recover.json'
    log_path = view_a.root / 'manager-A2-recover.log'
    log_tail = ''
    if log_path.is_file():
        try:
            log_tail = scrub_log(log_path.read_text(errors='replace')[-4000:])
        except OSError:
            log_tail = ''
    returncode = None
    if instance is not None and instance.proc is not None:
        returncode = instance.proc.poll()
    diagnostics = {
        'launcher': {'path': str(launcher_path), 'exists': launcher_path.is_file(),
                     'sha256': sha256_file(launcher_path), 'kind': 'legacy-compatible'},
        'identity_file': {'path': str(identity_path),
                          'present': identity_path.is_file(),
                          'size': identity_path.stat().st_size
                          if identity_path.is_file() else 0},
        'port': {'value': int(old_port),
                 'in_use_before_start': bool(port_state.get('in_use')),
                 'in_use_after_start': not port_closed(old_port)},
        'log': {'path': str(log_path), 'returncode': returncode, 'tail': log_tail},
        'ready': report.get('a2_ready'),
        'db_snapshot_restored': report.get('db_restored'),
    }
    diagnostics['failure'] = classify_launcher_failure(
        returncode=returncode,
        identity_present=diagnostics['identity_file']['present'],
        port_in_use=diagnostics['port']['in_use_after_start'],
        log_tail=log_tail,
        ready_error=(report.get('a2_ready') or {}).get('error'))
    report['a2_diagnostics'] = diagnostics
    step('start_a2_legacy_launcher', report['a2_started'],
         identity_file=diagnostics['identity_file']['present'],
         returncode=returncode, failure=diagnostics['failure'])

    if instance is not None and report['a2_started'] is not True:
        stop_instance(instance, reason='a2_not_ready')

    # 5) 入口 reload（把前端指回 A 的旧端口）
    if entry_main is not None and report['a2_started']:
        if entry_written:
            try:
                switch_upstream.reload_nginx(entry_main.state)
                step('reload_entry_nginx', True)
            except BaseException as exc:  # noqa: BLE001
                report['errors'].append('entry_reload:' + type(exc).__name__)
                step('reload_entry_nginx', False, error=type(exc).__name__)
        else:
            step('reload_entry_nginx', True, skipped='config_unchanged')
        # 6) 前端 200（A2 没 ready 时不要空等两分钟）
        front = (entry_main.wait_front(min(120.0, max(10.0, timeout)))
                 if report['a2_started'] else {'front_200': False})
        report['front_200'] = bool(front.get('front_200'))
        report['front'] = front
        step('front_200_after_rollback', report['front_200'],
             path=front.get('path'), status=front.get('status'))
    report['pass'] = bool(report['a2_started'] and report['front_200']
                          and not report['errors'])
    report['manual_intervention_required'] = not report['pass']
    if not report['pass']:
        report['errors'].append('rollback_failed:' + str(
            (diagnostics.get('failure') or {}).get('reason')))
    return report


def run_switch_phase(experiment: HandoffExperiment, ctx: dict) -> dict:
    args = ctx['args']
    rel_ms = experiment.timeline.rel_ms
    switch: dict = {'protocol': ('serial: A drain → in-flight zero → A exit+release → '
                                 'B authoritative → entry switch → probe/ledger')}
    experiment.timeline.set_phase('switch')
    entry_main: Entry = ctx['entry_main']
    entry_shadow: Entry = ctx['entry_shadow']
    a: Instance = ctx['a']
    b_auth = None
    ctx.setdefault('cleanup', [])
    a_keys: dict = ctx['a_keys']
    main_db: str = ctx['main_db']

    # 0) 影子侧退出（B 影子与 B 影子入口都不再写任何库/收任何流量）
    b_shadow_exit = stop_instance(ctx['b_shadow'], reason='shadow_shutdown')
    entry_shadow.stop()
    switch['shadow_shutdown'] = {'b_shadow_exit': b_shadow_exit,
                                 'shadow_entry_stopped': True}

    # 1) 已准入长流（经 A 主入口）→ drain → 在途归零
    holder = spawn_entry_stream(entry_main.url, a_keys['workbuddy'], INFLIGHT_CASE,
                                timeout=args.inflight_timeout, rel_ms=rel_ms)
    accepted = wait_frames(holder, timeout=60)
    begin = experiment.drain_action(a, 'begin')
    readyz_503 = experiment.wait_readyz(a, 503, timeout=args.drain_timeout)
    rejected_probe = run_case(entry_main.url, a_keys['workbuddy'],
                              CASE_BY_NAME['chat-nonstream'], timeout=30, rel_ms=rel_ms)
    rejected = {'status': rejected_probe.get('status'),
                'body_head': (rejected_probe.get('body_head') or '')[:200],
                'rejected': is_drain_rejection(rejected_probe.get('status'),
                                               rejected_probe.get('body_head') or '')}
    holder['thread'].join(timeout=args.inflight_timeout)
    in_flight = holder.get('record') or {}
    switch['a_drain'] = {
        'begin_status': begin.get('status'), 'readyz_503': readyz_503,
        'in_flight_accepted': accepted,
        'in_flight_terminal': in_flight.get('terminal'),
        'in_flight': {key: in_flight.get(key) for key in
                      ('status', 'frames', 'terminal', 'execution_id')},
        'rejected_after_drain': rejected,
    }

    # 1b) drain 没生效就绝不停止 A（否则会出现「未排空就被替换」的假切换）。
    if begin.get('status') != 200 or not readyz_503:
        switch['outcome'] = 'refused_precondition'
        switch['rollback'] = restore_a_service(
            experiment, ctx['view_a'], entry_main, old_port=a.port,
            original_conf=None, timeout=args.ready_timeout, rel_ms=rel_ms,
            db_path=main_db, live_instance=a,
            writer_processes=[ctx['b_shadow']])
        switch['service_restored'] = bool(switch['rollback'].get('pass'))
        switch['verdict'] = switch_verdict(switch)
        return switch

    # 2) A 退出 + 真实 release 观测（旧快照无 instance_leases → n/a）
    tables_a_schema = table_names(main_db)
    a_claimed = bool((a.info or {}).get('claimed')) and bool(
        (a.info or {}).get('instance_id'))
    applicability = release_applicability('instance_leases' in tables_a_schema,
                                          a_claimed)
    a_exit_code = a.stop(timeout=60)
    release = observe_release(experiment, a, applicable=applicability['applicable'])
    release['applicability'] = applicability
    release['exit_returncode'] = a_exit_code
    release['port_closed'] = port_closed(a.port)
    switch['a_release'] = release
    switch['a_schema_tables_before_migration'] = tables_a_schema
    ctx['inflight_id'] = in_flight.get('execution_id')

    # 2b) 切换前主库快照：回退必须回到旧代码能读的 schema（候选7 回退失败根因）。
    snapshot_path = experiment.root / 'main-db-pre-switch.db'
    snapshot = backup_main_db(main_db, snapshot_path)
    switch['db_snapshot'] = snapshot
    ctx['db_snapshot'] = snapshot.get('path') if snapshot.get('ok') else None
    ctx['db_snapshot_sha256'] = snapshot.get('sha256') or ''
    ctx['db_path'] = main_db
    experiment.timeline.emit('db_snapshot', ok=bool(snapshot.get('ok')),
                             path=snapshot.get('path'), sha256=snapshot.get('sha256'),
                             error=snapshot.get('error'))
    ctx['entry_main'] = entry_main
    if not snapshot.get('ok'):
        # 没有可恢复的快照就不启动 B：否则一旦失败无法把 A 拉回旧 schema。
        switch['outcome'] = 'refused_precondition'
        switch['service_restored'] = bool(
            restore_a_service(experiment, ctx['view_a'], entry_main,
                              old_port=a.port, original_conf=None,
                              timeout=args.ready_timeout, rel_ms=rel_ms,
                              cleanup=ctx.get('cleanup'), live_instance=a,
                              writer_processes=[ctx['b_shadow'], b_auth, *(ctx.get('cleanup') or [])],
                              db_path=main_db).get('pass'))
        switch['verdict'] = switch_verdict(switch)
        return switch
    if applicability['applicable'] and not release.get('observed'):
        # 串行协议要求先真实释放；不满足就不启动 B（避免第二个写者）。
        switch['outcome'] = 'refused_precondition'
        switch['service_restored'] = bool(
            restore_a_service(experiment, ctx['view_a'], entry_main,
                              old_port=a.port, original_conf=None,
                              timeout=args.ready_timeout, rel_ms=rel_ms,
                              cleanup=ctx.get('cleanup'), live_instance=a,
                              writer_processes=[ctx['b_shadow'], b_auth, *(ctx.get('cleanup') or [])],
                              db_snapshot=ctx.get('db_snapshot'),
                              db_snapshot_sha256=ctx.get('db_snapshot_sha256') or '',
                              db_path=main_db).get('pass'))
        switch['verdict'] = switch_verdict(switch)
        return switch

    # 3) B 权威启动（B 代码 + A 的主 env/DB）
    b_auth = None
    b_auth_info = None
    try:
        started = start_instance(ctx['view_b_auth'], 'B-auth', cleanup=ctx.get('cleanup'),
                                 ready_timeout=args.ready_timeout)
        b_auth = started['instance']
        b_auth_info = started['info']
        b_auth_row = None
        b_auth_lease: dict = {}
        if b_auth_info.get('claimed'):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                b_auth_row = experiment.instance_row(b_auth_info['instance_id'])
                if (b_auth_row or {}).get('role') == 'serving':
                    break
                time.sleep(0.1)
            b_auth_lease = {key: (b_auth_row or {}).get(key) for key in
                            ('instance_id', 'owner', 'role', 'fence', 'lease_until')}
        tables_after = table_names(main_db)
        created = sorted(set(tables_after) - set(tables_a_schema))
        switch['b_authoritative'] = {
            'started': bool(started['ready'].get('ready')),
            'readyz_200': bool(started['ready'].get('ready')),
            'ready': started['ready'], 'login_status': started.get('login_status'),
            'instance_id': (b_auth_info or {}).get('instance_id'),
            'info': public_instance_info(b_auth_info or {}),
            'lease_row': b_auth_lease,
            'checked_at': int(time.time()),
            'migration': {'tables_before': tables_a_schema,
                          'tables_after': tables_after,
                          'created_by_migration': created,
                          'required_present': [name for name in REQUIRED_LEASE_TABLES
                                               if name in tables_after]},
        }
        b_auth_keys = (b_auth_info or {}).get('keys') or {}
        if not b_auth_keys:
            # B 已启动并可能已迁移主库：先停它，再按快照回退 A。
            stop_instance(b_auth, reason='b_auth_keys_missing')
            switch['outcome'] = 'refused_precondition'
            switch['service_restored'] = bool(
                restore_a_service(experiment, ctx['view_a'], entry_main,
                                  old_port=a.port, original_conf=None,
                                  timeout=args.ready_timeout, rel_ms=rel_ms,
                                  cleanup=ctx.get('cleanup'), live_instance=a,
                              writer_processes=[ctx['b_shadow'], b_auth, *(ctx.get('cleanup') or [])],
                                  db_snapshot=ctx.get('db_snapshot'),
                                  db_snapshot_sha256=ctx.get('db_snapshot_sha256') or '',
                                  db_path=main_db).get('pass'))
            switch['verdict'] = switch_verdict(switch)
            return switch
    except BaseException as exc:  # noqa: BLE001
        if b_auth is not None:
            stop_instance(b_auth, reason='b_auth_start_exception')
        switch['b_authoritative'] = {'started': False,
                                     'error': type(exc).__name__ + ':' + str(exc)[:300]}
        switch['outcome'] = 'refused_precondition'
        switch['service_restored'] = bool(
            restore_a_service(experiment, ctx['view_a'], entry_main,
                              old_port=a.port, original_conf=None,
                              timeout=args.ready_timeout, rel_ms=rel_ms,
                              cleanup=ctx.get('cleanup'), live_instance=a,
                              writer_processes=[ctx['b_shadow'], b_auth, *(ctx.get('cleanup') or [])],
                              db_snapshot=ctx.get('db_snapshot'),
                              db_snapshot_sha256=ctx.get('db_snapshot_sha256') or '',
                              db_path=main_db).get('pass'))
        switch['verdict'] = switch_verdict(switch)
        return switch

    # 4) 入口真实切换（保存原字节 → 改写 upstream → nginx -t → reload → 前端 200）
    original_conf = entry_main.conf.read_bytes()
    entry_record = {'original_sha256': sha256_bytes(original_conf),
                    'nginx_t_ok': False, 'reload_ok': False, 'front_200': False,
                    'front_path': None, 'new_sha256': None, 't_ms': None}
    started_at = time.monotonic()
    try:
        switch_upstream.set_upstream_ports(entry_main.state, [b_auth.port])
        check = subprocess.run(switch_upstream.nginx_command(entry_main.state, '-t'),
                               capture_output=True, text=True)
        entry_record['nginx_t_ok'] = check.returncode == 0
        entry_record['nginx_t_detail'] = ((check.stderr or check.stdout) or '')[-500:]
        if check.returncode != 0:
            raise RuntimeError('nginx -t failed after upstream rewrite')
        switch_upstream.reload_nginx(entry_main.state)
        entry_record['reload_ok'] = True
        front = entry_main.wait_front(args.switch_probe_timeout)
        entry_record['front_200'] = bool(front.get('front_200'))
        entry_record['front_path'] = front.get('path')
        entry_record['new_sha256'] = sha256_bytes(entry_main.conf.read_bytes())
        entry_record['t_ms'] = round((time.monotonic() - started_at) * 1000, 3)
    except BaseException as exc:  # noqa: BLE001
        entry_record['error'] = type(exc).__name__ + ':' + str(exc)[:300]
        entry_record['t_ms'] = round((time.monotonic() - started_at) * 1000, 3)
        switch['entry'] = entry_record
        switch['probes'] = []
        stop_instance(b_auth, reason='entry_switch_failed')
        rollback = restore_a_service(experiment, ctx['view_a'], entry_main,
                                     old_port=a.port, original_conf=original_conf,
                                     timeout=args.ready_timeout, rel_ms=rel_ms,
                                     cleanup=ctx.get('cleanup'), live_instance=a,
                              writer_processes=[ctx['b_shadow'], b_auth, *(ctx.get('cleanup') or [])],
                                     db_snapshot=ctx.get('db_snapshot'),
                                     db_snapshot_sha256=ctx.get('db_snapshot_sha256') or '',
                                     db_path=main_db)
        switch['rollback'] = rollback
        switch['outcome'] = ('switch_failed_rollback_failed'
                             if not rollback.get('pass') else 'switch_failed')
        switch['ledger'] = switch_ledger_snapshot(
            main_db, [], ctx['a_ids'],
            shadow_unsettled_ids=ctx.get('a_unsettled_ids') or [],
            probe_unsettled_ids=[])
        switch['verdict'] = switch_verdict(switch)
        return switch
    switch['entry'] = entry_record

    # 5) 经入口的切换后探针
    probes = probe_switch_cases(entry_main, b_auth_keys,
                                timeout=args.switch_probe_timeout, rel_ms=rel_ms)
    switch['probes'] = probes
    switch['front_ready_after_switch'] = entry_main.front_probe('/api/readyz')

    # 6) 主库对账：A 影子期行不丢、B 权威期行各一行、无重复
    probe_ids = [probe.get('execution_id') for probe in probes]
    expected_ids = list(ctx['a_ids']) + ([ctx['inflight_id']]
                                         if ctx.get('inflight_id') else []) + probe_ids
    # 切换期 error/cancel 探针也可能留下未结算预留（与影子期同口径归因）。
    probe_unsettled_ids = [probe.get('execution_id') for probe in probes
                           if probe.get('kind') in ('error', 'cancel')]
    switch['ledger'] = switch_ledger_snapshot(
        main_db, expected_ids, ctx['a_ids'],
        shadow_unsettled_ids=ctx.get('a_unsettled_ids') or [],
        probe_unsettled_ids=probe_unsettled_ids)
    # 报告顶层直接列出归因结果（不只埋在 ledger 里），方便一眼看出是否误杀。
    switch['unexpected_unsettled_ids'] = (
        switch['ledger'].get('unexpected_unsettled_ids') or [])
    switch['unexpected_outcome_without_log_ids'] = (
        switch['ledger'].get('unexpected_outcome_without_log_ids') or [])
    switch['residual_attribution'] = {
        'unsettled': switch['ledger'].get('unsettled_attribution'),
        'outcome_without_log': switch['ledger'].get('outcome_without_log_attribution'),
        'allowed_set': {
            'shadow_unsettled_ids': ctx.get('a_unsettled_ids') or [],
            'switch_probe_ids': probe_unsettled_ids,
            'definition': ('允许集合 = 影子账本 A 侧 unsettled_request_ids + '
                           '切换期 error/cancel 探针 execution_id；只有不在集合里的'
                           '未结算/无日志 outcome 才判失败'),
        },
    }
    switch['ledger']['expected_ids'] = len(expected_ids)
    switch['verdict'] = switch_verdict(switch)
    if switch['verdict']['pass']:
        switch['outcome'] = 'switched'
        switch['service_restored'] = True
    else:
        stop_instance(b_auth, reason='post_switch_verdict_failed')
        rollback = restore_a_service(experiment, ctx['view_a'], entry_main,
                                     old_port=a.port, original_conf=original_conf,
                                     timeout=args.ready_timeout, rel_ms=rel_ms,
                                     cleanup=ctx.get('cleanup'), live_instance=a,
                              writer_processes=[ctx['b_shadow'], b_auth, *(ctx.get('cleanup') or [])],
                                     db_snapshot=ctx.get('db_snapshot'),
                                     db_snapshot_sha256=ctx.get('db_snapshot_sha256') or '',
                                     db_path=main_db)
        switch['rollback'] = rollback
        switch['outcome'] = ('switch_failed_rollback_failed'
                             if not rollback.get('pass') else 'switch_failed')
        switch['verdict'] = switch_verdict(switch)
    return switch


# ─────────────────────────── 主流程 ───────────────────────────
def build_report(args, experiment, facts: dict, entries: dict, window: dict,
                 shadow_ledger: dict, switch: dict | None, overall: dict,
                 sources: dict, started_at: str) -> dict:
    return {
        'schema': 'zhihui-r4-parallel-shadow/v1',
        'production_connected': False,
        'started_at': started_at,
        'finished_at': now_iso(),
        'sources': sources,
        'boundary': facts,
        'entries': entries,
        'parallel_window': window,
        'shadow_ledger': shadow_ledger,
        'switch_phase': switch,
        'verdict': overall,
        'evidence': {
            'shadow_timeline': str(experiment.out / 'shadow-timeline.ndjson'),
            'switch_timeline': str(experiment.out / 'switch-timeline.ndjson'),
            'shadow_ledger': str(experiment.out / 'shadow-ledger.json'),
            'switch_ledger': str(experiment.out / 'switch-ledger.json'),
        },
        'limits': [
            '只跑 disposable local VM；不联网；生产禁用。',
            '影子期不双写：A 独占主库，B 用独立库/入口，B 账本不并入 A。',
            '不写“零宕机”，只给观测区间；失败前的 200 不当失败后的恢复。',
            '切换阶段的旧快照 A 无实例租约表时 release 记 n/a，不据此判失败。',
            '回退默认复用当前 DB 并验证旧读者；仅明确指定 snapshot_if_unchanged 且完整对账一致时才允许 lab 快照恢复。',
            '切换后主库的未结算预留按“影子期可归因残值+切换期 error/cancel 探针”归因，'
            '只有不可归因的才判失败；rollback 步骤逐条记录在 switch_phase.rollback.steps。',
        ],
    }


class PhaseTimeline:
    """影子/切换两份 ndjson：同一单调时钟，事件按阶段分文件（不混算）。"""

    def __init__(self, shadow_path: Path, switch_path: Path) -> None:
        self.origin = time.monotonic()
        self._shadow = Timeline(shadow_path, self.origin)
        self._switch = Timeline(switch_path, self.origin)
        self.phase = 'shadow'

    def rel_ms(self, moment: float | None = None) -> float:
        return round(((moment or time.monotonic()) - self.origin) * 1000, 3)

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self._switch.emit('phase_begin', phase=phase)

    def emit(self, event: str, **fields) -> None:
        fields.setdefault('phase', self.phase)
        target = self._shadow if self.phase == 'shadow' else self._switch
        target.emit(event, **fields)

    def close(self) -> None:
        self._shadow.close()
        self._switch.close()


def parse_args(argv=None) -> argparse.Namespace:
    here = Path(__file__).resolve()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--qa-file', required=True)
    parser.add_argument('--repo', default=str(here.parents[2]))
    parser.add_argument('--a-repo', required=True,
                        help='旧候选快照（必须显式给出且 ≠ --repo）')
    parser.add_argument('--out', required=True)
    parser.add_argument('--pairs', type=int, default=60)
    parser.add_argument('--min-pairs', type=int, default=40)
    parser.add_argument('--interval', type=float, default=1.0)
    parser.add_argument('--frame-tolerance', type=int, default=0)
    parser.add_argument('--allow-divergence', type=int, default=0,
                        help='仅放宽非切换证据；allow_switch 判定恒用 0 差异')
    parser.add_argument('--chunk-delay', type=float, default=0.15)
    parser.add_argument('--chunk-count', type=int, default=20)
    parser.add_argument('--instance-ttl', type=int, default=30)
    parser.add_argument('--case-timeout', type=float, default=120.0)
    parser.add_argument('--ready-timeout', type=float, default=180.0)
    parser.add_argument('--drain-timeout', type=float, default=45.0)
    parser.add_argument('--inflight-timeout', type=float, default=120.0)
    parser.add_argument('--ledger-wait', type=float, default=30.0)
    parser.add_argument('--switch-probe-timeout', type=float, default=60.0)
    args = parser.parse_args(argv)
    for name, value in (('--pairs', args.pairs), ('--min-pairs', args.min_pairs),
                        ('--frame-tolerance', args.frame_tolerance),
                        ('--allow-divergence', args.allow_divergence)):
        if int(value) < 0:
            parser.error(f'{name} 必须 >= 0')
    if args.min_pairs > args.pairs:
        parser.error('--min-pairs 不能大于 --pairs')
    args._db_a, args._db_b, args._provider_base = '', '', ''
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    a_repo = Path(args.a_repo).resolve()
    out = Path(args.out).resolve()
    qa_file = Path(args.qa_file).resolve()
    qa = {}
    if qa_file.is_file():
        try:
            qa = json.loads(qa_file.read_text(encoding='utf-8'))
        except ValueError:
            qa = {}
    args.source_sha256 = qa.get('source_sha256')
    args.build_id = qa.get('build_id')
    facts = static_guard_facts(args, repo, a_repo)
    if not qa:
        print(json.dumps({'error': 'boundary_or_precondition_violation',
                          'violations': ['qa_file_invalid'], 'injected': False},
                         ensure_ascii=False), flush=True)
        return EXIT_BOUNDARY
    bad = guard_violations(facts)
    if bad:
        print(json.dumps({'error': 'boundary_or_precondition_violation',
                          'violations': bad, 'injected': False},
                         ensure_ascii=False), flush=True)
        return EXIT_BOUNDARY

    started_at = now_iso()
    options = SimpleNamespace(repo=str(repo), out=str(out), chunk_delay=args.chunk_delay,
                              chunk_count=args.chunk_count, instance_ttl=args.instance_ttl)
    experiment = HandoffExperiment(options)
    default_timeline = experiment.timeline
    default_timeline.close()
    (out / 'handoff-timeline.ndjson').unlink(missing_ok=True)
    experiment.timeline = PhaseTimeline(out / 'shadow-timeline.ndjson',
                                        out / 'switch-timeline.ndjson')
    report = {'schema': 'zhihui-r4-parallel-shadow/v1', 'status': 'running',
              'production_connected': False, 'started_at': started_at}
    entries: dict = {}
    window: dict = {}
    shadow_ledger: dict = {}
    switch: dict | None = None
    overall: dict = {}
    exit_code = EXIT_FAIL
    a = b_shadow = entry_main = entry_shadow = None
    sources: dict = {'a': {'repo': str(a_repo)}, 'b': {'repo': str(repo)},
                     'identical': False}
    cleanup_instances: list = []
    try:
        ctx_env = prepare_environment(experiment, repo, args)
        args._db_a, args._db_b = ctx_env['main_db'], ctx_env['shadow_db']
        args._provider_base = experiment.provider.base_url
        facts.update({'db_a': ctx_env['main_db'], 'db_b': ctx_env['shadow_db'],
                      'loopback_ok': _is_loopback_url(experiment.provider.base_url)})
        violations = guard_violations(facts)
        sources.update({
            'a': {'repo': str(a_repo),
                  'main_sha256': facts.get('a_main_sha256'),
                  'migrations_sha256': facts.get('a_migrations_sha256')},
            'b': {'repo': str(repo), 'source_sha256': args.source_sha256,
                  'build_id': args.build_id,
                  'main_sha256': facts.get('repo_main_sha256'),
                  'migrations_sha256': facts.get('repo_migrations_sha256')},
            'identical': False,
        })
        if violations:
            exit_code = EXIT_BOUNDARY
            overall = {'decision': 'refuse_switch', 'exit_code': EXIT_BOUNDARY,
                       'reasons': ['boundary:' + name for name in violations],
                       'outcome': 'boundary_violation', 'policy': 'boundary'}
            print(json.dumps({'error': 'boundary_or_precondition_violation',
                              'violations': violations, 'injected': False},
                             ensure_ascii=False), flush=True)
            return EXIT_BOUNDARY

        view_a = InstanceView(experiment, source=a_repo, env=experiment.env,
                              root=experiment.root / 'instance-a', label='A-old')
        view_b_shadow = InstanceView(experiment, source=repo, env=ctx_env['shadow_env'],
                                     root=experiment.root / 'instance-b-shadow',
                                     label='B-new-shadow')
        view_b_auth = InstanceView(experiment, source=repo, env=experiment.env,
                                   root=experiment.root / 'instance-b-auth',
                                   label='B-new-authoritative')

        # ── A（旧候选）权威启动 ────────────────────────────────────────
        # 旧快照没有实例运行时：用兼容 launcher（不 claim、不心跳）。
        # 若 A 自带 instancelease + instance_runtime（新到会自己 claim），则用标准
        # launcher，release 就按严格口径判定（Lead 修正 #2）。
        a_self_claims = ((a_repo / 'manager/server/instancelease.py').is_file()
                         and (a_repo / 'manager/server/instance_runtime.py').is_file())
        started_a = start_instance(
            view_a, 'A', ready_timeout=args.ready_timeout,
            launcher_text=None if a_self_claims else OLD_LAUNCHER)
        a = started_a['instance']
        a_info = started_a['info'] or {}
        a_keys = a_info.get('keys') or {}
        if not started_a['ready'].get('ready') or not a_keys:
            raise RuntimeError('A 旧候选未就绪：' + json.dumps(started_a['ready']))
        ctx = {'args': args, 'experiment': experiment, 'view_a': view_a,
               'view_b_auth': view_b_auth, 'a': a, 'a_keys': a_keys,
               'main_db': ctx_env['main_db'], 'a_ids': [], 'inflight_id': None,
               'cleanup': cleanup_instances}
        experiment.timeline.emit('a_serving', instance_id=a_info.get('instance_id'),
                                 keys=fingerprint_keys(a_keys),
                                 info=public_instance_info(a_info))

        # ── B（新候选）影子启动（独立 root/DB/端口） ──────────────────
        started_b = start_instance(view_b_shadow, 'B-shadow',
                                   ready_timeout=args.ready_timeout)
        b_shadow = started_b['instance']
        b_info = started_b['info'] or {}
        b_keys = b_info.get('keys') or {}
        if not started_b['ready'].get('ready') or not b_keys:
            raise RuntimeError('B 影子未就绪：' + json.dumps(started_b['ready']))
        experiment.timeline.emit('b_shadow_serving', keys=fingerprint_keys(b_keys),
                                 info=public_instance_info(b_info),
                                 db=ctx_env['shadow_db'])
        ctx['b_shadow'] = b_shadow

        # ── 两个真实 TLS 入口（各自独立 nginx prefix） ────────────────
        entry_main = Entry(experiment, 'main', a.port)
        start_main = entry_main.start(timeout=args.ready_timeout)
        entry_shadow = Entry(experiment, 'shadow', b_shadow.port)
        start_shadow = entry_shadow.start(timeout=args.ready_timeout)
        ctx['entry_main'] = entry_main
        ctx['entry_shadow'] = entry_shadow
        entries = {
            'a': {**entry_main.facts(experiment),
                  'front_ready': bool((start_main.get('front') or {}).get('front_200'))},
            'b': {**entry_shadow.facts(experiment),
                  'front_ready': bool((start_shadow.get('front') or {}).get('front_200'))},
        }
        entry_facts = entry_facts_verdict(entries)
        if not entry_facts['ok']:
            raise RuntimeError('入口不是真实独立 TLS 或前端未 ready：'
                               + json.dumps(entry_facts))
        experiment.timeline.emit('entries_ready', a=entries['a'], b=entries['b'])

        # ── 影子窗口 ─────────────────────────────────────────────────
        window_started = experiment.timeline.rel_ms()
        pairs: list[dict] = []
        a_ids: list[str] = []
        b_ids: list[str] = []
        a_cancel_ids: list[str] = []
        b_cancel_ids: list[str] = []
        a_error_ids: list[str] = []
        b_error_ids: list[str] = []
        for index in range(args.pairs):
            case = CASES[index % len(CASES)]
            a_record, b_record = run_pair(entry_main, a_keys[case['platform']],
                                          entry_shadow, b_keys[case['platform']],
                                          case, timeout=args.case_timeout,
                                          rel_ms=experiment.timeline.rel_ms)
            pair = compare_pair(case, a_record, b_record, args.frame_tolerance)
            pairs.append(pair)
            if a_record.get('execution_id'):
                a_ids.append(a_record['execution_id'])
            if b_record.get('execution_id'):
                b_ids.append(b_record['execution_id'])
            if case['kind'] == 'cancel':
                if a_record.get('execution_id'):
                    a_cancel_ids.append(a_record['execution_id'])
                if b_record.get('execution_id'):
                    b_cancel_ids.append(b_record['execution_id'])
            if case['kind'] == 'error':
                if a_record.get('execution_id'):
                    a_error_ids.append(a_record['execution_id'])
                if b_record.get('execution_id'):
                    b_error_ids.append(b_record['execution_id'])
            experiment.timeline.emit('pair_result', pair=pair_summary(pair),
                                     a_frames=a_record.get('frames'),
                                     b_frames=b_record.get('frames'))
            if index + 1 < args.pairs:
                time.sleep(max(0.0, args.interval))
        window_ended = experiment.timeline.rel_ms()
        window = window_metrics(pairs)
        window.update({
            'started_ms': window_started, 'ended_ms': window_ended,
            'duration_s': round((window_ended - window_started) / 1000, 3),
            'thresholds': {'min_pairs': args.min_pairs,
                           'frame_tolerance': args.frame_tolerance,
                           'allow_divergence': args.allow_divergence,
                           'a_max_unexpected_failures': 0,
                           'b_max_unexpected_failures': 0,
                           'divergence_limit_for_switch': 0},
            'auth': {'a_key_fingerprints': fingerprint_keys(a_keys),
                     'b_key_fingerprints': fingerprint_keys(b_keys),
                     'plaintext_recorded': False},
            'request_body': '同一用例的请求体逐字节相同；仅 Authorization 各用各的 key',
        })
        gate = shadow_window_verdict(window, min_pairs=args.min_pairs,
                                     allow_divergence=args.allow_divergence)
        window.update({'strict_pass': gate['strict']['pass'],
                       'configured_pass': gate['configured']['pass'],
                       'checks': gate['strict']['checks'],
                       'reasons': gate['reasons'],
                       'policy_note': gate['policy_note'],
                       'pass': gate['pass']})

        # ── 影子账本（A 主库 / B 影子库分开对账） ────────────────────
        shadow_ledger = settle_shadow_ledger(
            ctx_env['main_db'], ctx_env['shadow_db'], a_ids, b_ids,
            a_cancel_ids, b_cancel_ids, wait=args.ledger_wait,
            a_expected_unsettled_ids=a_cancel_ids + a_error_ids,
            b_expected_unsettled_ids=b_cancel_ids + b_error_ids)
        ctx.update({'a_ids': a_ids, 'b_ids': b_ids, 'inflight_id': None,
                    # 切换账本归因用：影子期 A 侧全部可归因未结算 ID（cancel+error）。
                    'a_unsettled_ids': list(
                        (shadow_ledger.get('a') or {}).get('unsettled_request_ids') or [])})

        # ── 切换判定 ─────────────────────────────────────────────────
        shadow_verdict = {'pass': bool(window['pass'] and shadow_ledger['pass']),
                          'reasons': ([] if window['pass'] else
                                      ['window:' + name for name in window['reasons']])
                                     + ([] if shadow_ledger['pass'] else
                                        ['ledger:' + name for name in
                                         shadow_ledger['verdict']['reasons']])}
        if not shadow_verdict['pass']:
            switch = {'outcome': 'not_attempted', 'service_restored': True,
                      'verdict': {'pass': False, 'checks': {},
                                  'reasons': ['shadow_gate_not_passed']}}
        else:
            switch = run_switch_phase(experiment, ctx)
        overall = overall_verdict({'shadow': shadow_verdict, 'switch': switch})
        exit_code = overall['exit_code']
        experiment.timeline.emit('experiment_done', verdict=overall)
    except BaseException as exc:  # noqa: BLE001
        report.update({'status': 'failed',
                       'error_type': type(exc).__name__,
                       'error': str(exc)[:800]})
        overall = {'decision': 'refuse_switch', 'exit_code': EXIT_FAIL,
                   'reasons': ['experiment_error:' + type(exc).__name__],
                   'outcome': 'error', 'policy': 'error'}
        exit_code = EXIT_FAIL
    finally:
        seen = set()
        for instance in (*cleanup_instances, b_shadow, a):
            try:
                if instance is None or id(instance) in seen:
                    continue
                seen.add(id(instance))
                if instance.proc is not None and instance.proc.poll() is None:
                    instance.stop(timeout=30)
            except Exception:  # noqa: BLE001
                pass
        for entry in (entry_shadow, entry_main):
            try:
                if entry is not None:
                    entry.stop()
            except Exception:  # noqa: BLE001
                pass
        if experiment.provider is not None:
            try:
                experiment.provider.stop()
            except Exception:  # noqa: BLE001
                pass
        experiment.timeline.close()
        report.update(build_report(args, experiment, facts,
                                   entries, window, shadow_ledger,
                                   switch, overall, sources, started_at))
        report['status'] = ('passed' if exit_code == EXIT_PASS else
                            'boundary' if exit_code == EXIT_BOUNDARY else
                            'switch_refused' if exit_code == EXIT_SWITCH_REFUSED else
                            'failed')
        try:
            (out / 'parallel-shadow-report.json').write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8')
            (out / 'shadow-ledger.json').write_text(
                json.dumps(shadow_ledger, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8')
            (out / 'switch-ledger.json').write_text(
                json.dumps((switch or {}).get('ledger') or {},
                           ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8')
        except OSError:
            pass
    print(json.dumps({'status': report.get('status'),
                      'exit_code': exit_code,
                      'decision': (overall or {}).get('decision'),
                      'reasons': (overall or {}).get('reasons'),
                      'report': str(out / 'parallel-shadow-report.json')},
                     ensure_ascii=False, indent=2), flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
