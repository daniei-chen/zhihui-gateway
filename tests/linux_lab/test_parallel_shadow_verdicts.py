"""r4_parallel_shadow 判定逻辑反例（离线，Windows 可跑）。

重点覆盖 Lead 2026-10-26 的五条修正：

1. 取消/错误用例**不比 SSE 帧数**（取消只要求两侧记录到客户端取消，错误只比
   状态码类别与错误体形状）；正常完成才比帧数+终止信号。
2. `--a-repo` 必须显式给出且 ≠ repo（同路径 / 同内容都拒绝）；旧快照 A 的
   release 观测记 n/a 时**不得**判失败。
3. 入口必须是真实 TLS + 独立 prefix（报告记录证书/配置指纹）。
4. 门槛：min_pairs≥40、覆盖≥3 协议+错误+取消、A 失败=0、差异=0；`allow-divergence`
   只放宽非切换证据，不影响 allow_switch 严格门。
5. A/B 独立库，request_id 互不越界（交叉污染即失败）。

另覆盖退出码 0/1/3 的判定路径。
"""
import importlib.util
import json
import re
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_SPEC = importlib.util.spec_from_file_location('r4_parallel_shadow',
                                              HERE / 'r4_parallel_shadow.py')
r4 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(r4)

LAB = '/home/gateway-lab/lab'


def base_facts():
    return {
        'hostname': 'zhihui-local-lab', 'home': '/home/gateway-lab', 'lab': LAB,
        'repo': LAB + '/snapshots/30f140ee/workspace', 'repo_exists': True,
        'repo_has_manager': True, 'repo_main_sha256': 'b' * 64,
        'repo_migrations_sha256': 'b2' * 32,
        'a_repo': LAB + '/snapshots/ee89f51e/workspace', 'a_repo_exists': True,
        'a_repo_has_manager': True, 'a_main_sha256': 'a' * 64,
        'a_migrations_sha256': 'a2' * 32,
        'out': LAB + '/evidence/run/r4-parallel-shadow', 'out_exists': False,
        'qa_file': LAB + '/profiles/batch.json', 'qa_exists': True,
        'qa_production_connected': False,
        'qa_workspace': LAB + '/snapshots/30f140ee/workspace',
        'db_a': '/tmp/iso-main/db/manager.db',
        'db_b': '/tmp/iso-shadow/db/manager.db', 'loopback_ok': True,
    }


def record(*, status=200, frames=20, terminal='completed', cancelled=False,
           terminals=(), body_head='', execution_id=None, error=None, stream=True):
    return {'status': status, 'frames': frames, 'stream': stream,
            'saw_done': '[DONE]' in terminals,
            'terminal': terminal, 'cancelled': cancelled, 'terminals': list(terminals),
            'body_head': body_head, 'execution_id': execution_id, 'error': error,
            'first_frame_ms': 1.0, 't_start_ms': 0.0, 't_end_ms': 2.0}


def ok_stream(**over):
    base = record(frames=20, terminal='completed', terminals=('[DONE]',))
    base.update(over)
    return base


def error_body(code='upstream_error', status_text='upstream 500'):
    return json.dumps({'error': {'message': status_text, 'type': 'server_error',
                                 'code': code}})


def pair_for(case_name, a=None, b=None, tolerance=0):
    case = r4.CASE_BY_NAME[case_name]
    return r4.compare_pair(case, a or ok_stream(), b or ok_stream(), tolerance)


def equal_pairs(count=60):
    """构造与真实窗口同构的等值 pair 列表（各用例都有覆盖）。"""
    pairs = []
    for index in range(count):
        case = r4.CASES[index % len(r4.CASES)]
        if case['kind'] == 'cancel':
            a = record(frames=1, terminal='client_cancelled', cancelled=True)
            b = record(frames=7, terminal='client_cancelled', cancelled=True)
        elif case['kind'] == 'error':
            status = case['expected_class'] * 100
            body = error_body()
            a = record(status=status, frames=0, terminal='http_error', body_head=body)
            b = record(status=status, frames=0, terminal='http_error', body_head=body)
        elif case['stream']:
            a = b = ok_stream()
        else:
            body = json.dumps({'object': 'chat.completion', 'model': 'fake-model',
                               'choices': [{'finish_reason': 'stop'}],
                               'usage': {'prompt_tokens': 7}})
            a = record(frames=0, terminal='completed', body_head=body, stream=False)
            b = record(frames=0, terminal='completed', body_head=body, stream=False)
        pairs.append(r4.compare_pair(case, a, b, 0))
    return pairs


def clean_window(**over):
    window = r4.window_metrics(equal_pairs())
    window.update(over)
    return window


def clean_shadow_ledger():
    return {
        'db_isolated': True,
        'a_execution_ids_available': True, 'b_execution_ids_available': True,
        'a': {'duplicate_request_ids': 0, 'missing': [], 'duplicate': [],
              'request_logs_tokens': 100, 'usage_daily_tokens': 100,
              'request_logs_available': True,
              'tokens_applicable': True, 'cancel_terminals_missing': [],
              'unexpected_outcome_without_log': 0, 'open_or_unknown_reservations': 0,
              'unsettled_request_ids': [], 'unexpected_unsettled_ids': []},
        'b': {'duplicate_request_ids': 0, 'missing': [], 'duplicate': [],
              'request_logs_tokens': 100, 'usage_daily_tokens': 100,
              'request_logs_available': True,
              'tokens_applicable': True, 'cancel_terminals_missing': [],
              'unexpected_outcome_without_log': 0, 'open_or_unknown_reservations': 0,
              'unsettled_request_ids': [], 'unexpected_unsettled_ids': []},
        'cross_contamination': {'a_ids_in_b': 0, 'b_ids_in_a': 0},
    }


