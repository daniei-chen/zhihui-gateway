"""v1.2.60：待核对列表的查询效率，以及告警要说出「去哪儿处理」。

## 一、一条长期全表扫描的查询

`GET /api/budgets/review` 按 `state='unknown'` 过滤、按 `(created_at, request_id)`
排序并做游标翻页。

原先的索引是**部分索引**，键序以 `created_at` 开头、状态条件写在 `WHERE` 里。SQLite
对「状态等值 + 按 created_at 排序」这种组合没有把它选进计划。实测计划是
`SCAN + 临时 B 树排序` —— 每次翻页都全表扫一遍。

当时表 19.4 万行、`unknown` 只有 1474 行：**扫了 130 倍的无用数据**，而且随每天
新增约 5000 行继续变差。

新增的索引把状态放进键首位。实测计划变成 `SEARCH ... USING COVERING INDEX`
（连回表都省了）。在 19.4 万行的副本上建它耗时 0.63 秒，一次性。

## 二、告警说了「需要人」却没说去哪儿

这条告警长期在报「需要人工核对」，但没有给出处理入口。看到日志的人要么去翻代码，
要么忽略 —— 后者更常见。一条说「需要人」却不给入口的告警，很快会被当成背景噪音。

处理入口一直是齐的（列表、证据、核销），缺的只是把路径写出来。
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from server import operational

SOURCE = (Path(__file__).parents[1] / 'budget.py').read_text(encoding='utf-8')


SUMMARY = {
    'needs_human_old': 803,
    'review_buckets': {'new_1h': 0, 'older_1h': 803, 'older_7d': 241,
                       'upstream_rejected': 295},
    'auto_reconcilable': 0,
    'archived_pending': 671,
    'token_lower_bound': 17929389741,
}


class AlertPointsAtTheEntryPointTests(unittest.TestCase):
    def test_detail_names_the_review_endpoint(self):
        active, detail = operational.budget_unknown_alert(SUMMARY)
        self.assertTrue(active)
        self.assertIn('/api/budgets/review', detail,
                      '告警又变成只说「需要人」而不给入口了')

    def test_detail_names_the_evidence_endpoint(self):
        _active, detail = operational.budget_unknown_alert(SUMMARY)
        self.assertIn('/api/budgets/review/evidence', detail)

    def test_detail_names_the_settle_endpoint(self):
        _active, detail = operational.budget_unknown_alert(SUMMARY)
        self.assertIn('/api/budgets/{request_id}/resolve', detail)

    def test_the_counts_are_still_there(self):
        """加指引不能把原来的数字挤掉 —— 那才是判断严重程度的依据。"""
        _active, detail = operational.budget_unknown_alert(SUMMARY)
        for token in ('803', '241', '295', '671', '17929389741'):
            self.assertIn(token, detail)

    def test_no_human_rows_means_inactive(self):
        active, _detail = operational.budget_unknown_alert({'needs_human_old': 0})
        self.assertFalse(active, '没有待核对项时不该报')


class TheIndexIsDeclaredTests(unittest.TestCase):
    def test_schema_declares_the_state_scan_index(self):
        self.assertIn('idx_budget_state_scan', SOURCE)
        self.assertIn('ON budget_reservations(state,created_at,request_id)', SOURCE)

    def test_it_is_idempotent(self):
        """必须是 IF NOT EXISTS —— 启动时会反复执行建表语句。"""
        start = SOURCE.index('idx_budget_state_scan')
        head = SOURCE[max(0, start - 200):start]
        self.assertIn('CREATE INDEX IF NOT EXISTS', head)

    def test_the_reason_is_recorded(self):
        self.assertIn('130', SOURCE, '把倍数写进注释，便于日后对照')
        self.assertIn('COVERING INDEX', SOURCE)


class TheIndexActuallyGetsUsedTests(unittest.TestCase):
    """用真实的 SQLite 验证计划，而不是只看源码里有没有那行。"""

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE budget_reservations (
                request_id TEXT PRIMARY KEY, key_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL, state TEXT NOT NULL,
                reserved_tokens INTEGER NOT NULL DEFAULT 0,
                reserved_credit REAL NOT NULL DEFAULT 0,
                charged_tokens INTEGER NOT NULL DEFAULT 0,
                charged_credit REAL NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL, resolution TEXT);
            -- 旧的、没被选中的部分索引，保持与生产一致
            CREATE INDEX idx_budget_unresolved_scan
                ON budget_reservations(created_at,request_id)
                WHERE state IN ('reserved','forwarded','unknown');
        """)
        self.db.execute('CREATE INDEX idx_budget_state_scan '
                        'ON budget_reservations(state,created_at,request_id)')
        # 灌一批数据：大部分已结清，少量 unknown —— 与生产比例接近
        rows = []
        for index in range(2000):
            state = 'unknown' if index % 100 == 0 else 'settled'
            rows.append((f'r{index}', 1, 1_700_000_000 + index, state))
        self.db.executemany(
            'INSERT INTO budget_reservations(request_id,key_id,created_at,state,updated_at) '
            'VALUES(?,?,?,?,0)', rows)

    def _plan(self, sql, params=()):
        rows = self.db.execute('EXPLAIN QUERY PLAN ' + sql, params).fetchall()
        return ' | '.join(str(row[-1]) for row in rows)

    def test_the_review_query_avoids_a_full_scan(self):
        plan = self._plan(
            "SELECT * FROM budget_reservations WHERE state=? "
            "ORDER BY created_at,request_id LIMIT 501", ('unknown',))
        self.assertIn('idx_budget_state_scan', plan,
                      f'计划没有用上新索引：{plan}')
        self.assertNotIn('TEMP B-TREE', plan,
                         f'仍然在临时排序：{plan}')
        self.assertNotIn('SCAN budget_reservations', plan,
                         f'仍然在全表扫描：{plan}')

    def test_cursor_pagination_also_uses_it(self):
        """翻页带游标时同样不该退化。"""
        plan = self._plan(
            "SELECT * FROM budget_reservations WHERE state=? "
            "AND (created_at,request_id)>(?,?) "
            "ORDER BY created_at,request_id LIMIT 501",
            ('unknown', 1_700_000_500, 'r500'))
        self.assertIn('idx_budget_state_scan', plan, f'翻页退化：{plan}')

    def test_the_old_partial_index_alone_would_not_help(self):
        """记录事实：只保留旧索引时，这个查询确实用不上它。

        这条不是在测我们的代码，而是防止有人「优化」掉新索引后以为还有退路。
        """
        fresh = sqlite3.connect(':memory:')
        self.addCleanup(fresh.close)
        fresh.executescript("""
            CREATE TABLE budget_reservations (
                request_id TEXT PRIMARY KEY, key_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL, state TEXT NOT NULL,
                reserved_tokens INTEGER NOT NULL DEFAULT 0,
                reserved_credit REAL NOT NULL DEFAULT 0,
                charged_tokens INTEGER NOT NULL DEFAULT 0,
                charged_credit REAL NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL, resolution TEXT);
            CREATE INDEX idx_budget_unresolved_scan
                ON budget_reservations(created_at,request_id)
                WHERE state IN ('reserved','forwarded','unknown');
        """)
        plan = ' | '.join(
            str(row[-1]) for row in fresh.execute(
                'EXPLAIN QUERY PLAN SELECT * FROM budget_reservations '
                "WHERE state=? ORDER BY created_at,request_id LIMIT 501",
                ('unknown',)).fetchall())
        self.assertIn('TEMP B-TREE', plan,
                      f'旧索引居然够用？那新索引的前提就不成立了：{plan}')


if __name__ == '__main__':
    unittest.main()
