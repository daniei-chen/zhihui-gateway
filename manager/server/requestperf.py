"""Payload-free monotonic spans. Nested spans are not additive latency phases."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import math
import threading
import time
from functools import wraps

SCHEMA = '''
CREATE TABLE IF NOT EXISTS request_performance(
 request_id TEXT PRIMARY KEY, ts INTEGER NOT NULL, protocol TEXT NOT NULL,
 terminal_state TEXT, timings TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_performance_ts ON request_performance(ts);
'''
SPANS = frozenset(('preauthorization', 'admission_wait', 'body_receive', 'json_parse', 'forward_prepare',
                   'json_encode', 'budget_prepare', 'transport_slot_wait',
                   'transport_headers', 'connect', 'tls', 'request_send',
                   'headers_wait', 'settlement', 'finalize'))
MARKS = frozenset(('body_received', 'forward_prepared', 'first_useful', 'response_headers'))
_lock = threading.Lock()
_totals = {}
_writes = 0


@dataclass
class Trace:
    started: float
    spans: dict = field(default_factory=dict)
    marks: dict = field(default_factory=dict)
    sizes: dict = field(default_factory=dict)

    def add(self, name, seconds):
        if name in SPANS and math.isfinite(seconds) and seconds >= 0:
            self.spans[name] = self.spans.get(name, 0.0) + seconds * 1000

    def mark(self, name):
        if name in MARKS and name not in self.marks:
            self.marks[name] = max(0.0, (time.monotonic() - self.started) * 1000)

    def size(self, name, value):
        if name in ('received_bytes', 'forward_bytes') and type(value) is int and value >= 0:
            self.sizes[name] = value

    def payload(self):
        return {'version': 1, 'spans_ms': {k: round(v, 3) for k, v in self.spans.items()},
                'marks_ms': {k: round(v, 3) for k, v in self.marks.items()},
                'sizes': dict(self.sizes)}


def current():
    from . import observability
    return getattr(observability.current.get(), 'performance', None)


@contextmanager
def measure(name, trace=None):
    trace = trace if trace is not None else current()
    if trace is None:
        yield
        return
    began = time.monotonic()
    try:
        yield
    finally:
        trace.add(name, time.monotonic() - began)


def mark(name):
    trace = current()
    if trace is not None:
        trace.mark(name)


def timed(name):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with measure(name):
                return function(*args, **kwargs)
        return call
    return decorate


def record(state):
    global _writes
    trace = getattr(state, 'performance', None)
    if trace is None:
        return
    from . import db
    payload = trace.payload()
    db.execute('INSERT OR IGNORE INTO request_performance VALUES(?,?,?,?,?)',
               (state.request_id, int(time.time()), state.protocol, state.terminal_state,
                json.dumps(payload, separators=(',', ':'), allow_nan=False)))
    with _lock:
        for name, value in payload['spans_ms'].items():
            count, total, largest = _totals.get(name, (0, 0.0, 0.0))
            _totals[name] = (count + 1, total + value, max(largest, value))
        _writes += 1
        prune = _writes % 2000 == 0
    if prune:
        db.execute('DELETE FROM request_performance WHERE request_id IN '
                   '(SELECT request_id FROM request_performance WHERE ts<? ORDER BY ts LIMIT 2000)',
                   (int(time.time()) - 7 * 86400,))


def snapshot():
    with _lock:
        spans = {name: {'count': n, 'total_ms': round(total, 3),
                        'mean_ms': round(total / n, 3), 'max_ms': round(largest, 3)}
                 for name, (n, total, largest) in _totals.items()}
    return {'scope': 'current_process', 'spans': spans,
            'note': 'Monotonic nested spans; do not sum overlapping spans or interpret first_useful as visible text.'}