def clean_switch():
    now = int(time.time())
    return {
        'a_drain': {'begin_status': 200, 'readyz_503': True,
                    'in_flight_accepted': True, 'in_flight_terminal': 'completed',
                    'rejected_after_drain': {'status': 503, 'rejected': True}},
        # 旧快照 A 没有 instance_leases → release 记 n/a，不算失败。
        'a_release': {'applicable': False, 'row': None, 'fence_before': None,
                      'reasons': ['legacy_schema_without_instance_leases']},
        'b_authoritative': {'started': True, 'readyz_200': True,
                            'lease_row': {'role': 'serving', 'owner': 'lab:inst',
                                          'lease_until': now + 30, 'fence': 2},
                            'checked_at': now,
                            'migration': {'tables_before': ['request_logs'],
                                          'tables_after': ['request_logs',
                                                           'instance_leases',
                                                           'task_leases'],
                                          'created_by_migration': ['instance_leases',
                                                                   'task_leases']}},
        'entry': {'original_sha256': 'old', 'new_sha256': 'new', 'nginx_t_ok': True,
                  'reload_ok': True, 'front_200': True, 'front_path': '/api/readyz'},
        'probes': [
            {'case': 'chat-stream', 'expected': 200, 'expected_class': '2xx',
             'status': 200, 'terminal': 'completed'},
            {'case': 'error-500', 'expected': None, 'expected_class': '5xx',
             'status': 500, 'terminal': 'http_error'},
        ],
        'ledger': {'duplicate_request_ids': 0, 'missing': [], 'duplicate': [],
                   'request_logs_tokens': 120, 'usage_daily_tokens': 120,
                   'request_logs_available': True,
                   'tokens_applicable': True, 'unexpected_outcome_without_log': 0,
                   'open_or_unknown_reservations': 0, 'a_shadow_rows_missing': [],
                   'unexpected_outcome_without_log_ids': [],
                   'unexpected_unsettled_ids': [],
                   'unsettled_attribution': {'allowed_from_shadow': [],
                                             'allowed_from_switch_probes': [],
                                             'symmetric_residual': [],
                                             'unexpected': [], 'pass': True},
                   'expected_ids': 6},
        'outcome': 'switched', 'service_restored': True,
    }


# ─────────────────────────── 边界 / a_repo 自欺 ───────────────────────────
class GuardTest(unittest.TestCase):
    def test_clean_facts_pass(self):
        self.assertEqual(r4.guard_violations(base_facts()), [])

    def test_a_repo_required(self):
        facts = base_facts()
        facts['a_repo'] = ''
        self.assertIn('a_repo_required', r4.guard_violations(facts))

    def test_a_repo_same_as_repo_rejected(self):
        facts = base_facts()
        facts['a_repo'] = facts['repo']
        self.assertIn('a_repo_same_as_repo', r4.guard_violations(facts))

    def test_a_repo_same_content_rejected(self):
        facts = base_facts()
        facts['a_main_sha256'] = facts['repo_main_sha256']
        facts['a_migrations_sha256'] = facts['repo_migrations_sha256']
        self.assertIn('a_repo_same_content', r4.guard_violations(facts))

    def test_a_repo_outside_snapshots_rejected(self):
        facts = base_facts()
        facts['a_repo'] = '/home/gateway-lab/lab/elsewhere/workspace'
        self.assertIn('a_repo_outside_snapshots', r4.guard_violations(facts))

    def test_out_exists_rejected(self):
        facts = base_facts()
        facts['out_exists'] = True
        self.assertIn('out_exists_refuse_overwrite', r4.guard_violations(facts))

    def test_production_connected_rejected(self):
        facts = base_facts()
        facts['qa_production_connected'] = True
        self.assertIn('production_connected_not_false', r4.guard_violations(facts))

    def test_qa_workspace_mismatch_rejected(self):
        facts = base_facts()
        facts['qa_workspace'] = LAB + '/snapshots/deadbeef/workspace'
        self.assertIn('qa_workspace_mismatch', r4.guard_violations(facts))

    def test_shared_db_rejected(self):
        facts = base_facts()
        facts['db_b'] = facts['db_a']
        self.assertIn('shadow_db_not_isolated', r4.guard_violations(facts))

    def test_non_loopback_rejected(self):
        facts = base_facts()
        facts['loopback_ok'] = False
        self.assertIn('non_loopback_endpoint', r4.guard_violations(facts))

    def test_wrong_host_and_home_rejected(self):
        facts = base_facts()
        facts['hostname'] = 'prod-gateway'
        facts['home'] = '/root'
        bad = r4.guard_violations(facts)
        self.assertIn('hostname_not_lab', bad)
        self.assertIn('home_not_gateway_lab', bad)


# ─────────────────────────── 请求体 / 终止分类 ───────────────────────────
class CaseBodyTest(unittest.TestCase):
    def test_body_is_deterministic_so_both_sides_get_same_bytes(self):
        for case in r4.CASES:
            first = r4.case_body_bytes(case)
            second = r4.case_body_bytes(case)
            self.assertEqual(first, second, case['name'])
            payload = json.loads(first)
            self.assertEqual(payload['model'], case['model'])
            self.assertEqual(bool(payload.get('stream')), case['stream'])

    def test_terminal_classifier(self):
        self.assertEqual(r4.classify_terminal(record(terminals=('[DONE]',))), 'completed')
        self.assertEqual(r4.classify_terminal(record(terminal=None, terminals=())),
                         'stream_cut')
        # 非流式 200 没有 SSE 终止帧，不能误判成 stream_cut。
        self.assertEqual(r4.classify_terminal(
            record(terminal=None, terminals=(), stream=False)), 'completed')
        self.assertEqual(r4.classify_terminal(record(status=500, terminal=None)), 'http_error')
        self.assertEqual(r4.classify_terminal(record(status=None, terminal=None,
                                                     error='ConnectError')), 'connect_error')
        self.assertEqual(r4.classify_terminal(record(cancelled=True, terminal=None)),
                         'client_cancelled')

    def test_drain_rejection_requires_gateway_draining(self):
        body = json.dumps({'error': {'code': 'gateway_draining', 'message': 'draining'}})
        self.assertTrue(r4.is_drain_rejection(503, body))
        self.assertFalse(r4.is_drain_rejection(200, body))
        self.assertFalse(r4.is_drain_rejection(503, '{"error": {"code": "x"}}'))


