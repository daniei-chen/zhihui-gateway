"""Counterexamples for the R4 writer-handoff ``b_start_failure`` verdicts (offline, Windows-safe)."""
import copy
import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_SPEC = importlib.util.spec_from_file_location('r4_writer_handoff_failure',
                                              HERE / 'r4_writer_handoff_failure.py')
r4 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(r4)

CLEAN_LEDGER = {'duplicate_request_ids': 0, 'unexpected_outcome_without_log': 0,
                'settled_reservation_without_log': 0, 'open_or_unknown_reservations': 0,
                'request_logs_tokens': 30, 'usage_daily_tokens': 30, 'request_logs_rows': 3}
BASE_FACTS = {'hostname': 'zhihui-local-lab', 'home': '/home/gateway-lab',
              'lab': '/home/gateway-lab/lab', 'repo': '/home/gateway-lab/lab/snapshots/s1',
              'out': '/home/gateway-lab/lab/evidence/writer-handoff-1',
              'qa_file': '/home/gateway-lab/lab/profiles/batchC.json', 'qa_exists': True,
              'out_exists': False, 'production_connected': False,
              'qa_workspace': '/home/gateway-lab/lab/snapshots/s1'}


def baseline():
    return {'readyz_status': 200,
            'inference': {'status': 200, 'terminal': 'completed', 'request_id': 'req-a',
                          'frames': 20},
            'log_landed': True,
            'log_row': {'request_id': 'req-a', 'status': 200, 'prompt_tokens': 5,
                        'completion_tokens': 5}}


def drain():
    return {'begin_status': 200, 'readyz_503': True, 'in_flight_accepted': True,
            'in_flight_terminal': 'completed', 'in_flight_drained': True,
            'in_flight': {'status': 200, 'terminal': 'completed', 'frames': 20,
                          'request_id': 'req-b'},
            'rejected_after_drain': {'status': 503, 'code': 'gateway_draining',
                                     'rejected': True}}


def release():
    return {'observed': True, 'reasons': [],
            'row': {'instance_id': 'inst-a', 'owner': None, 'fence': 1,
                    'role': 'stopped', 'lease_until': 0},
            'fence_before': 1, 'exit_returncode': 0, 'release_error': None}


def b_failure():
    return {'returncode': 1, 'timed_out': False,
            'manager_root': '/tmp/r3l2-x/missing-manager-root', 'port': 18081,
            'identity_file': '/tmp/r3l2-x/instance-B.json',
            'identity_file_present': False, 'info': {},
            'readyz_status': None, 'readyz_error': 'ConnectError: [Errno 111] refused',
            'instance_rows_before': 1, 'instance_rows_after': 1,
            'new_instance_rows': [], 'log_tail': 'ModuleNotFoundError: No module named server'}


def a2():
    return {'started': True, 'readyz_status': 200, 'port': 18090,
            'inference_status': 200,
            'inference_terminal': 'completed', 'inference_request_id': 'req-c',
            'log_landed': True, 'log_row': {'request_id': 'req-c', 'status': 200},
            'new_instance_id': True, 'info': {'instance_id': 'inst-a2', 'fence': 2}}


def branch():
    return {'scenario': 'b_start_failure', 'a_baseline': baseline(), 'a_drain': drain(),
            'a_release': release(), 'b_start_failure': b_failure(), 'a2_recovery': a2()}


def evidence(branch_data=None, ledger=None):
    return {'branches': {'b_start_failure': branch_data if branch_data is not None else branch()},
            'ledger': ledger if ledger is not None else dict(CLEAN_LEDGER)}


def mutate(path, value):
    data = branch()
    cursor = data
    parts = path.split('.')
    for part in parts[:-1]:
        cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
    cursor[parts[-1]] = value
    return r4.verdict(evidence(data))


def ledger_mutate(key, value):
    ledger = dict(CLEAN_LEDGER)
    ledger[key] = value
    return r4.verdict(evidence(ledger=ledger))


