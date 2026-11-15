"""v1.2.41：用量累计并成一条语句；请求日志幂等交给唯一索引。

`bump_usage` 原先写 6 条语句：两条 UPSERT，然后 4 条 `UPDATE` 把 `credit_units`
加上去、再用它反推展示用的 `credit`。那 4 条都在读改写同一行。

`request_logs` 的幂等原先靠"先 SELECT 再 INSERT"，而唯一索引本来就保证这件事。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from server import db


class BumpUsageWritesOncePerRowTests(unittest.TestCase):
    def test_two_upserts_and_nothing_else(self):
        with mock.patch.object(db, 'execute') as execute:
            db.bump_usage(1, 'model-a', 10, 20, credit=0.5)
        self.assertEqual(execute.call_count, 2,
                         '日表与小时表各一条 UPSERT，不应再有额外的 UPDATE')

    def test_both_statements_carry_credit_units(self):
        with mock.patch.object(db, 'execute') as execute:
            db.bump_usage(1, 'model-a', 10, 20, credit=0.5)
        for call in execute.call_args_list:
            sql = call.args[0]
            self.assertIn('credit_units', sql)
            self.assertIn('ON CONFLICT', sql)

    def test_credit_is_derived_from_units_in_the_conflict_branch(self):
        """`credit` 必须由 `credit_units` 派生，两者不能各算各的。"""
        with mock.patch.object(db, 'execute') as execute:
            db.bump_usage(1, 'model-a', 10, 20, credit=0.5)
        for call in execute.call_args_list:
            sql = call.args[0]
            self.assertIn('credit = (coalesce(credit_units, 0) + excluded.credit_units)', sql)

    def test_no_leftover_update_statements_in_the_module(self):
        source = (Path(__file__).parents[1] / 'db.py').read_text(encoding='utf-8')
        start = source.index('def bump_usage(')
        body = source[start:start + 4000]
        self.assertNotIn("UPDATE usage_daily SET credit", body)
        self.assertNotIn("UPDATE usage_hourly SET credit", body)


class RequestLogIdempotencyTests(unittest.TestCase):
    def test_insert_is_or_ignore_and_returns_rowcount(self):
        source = (Path(__file__).parents[1] / 'db.py').read_text(encoding='utf-8')
        start = source.index('def add_request_log(')
        body = source[start:start + 2500]
        self.assertIn('INSERT OR IGNORE INTO request_logs', body)
        self.assertIn('inserted = execute_count(', body)
        self.assertIn('return inserted', body)

    def test_settlement_uses_the_return_value_instead_of_selecting_first(self):
        source = (Path(__file__).parents[1] / 'routers' / 'gateway.py').read_text(encoding='utf-8')
        self.assertNotIn("db.query_one('SELECT id FROM request_logs WHERE request_id=?'", source,
                         '幂等不该再靠一次额外的 SELECT')
        self.assertIn('inserted = db.add_request_log(**facts)', source)
        self.assertIn('if state and not inserted:', source)


if __name__ == '__main__':
    unittest.main()
