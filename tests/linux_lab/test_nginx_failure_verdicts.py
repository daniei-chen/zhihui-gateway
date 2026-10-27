"""Counterexamples for R4 nginx/TLS/SSE failure-injection verdicts (offline, Windows-safe)."""
import copy, importlib.util, unittest
from pathlib import Path
def load():
    spec = importlib.util.spec_from_file_location('r4_nginx_failure_injection', Path(__file__).with_name('r4_nginx_failure_injection.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
r4 = load()
CLEAN = {'duplicate_request_ids': 0, 'unexpected_outcome_without_log': 0, 'settled_reservation_without_log': 0, 'open_or_unknown_reservations': 0}
SHA = {'unchanged': True, 'sha256_before': 'sha-aa', 'sha256_after_rollback': 'sha-aa', 'markers_intact': True}
QUIET_RELOAD = {'observed': 0, 'worker_pids_same': True, 'error_log_reload_lines': 0}
GOOD_RUN = {'label': 'injected', 'exit_code': 1, 'stderr': 'nginx: [emerg] unknown directive "r4_injected_unknown_directive" in nginx.conf:42', 'classification': {'failed': True, 'kind': 'syntax_error'}}
OK_RUN = {'label': 'rollback', 'exit_code': 0, 'stderr': '', 'classification': {'failed': False, 'kind': 'ok'}}
def sse(terminal='completed'):
    return {'accepted': True, 'status': 200, 'first_frame_ms': 100.0, 'frames': 5, 'terminal': terminal, 'cut': terminal == 'stream_cut', 'terminal_recorded': True, 'request_id': 'req-1', 'error': None, 't_start_ms': 0.0, 't_end_ms': 900.0, 'first_frame_before_injection': True, 'ends_after_injection': True}
def quiet():
    return {'mode': 'quiet', 'samples': 100, 'abnormal_count': 0, 'measured_ms': None, 'recovery': None, 'first_abnormal': None, 'last_abnormal': None, 'coverage_upper_bound_ms': 50.0, 'invalid_reasons': []}
def outage(recovery=True, window=True):
    if not window: return quiet()
    return {'mode': 'outage', 'samples': 80, 'abnormal_count': 2,
            'first_abnormal': {'t_start_ms': 200.0, 't_end_ms': 220.0, 'status': 503, 'error': None},
            'last_abnormal': {'t_start_ms': 380.0, 't_end_ms': 400.0, 'status': 503, 'error': None},
            'recovery': {'t_start_ms': 500.0, 't_end_ms': 505.0, 'status': 200, 'error': None} if recovery else None,
            'measured_ms': 305.0 if recovery else None, 'coverage_upper_bound_ms': 30.0,
            'invalid_reasons': [] if recovery else ['no_recovery_after_last_failure']}
def sc1():
    return {'attempt': {'child_exit_code': 1, 'event_names': ['new_ready_preflight'], 'failure_message': 'candidate not ready; old instance and entry left unchanged'},
            'preconditions': {'a_readyz': 200, 'b_readyz': 503, 'a_port': 18081, 'entry_ports': [18081]},
            'config': dict(SHA), 'reload': dict(QUIET_RELOAD), 'probe': quiet(), 'a_readyz_after': 200,
            'sse': sse(), 'ledger': dict(CLEAN)}
def sc2():
    return {'attempt': {'child_exit_code': 1, 'event_names': ['new_ready_preflight', 't_drain_signal'], 'failure_message': 'nginx -t failed: nginx: [emerg] unknown directive'},
            'injection': {'kind': 'config_syntax_error', 'nginx_t_runs': [GOOD_RUN, OK_RUN]},
            'rollback': {'present': True, 'ok': True, 'errors': []}, 'config': dict(SHA), 'reload': {'observed': 1},
            'nginx_t_after': {'kind': 'ok'}, 'probe': outage(), 'front_after_recovery': 200,
            'sse': sse(), 'ledger': dict(CLEAN)}
def sc3():
    return {'attempt': {'child_exit_code': 1, 'event_names': ['new_ready_preflight'], 'failure_message': 'entry not ready after reload; refusing to drain old instance'},
            'preconditions': {'b_readyz': 200}, 'postconditions': {'b_readyz': 503}, 'a_readyz_after': 200,
            'injection': {'kind': 'candidate_drain', 'b_drained': True}, 'rollback': {'present': True, 'ok': True, 'errors': []},
            'config': {'sha256_before': 'sha-aa', 'sha256_after_rollback': 'sha-aa'},
            'reload': {'observed': 1}, 'nginx_t_after': {'kind': 'ok'}, 'probe': outage(),
            'front_after_recovery': 200, 'sse': sse(), 'ledger': dict(CLEAN)}
def sc4():
    bad_run = dict(GOOD_RUN, label='rollback_restored_nginx_conf', stderr='nginx: [emerg] unknown directive "r4_injected_include_directive" in include:9')
    return {'attempt': {'child_exit_code': 78, 'event_names': ['rollback_done'], 'failure_message': 'nginx -t failed: nginx: [emerg] unknown directive'},
            'injection': {'kind': 'include_syntax_error', 'nginx_t_runs': [GOOD_RUN, bad_run], 'include_sha256_before': 'inc-before', 'targets': [{'sha256_before': 'inc-before', 'sha256_after': 'inc-corrupt'}]},
            'rollback': {'present': True, 'ok': False, 'errors': ['entry_restore:SystemExit']},
            'config': {'sha256_before': 'sha-aa', 'sha256_after_rollback': 'sha-aa'},
            'manual_intervention': {'required': True, 'exit_code': 78, 'recovered': True, 'recovery': {'nginx_t': {'kind': 'ok'}, 'reload_rc': 0, 'front_status': 200, 'include_sha256': 'inc-before'}, 'nginx_t_failures': [{'label': 'injected', 'exit_code': 1, 'stderr': GOOD_RUN['stderr']}]},
            'sse': sse(), 'ledger': dict(CLEAN)}
BASELINES = {'sc1': sc1, 'sc2': sc2, 'sc3': sc3, 'sc4': sc4}
def mutate(evidence, path, value):
    clone = copy.deepcopy(evidence)
    cursor = clone
    parts = path.split('.')
    for part in parts[:-1]: cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
    cursor[parts[-1]] = value
    return clone
def bad(name, path, value):
    return r4.JUDGES[name](mutate(BASELINES[name](), path, value))['matched']
BASE_FACTS = {'hostname': 'zhihui-local-lab', 'home': '/home/gateway-lab', 'lab': '/home/gateway-lab/lab',
              'qa_path': '/home/gateway-lab/lab/current-qa.json', 'qa_exists': True, 'production_connected': False,
              'workspace': '/home/gateway-lab/lab/snapshots/s1', 'out': '/home/gateway-lab/lab/r4-failure-runs/x',
              'root': '/home/gateway-lab/lab/r3l2-runs/y', 'nginx_conf': '/home/gateway-lab/lab/r3l2-runs/y/nginx.conf',
              'instances': [{'port': 18081, 'alive': True}, {'port': 18082, 'alive': True}],
              'env_script_exists': True, 'switch_script_exists': True, 'host_port_free': True, 'state_ready': True, 'sudo_ok': True}
class CleanBaselineTest(unittest.TestCase):
    def test_all_four_clean_baselines_pass(self):
        for name in BASELINES:
            with self.subTest(scenario=name): self.assertTrue(r4.JUDGES[name](BASELINES[name]())['matched'])
    def test_sse_status_200_alone_is_not_enough(self):
        self.assertFalse(bad('sc1', 'sse.first_frame_ms', None))
    def test_ledger_duplicates_cannot_pass(self):
        self.assertFalse(bad('sc1', 'ledger.duplicate_request_ids', 1))
class InjectionHonestyTest(unittest.TestCase):
    def test_zero_controller_exit_cannot_pass(self):
        for name in ('sc1', 'sc4'):
            with self.subTest(scenario=name): self.assertFalse(bad(name, 'attempt.child_exit_code', 0))
    def test_nginx_t_zero_exit_cannot_be_an_injection(self):
        self.assertFalse(bad('sc2', 'injection.nginx_t_runs.0.classification', {'failed': False, 'kind': 'ok'}))
    def test_other_failure_kind_cannot_pass_as_syntax_injection(self):
        self.assertFalse(bad('sc2', 'injection.nginx_t_runs.0.classification', {'failed': True, 'kind': 'other_failure'}))
    def test_markers_lost_cannot_pass(self):
        self.assertFalse(bad('sc2', 'config.markers_intact', False))
    def test_s4_zero_exit_cannot_pass(self):
        self.assertFalse(bad('sc4', 'attempt.child_exit_code', 0))
    def test_s4_rollback_that_succeeded_cannot_pass(self):
        self.assertFalse(bad('sc4', 'rollback', {'present': True, 'ok': True, 'errors': []}))
    def test_s4_missing_rollback_done_is_insufficient(self):
        self.assertFalse(bad('sc4', 'rollback', {'present': False, 'ok': None, 'errors': []}))
    def test_s4_missing_manual_intervention_cannot_pass(self):
        self.assertFalse(bad('sc4', 'manual_intervention', {}))
class QuietInstanceHonestyTest(unittest.TestCase):
    def test_config_sha_change_cannot_pass(self):
        self.assertFalse(bad('sc1', 'config.sha256_after_rollback', 'sha-bb'))
    def test_reload_observed_cannot_pass(self):
        self.assertFalse(bad('sc1', 'reload', {'observed': 1, 'worker_pids_same': False, 'error_log_reload_lines': 3}))
    def test_worker_change_without_log_is_still_a_reload(self):
        self.assertFalse(bad('sc1', 'reload.worker_pids_same', False))
class ReadinessWindowTest(unittest.TestCase):
    def test_s3_draining_old_instance_is_a_failure(self):
        self.assertFalse(bad('sc3', 'attempt.event_names', ['new_ready_preflight', 't_drain_signal']))
    def test_s3_missing_failure_window_cannot_pass(self):
        self.assertFalse(bad('sc3', 'probe', outage(window=False)))
    def test_s3_pre_failure_200_only_is_not_recovery(self):
        self.assertFalse(bad('sc3', 'probe', outage(recovery=False)))
    def test_s2_pre_failure_200_only_is_not_recovery(self):
        self.assertFalse(bad('sc2', 'probe', outage(recovery=False)))
    def test_recovery_before_last_failure_is_rejected(self):
        probe = outage(); probe['recovery'] = {'t_start_ms': 250.0, 't_end_ms': 255.0, 'status': 200, 'error': None}
        self.assertFalse(bad('sc2', 'probe', probe))
class BoundaryAndSelectionTest(unittest.TestCase):
    def test_clean_boundary_has_no_violations(self):
        self.assertEqual(r4.boundary_violations(dict(BASE_FACTS)), [])
    def test_boundary_violations_are_named(self):
        cases = [('hostname', 'other-host', 'hostname_not_lab'), ('home', '/root', 'home_not_gateway_lab'),
                 ('production_connected', True, 'production_connected_not_false'), ('qa_path', '/tmp/qa.json', 'qa_outside_lab'),
                 ('qa_exists', False, 'qa_file_missing'), ('out', '/tmp/out', 'out_outside_lab'),
                 ('workspace', '/tmp/snap', 'workspace_outside_snapshots'), ('nginx_conf', '/etc/nginx/nginx.conf', 'nginx_conf_outside_experiment'),
                 ('instances', [{'port': 1, 'alive': True}], 'need_two_instances'),
                 ('instances', [{'port': 1, 'alive': True}, {'port': 1, 'alive': True}], 'duplicate_instance_ports'),
                 ('instances', [{'port': 1, 'alive': True}, {'port': 2, 'alive': False}], 'instance_not_alive'),
                 ('host_port_free', False, 'host_port_in_use'), ('sudo_ok', False, 'sudo_unavailable'),
                 ('env_script_exists', False, 'env_script_missing'), ('switch_script_exists', False, 'switch_script_missing')]
        for key, value, expected in cases:
            with self.subTest(key=key):
                facts = dict(BASE_FACTS); facts[key] = value
                self.assertIn(expected, r4.boundary_violations(facts))
    def test_pre_env_facts_skip_instance_checks(self):
        before = {'hostname': 'zhihui-local-lab', 'home': '/home/gateway-lab', 'lab': '/home/gateway-lab/lab',
                  'qa_path': '/home/gateway-lab/lab/qa.json', 'qa_exists': True, 'production_connected': False,
                  'workspace': '/home/gateway-lab/lab/snapshots/s1', 'out': '/home/gateway-lab/lab/out',
                  'env_script_exists': True, 'switch_script_exists': True, 'host_port_free': True, 'state_ready': False}
        self.assertEqual(r4.boundary_violations(before), [])
    def test_unknown_and_duplicate_scenarios_rejected(self):
        self.assertEqual(r4.select_scenarios(['sc1', 'sc2'])['errors'], [])
        self.assertIn('unknown_scenarios:sc9', r4.select_scenarios(['sc1', 'sc9'])['errors'])
        self.assertIn('duplicate_scenarios', r4.select_scenarios(['sc1', 'sc1'])['errors'])
        self.assertEqual(r4.select_scenarios(None)['selected'], list(r4.SCENARIOS))
class PureHelperTest(unittest.TestCase):
    def test_syntax_injection_changes_bytes_and_keeps_markers(self):
        original = 'http {\n%s\nupstream x {}\n%s\n}\n' % (r4.MARK_BEGIN, r4.MARK_END)
        injected = r4.inject_syntax_error(original)
        self.assertNotEqual(injected, original); self.assertTrue(r4.markers_intact(injected))
        self.assertIn('r4_injected_unknown_directive', injected)
        self.assertIn('r4_injected_include_directive', r4.inject_include_error('a b\n'))
    def test_nginx_t_classification_never_turns_zero_into_failure(self):
        self.assertEqual(r4.classify_nginx_t(0, '', '')['kind'], 'ok')
        syntax = r4.classify_nginx_t(1, '', 'nginx: [emerg] unknown directive "x" in nginx.conf:3')
        self.assertEqual((syntax['failed'], syntax['kind']), (True, 'syntax_error'))
        other = r4.classify_nginx_t(1, '', 'nginx: [emerg] bind() to 0.0.0.0:80 failed (13: Permission denied)')
        self.assertEqual((other['failed'], other['kind']), (True, 'other_failure'))
        self.assertEqual(r4.classify_nginx_t(None, '', '')['kind'], 'not_run')
    def test_upstream_ports_and_probe_envelope(self):
        text = 'http {\n%s\nupstream b {\n server 127.0.0.1:18081 max_fails=1 fail_timeout=2s;\n server 127.0.0.1:18082 max_fails=1 fail_timeout=2s;\n}\n%s\n}' % (r4.MARK_BEGIN, r4.MARK_END)
        self.assertEqual(r4.upstream_ports(text), [18081, 18082])
        quiet_result = r4.probe_envelope([{'t_start': i * .05, 't_end': i * .05 + .01, 'status': 200, 'error': None} for i in range(40)], 0, 2)
        self.assertIsNone(quiet_result['measured_ms']); self.assertIsNotNone(quiet_result['coverage_upper_bound_ms'])
        self.assertEqual(quiet_result['invalid_reasons'], [])
        self.assertIn('fewer_than_20_probes', r4.probe_envelope([], 0, 2)['invalid_reasons'])
if __name__ == '__main__':
    unittest.main()
