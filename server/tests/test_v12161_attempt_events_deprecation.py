"""v1.2.161：给已停止写入的 `attempt_events` 标明它的继任者。

## 为什么

2026-11-15 排查计费事实时，看到 `attempt_billing_facts` 最近 24 小时 100% 是 `unknown`，
于是一路查到 `attempt_events` —— 那张表最近五天**一条数据都没有**。

当时的推断是「采集器停了十二天」。**这个推断是错的。**
真相是上游从 11-03 起在事件里带上 `dispatch_id`，写入被有意交给了新表
`provider_attempt_events_v2`（13 万行，当天还在增长）。

**而旧表的 schema 里一个字都没写这件事。** 所以按表名查的人只能得出「没有 attempt」，
然后再花十几步去证伪它。

## 为什么不删表

11-03 之前的历史在里面，而且不可变（表上有阻止 UPDATE/DELETE 的触发器）。
**删表等于销毁审计记录。** 留着 + 写清楚，成本最低。

## 本测试守什么

**最重要的一条是「SQL 还能执行」。**

这段说明是写进 SQL 字符串里的注释 —— **注释写错（比如混进未转义的引号、
或者让某条语句断开）会让整段 schema 执行失败**，而那意味着服务起不来。
只检查 Python 语法是不够的：字符串内部出错时 Python 语法完全正常。

所以这里**真的把 schema 丢进内存库执行一遍**，而不是 grep 字符串。
"""
from __future__ import annotations

import ast
import sqlite3
import unittest
from pathlib import Path

SOURCE_PATH = Path(__file__).resolve().parents[1] / 'ledger.py'
SOURCE = SOURCE_PATH.read_text(encoding='utf-8')


def _schema_literal() -> str:
    """从源码里取出 `SCHEMA` 常量的值，不 import 模块（避免连带副作用）。"""
    for node in ast.parse(SOURCE).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'SCHEMA':
                    return ast.literal_eval(node.value)
    raise AssertionError('没找到 SCHEMA 常量')


class TheSchemaStillRunsTests(unittest.TestCase):
    """注释写坏 SQL，服务就起不来。这条必须真的执行。"""

    def test_python_syntax(self):
        ast.parse(SOURCE)

    def test_schema_executes_cleanly(self):
        schema = _schema_literal()
        conn = sqlite3.connect(':memory:')
        try:
            conn.executescript(schema)       # 失败会直接抛，测试红
        finally:
            conn.close()

    def test_attempt_events_still_created(self):
        conn = sqlite3.connect(':memory:')
        try:
            conn.executescript(_schema_literal())
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn('attempt_events', names,
                      '加了注释之后表不见了 —— 语句可能被注释吃掉')


class TheDeprecationIsDocumentedTests(unittest.TestCase):
    def test_it_says_writing_stopped(self):
        self.assertIn('已停止写入', _schema_literal())

    def test_it_names_the_successor(self):
        self.assertIn('provider_attempt_events_v2', _schema_literal())

    def test_it_says_why_history_is_kept(self):
        schema = _schema_literal()
        self.assertIn('审计记录', schema,
                      '没说明为什么保留旧表，后人会想删掉它')

    def test_it_records_the_actual_misjudgement(self):
        """把「差点据此判断采集停了十二天」写下来，这是最有价值的一句。"""
        schema = _schema_literal()
        self.assertTrue('十二天' in schema or '12 天' in schema,
                        '没写出这次误判，下一个人还会重走')


class ItDoesNotChangeBehaviourTests(unittest.TestCase):
    def test_immutability_triggers_still_fire(self):
        """不可变约束必须还在 —— 旧表虽然是历史，仍不许改。

        这里**真的在库里试一次 UPDATE**，而不是数触发器名字出现几次：
        名字对但触发器建在别的表上，数字符照样过。
        """
        conn = sqlite3.connect(':memory:')
        try:
            conn.executescript(_schema_literal())
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertIn('attempt_events_no_update', names, '更新保护触发器不见了')
            self.assertIn('attempt_events_no_delete', names, '删除保护触发器不见了')
            conn.execute("INSERT INTO attempt_events VALUES"
                         "('r1',1,'selected',1,'uid','m','cn',200,'')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE attempt_events SET status=500")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM attempt_events")
        finally:
            conn.close()

    def test_table_columns_unchanged(self):
        conn = sqlite3.connect(':memory:')
        try:
            conn.executescript(_schema_literal())
            cols = [row[1] for row in conn.execute('PRAGMA table_info(attempt_events)')]
        finally:
            conn.close()
        self.assertEqual(cols, ['request_id', 'attempt', 'phase', 'ts',
                                'account_uid', 'resolved_model', 'realm',
                                'status', 'config_hash'],
                         '列定义被改动了 —— 这一版只该加注释')


if __name__ == '__main__':
    unittest.main()
