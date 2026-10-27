"""Counterexamples for release evidence that must not turn failures green."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entry = load('r3l2_entry_handoff')
handoff = load('r3l2_handoff')


class ProbeMeasurementTest(unittest.TestCase):
    def samples(self, count=20):
        return [{'t_start': i * .1, 't_end': i * .1 + .01,
                 'status': 200, 'error': None} for i in range(count)]

    def test_failure_uses_recovery_after_failure_not_earlier_success(self):
        rows = self.samples()
        rows[2]['status'] = 503
        result = entry.summarize_front_window(rows, 0, 2)
        self.assertAlmostEqual(result['measured_ms'], 110)
        self.assertEqual(result['recovery'], rows[3])
        self.assertEqual(result['invalid_reasons'], [])

    def test_quiet_interval_bound_covers_worst_probe_gap_not_median(self):
        rows = self.samples()
        for row in rows[10:]:
            row['t_start'] += 4
            row['t_end'] += 4
        result = entry.summarize_front_window(rows, 0, 6)
        self.assertGreaterEqual(result['upper_bound_ms'], 4110)
        self.assertIsNone(result['measured_ms'])
        self.assertEqual(result['invalid_reasons'], [])

    def test_no_samples_and_sparse_samples_are_not_valid_measurements(self):
        for count in (0, 1, 19):
            with self.subTest(count=count):
                result = entry.summarize_front_window(self.samples(count), 0, 2)
                self.assertIn('fewer_than_20_probes', result['invalid_reasons'])

    def test_failure_without_subsequent_recovery_is_not_no_outage(self):
        rows = self.samples()
        rows[-1]['status'] = 503
        result = entry.summarize_front_window(rows, 0, 2)
        self.assertIn('no_recovery_after_last_failure', result['invalid_reasons'])
        self.assertIsNone(result['upper_bound_ms'])
        self.assertIsNone(result['measured_ms'])

    def test_excessive_failure_fraction_is_not_a_valid_latency_estimate(self):
        rows = self.samples()
        rows[2]['status'] = rows[3]['status'] = 503
        result = entry.summarize_front_window(rows, 0, 2)
        self.assertIn('failure_fraction_above_5_percent', result['invalid_reasons'])


class ReleaseVerdictTest(unittest.TestCase):
    def handoff_experiment(self, branches):
        experiment = object.__new__(handoff.HandoffExperiment)
        experiment.args = SimpleNamespace(with_failure_branches=True)
        experiment.report = {'metrics': {'admission_rejected': {'count': 1, 'attempts': 1},
            'N_cut': {'count': 0, 'accepted_streams': 1},
            'T_admission': {'value_ms': 100}, 'billing': {'verdict': 'PASS'}},
            'branches': branches}
        return experiment

    def payload(self):
        return {'entry_verdict': 'observed_no_outage', 'measurement_valid': True,
            'invalid_reasons': [], 'T_admission_front_ms': None,
            'T_admission_front_upper_bound_ms': 100,
            'switch_self_reported': {'segments_s': {'first_front_200_s': .1}},
            'switch_info': {'returncode': 0, 'elapsed_s': .2},
            'front_samples_in_window': 40, 'front_sample_gap_median_ms': 50,
            'front_rejected_samples': 0, 'front_connection_errors': 0,
            'accepted_streams': 4, 'stream_cuts': 0,
            'in_flight_stream_done_after_switch_ms': 1000,
            'baseline_streams': {p: {'status': 200, 'terminal': 'completed'}
                                 for p in ('chat', 'messages', 'responses')},
            'in_flight_stream': {'status': 200, 'terminal': 'completed'},
            'ledger': {'duplicate_request_ids': 0, 'unexpected_outcome_without_log': 0,
                       'settled_reservation_without_log': 0}}

    def entry_verdict(self, payload):
        experiment = object.__new__(entry.EntryExperiment)
        experiment.report = {'modes': {'switch-then-drain': payload}}
        return experiment._verdict()

    def test_failed_failure_branch_cannot_pass_on_good_main_path(self):
        experiment = self.handoff_experiment({'rollback': {'pass': True}, 'kill': {'pass': False}})
        self.assertEqual(experiment._verdict()['verdict'], 'ATTENTION')

    def test_missing_failure_branches_cannot_pass(self):
        self.assertEqual(self.handoff_experiment({})._verdict()['verdict'], 'ATTENTION')

    def test_all_failure_branches_verified_can_pass(self):
        experiment = self.handoff_experiment({'rollback': {'pass': True}, 'kill': {'pass': True}})
        self.assertEqual(experiment._verdict()['verdict'], 'PASS')

    def test_failed_switch_command_cannot_pass_after_entry_recovers(self):
        payload = self.payload()
        payload['switch_info']['returncode'] = 1
        self.assertFalse(self.entry_verdict(payload)['all_ok'])

    def test_failed_protocol_cannot_pass_with_zero_cuts(self):
        payload = self.payload()
        payload['baseline_streams']['messages'] = {'status': 503, 'terminal': 'http_error'}
        self.assertFalse(self.entry_verdict(payload)['all_ok'])

    def test_complete_successful_entry_can_pass(self):
        self.assertTrue(self.entry_verdict(self.payload())['all_ok'])


if __name__ == '__main__':
    unittest.main()