class CleanBaselineTest(unittest.TestCase):
    def test_clean_branch_passes(self):
        result = r4.verdict(evidence())
        self.assertTrue(result['pass'], result['reasons'])
        self.assertEqual(sorted(result['checks']),
                         ['a2_recovered', 'a_baseline_ok', 'a_drain_ok', 'a_released',
                          'b_start_failed', 'ledger_ok'])

    def test_missing_branch_cannot_pass(self):
        self.assertFalse(r4.verdict({'branches': {}, 'ledger': dict(CLEAN_LEDGER)})['pass'])


class BStartFailureHonestyTest(unittest.TestCase):
    def test_b_zero_exit_cannot_pass(self):
        self.assertFalse(mutate('b_start_failure.returncode', 0)['pass'])

    def test_b_missing_exit_code_cannot_pass(self):
        self.assertFalse(mutate('b_start_failure.returncode', None)['pass'])

    def test_b_timeout_with_killed_process_is_not_startup_failure(self):
        self.assertFalse(mutate('b_start_failure.timed_out', True)['pass'])

    def test_b_identity_file_written_cannot_pass(self):
        self.assertFalse(mutate('b_start_failure.identity_file_present', True)['pass'])

    def test_b_instance_row_claimed_cannot_pass(self):
        self.assertFalse(mutate('b_start_failure.new_instance_rows',
                                [{'instance_id': 'inst-b', 'owner': 'b', 'role': 'serving'}])['pass'])

    def test_b_serving_200_cannot_pass(self):
        self.assertFalse(mutate('b_start_failure.readyz_status', 200)['pass'])


class ReleaseObservationTest(unittest.TestCase):
    def test_unreleased_row_cannot_pass(self):
        self.assertFalse(mutate('a_release.row.role', 'serving')['pass'])

    def test_owner_not_empty_cannot_pass(self):
        self.assertFalse(mutate('a_release.row.owner', 'ghost-owner')['pass'])

    def test_lease_until_not_zero_cannot_pass(self):
        self.assertFalse(mutate('a_release.row.lease_until', 1234)['pass'])

    def test_fence_change_cannot_pass(self):
        self.assertFalse(mutate('a_release.row.fence', 2)['pass'])

    def test_missing_release_row_cannot_pass(self):
        self.assertFalse(mutate('a_release.row', {})['pass'])


class RecoveryHonestyTest(unittest.TestCase):
    def test_a2_readyz_not_200_cannot_pass(self):
        self.assertFalse(mutate('a2_recovery.readyz_status', 503)['pass'])

    def test_a2_inference_not_200_cannot_pass(self):
        self.assertFalse(mutate('a2_recovery.inference_status', 503)['pass'])

    def test_a2_inference_cut_cannot_pass(self):
        self.assertFalse(mutate('a2_recovery.inference_terminal', 'stream_cut')['pass'])

    def test_a2_request_log_missing_cannot_pass(self):
        self.assertFalse(mutate('a2_recovery.log_landed', False)['pass'])

    def test_a2_reused_identity_cannot_pass(self):
        self.assertFalse(mutate('a2_recovery.new_instance_id', False)['pass'])

    def test_a2_port_placeholder_cannot_pass(self):
        self.assertFalse(mutate('a2_recovery.port', '<free-port>')['pass'])

    def test_a2_port_missing_cannot_pass(self):
        self.assertFalse(mutate('a2_recovery.port', None)['pass'])

    def test_baseline_request_log_missing_cannot_pass(self):
        self.assertFalse(mutate('a_baseline.log_landed', False)['pass'])

    def test_baseline_inference_failed_cannot_pass(self):
        self.assertFalse(mutate('a_baseline.inference.status', 500)['pass'])


class DrainHonestyTest(unittest.TestCase):
    def test_readyz_not_503_after_drain_cannot_pass(self):
        self.assertFalse(mutate('a_drain.readyz_503', False)['pass'])

    def test_new_admission_not_rejected_cannot_pass(self):
        self.assertFalse(mutate('a_drain.rejected_after_drain.rejected', False)['pass'])

    def test_in_flight_stream_cut_cannot_pass(self):
        result = mutate('a_drain.in_flight_terminal', 'stream_cut')
        self.assertFalse(result['pass'])
        self.assertIn('a_drain_ok:inflight_stream_cut', result['reasons'])

    def test_in_flight_completed_passes(self):
        result = mutate('a_drain.in_flight_terminal', 'completed')
        self.assertTrue(result['pass'], result['reasons'])

    def test_in_flight_terminal_missing_cannot_pass(self):
        self.assertFalse(mutate('a_drain.in_flight_terminal', None)['pass'])

    def test_in_flight_never_accepted_cannot_pass(self):
        self.assertFalse(mutate('a_drain.in_flight_accepted', False)['pass'])