# ─────────────────────────── 比对规则（修正 #1） ───────────────────────────
class PairCompareTest(unittest.TestCase):
    def test_normal_stream_equal_passes(self):
        pair = pair_for('chat-stream')
        self.assertTrue(pair['equal'], pair['reasons'])
        self.assertTrue(pair['compared']['frames'])

    def test_normal_stream_frame_mismatch_fails(self):
        pair = pair_for('chat-stream', b=ok_stream(frames=19))
        self.assertFalse(pair['equal'])
        self.assertIn('sse_frame_count_mismatch', pair['reasons'])

    def test_normal_stream_terminal_mismatch_fails(self):
        pair = pair_for('chat-stream', b=ok_stream(terminal='stream_cut', terminals=()))
        self.assertFalse(pair['equal'])
        self.assertIn('terminal_mismatch', pair['reasons'])

    def test_normal_nonstream_shape_mismatch_fails(self):
        body_a = json.dumps({'object': 'chat.completion', 'model': 'fake-model',
                             'choices': [{'finish_reason': 'stop'}], 'usage': {}})
        body_b = json.dumps({'object': 'chat.completion', 'model': 'fake-model',
                             'choices': [{'finish_reason': 'length'}], 'usage': {}})
        pair = pair_for('chat-nonstream',
                        record(frames=0, body_head=body_a, stream=False),
                        record(frames=0, body_head=body_b, stream=False))
        self.assertFalse(pair['equal'])
        self.assertIn('success_body_shape_mismatch', pair['reasons'])

    def test_normal_nonstream_success_passes(self):
        body = json.dumps({'object': 'chat.completion', 'model': 'fake-model',
                           'choices': [{'finish_reason': 'stop'}], 'usage': {}})
        pair = pair_for('chat-nonstream',
                        record(frames=0, terminal=None, body_head=body, stream=False),
                        record(frames=0, terminal=None, body_head=body, stream=False))
        self.assertTrue(pair['equal'], pair['reasons'])
        self.assertFalse(pair['a_unexpected'])

    def test_cancel_with_different_frame_counts_passes(self):
        """修正 #1：取消用例帧数天然不同，不能按帧数判失败。"""
        a = record(frames=1, terminal='client_cancelled', cancelled=True)
        b = record(frames=9, terminal='client_cancelled', cancelled=True)
        pair = pair_for('client-cancel', a, b)
        self.assertTrue(pair['equal'], pair['reasons'])
        self.assertFalse(pair['compared']['frames'])

    def test_cancel_without_client_record_fails(self):
        a = record(frames=3, terminal='completed', terminals=('[DONE]',))
        pair = pair_for('client-cancel', a, record(frames=3, cancelled=True,
                                                   terminal='client_cancelled'))
        self.assertFalse(pair['equal'])
        self.assertIn('a_cancel_not_observed', pair['reasons'])

    def test_cancel_with_5xx_fails(self):
        a = record(status=500, frames=0, terminal='http_error')
        b = record(frames=2, terminal='client_cancelled', cancelled=True)
        pair = pair_for('client-cancel', a, b)
        self.assertFalse(pair['equal'])
        self.assertIn('a_cancel_not_accepted', pair['reasons'])

    def test_error_compares_class_and_shape_not_frames(self):
        body = error_body()
        pair = pair_for('error-500',
                        record(status=500, frames=0, terminal='http_error', body_head=body),
                        record(status=500, frames=0, terminal='http_error', body_head=body))
        self.assertTrue(pair['equal'], pair['reasons'])
        self.assertFalse(pair['compared']['frames'])
        self.assertTrue(pair['compared']['error_shape'])

    def test_error_shape_mismatch_fails(self):
        pair = pair_for('error-500',
                        record(status=500, body_head=error_body('upstream_error')),
                        record(status=500, body_head=error_body('internal_error')))
        self.assertFalse(pair['equal'])
        self.assertIn('error_body_shape_mismatch', pair['reasons'])

    def test_error_class_mismatch_fails(self):
        pair = pair_for('error-400',
                        record(status=400, body_head=error_body()),
                        record(status=500, body_head=error_body()))
        self.assertFalse(pair['equal'])
        self.assertIn('error_status_class_mismatch', pair['reasons'])

    def test_coverage_counts_three_protocols(self):
        coverage = r4.coverage_facts(equal_pairs(60))
        self.assertGreaterEqual(coverage['normal_protocol_count'], 3)
        self.assertGreaterEqual(coverage['error_pairs'], 1)
        self.assertGreaterEqual(coverage['cancel_pairs'], 1)


# ─────────────────────────── 门槛（修正 #4） ───────────────────────────
class ShadowGateTest(unittest.TestCase):
    def test_clean_window_passes_strict_gate(self):
        gate = r4.shadow_window_verdict(clean_window(), min_pairs=40,
                                        allow_divergence=0)
        self.assertTrue(gate['pass'], gate['reasons'])
        self.assertTrue(gate['strict']['pass'])

    def test_too_few_pairs_fails(self):
        gate = r4.shadow_window_verdict(clean_window(request_pairs=12), min_pairs=40,
                                        allow_divergence=0)
        self.assertFalse(gate['pass'])
        self.assertIn('sample_count', gate['reasons'])

    def test_missing_protocol_coverage_fails(self):
        window = clean_window()
        window['coverage']['normal_protocol_count'] = 2
        self.assertIn('protocol_coverage',
                      r4.shadow_window_verdict(window, min_pairs=40,
                                               allow_divergence=0)['reasons'])

    def test_missing_error_coverage_fails(self):
        window = clean_window()
        window['coverage']['error_pairs'] = 0
        self.assertIn('error_coverage',
                      r4.shadow_window_verdict(window, min_pairs=40,
                                               allow_divergence=0)['reasons'])

    def test_a_unexpected_failure_fails(self):
        window = clean_window(a_unexpected_failures=1)
        gate = r4.shadow_window_verdict(window, min_pairs=40, allow_divergence=0)
        self.assertFalse(gate['pass'])
        self.assertIn('a_unexpected_failures_zero', gate['reasons'])

    def test_b_divergence_fails(self):
        window = clean_window(divergences=1)
        self.assertIn('divergences_within_allowance',
                      r4.shadow_window_verdict(window, min_pairs=40,
                                               allow_divergence=0)['reasons'])

    def test_allow_divergence_never_relaxes_switch_gate(self):
        window = clean_window(divergences=2)
        gate = r4.shadow_window_verdict(window, min_pairs=40, allow_divergence=2)
        # 证据侧配置门可以放宽，但 allow_switch 用的严格门必须仍然失败。
        self.assertFalse(gate['pass'])
        self.assertTrue(gate['configured']['pass'])
        self.assertIn('只用于非切换证据', gate['policy_note'])

    def test_window_metrics_has_report_fields(self):
        """报告 parallel_window 必须有请求数/成功失败数/一致性/SSE 比对。"""
        window = clean_window()
        self.assertEqual(window['a']['requests'], window['request_pairs'])
        self.assertEqual(window['a']['failed'], 0)
        self.assertEqual(window['b']['failed'], 0)
        self.assertGreater(window['a']['success'], 0)
        self.assertGreater(window['consistency']['sse_frames']['checked'], 0)
        self.assertGreater(window['consistency']['error_body']['checked'], 0)
        self.assertEqual(window['sse']['mismatches'], 0)
        self.assertEqual(window['consistency']['status']['equal'],
                         window['consistency']['status']['checked'])


