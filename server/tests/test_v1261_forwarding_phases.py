"""v1.2.61：转发阶段链路的读取入口。

## 为什么补这个

`forwarding_events` 从上线起就在写 —— 每次转发记下选号、失败换号、拿到响应头、
流终止等阶段，还带不可改、不可删的触发器，是设计上的审计留痕。

**但代码里没有任何一处读它。** 2026-11-14 审计扫了全部表，它是四张「只写不读」表里
最大的那张：**38 万行、约 89 MB**，每天还在长。

留痕没有读取入口等于没有留痕 —— 真出问题时只能手工开库翻。

这个端点与已有的「结局」「尝试」「供应商尝试」并列，补齐最后一段。
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi import HTTPException

from server.routers import logs

ROW = {
    'attempt': 1, 'phase': 'selected', 'ts': 1_700_000_000,
    'upstream_id': 'default', 'requested_model': 'gpt-x',
    'resolved_model': 'gpt-x-resolved', 'realm': 'cn', 'protocol': 'openai',
    'transform': None, 'outcome': None,
}


def rows_from(values):
    """模拟 db.query：返回带 .keys() 的映射对象。"""
    return [dict(value) for value in values]


class ForwardingPhasesTests(unittest.TestCase):
    def _call(self, values):
        with mock.patch.object(logs.db, 'query', return_value=rows_from(values)):
            return logs.forwarding('rid-1', user={'username': 'admin'})

    def test_it_summarises_a_single_attempt(self):
        result = self._call([ROW, dict(ROW, phase='headers', ts=1_700_000_001)])
        self.assertEqual(result['request_id'], 'rid-1')
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(result['attempt_numbers'], [1])
        self.assertEqual(result['last_phase'], 'headers')
        self.assertEqual(result['elapsed_ms'], 1000)

    def test_it_reports_multiple_attempts(self):
        """换过号要看得出来 —— 这正是排查「为什么慢」时最想知道的事。"""
        result = self._call([
            dict(ROW, attempt=1, phase='selected'),
            dict(ROW, attempt=1, phase='capacity_lost', ts=1_700_000_002),
            dict(ROW, attempt=2, phase='selected', ts=1_700_000_003),
            dict(ROW, attempt=2, phase='headers', ts=1_700_000_004),
        ])
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['attempt_numbers'], [1, 2])
        self.assertEqual(result['last_phase'], 'headers')

    def test_empty_result_is_explicit_not_an_error(self):
        """没记录要说清「可能没到转发阶段」，而不是返回一个空对象让人猜。"""
        result = self._call([])
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['phases'], [])
        self.assertIn('未到达转发阶段', result['note'])

    def test_it_never_returns_the_config_hash(self):
        """内部标识对定位问题没帮助，却会漏实现细节。"""
        with mock.patch.object(logs.db, 'query', return_value=rows_from([ROW])) as query:
            result = logs.forwarding('rid-1', user={'username': 'admin'})
        sql = query.call_args[0][0]
        self.assertNotIn('config_hash', sql, '查询里带上了内部标识')
        self.assertNotIn('config_hash', result['phases'][0])

    def test_it_selects_only_named_columns(self):
        """不要 `SELECT *` —— 表结构以后加列会顺手漏出去。"""
        with mock.patch.object(logs.db, 'query', return_value=rows_from([ROW])) as query:
            logs.forwarding('rid-1', user={'username': 'admin'})
        sql = query.call_args[0][0]
        self.assertNotIn('SELECT *', sql)

    def test_it_queries_by_request_id_with_ordering(self):
        with mock.patch.object(logs.db, 'query', return_value=rows_from([ROW])) as query:
            logs.forwarding('rid-1', user={'username': 'admin'})
        sql, params = query.call_args[0]
        self.assertIn('WHERE request_id=?', sql)
        self.assertIn('ORDER BY attempt,ts', sql)
        self.assertEqual(params, ('rid-1',))

    def test_missing_request_id_is_a_400(self):
        for bad in ('', 'x' * 129):
            with self.assertRaises(HTTPException) as caught:
                logs.forwarding(bad, user={'username': 'admin'})
            self.assertEqual(caught.exception.status_code, 400)

    def test_it_requires_a_panel_reader(self):
        """端点必须挂权限依赖，不能裸奔。"""
        import inspect
        signature = inspect.signature(logs.forwarding)
        self.assertIn('user', signature.parameters)
        source = inspect.getsource(logs.forwarding)
        self.assertIn('require_panel_reader', source.split('\n')[0] + source)


if __name__ == '__main__':
    unittest.main()
