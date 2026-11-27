"""Real HTTP stream continuity evidence and rejection of incomplete claims."""
import asyncio
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from deploy import verify_rolling_streams as proof


class RealLoopbackCutoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_relay_holds_both_streams_across_both_route_changes(self):
        binds, original = [], socket.socket.bind

        def bind(listener, address):
            binds.append(address)
            return original(listener, address)

        with mock.patch.object(socket.socket, 'bind', bind):
            result = await proof.run_probe(hold_seconds=0.04, timeout=2)
        self.assertEqual(result['result'], 'passed', result)
        self.assertTrue(proof.validate_evidence(result))
        self.assertTrue(result['synthetic_cutover_verified'])
        self.assertFalse(result['production_zero_downtime_verified'])
        self.assertEqual(binds, [('127.0.0.1', 0)] * 3)
        self.assertEqual([item['target'] for item in result['cutovers']], ['new', 'old'])
        self.assertEqual([item['generation'] for item in result['streams']], ['old', 'new'])
        self.assertEqual([item['frames'] for item in result['streams']], [proof.FRAMES] * 2)
        self.assertEqual(result['active_after'], {'old': 0, 'new': 0})
        self.assertEqual(len(result['upstream_calls']), len(result['probes']) + 2)
        self.assertIn('server/rollingrelay.py', result['source']['files'])

    async def test_clean_http_eof_without_a_terminal_is_failure(self):
        for fault in ('truncate', 'missing-terminal'):
            with self.subTest(fault=fault):
                result = await proof.run_probe(hold_seconds=0.01, timeout=1, fault=fault)
                self.assertEqual(result['result'], 'failed')
                self.assertEqual(result['error_type'], 'EvidenceError')
                self.assertFalse(proof.validate_evidence(result))
                self.assertFalse(result['production_zero_downtime_verified'])

    async def test_out_of_order_data_is_failure_even_with_200_status(self):
        result = await proof.run_probe(hold_seconds=0.01, timeout=1, fault='sequence')
        self.assertEqual(result['result'], 'failed')
        self.assertFalse(result['synthetic_cutover_verified'])

    async def test_stalled_stream_is_bounded_and_does_not_claim_success(self):
        result = await asyncio.wait_for(
            proof.run_probe(hold_seconds=0.01, timeout=0.2, fault='stall'), 4)
        self.assertEqual(result['result'], 'failed')
        self.assertFalse(result['synthetic_cutover_verified'])
        self.assertFalse(proof.validate_evidence(result))

    async def test_task_cancellation_closes_all_fixture_listeners(self):
        listeners, original = [], socket.socket.bind
        frame_sent, provider_call = asyncio.Event(), proof._Provider.__call__

        def bind(listener, address):
            listeners.append(listener)
            return original(listener, address)

        async def provider(provider, scope, receive, send):
            async def tracked_send(message):
                await send(message)
                if b'-old-stream' in message.get('body', b''):
                    frame_sent.set()
            return await provider_call(provider, scope, receive, tracked_send)

        with mock.patch.object(socket.socket, 'bind', bind), mock.patch.object(proof._Provider, '__call__', provider):
            task = asyncio.create_task(proof.run_probe(hold_seconds=2, timeout=3))
            await asyncio.wait_for(frame_sent.wait(), 2)
            self.assertEqual(len(listeners), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(all(listener.fileno() == -1 for listener in listeners))


class EvidenceValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = asyncio.run(proof.run_probe(hold_seconds=0.02, timeout=2))
        if cls.report['result'] != 'passed':
            raise AssertionError(cls.report)

    def test_old_completed_request_counts_do_not_prove_stream_continuity(self):
        self.assertFalse(proof.validate_evidence({'requests': 20, 'failures': 0,
            'evidence_scope': 'gateway_completed_requests', 'zero_downtime_verified': False}))

    def test_proof_expires_and_cannot_be_from_the_future(self):
        created = self.report['created_at']
        self.assertTrue(proof.validate_evidence(self.report, now=created))
        self.assertFalse(proof.validate_evidence(self.report, now=created + proof.MAX_EVIDENCE_AGE + 1))
        self.assertFalse(proof.validate_evidence(self.report, now=created - 6))

    def test_truncated_digest_missing_terminal_and_replayed_post_are_rejected(self):
        for mutation in (
            lambda r: r['streams'][0].update(sha256='0' * 64),
            lambda r: r['streams'][0].update(terminal=False),
            lambda r: r['upstream_calls'].append(dict(r['upstream_calls'][0])),
            lambda r: r['streams'][0].update(frames=proof.FRAMES - 1),
            lambda r: r['streams'][1].update(generation='old'),
            lambda r: r.update(transport_errors=1),
            lambda r: r.update(active_after={'old': 1, 'new': 0}),
        ):
            altered = copy.deepcopy(self.report)
            mutation(altered)
            self.assertFalse(proof.validate_evidence(altered))

    def test_stream_finished_before_cutover_is_not_cross_cutover_evidence(self):
        altered = copy.deepcopy(self.report)
        altered['streams'][0]['last_ns'] = altered['cutovers'][0]['at_ns'] - 1
        self.assertFalse(proof.validate_evidence(altered))

    def test_success_flags_and_source_identity_are_required(self):
        for mutation in (
            lambda r: r.update(result='failed'),
            lambda r: r.update(synthetic_cutover_verified=False),
            lambda r: r.update(production_zero_downtime_verified=True),
            lambda r: r['source'].update(commit=None),
            lambda r: r['source']['files'].pop('server/rollingrelay.py'),
        ):
            altered = copy.deepcopy(self.report)
            mutation(altered)
            self.assertFalse(proof.validate_evidence(altered))

    def test_missing_probe_phase_and_missing_ownership_at_cutover_are_rejected(self):
        altered = copy.deepcopy(self.report)
        altered['probes'] = [p for p in altered['probes'] if p.get('phase') != 'after-first']
        self.assertFalse(proof.validate_evidence(altered))
        altered = copy.deepcopy(self.report)
        altered['cutovers'][0]['active']['old'] = 0
        self.assertFalse(proof.validate_evidence(altered))

    def test_validation_remains_fail_closed_under_python_optimization(self):
        code = ('from deploy.verify_rolling_streams import validate_evidence; '
                'raise SystemExit(1 if validate_evidence({}) else 0)')
        completed = subprocess.run([sys.executable, '-B', '-O', '-c', code],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_malformed_documents_return_false(self):
        for value in (None, [], True, {}, {'schema': True}, {'schema': 1, 'scope': proof.SCOPE}):
            self.assertFalse(proof.validate_evidence(value))


class ProofCliTests(unittest.TestCase):
    def test_cli_only_runs_temporary_synthetic_endpoints_and_writes_private_report(self):
        tool = Path(__file__).resolve().parents[2] / 'deploy' / 'verify_rolling_streams.py'
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'proof.json'
            result = subprocess.run([sys.executable, '-B', '-I', str(tool),
                '--hold-seconds', '0.02', '--timeout', '2', '--output', str(output)],
                capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text())
            self.assertTrue(proof.validate_evidence(report))
            self.assertFalse(output.stat().st_mode & 0o077)
            before = output.read_bytes()
            def completed_probe(coroutine):
                coroutine.close()
                return report
            with mock.patch.object(proof.asyncio, 'run', side_effect=completed_probe), redirect_stdout(io.StringIO()):
                self.assertEqual(proof.main(['--output', str(output)]), 1)
            self.assertEqual(output.read_bytes(), before)
            with mock.patch.object(proof.socket, 'socket', side_effect=AssertionError('no listeners during validation')), redirect_stdout(io.StringIO()):
                self.assertEqual(proof.main(['--verify-evidence', str(output)]), 0)
                report['created_at'] -= proof.MAX_EVIDENCE_AGE + 1
                output.write_text(json.dumps(report))
                self.assertEqual(proof.main(['--verify-evidence', str(output)]), 1)

    def test_probe_bounds_are_enforced(self):
        for kwargs in ({'hold_seconds': 0}, {'hold_seconds': 11}, {'timeout': 0}, {'timeout': 61}):
            with self.assertRaises(ValueError):
                asyncio.run(proof.run_probe(**kwargs))


if __name__ == '__main__':
    unittest.main()