# ─────────────────────────── 账本 / 交叉污染（修正 #5） ───────────────────────────
class LedgerGateTest(unittest.TestCase):
    def test_clean_shadow_ledger_passes(self):
        self.assertTrue(r4.ledger_gate(clean_shadow_ledger())['pass'])

    def test_cross_contamination_fails(self):
        ledger = clean_shadow_ledger()
        ledger['cross_contamination']['a_ids_in_b'] = 1
        result = r4.ledger_gate(ledger)
        self.assertFalse(result['pass'])
        self.assertIn('a_ids_not_in_b', result['reasons'])

    def test_shared_db_fails(self):
        ledger = clean_shadow_ledger()
        ledger['db_isolated'] = False
        self.assertIn('db_isolated', r4.ledger_gate(ledger)['reasons'])

    def test_cancel_terminal_missing_fails(self):
        ledger = clean_shadow_ledger()
        ledger['b']['cancel_terminals_missing'] = ['req-cancel-1']
        self.assertIn('b_cancel_terminals_recorded',
                      r4.ledger_gate(ledger)['reasons'])

    def test_a_missing_or_duplicate_logs_fail(self):
        ledger = clean_shadow_ledger()
        ledger['a']['missing'] = ['req-a']
        ledger['a']['duplicate'] = ['req-b']
        reasons = r4.ledger_gate(ledger)['reasons']
        self.assertIn('a_no_missing_logs', reasons)
        self.assertIn('a_no_duplicate_logs', reasons)

    def test_token_mismatch_fails(self):
        ledger = clean_shadow_ledger()
        ledger['b']['usage_daily_tokens'] = 99
        self.assertIn('b_tokens_consistent', r4.ledger_gate(ledger)['reasons'])

    def test_missing_execution_ids_fail(self):
        """A/B 都没拿到执行 ID 时不能“空对空”通过。"""
        ledger = clean_shadow_ledger()
        ledger['a_execution_ids_available'] = False
        ledger['b_execution_ids_available'] = False
        reasons = r4.ledger_gate(ledger)['reasons']
        self.assertIn('a_execution_ids_available', reasons)
        self.assertIn('b_execution_ids_available', reasons)

    def test_unavailable_request_logs_table_fails(self):
        """查询 request_logs 失败时不把 duplicate=0 当干净。"""
        ledger = clean_shadow_ledger()
        ledger['a']['request_logs_available'] = False
        self.assertIn('a_request_logs_available', r4.ledger_gate(ledger)['reasons'])


# ─────────────────────────── 入口（修正 #3） ───────────────────────────
class EntryFactsTest(unittest.TestCase):
    def entries(self):
        return {'a': {'port': 1001, 'url': 'https://127.0.0.1:1001',
                      'prefix': LAB + '/runs/x/entry-a', 'prefix_isolated': True,
                      'tls_ok': True, 'cert_sha256': 'c1', 'conf_sha256': 'f1',
                      'front_ready': True},
                'b': {'port': 1002, 'url': 'https://127.0.0.1:1002',
                      'prefix': LAB + '/runs/x/entry-b', 'prefix_isolated': True,
                      'tls_ok': True, 'cert_sha256': 'c2', 'conf_sha256': 'f2',
                      'front_ready': True}}

    def test_two_independent_tls_entries_pass(self):
        self.assertTrue(r4.entry_facts_verdict(self.entries())['ok'])

    def test_plain_http_entry_fails(self):
        entries = self.entries()
        entries['b']['url'] = 'http://127.0.0.1:1002'
        entries['b']['tls_ok'] = False
        reasons = r4.entry_facts_verdict(entries)['reasons']
        self.assertIn('b_entry_not_tls', reasons)

    def test_entry_not_reachable_fails(self):
        entries = self.entries()
        entries['a']['front_ready'] = False
        self.assertIn('a_entry_not_reachable',
                      r4.entry_facts_verdict(entries)['reasons'])

    def test_missing_cert_fingerprint_fails(self):
        entries = self.entries()
        entries['a']['cert_sha256'] = ''
        self.assertIn('a_cert_fingerprint_missing',
                      r4.entry_facts_verdict(entries)['reasons'])

    def test_same_prefix_fails(self):
        entries = self.entries()
        entries['b']['prefix'] = entries['a']['prefix']
        self.assertIn('entry_prefixes_identical',
                      r4.entry_facts_verdict(entries)['reasons'])


# ─────────────────────────── 切换判定（修正 #2） ───────────────────────────
class SwitchVerdictTest(unittest.TestCase):
    def test_release_na_for_legacy_schema(self):
        """旧快照无 instance_leases：release 记 n/a，不判失败（Lead 修正 #2）。"""
        verdict = r4.release_applicability(False, False)
        self.assertFalse(verdict['applicable'])
        self.assertEqual(verdict['reason'], 'legacy_schema_without_instance_leases')

    def test_release_strict_when_lease_held(self):
        verdict = r4.release_applicability(True, True)
        self.assertTrue(verdict['applicable'])
        self.assertEqual(verdict['reason'], 'instance_lease_held')

    def test_release_na_when_table_present_without_claim(self):
        verdict = r4.release_applicability(True, False)
        self.assertFalse(verdict['applicable'])
        self.assertEqual(verdict['reason'],
                         'lease_table_present_but_instance_did_not_claim')

    def test_clean_switch_passes_with_na_release(self):
        verdict = r4.switch_verdict(clean_switch())
        self.assertTrue(verdict['pass'], verdict['reasons'])
        self.assertFalse(verdict['release_applicable'])

    def test_applicable_release_not_observed_fails(self):
        switch = clean_switch()
        switch['a_release'] = {'applicable': True, 'fence_before': 1,
                               'row': {'role': 'serving', 'owner': 'x',
                                       'lease_until': 123, 'fence': 1}}
        verdict = r4.switch_verdict(switch)
        self.assertFalse(verdict['pass'])
        self.assertIn('a_release:role_not_stopped', verdict['reasons'])

    def test_applicable_release_observed_passes(self):
        switch = clean_switch()
        switch['a_release'] = {'applicable': True, 'fence_before': 1,
                               'row': {'role': 'stopped', 'owner': '',
                                       'lease_until': 0, 'fence': 1}}
        self.assertTrue(r4.switch_verdict(switch)['pass'])

    def test_migration_missing_tables_fails(self):
        switch = clean_switch()
        switch['b_authoritative']['migration']['tables_after'] = ['request_logs']
        verdict = r4.switch_verdict(switch)
        self.assertFalse(verdict['pass'])
        self.assertTrue(any('migration_missing_tables' in reason
                            for reason in verdict['reasons']))

    def test_b_lease_not_serving_fails(self):
        switch = clean_switch()
        switch['b_authoritative']['lease_row']['role'] = 'stopped'
        self.assertIn('b_authoritative:b_lease_role_not_serving',
                      r4.switch_verdict(switch)['reasons'])

    def test_b_readyz_not_200_fails(self):
        switch = clean_switch()
        switch['b_authoritative']['readyz_200'] = False
        self.assertFalse(r4.switch_verdict(switch)['pass'])

    def test_entry_not_switched_fails(self):
        switch = clean_switch()
        switch['entry']['front_200'] = False
        self.assertIn('entry_not_switched', r4.switch_verdict(switch)['reasons'])

    def test_probe_unexpected_status_fails(self):
        switch = clean_switch()
        switch['probes'][0]['status'] = 502
        verdict = r4.switch_verdict(switch)
        self.assertFalse(verdict['pass'])
        self.assertIn('switch_probe:probe_status:chat-stream', verdict['reasons'])

    def test_switch_ledger_missing_rows_fails(self):
        switch = clean_switch()
        switch['ledger']['missing'] = ['req-a1']
        self.assertIn('switch_ledger:missing_logs', r4.switch_verdict(switch)['reasons'])

    def test_switch_ledger_without_expected_ids_fails(self):
        switch = clean_switch()
        switch['ledger']['expected_ids'] = 0
        self.assertFalse(r4.switch_verdict(switch)['pass'])

    def test_a_drain_not_effective_fails(self):
        switch = clean_switch()
        switch['a_drain']['readyz_503'] = False
        verdict = r4.switch_verdict(switch)
        self.assertFalse(verdict['pass'])
        self.assertIn('a_drain:readyz_not_503', verdict['reasons'])


