"""K25 · request_logs 真实查询形状的索引计划测试（本地临时库）。

这些断言只依赖 `EXPLAIN QUERY PLAN` 与索引存在性，不依赖具体耗时（耗时证据见
`tools/k25_log_index_bench.py` 的实测输出与 `design/manager/K25-日志查询与索引.md`）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402

COUNT_SQL = 'SELECT COUNT(*) AS c FROM request_logs l {clause}'
LIST_SQL = ('SELECT l.*, k.name AS key_name FROM request_logs l '
            'LEFT JOIN api_keys k ON k.id = l.key_id {clause} '
            'ORDER BY l.id DESC LIMIT ? OFFSET ?')


class LogQueryPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k25.db'
        db._conn = None
        db.connect()
        db.executemany(
            'INSERT INTO api_keys(id,name,key_hash,prefix,created_at) VALUES(?,?,?,?,?)',
            [(i, f'k{i}', f'h{i}', f'p{i}', 0) for i in range(1, 21)])
        rows = []
        for i in range(3000):
            rows.append((1700000000 + i, (i % 20) + 1, '10.0.0.1', 'deepseek-v3',
                         'deepseek-v3', 200, 100, 50, 900, 120, 'ua', None, 1, 0.01,
                         'global' if i % 3 == 0 else 'cn', None, None, None, None,
                         f'rid-{i}', '/v1/chat/completions', 'POST', 'openai', 200,
                         'completed', None, 'test', 'upstream', 1, 120, 'workbuddy'))
        db.executemany(
            'INSERT INTO request_logs(ts,key_id,ip,model,mapped_model,status,'
            'prompt_tokens,completion_tokens,latency_ms,first_token_ms,ua,error,stream,'
            'credit,realm,account,cache_hit_tokens,cache_miss_tokens,cache_write_tokens,'
            'request_id,endpoint,method,protocol,http_status,terminal_state,error_stage,'
            'release_id,usage_source,usage_complete,first_byte_ms,platform) '
            'VALUES(' + ','.join('?' * 31) + ')', rows)

    def tearDown(self) -> None:
        try:
            db._conn.close()  # type: ignore[union-attr]
        finally:
            db._conn = None
            config.DB_PATH = self._orig
            self._tmp.cleanup()

    def _plan(self, sql: str, args: tuple = ()) -> str:
        return ' <- '.join(str(r['detail']) for r in db.query('EXPLAIN QUERY PLAN ' + sql, args))

    # ── 索引确实存在 ────────────────────────────────────────────────
    def test_indexes_exist(self) -> None:
        names = {str(r['name']) for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn('idx_logs_key_id_id', names)
        self.assertIn('idx_logs_unfilled_ts', names)
        # 实测会让列表页退化到临时 B 树排序的两个索引**不许**出现在候选实现里
        self.assertNotIn('idx_logs_realm_id', names)
        self.assertNotIn('idx_logs_status_ts', names)

    # ── 密钥过滤：列表与计数都要走索引，且不排序 ────────────────────
    def test_key_id_filter_uses_index_without_sort(self) -> None:
        plan = self._plan(LIST_SQL.format(clause='WHERE l.key_id = ?'), (7, 20, 0))
        self.assertIn('idx_logs_key_id_id', plan)
        self.assertNotIn('TEMP B-TREE', plan, f'列表页不应再排序：{plan}')
        count = self._plan(COUNT_SQL.format(clause='WHERE l.key_id = ?'), (7,))
        self.assertIn('idx_logs_key_id_id', count)
        self.assertIn('COVERING INDEX', count, f'计数应走覆盖索引：{count}')

    def test_key_id_deep_offset_still_uses_index(self) -> None:
        plan = self._plan(LIST_SQL.format(clause='WHERE l.key_id = ?'), (7, 20, 2000))
        self.assertIn('idx_logs_key_id_id', plan)
        self.assertNotIn('TEMP B-TREE', plan)

    # ── 无筛选列表：保持「反向 rowid 扫描 + LIMIT 提前退出」──────────
    def test_unfiltered_list_does_not_sort(self) -> None:
        plan = self._plan(LIST_SQL.format(clause=''), (20, 0))
        self.assertNotIn('TEMP B-TREE', plan, f'无筛选列表不应排序：{plan}')

    # ── realm / status 过滤：当前查询文本下不加有害索引 ──────────────
    def test_realm_or_null_filter_does_not_sort(self) -> None:
        plan = self._plan(LIST_SQL.format(clause='WHERE (l.realm = ? OR l.realm IS NULL)'),
                          ('cn', 20, 0))
        self.assertNotIn('TEMP B-TREE', plan,
                         f'realm 过滤不应引入排序（那会把 0.15ms 变成 157ms）：{plan}')

    def test_status_range_filter_does_not_sort(self) -> None:
        plan = self._plan(
            LIST_SQL.format(clause='WHERE l.status >= 200 AND l.status < 300'), (20, 0))
        self.assertNotIn('TEMP B-TREE', plan)

    # ── 既有索引不被破坏：days 过滤仍走 ts 索引 ─────────────────────
    def test_days_filter_still_uses_ts_index(self) -> None:
        count = self._plan(COUNT_SQL.format(clause='WHERE l.ts >= ?'), (0,))
        self.assertIn('idx_logs_ts', count)

    def test_account_backfill_uses_partial_index(self) -> None:
        plan = self._plan(
            'SELECT id FROM request_logs WHERE account IS NULL AND ts BETWEEN ? AND ? '
            'AND (mapped_model = ? OR model = ?) ORDER BY ABS(ts - ?) LIMIT 1',
            (0, 10 ** 10, 'deepseek-v3', 'deepseek-v3', 0))
        self.assertIn('idx_logs_unfilled_ts', plan, f'账号回填应走部分索引：{plan}')


if __name__ == '__main__':
    unittest.main()
