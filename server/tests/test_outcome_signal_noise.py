"""`request_outcomes` 不该被心跳与额度轮询淹没。

## 为什么单列

这不是功能缺陷，而是**信号质量问题**：2026-11-24 实测 7 天 553,415 行里
- 21,786 行是 daemon 心跳 `/api/system/internal-session-verify`（恒 204）；
- `/v1/usage` 客户端轮询 6,721 次（占全部请求 27%）。

合起来超过四分之一。分母混进这么多无意义请求之后，「错误率」永远算不准，
也就没法按比例设告警阈值 —— 而这张表本来就是唯一能回答「到底哪里在错」的地方。

处理方式刻意保守：**只在成功时不记**。失败（心跳挂了、额度查询 429/401）
照常落库，因为那恰恰是最需要看见的。
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from server import budget, budgetowner, config, db, keysvc, observability

HEARTBEAT = '/api/system/internal-session-verify'
POLL = '/v1/usage'


class RecorderPredicateTest(unittest.TestCase):
    def test_successful_heartbeat_is_not_recorded(self) -> None:
        self.assertFalse(observability._record_outcome(HEARTBEAT, 204))
        self.assertFalse(observability._record_outcome(HEARTBEAT, 200))

    def test_failing_heartbeat_is_still_recorded(self) -> None:
        self.assertTrue(observability._record_outcome(HEARTBEAT, 500))
        self.assertTrue(observability._record_outcome(HEARTBEAT, 502))

    def test_successful_quota_poll_is_not_recorded(self) -> None:
        self.assertFalse(observability._record_outcome(POLL, 200))

    def test_failing_quota_poll_is_still_recorded(self) -> None:
        self.assertTrue(observability._record_outcome(POLL, 429))
        self.assertTrue(observability._record_outcome(POLL, 401))

    def test_ordinary_endpoints_are_always_recorded(self) -> None:
        for path in ('/v1/chat/completions', '/v1/messages', '/v1/responses',
                     '/api/status', '/v1/models'):
            with self.subTest(path=path):
                self.assertTrue(observability._record_outcome(path, 200))

    def test_newapi_token_poll_is_the_same_thing_as_the_other_poll(self) -> None:
        """`/api/usage/token` 与 `/v1/usage` 是同一件事。

        2026-11-26 实测 24 小时：`/api/usage/token` 1,632 条**全是 200**，
        而 `/v1/usage` 因为已经豁免，只剩 45 条 401 失败 ——
        差别只在豁免清单里漏了一行。
        """
        self.assertFalse(observability._record_outcome('/api/usage/token', 200))
        self.assertTrue(observability._record_outcome('/api/usage/token', 401))

    def test_trailing_slash_does_not_escape_the_exemption(self) -> None:
        # 两条路由都注册了（gateway 里成对出现），尾斜杠不该绕开豁免。
        self.assertFalse(observability._record_outcome('/api/usage/token/', 200))
        self.assertFalse(observability._record_outcome('/v1/usage/', 200))
        # 归一化不能连失败的也放过。
        self.assertTrue(observability._record_outcome('/v1/usage/', 503))

    def test_unknown_status_is_treated_as_a_failure(self) -> None:
        """`http_status` 缺失时按失败处理 —— 宁可多记，不要丢信号。"""
        self.assertTrue(observability._record_outcome(HEARTBEAT, 0))


class OutcomeTableTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR, db._conn)
        root = Path(self.temporary.name)
        config.DB_PATH = root / 'fixture.db'
        config.USERS_FILE = root / 'users.json'
        config.STATIC_DIR = root / 'no-static'
        config.USERS_FILE.write_text(json.dumps({'secret': 'synthetic-outcome-only', 'users': [
            {'username': 'admin', 'role': 'admin', 'sv': 1, 'pwd_hash': 'synthetic'}]}),
            encoding='utf-8')
        db._conn = None
        db.connect()
        budget.initialize()
        observability.ensure_outcome_schema()
        from server.main import app
        self.client = TestClient(app)
        self.key = keysvc.create_key(name='synthetic-outcome', quota=1000000)

    def tearDown(self):
        self.client.close()
        budgetowner.close()
        if db._conn is not None:
            db._conn.close()
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR, db._conn = self.previous
        self.temporary.cleanup()

    def _rows(self, endpoint: str) -> int:
        row = db.query_one('SELECT count(*) n FROM request_outcomes WHERE endpoint=?',
                           (endpoint,))
        return int(row['n'])

    def test_successful_poll_writes_no_row(self) -> None:
        response = self.client.get(POLL, headers={'Authorization': f'Bearer {self.key["key"]}'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._rows(POLL), 0,
                         '成功的额度轮询不该落 request_outcomes（它是纯噪声）')

    def test_failed_poll_still_writes_a_row(self) -> None:
        response = self.client.get(POLL)          # 无密钥 → 401
        self.assertEqual(response.status_code, 401, response.text)
        self.assertGreater(self._rows(POLL), 0,
                           '失败的额度查询必须留痕：那才是要看的东西')

    def test_inference_is_always_recorded(self) -> None:
        """推理请求不受豁免影响 —— 这是它存在的理由。"""
        self.client.get('/v1/models', headers={'Authorization': f'Bearer {self.key["key"]}'})
        self.assertGreater(self._rows('/v1/models'), 0)


if __name__ == '__main__':
    unittest.main()