# ─────────────────────────── 总判定与退出码 ───────────────────────────
class OverallVerdictTest(unittest.TestCase):
    def test_all_pass_exit_zero(self):
        switch = clean_switch()
        switch['verdict'] = r4.switch_verdict(switch)
        self.assertTrue(switch['verdict']['pass'], switch['verdict']['reasons'])
        verdict = r4.overall_verdict({
            'shadow': {'pass': True, 'reasons': []},
            'switch': switch})
        self.assertEqual(verdict['exit_code'], r4.EXIT_PASS)
        self.assertEqual(verdict['decision'], 'allow_switch')

    def test_shadow_failure_exit_one(self):
        verdict = r4.overall_verdict({
            'shadow': {'pass': False, 'reasons': ['sample_count']},
            'switch': {'outcome': 'not_attempted'}})
        self.assertEqual(verdict['exit_code'], r4.EXIT_FAIL)
        self.assertEqual(verdict['decision'], 'refuse_switch')

    def test_switch_refused_with_restored_service_exit_three(self):
        verdict = r4.overall_verdict({
            'shadow': {'pass': True, 'reasons': []},
            'switch': {'outcome': 'refused_precondition', 'service_restored': True,
                       'verdict': {'pass': False,
                                   'reasons': ['b_authoritative:b_readyz_not_200']}}})
        self.assertEqual(verdict['exit_code'], r4.EXIT_SWITCH_REFUSED)
        self.assertEqual(verdict['decision'], 'refuse_switch')

    def test_switch_refused_without_restore_exit_one(self):
        verdict = r4.overall_verdict({
            'shadow': {'pass': True, 'reasons': []},
            'switch': {'outcome': 'refused_precondition_restore_failed',
                       'service_restored': False,
                       'verdict': {'pass': False, 'reasons': ['b_not_started']}}})
        self.assertEqual(verdict['exit_code'], r4.EXIT_FAIL)
        self.assertIn('service_not_restored', verdict['reasons'])

    def test_switch_failed_rollback_failed_exit_one_manual(self):
        verdict = r4.overall_verdict({
            'shadow': {'pass': True, 'reasons': []},
            'switch': {'outcome': 'switch_failed_rollback_failed',
                       'service_restored': False,
                       'verdict': {'pass': False,
                                   'reasons': ['entry_not_switched'],
                                   'manual_intervention_required': True}}})
        self.assertEqual(verdict['exit_code'], r4.EXIT_FAIL)
        self.assertIn('manual_intervention_required', verdict['reasons'])

    def test_committed_switch_verdict_failure_exit_one(self):
        switch = clean_switch()
        switch['entry']['front_200'] = False
        switch['outcome'] = 'switch_failed'
        verdict = r4.overall_verdict({'shadow': {'pass': True, 'reasons': []},
                                      'switch': switch})
        self.assertEqual(verdict['exit_code'], r4.EXIT_FAIL)