class LedgerHonestyTest(unittest.TestCase):
    def test_duplicate_request_ids_fail(self):
        self.assertFalse(ledger_mutate('duplicate_request_ids', 1)['pass'])

    def test_unexpected_outcome_without_log_fails(self):
        self.assertFalse(ledger_mutate('unexpected_outcome_without_log', 2)['pass'])

    def test_settled_reservation_without_log_fails(self):
        self.assertFalse(ledger_mutate('settled_reservation_without_log', 1)['pass'])

    def test_open_or_unknown_reservation_fails(self):
        self.assertFalse(ledger_mutate('open_or_unknown_reservations', 1)['pass'])

    def test_token_mismatch_fails(self):
        self.assertFalse(ledger_mutate('usage_daily_tokens', 29)['pass'])


class BoundaryAndCommandTest(unittest.TestCase):
    def test_clean_boundary_has_no_violations(self):
        self.assertEqual(r4.guard_violations(dict(BASE_FACTS)), [])

    def test_boundary_violations_are_named(self):
        cases = [('hostname', 'other-host', 'hostname_not_lab'),
                 ('home', '/root', 'home_not_gateway_lab'),
                 ('repo', '/tmp/repo', 'repo_outside_snapshots'),
                 ('out', '/tmp/out', 'out_outside_lab'),
                 ('qa_file', '/tmp/qa.json', 'qa_outside_lab'),
                 ('qa_exists', False, 'qa_file_missing'),
                 ('out_exists', True, 'out_exists_refuse_overwrite'),
                 ('production_connected', True, 'production_connected_not_false'),
                 ('qa_workspace', '/home/gateway-lab/lab/snapshots/other',
                  'qa_workspace_mismatch')]
        for key, value, expected in cases:
            with self.subTest(key=key):
                facts = dict(BASE_FACTS)
                facts[key] = value
                self.assertIn(expected, r4.guard_violations(facts))

    def test_new_instance_rows_detects_only_new_claims(self):
        before = [{'instance_id': 'a'}, {'instance_id': 'old'}]
        after = [{'instance_id': 'a'}, {'instance_id': 'old'}, {'instance_id': 'b'}]
        self.assertEqual([row['instance_id'] for row in r4.new_instance_rows(before, after)],
                         ['b'])
        self.assertEqual(r4.new_instance_rows(before, before), [])

    def test_recovery_steps_are_command_level(self):
        steps = r4.recovery_steps({'repo': '/home/gateway-lab/lab/snapshots/s1',
                                   'root': '/tmp/r3l2-x', 'db': '/tmp/r3l2-x/manager.db',
                                   'out': '/home/gateway-lab/lab/evidence/w1',
                                   'a2_port': 18090,
                                   'data_dir': '/tmp/r3l2-x/data',
                                   'provider': 'http://127.0.0.1:19000',
                                   'keys_file': '/tmp/r3l2-x/keys.json',
                                   'threeway_spec': '{"qoder": ["http://127.0.0.1:19000", "x"]}'})
        joined = '\n'.join(steps)
        for token in ('manager-B.log', 'sqlite3', 'launch_manager.py', 'curl -fsS',
                      'instance_leases', '/api/readyz', 'usage_daily', '127.0.0.1:18090'):
            self.assertIn(token, joined)
        self.assertGreaterEqual(len(steps), 5)

    def test_release_observed_requires_all_fields(self):
        row = {'instance_id': 'a', 'owner': None, 'fence': 1, 'role': 'stopped',
               'lease_until': 0}
        self.assertTrue(r4.release_observed(row, 1)['observed'])
        self.assertFalse(r4.release_observed({}, 1)['observed'])
        self.assertFalse(r4.release_observed(dict(row, owner='x'), 1)['observed'])


if __name__ == '__main__':
    unittest.main()
