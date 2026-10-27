"""Opt-in SQLite lifetime diagnostic for the isolated test harness only.

Load with pytest -p zhihui_harness.sqlite_trace. No SQL or credentials are
recorded, and weak finalizers never retain a connection or change GC timing.
"""
import json
import os
from pathlib import Path
import sqlite3
import threading
import traceback
import weakref

_original = sqlite3.connect
_lock = threading.Lock()
_current = 'session'
_counter = 0
_destination = None


def _write(record):
    if _destination is not None:
        with _lock, _destination.open('a', encoding='utf-8') as output:
            output.write(json.dumps(record, ensure_ascii=False) + '\n')


class _Connection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        global _counter
        super().__init__(*args, **kwargs)
        with _lock:
            _counter += 1
            serial = _counter
        self._trace_state = {'id': serial, 'closed': False, 'test': _current}
        _write({**self._trace_state, 'event': 'open', 'path': str(args[0]),
                'stack': traceback.format_stack(limit=12)})
        weakref.finalize(self, _finalized, self._trace_state)

    def close(self):
        super().close()
        self._trace_state['closed'] = True
        _write({**self._trace_state, 'event': 'close', 'current_test': _current})


def _finalized(state):
    _write({**state, 'event': 'finalized', 'current_test': _current})


def _connect(*args, **kwargs):
    # Respect callers supplying a custom factory, including the positional form.
    if len(args) < 6 and 'factory' not in kwargs:
        kwargs['factory'] = _Connection
    return _original(*args, **kwargs)


def pytest_configure(config):
    global _destination
    root = Path(os.environ['ZH_ISO_ROOT']).resolve()
    _destination = root / 'sqlite-lifetimes.jsonl'
    sqlite3.connect = _connect


def pytest_runtest_setup(item):
    global _current
    _current = item.nodeid


def pytest_unconfigure(config):
    sqlite3.connect = _original