# ─────────────────────────── 真实入口配置 / 阶段文件分离 ───────────────────────────
class EntryConfTest(unittest.TestCase):
    def conf(self):
        return r4.build_entry_conf(
            root='/home/gateway-lab/lab/runs/x/entry-a',
            cert='/home/gateway-lab/lab/runs/x/entry-a/tls.crt',
            key='/home/gateway-lab/lab/runs/x/entry-a/tls.key',
            listen_port=18443, upstream_port=18080)

    def test_conf_is_loopback_tls_with_marker_block(self):
        text = self.conf()
        self.assertIn(r4.switch_upstream.MARK_BEGIN, text)
        self.assertIn(r4.switch_upstream.MARK_END, text)
        self.assertIn('listen 127.0.0.1:18443 ssl', text)
        self.assertIn('server 127.0.0.1:18080;', text)
        self.assertIn('ssl_certificate ', text)
        self.assertNotIn('0.0.0.0', text)

    def test_upstream_rewrite_reuses_existing_controller(self):
        """切换阶段直接复用 switch_upstream 的改写原语（不复制实现）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nginx.conf'
            path.write_text(self.conf(), encoding='utf-8')
            r4.switch_upstream.set_upstream_ports({'nginx_conf': str(path)}, [2222])
            rewritten = path.read_text(encoding='utf-8')
            # switch_upstream 的改写格式带 max_fails/fail_timeout；说明复用的是它而不是自写逻辑。
            self.assertIn('server 127.0.0.1:2222 max_fails=1 fail_timeout=2s;', rewritten)
            self.assertNotIn('server 127.0.0.1:18080;', rewritten)
            self.assertIn(r4.switch_upstream.MARK_BEGIN, rewritten)


class PhaseTimelineTest(unittest.TestCase):
    def test_shadow_and_switch_evidence_are_separate_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            timeline = r4.PhaseTimeline(Path(tmp) / 'shadow.ndjson',
                                        Path(tmp) / 'switch.ndjson')
            timeline.emit('shadow_event', n=1)
            timeline.set_phase('switch')
            timeline.emit('switch_event', n=2)
            timeline.close()
            shadow = [json.loads(line) for line in
                      (Path(tmp) / 'shadow.ndjson').read_text(encoding='utf-8').splitlines()]
            switch = [json.loads(line) for line in
                      (Path(tmp) / 'switch.ndjson').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([row['event'] for row in shadow], ['shadow_event'])
        self.assertIn('switch_event', [row['event'] for row in switch])
        self.assertNotIn('switch_event', [row['event'] for row in shadow])
        self.assertTrue(all(row.get('phase') == 'shadow' for row in shadow))


# ─────────────────────────── 真实 SQLite 账本逻辑（离线） ───────────────────────────
SCHEMA = '''
CREATE TABLE request_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, key_id INTEGER,
  model TEXT, status INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0, request_id TEXT, endpoint TEXT,
  terminal_state TEXT, error TEXT);
CREATE TABLE request_outcomes(request_id TEXT PRIMARY KEY, ts INTEGER, endpoint TEXT,
  protocol TEXT, http_status INTEGER, terminal_state TEXT);
CREATE TABLE budget_reservations(request_id TEXT PRIMARY KEY, state TEXT);
CREATE TABLE usage_daily(day TEXT, key_id INTEGER, model TEXT, requests INTEGER,
  prompt_tokens INTEGER, completion_tokens INTEGER);
'''


def make_db(path, *, request_id='exec-a1', tokens=(7, 5), usage=True):
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO request_logs(ts,key_id,model,status,prompt_tokens,"
                     "completion_tokens,request_id,endpoint,terminal_state) "
                     "VALUES(1,1,'fake-model',200,?,?,?,'/v1/chat/completions','completed')",
                     (tokens[0], tokens[1], request_id))
        conn.execute("INSERT INTO request_outcomes(request_id,ts,endpoint,protocol,"
                     "http_status,terminal_state) VALUES(?,1,"
                     "'/v1/chat/completions','openai',200,'completed')", (request_id,))
        conn.execute("INSERT INTO budget_reservations(request_id,state) VALUES(?,'settled')",
                     (request_id,))
        if usage:
            conn.execute('INSERT INTO usage_daily(day,key_id,model,requests,prompt_tokens,'
                         'completion_tokens) VALUES(?,1,?,1,?,?)',
                         ('2026-10-26', 'fake-model', tokens[0], tokens[1]))
        conn.commit()
    finally:
        conn.close()
    return path


class SqliteLedgerTest(unittest.TestCase):
    def test_completeness_and_duplicates(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(Path(tmp) / 'main.db')
            self.assertEqual(r4.ledger_completeness(db, ['exec-a1'])['missing'], [])
            missing = r4.ledger_completeness(db, ['exec-a1', 'exec-missing'])
            self.assertEqual(missing['missing'], ['exec-missing'])
            import sqlite3
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO request_logs(ts,key_id,model,status,request_id,endpoint) "
                         "VALUES(2,1,'fake-model',200,'exec-dup','/v1/chat/completions')")
            conn.execute("INSERT INTO request_logs(ts,key_id,model,status,request_id,endpoint) "
                         "VALUES(3,1,'fake-model',200,'exec-dup','/v1/chat/completions')")
            conn.commit()
            conn.close()
            duplicate = r4.ledger_completeness(db, ['exec-dup'])
            self.assertEqual(duplicate['duplicate'], ['exec-dup'])

    def test_cancel_terminals_must_be_client_cancelled(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(Path(tmp) / 'main.db')
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO request_logs(ts,key_id,model,status,request_id,endpoint,"
                         "terminal_state) VALUES(4,1,'fake-model',200,'exec-cancel',"
                         "'/v1/chat/completions','client_cancelled')")
            conn.commit()
            conn.close()
            self.assertEqual(r4.cancel_terminals(db, ['exec-cancel'])['missing'], [])
            self.assertEqual(r4.cancel_terminals(db, ['exec-a1'])['missing'], ['exec-a1'])

    def test_snapshot_detects_duplicates_tokens_and_orphans(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(Path(tmp) / 'main.db')
            snapshot = r4.ledger_snapshot(db)
            self.assertTrue(snapshot['request_logs_available'])
            self.assertEqual(snapshot['duplicate_request_ids'], 0)
            self.assertEqual(snapshot['request_logs_tokens'],
                             snapshot['usage_daily_tokens'])
            self.assertEqual(snapshot['unexpected_outcome_without_log'], 0)
            self.assertEqual(snapshot['open_or_unknown_reservations'], 0)
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO request_outcomes(request_id,ts,protocol,http_status) "
                         "VALUES('ghost',5,'openai',500)")
            conn.commit()
            conn.close()
            after = r4.ledger_snapshot(db)
            self.assertEqual(after['unexpected_outcome_without_log'], 1)

    def test_shadow_ledger_flags_cross_contamination(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            main_db = make_db(Path(tmp) / 'main.db', request_id='exec-a1')
            shadow_db = make_db(Path(tmp) / 'shadow.db', request_id='exec-b1')
            conn = sqlite3.connect(shadow_db)
            # 影子库出现 A 的 request_id = 交叉污染
            conn.execute("INSERT INTO request_logs(ts,key_id,model,status,request_id,endpoint) "
                         "VALUES(9,1,'fake-model',200,'exec-a1','/v1/chat/completions')")
            conn.commit()
            conn.close()
            ledger = r4.build_shadow_ledger(main_db, shadow_db, ['exec-a1'],
                                            ['exec-b1'], [], [])
            self.assertGreater(ledger['cross_contamination']['a_ids_in_b'], 0)
            self.assertFalse(ledger['pass'])

    def test_shadow_ledger_clean_isolated_dbs_pass(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            main_db = make_db(Path(tmp) / 'main.db', request_id='exec-a1')
            shadow_db = make_db(Path(tmp) / 'shadow.db', request_id='exec-b1')
            ledger = r4.build_shadow_ledger(main_db, shadow_db, ['exec-a1'],
                                            ['exec-b1'], [], [])
            self.assertTrue(ledger['pass'], ledger['verdict']['reasons'])
            self.assertEqual(ledger['cross_contamination']['a_ids_in_b'], 0)
            self.assertEqual(ledger['cross_contamination']['b_ids_in_a'], 0)


class LoopbackUrlTest(unittest.TestCase):
    """FakeProvider.base_url 带 scheme；loopback 判定必须接受 URL 形式（候选4 S5 的 exit 2 回归）。"""

    def test_loopback_urls_accepted(self):
        for value in ('http://127.0.0.1:42761', 'https://localhost:443',
                      'http://[::1]:8080', '127.0.0.1:9000', 'localhost:1'):
            with self.subTest(value=value):
                self.assertTrue(r4._is_loopback_url(value))

    def test_missing_provider_is_loopback(self):
        for value in (None, ''):
            with self.subTest(value=value):
                self.assertTrue(r4._is_loopback_url(value))

    def test_non_loopback_rejected(self):
        for value in ('http://192.168.1.5:80', 'http://example.com/',
                      'https://10.0.0.2:443', 'http://127.0.0.1.evil.com:80'):
            with self.subTest(value=value):
                self.assertFalse(r4._is_loopback_url(value))


class NoBoolMethodCallTest(unittest.TestCase):
    """防止 `bool(Path).is_file()` 这类 AttributeError（候选5 S5 的 exit 1 回归）。"""

    def test_no_bool_result_method_call(self):
        source = (HERE / 'r4_parallel_shadow.py').read_text(encoding='utf-8')
        self.assertIsNone(re.search(r'bool\([^()\n]*\)\s*\.\w+', source),
                          'bool(...) 的结果不得当作对象调用方法')


class UnsettledReservationGateTest(unittest.TestCase):
    """未结算预留必须“可归因 + A/B 对称”（候选6 的 24/24 对称残值误杀回归）。"""

    def ledger(self, a_unsettled=0, b_unsettled=0, a_unexpected=(), b_unexpected=()):
        ledger = clean_shadow_ledger()
        ledger['a']['open_or_unknown_reservations'] = a_unsettled
        ledger['b']['open_or_unknown_reservations'] = b_unsettled
        ledger['a']['unexpected_unsettled_ids'] = list(a_unexpected)
        ledger['b']['unexpected_unsettled_ids'] = list(b_unexpected)
        return ledger

    def test_clean_ledger_passes(self):
        self.assertTrue(r4.ledger_gate(self.ledger())['pass'])

    def test_symmetric_cancel_error_residual_is_allowed(self):
        # A/B 各有 24 条 cancel/error 残值、均已归因 → 不能判失败。
        result = r4.ledger_gate(self.ledger(a_unsettled=24, b_unsettled=24))
        self.assertTrue(result['pass'], result['reasons'])

    def test_asymmetric_residual_rejected(self):
        result = r4.ledger_gate(self.ledger(a_unsettled=24, b_unsettled=0))
        self.assertIn('unsettled_symmetric', result['reasons'])

    def test_unattributable_residual_rejected(self):
        result = r4.ledger_gate(self.ledger(a_unsettled=2, b_unsettled=2,
                                            b_unexpected=['exec-b-x']))
        self.assertIn('b_no_unexpected_unsettled', result['reasons'])
        result_a = r4.ledger_gate(self.ledger(a_unsettled=2, b_unsettled=2,
                                              a_unexpected=['exec-a-x']))
        self.assertIn('a_no_unexpected_unsettled', result_a['reasons'])


# ─────────────────── 切换账本残值归因（候选7 exit 1 修复） ───────────────────
class SwitchResidualAttributionTest(unittest.TestCase):
    """切换账本不能只查 open_or_unknown_reservations==0。

    候选7 的 A 主库天然带着影子窗口 cancel/error 的 24 条可归因残值（影子账本已
    pass），旧判定直接把切换判失败 → 回退 → exit 1。新口径：只对不在允许集合里
    的未结算判失败，并在报告里列 unexpected_unsettled_ids。
    """

    def test_attributable_residual_does_not_fail_switch(self):
        switch = clean_switch()
        switch['ledger']['open_or_unknown_reservations'] = 24
        switch['ledger']['unexpected_unsettled_ids'] = []
        switch['ledger']['unsettled_attribution'] = {
            'allowed_from_shadow': [f'exec-a-{i}' for i in range(24)],
            'allowed_from_switch_probes': ['exec-probe-err'],
            'symmetric_residual': [f'exec-a-{i}' for i in range(24)],
            'unexpected': [], 'pass': True}
        verdict = r4.switch_verdict(switch)
        self.assertTrue(verdict['pass'], verdict['reasons'])

    def test_unattributable_unsettled_fails_switch(self):
        switch = clean_switch()
        switch['ledger']['open_or_unknown_reservations'] = 25
        switch['ledger']['unexpected_unsettled_ids'] = ['exec-ghost']
        reasons = r4.switch_verdict(switch)['reasons']
        self.assertIn('switch_ledger:unexpected_unsettled_reservations', reasons)

    def test_legacy_evidence_falls_back_to_zero_check(self):
        switch = clean_switch()
        switch['ledger'].pop('unexpected_unsettled_ids')
        switch['ledger'].pop('unexpected_outcome_without_log_ids')
        switch['ledger']['open_or_unknown_reservations'] = 1
        reasons = r4.switch_verdict(switch)['reasons']
        self.assertIn('switch_ledger:open_or_unknown_reservations', reasons)

    def test_unexpected_outcome_residual_fails_switch(self):
        switch = clean_switch()
        switch['ledger']['unexpected_outcome_without_log_ids'] = ['exec-orphan']
        reasons = r4.switch_verdict(switch)['reasons']
        self.assertIn('switch_ledger:unexpected_outcome_without_log_ids', reasons)


class ResidualAttributionPureTest(unittest.TestCase):
    def test_symmetric_residual_is_attributable(self):
        result = r4.attribute_residual(['s1', 's2', 'p1'],
                                       shadow_ids=['s1', 's2'],
                                       probe_ids=['p1'])
        self.assertTrue(result['pass'])
        self.assertEqual(result['symmetric_residual'], ['s1', 's2', 'p1'])
        self.assertEqual(result['unexpected'], [])

    def test_unattributable_residual_rejected(self):
        result = r4.attribute_residual(['s1', 'ghost'], ['s1'], [])
        self.assertFalse(result['pass'])
        self.assertEqual(result['unexpected'], ['ghost'])

    def test_empty_residual_passes(self):
        self.assertTrue(r4.attribute_residual([], [], [])['pass'])

    def test_outcome_residual_attribution(self):
        known = r4.attribute_outcome_residual(['a1', 'ghost'], ['a1'])
        self.assertFalse(known['pass'])
        self.assertEqual(known['known_residual'], ['a1'])
        self.assertEqual(known['unexpected'], ['ghost'])
        self.assertTrue(r4.attribute_outcome_residual([], ['a1'])['pass'])


class SwitchLedgerSnapshotAttributionTest(unittest.TestCase):
    def _db(self, path):
        import sqlite3
        conn = sqlite3.connect(path)
        try:
            conn.executescript(SCHEMA)
            for rid in ('exec-a1', 'exec-a2', 'exec-a3', 'exec-probe-err'):
                conn.execute("INSERT INTO request_logs(ts,key_id,model,status,request_id,"
                             "endpoint) VALUES(1,1,'fake-model',200,?,'/v1/chat/completions')",
                             (rid,))
            # 影子期 cancel/error 残值 + 切换期 error 探针残值 + 一条不可归因
            conn.execute("INSERT INTO budget_reservations VALUES('exec-a1','unknown')")
            conn.execute("INSERT INTO budget_reservations VALUES('exec-a2','reserved')")
            conn.execute("INSERT INTO budget_reservations VALUES('exec-probe-err','forwarded')")
            conn.execute("INSERT INTO budget_reservations VALUES('exec-ghost','unknown')")
            conn.execute('INSERT INTO usage_daily(day,key_id,model,requests,prompt_tokens,'
                         'completion_tokens) VALUES(?,1,?,1,7,5)', ('2026-10-26', 'fake-model'))
            conn.commit()
        finally:
            conn.close()
        return path

    def test_snapshot_attributes_residuals_and_reports_unexpected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(Path(tmp) / 'main.db')
            snapshot = r4.switch_ledger_snapshot(
                db, ['exec-a3'], ['exec-a3'],
                shadow_unsettled_ids=['exec-a1', 'exec-a2'],
                probe_unsettled_ids=['exec-probe-err'])
            self.assertEqual(snapshot['unexpected_unsettled_ids'], ['exec-ghost'])
            attribution = snapshot['unsettled_attribution']
            self.assertEqual(attribution['allowed_from_shadow'],
                             ['exec-a1', 'exec-a2'])
            self.assertEqual(attribution['allowed_from_switch_probes'],
                             ['exec-probe-err'])
            self.assertFalse(attribution['pass'])

    def test_snapshot_passes_when_all_residuals_attributable(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(Path(tmp) / 'main.db')
            conn = sqlite3.connect(db)
            conn.execute("DELETE FROM budget_reservations WHERE request_id='exec-ghost'")
            conn.commit()
            conn.close()
            snapshot = r4.switch_ledger_snapshot(
                db, ['exec-a3'], ['exec-a3'],
                shadow_unsettled_ids=['exec-a1', 'exec-a2'],
                probe_unsettled_ids=['exec-probe-err'])
            self.assertEqual(snapshot['unexpected_unsettled_ids'], [])
            self.assertTrue(snapshot['unsettled_attribution']['pass'])


class DbSnapshotRollbackTest(unittest.TestCase):
    """回退必须回到旧代码能读的主库（候选7 的 ReaderTooOld 根因）。"""

    def test_backup_and_restore_roundtrip(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(Path(tmp) / 'main.db')
            snapshot_path = Path(tmp) / 'pre-switch.db'
            backup = r4.backup_main_db(db, snapshot_path)
            self.assertTrue(backup['ok'], backup.get('error'))
            self.assertEqual(len(backup['sha256']), 64)
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO request_logs(ts,key_id,model,status,request_id,"
                         "endpoint) VALUES(9,1,'fake-model',200,'exec-after','/v1/x')")
            conn.commit()
            conn.close()
            self.assertEqual(r4.ledger_completeness(
                db, ['exec-after'])['missing'], [])
            restored = r4.restore_main_db(db, snapshot_path,
                                          expected_sha256=backup['sha256'])
            self.assertFalse(restored['ok'])
            self.assertEqual(restored['error'], 'business_data_changed_since_snapshot')
            # 已接收的新业务数据不能被回退覆盖；这是比原先“删除新写入”更严格的门禁。
            self.assertEqual(r4.ledger_completeness(
                db, ['exec-after'])['missing'], [])
            self.assertEqual(r4.ledger_completeness(db, ['exec-a1'])['missing'], [])

    def test_restore_rejects_snapshot_hash_mismatch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = str(make_db(Path(tmp) / 'main.db'))
            snapshot_path = Path(tmp) / 'pre-switch.db'
            backup = r4.backup_main_db(db, snapshot_path)
            self.assertTrue(backup['ok'])
            restored = r4.restore_main_db(db, snapshot_path,
                                          expected_sha256='0' * 64)
            self.assertFalse(restored['ok'])
            self.assertEqual(restored['error'], 'snapshot_sha256_mismatch')

    def test_restore_removes_stale_wal_sidecars(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(Path(tmp) / 'main.db')
            snapshot_path = Path(tmp) / 'pre-switch.db'
            backup = r4.backup_main_db(db, snapshot_path)
            self.assertTrue(backup['ok'])
            Path(str(db) + '-wal').write_bytes(b'stale-wal')
            Path(str(db) + '-shm').write_bytes(b'stale-shm')
            restored = r4.restore_main_db(db, snapshot_path)
            self.assertTrue(restored['ok'])
            self.assertIn('main.db-wal', restored['removed_wal'])
            self.assertFalse(Path(str(db) + '-wal').exists())


class RollbackDiagnosticsTest(unittest.TestCase):
    def test_scrub_log_removes_secrets(self):
        raw = ('boom Bearer wbk_abcdef123456 password=hunter2 token: xyz '
               'Authorization: Bearer abc.def')
        text = r4.scrub_log(raw)
        self.assertNotIn('wbk_abcdef123456', text)
        self.assertNotIn('hunter2', text)
        self.assertNotIn('Bearer abc.def', text)
        self.assertIn('***', text)

    def test_classify_reader_too_old(self):
        result = r4.classify_launcher_failure(
            returncode=1, identity_present=False, port_in_use=False,
            log_tail='schemaver.ReaderTooOld: 库需要至少 v8 的读代码，当前代码只支持 v6')
        self.assertEqual(result['reason'], 'db_reader_too_old')

    def test_classify_port_in_use(self):
        result = r4.classify_launcher_failure(
            returncode=None, identity_present=False, port_in_use=True,
            log_tail='OSError: [Errno 98] Address already in use')
        self.assertEqual(result['reason'], 'port_in_use')

    def test_classify_import_failure(self):
        result = r4.classify_launcher_failure(
            returncode=1, identity_present=False, port_in_use=False,
            log_tail='ModuleNotFoundError: No module named server')
        self.assertEqual(result['reason'], 'launcher_import_failed')

    def test_classify_exit_and_timeout(self):
        self.assertEqual(r4.classify_launcher_failure(
            returncode=2, identity_present=False, port_in_use=False,
            log_tail='usage: serve.py')['reason'], 'process_exit_code_2')
        self.assertEqual(r4.classify_launcher_failure(
            returncode=None, identity_present=False, port_in_use=False,
            log_tail='')['reason'], 'identity_file_missing')

    def test_wait_port_free_detects_bound_port(self):
        import socket
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        try:
            self.assertTrue(r4.wait_port_free(port, timeout=0.3)['in_use'])
        finally:
            listener.close()
        self.assertTrue(r4.wait_port_free(port, timeout=3)['free'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
