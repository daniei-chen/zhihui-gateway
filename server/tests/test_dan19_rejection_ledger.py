"""DAN-19：拒绝请求的不可变账本写入移出返回路径 + usage_events 有归档出口。

两条断言都先红后绿（修前实测输出见 DAN-19 回帖）：

1. `test_rejection_response_does_not_wait_for_ledger_write`
   把 `ledger.record` 换成慢实现（0.5s），打一个**会被拒绝**的请求
   （`keysvc.validate` 的 `quota_exhausted` 分支）。拒绝路径若同步写不可变账本，
   响应就会跟着慢实现走；修后响应 < 0.1s，账本行由后台补齐且只补一次。
2. `test_usage_events_has_archive_outlet`
   `archiveworker.run_once()` 必须把 `usage_events` 导出成归档文件、取回校验通过、
   且**不删源行**（不可变表只导出快照）；保留期可用 `config.USAGE_EVENTS_ARCHIVE_DAYS`
   调整。

断言的都是行为，不断言源码文本。
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from fastapi import FastAPI
import httpx

from server import archiveworker, config, db, ledger, observability
from server.routers import gateway
from server.tests.authorization_test_support import isolated_data


class _ExecutionId:
    """给测试请求一个固定的执行 ID（生产里由 RequestAuditMiddleware 提供）。

    没有执行 ID 时 `ledger.record` 直接 return，测不出「拒绝路径同步写账本」。
    """

    def __init__(self, app, execution_id: str) -> None:
        self.app = app
        self.execution_id = execution_id

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        token = observability.current.set(observability.RequestAudit(
            self.execution_id, '/v1/chat/completions', 'POST', 'openai', time.monotonic()))
        try:
            return await self.app(scope, receive, send)
        finally:
            observability.current.reset(token)


def _quota_exhausted_key() -> str:
    """建一把配额已用尽的密钥，返回它的明文。"""
    from server import keysvc
    created = keysvc.create_key('dan19-rejection', quota=1000)
    db.execute('UPDATE api_keys SET used_tokens=1000 WHERE id=?', (created['id'],))
    return created['key']


class RejectionLedgerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(isolated_data())
        self.token = _quota_exhausted_key()
        self.app = FastAPI()
        self.app.include_router(gateway.router)

    async def _reject(self, execution_id: str) -> tuple[int, float]:
        app = _ExecutionId(self.app, execution_id)
        transport = httpx.ASGITransport(app=app, client=('203.0.113.9', 1234))
        async with httpx.AsyncClient(transport=transport, base_url='http://fixture') as client:
            started = time.monotonic()
            response = await client.post(
                '/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.token}'},
                json={'model': 'synthetic', 'messages': [{'role': 'user', 'content': 'hi'}]},
            )
            return response.status_code, time.monotonic() - started

    async def test_rejection_response_does_not_wait_for_ledger_write(self) -> None:
        """修前：拒绝路径在事务里同步调 ledger.record，慢实现直接拖慢响应。"""
        original = ledger.record

        def slow_record(*args, **kwargs):
            time.sleep(0.5)
            return original(*args, **kwargs)

        with mock.patch.object(ledger, 'record', slow_record):
            status, elapsed = await self._reject('dan19-slow-ledger')

        self.assertEqual(status, 429)
        self.assertLess(elapsed, 0.1,
                        f'拒绝路径仍被账本写入拖住：{elapsed:.3f}s')

        # 慢实现确实被调用过（账本行没有丢），只是不在请求返回路径上。
        self.assertEqual(
            db.query_one('SELECT COUNT(*) c FROM request_logs WHERE request_id=?',
                         ('dan19-slow-ledger',))['c'], 1)

        from server import ledgerbackfill
        self.assertEqual(ledgerbackfill.backfill(), 1)
        row = db.query_one('SELECT terminal_state, counted FROM usage_events WHERE request_id=?',
                           ('dan19-slow-ledger',))
        self.assertIsNotNone(row, '拒绝请求的不可变账本行没有被补齐')
        self.assertEqual(row['terminal_state'], 'rejected')
        self.assertEqual(row['counted'], 0)
        # 幂等：再补一次不产生第二行。
        self.assertEqual(ledgerbackfill.backfill(), 0)
        self.assertEqual(
            db.query_one('SELECT COUNT(*) c FROM usage_events WHERE request_id=?',
                         ('dan19-slow-ledger',))['c'], 1)

    async def test_backfill_replays_a_crash_lost_row_exactly_once(self) -> None:
        """崩溃不丢、重放不重复：只留下 request_logs（持久事实）也能补回账本行。"""
        status, _ = await self._reject('dan19-crash')
        self.assertEqual(status, 429)

        from server import ledgerbackfill
        # 这次拒绝请求自己的账本行也是延后写的，先把它补齐 —— 下面只观察
        # 「崩溃行」这一条，否则计数会把两条混在一起。
        self.assertEqual(ledgerbackfill.backfill(), 1)
        # 模拟「请求日志已提交、账本行还没写就崩了」：先把账本行删掉不可能
        # （不可变表有 no_delete 触发器），所以直接构造一条只有 request_logs 的
        # 事实，等价于崩溃现场。
        db.execute(
            "INSERT INTO request_logs(ts,key_id,model,status,request_id,terminal_state,"
            "prompt_tokens,completion_tokens,http_status,realm,release_id) "
            "VALUES(?,?,?,?,?,?,0,0,?,?,?)",
            (int(time.time()), None, 'synthetic', 429, 'dan19-crash-row', 'rejected',
             429, 'cn', observability.RELEASE_ID))
        self.assertIsNone(db.query_one(
            'SELECT event_id FROM usage_events WHERE request_id=?', ('dan19-crash-row',)))

        self.assertEqual(ledgerbackfill.backfill(), 1)
        self.assertEqual(ledgerbackfill.backfill(), 0)
        self.assertEqual(
            db.query_one('SELECT COUNT(*) c FROM usage_events WHERE request_id=?',
                         ('dan19-crash-row',))['c'], 1)


class UsageEventsArchiveOutletTest(unittest.TestCase):
    """`usage_events` 是不可变表：不能删，但必须有归档出口。"""

    def setUp(self) -> None:
        self.enterContext(isolated_data())
        self.out = os.path.join(os.path.dirname(str(config.DB_PATH)), 'archives')
        db.connect()
        from server import budget
        budget.initialize()
        self.addCleanup(self._close)

    def _close(self) -> None:
        if db._conn is not None:
            db._conn.close()
            db._conn = None

    def _insert_event(self, request_id: str, ts: int) -> None:
        db.execute(
            'INSERT INTO usage_events(event_id,request_id,attempt,event_type,ts,key_id,'
            'model,realm,prompt_tokens,completion_tokens,credit_units,counted,'
            'terminal_state,provenance,release_id) '
            "VALUES(?,?,1,'usage',?,NULL,'synthetic','cn',3,4,?,1,'completed',"
            "'provider-reported',NULL)",
            (f'{request_id}:1:usage', request_id, ts, 500))

    def test_usage_events_is_exported_without_deleting_source_rows(self) -> None:
        old = int(time.time()) - 400 * 86400
        self._insert_event('dan19-old', old)
        self._insert_event('dan19-fresh', int(time.time()))

        result = archiveworker.run_once(config.DB_PATH, self.out)
        self.assertTrue(result['ok'], result)

        runs = db.query("SELECT * FROM archive_runs WHERE dataset='usage_events' "
                        "AND state='verified'")
        self.assertTrue(runs, 'usage_events 没有归档出口：archive_runs 里没有记录')
        exported = os.path.join(self.out, runs[0]['path'].rsplit('/', 1)[-1])
        with open(exported, encoding='utf-8') as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual([row['request_id'] for row in rows], ['dan19-old'])
        # 不可变表：导出不等于删除，源行必须还在。
        self.assertEqual(
            db.query_one('SELECT COUNT(*) c FROM usage_events')['c'], 2)

    def test_retention_window_is_configurable(self) -> None:
        old = int(time.time()) - 40 * 86400
        self._insert_event('dan19-window', old)
        with mock.patch.object(config, 'USAGE_EVENTS_ARCHIVE_DAYS', 30):
            archiveworker.run_once(config.DB_PATH, self.out)
        runs = db.query("SELECT * FROM archive_runs WHERE dataset='usage_events'")
        self.assertTrue(runs, '配置的保留期没有生效：usage_events 仍未归档')
        self.assertEqual(
            db.query_one('SELECT COUNT(*) c FROM usage_events')['c'], 1)


if __name__ == '__main__':
    unittest.main()
