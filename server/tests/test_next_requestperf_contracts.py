import ast
import asyncio
from pathlib import Path
import unittest
from unittest.mock import patch

from server import requestperf


class AdmissionPerformanceContractTest(unittest.TestCase):
    def test_admission_wait_survives_trace_serialization(self):
        trace = requestperf.Trace(started=10.0)
        with patch.object(requestperf.time, 'monotonic', side_effect=[10.0, 10.125]):
            with requestperf.measure('admission_wait', trace):
                pass
        self.assertEqual(trace.payload()['spans_ms'].get('admission_wait'), 125.0)

    def test_cancelled_wait_still_has_duration(self):
        trace = requestperf.Trace(started=10.0)
        with patch.object(requestperf.time, 'monotonic', side_effect=[10.0, 10.125]):
            with self.assertRaises(asyncio.CancelledError):
                with requestperf.measure('admission_wait', trace):
                    raise asyncio.CancelledError()
        self.assertEqual(trace.payload()['spans_ms'].get('admission_wait'), 125.0)

    def test_invalid_wait_cannot_poison_trace(self):
        trace = requestperf.Trace(started=0)
        for value in (float('nan'), float('inf'), -1):
            trace.add('admission_wait', value)
        self.assertNotIn('admission_wait', trace.payload()['spans_ms'])
        trace.add('admission_wait', 0)
        self.assertEqual(trace.payload()['spans_ms']['admission_wait'], 0)

    def test_production_fixed_measure_and_mark_names_are_registered(self):
        root = Path(requestperf.__file__).resolve().parent
        unknown = []
        callsites = 0
        for path in root.rglob('*.py'):
            if 'tests' in path.relative_to(root).parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                receiver = node.func.value
                if not ((isinstance(receiver, ast.Name) and receiver.id == 'requestperf')
                        or (isinstance(receiver, ast.Attribute) and receiver.attr == 'requestperf')):
                    continue
                kind = node.func.attr
                if kind not in ('measure', 'timed', 'mark') or not node.args:
                    continue
                name = node.args[0]
                if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
                    continue
                callsites += 1
                allowed = requestperf.MARKS if kind == 'mark' else requestperf.SPANS
                if name.value not in allowed:
                    unknown.append((str(path.relative_to(root)), node.lineno, kind, name.value))
        self.assertGreater(callsites, 0)
        self.assertEqual(unknown, [])
