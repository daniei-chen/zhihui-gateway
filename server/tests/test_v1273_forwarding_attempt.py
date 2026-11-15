"""v1.2.73：转发留痕里的 `attempt` 曾硬编码为 1。

## 问题

`ledger.forwarding()` 原本这样写：

```python
db.execute('INSERT OR IGNORE INTO forwarding_events VALUES(?,?,?,...)',
           (state.request_id, 1, phase, ...))   # ← attempt 恒为 1
```

而这张表的唯一约束是 `(request_id, attempt, phase)`。于是**重试时同一个 phase 的
第二条会被 `INSERT OR IGNORE` 静默丢掉**。

后果：这个字段存在，但**永远说「只试了一次」**。想知道某个请求重试了几次，从这张表
里查不到；而本该记录真实次数的 `attempt_events` 从 2026-11-03 起就没人写了。

**两个来源都没有真实次数 —— 这个维度完全不可观测。**

## 修法

优先从 state 读调用方设好的当前尝试序号，读不到退回 1（与改动前一致）。
重试的两条路径（上游拒绝、连接失败）都会把它写到 state 上。
"""
from __future__ import annotations

import pathlib
import unittest
from unittest import mock

from server import ledger


class _State:
    request_id = 'r-1'
    upstream_id = 'default'
    requested_model = 'm'
    resolved_model = 'M'
    realm = 'cn'
    protocol = 'openai'
    transform = None
    config_hash = None


class ForwardingAttemptTests(unittest.TestCase):
    """`ledger.forwarding` 内部是 `from . import db`，所以是要 patch `server.db.execute`。"""

    def setUp(self):
        self.calls = []
        self._patcher = mock.patch('server.db.execute',
                                   side_effect=lambda sql, args: self.calls.append((sql, args)))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def _attempt_for(self, value):
        state = _State()
        if value is not None:
            state.forwarding_attempt = value
        self.calls.clear()
        ledger.forwarding(state, 'prepared')
        self.assertEqual(len(self.calls), 1)
        return self.calls[0][1][1]

    def test_missing_field_defaults_to_one(self):
        self.assertEqual(self._attempt_for(None), 1)

    def test_real_attempt_numbers_are_preserved(self):
        for attempt in (1, 2, 3, 32):
            self.assertEqual(self._attempt_for(attempt), attempt)

    def test_out_of_range_falls_back_to_one(self):
        for attempt in (0, -1, 33, 999):
            self.assertEqual(self._attempt_for(attempt), 1)

    def test_garbage_falls_back_to_one(self):
        for attempt in ('x', '', [], {}, 1.5):
            self.assertEqual(self._attempt_for(attempt), 1)

    def test_bool_is_not_accepted_as_a_number(self):
        """`True` 在 Python 里是 int，但作为尝试序号没有意义。"""
        self.assertEqual(self._attempt_for(True), 1)


class TheUniqueConstraintCanNowHoldRetriesTests(unittest.TestCase):
    """真数据库上验证：不同 attempt 的同一 phase 能共存。"""

    def test_same_phase_at_different_attempts_both_persist(self):
        import sqlite3
        conn = sqlite3.connect(':memory:')
        conn.execute(
            'CREATE TABLE forwarding_events (request_id TEXT, attempt INTEGER, '
            'phase TEXT, PRIMARY KEY (request_id, attempt, phase))')
        for attempt in (1, 2):
            conn.execute(
                'INSERT OR IGNORE INTO forwarding_events VALUES(?,?,?)',
                ('r-1', attempt, 'prepared'))
        rows = conn.execute(
            'SELECT attempt FROM forwarding_events WHERE request_id=? ORDER BY attempt',
            ('r-1',)).fetchall()
        conn.close()
        self.assertEqual([r[0] for r in rows], [1, 2],
                         '不同 attempt 的同一阶段必须能共存，否则重试记录还是会丢')

    def test_the_old_hardcoded_value_would_have_lost_one(self):
        """反证：两次都写 attempt=1 时，第二条必定被丢弃。"""
        import sqlite3
        conn = sqlite3.connect(':memory:')
        conn.execute(
            'CREATE TABLE forwarding_events (request_id TEXT, attempt INTEGER, '
            'phase TEXT, PRIMARY KEY (request_id, attempt, phase))')
        for _ in range(2):
            conn.execute('INSERT OR IGNORE INTO forwarding_events VALUES(?,?,?)',
                         ('r-1', 1, 'prepared'))
        count = conn.execute('SELECT count(*) FROM forwarding_events').fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, '这正是旧写法丢记录的方式')


class RetryPathsMarkTheStateTests(unittest.TestCase):
    SOURCE = (pathlib.Path(__file__).parents[1] / 'routers' / 'gateway.py').read_text(encoding='utf-8')

    def test_both_retry_paths_set_the_field(self):
        self.assertEqual(self.SOURCE.count('forwarding_attempt = attempt'), 2,
                         '上游拒绝与连接失败两条重试路径都要标记')

    def test_the_field_name_matches_what_the_ledger_reads(self):
        ledger_source = (pathlib.Path(__file__).parents[1] / 'ledger.py').read_text(encoding='utf-8')
        self.assertIn('forwarding_attempt', ledger_source)
        self.assertIn('forwarding_attempt', self.SOURCE)


if __name__ == '__main__':
    unittest.main()
