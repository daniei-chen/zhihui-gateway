"""「上游明确拒绝且未处理」的重试**真的会重发请求**（全链路验证）。

## 为什么还要这个文件

`test_rejection_retry.py` 只钉住了两件静态事实：判据
（`_is_not_processed_rejection`）和上限（`_rejection_retry_limit`）。
但真正危险的是**接线**：重试循环有没有接上、接到哪个阶段、会不会把
已经向客户端写过的响应再重放一遍。

所以这里走完整的转发链路（TestClient → gateway → 假上游），用计数断言
「上游被调了几次」：

| 场景 | 上游被调用次数 | 客户端看到 |
|---|---|---|
| 首次 503+标记，第二次 200 | **2** | 200 |
| 两次都是 503+标记（上限 2） | **2** | 503 |
| 设置 `upstream_rejection_retry=1` | **1** | 503 |
| 503 但错误体无标记（不属于该类） | **1** | 503 |
| 流式：首次 503+标记，第二次 200 | **2** | 200 |

关键安全性质：**只在这类「未处理拒绝」上重放**，且流式重试发生在向客户端
写任何字节之前 —— 不会重复生成、不会重复计费。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc  # noqa: E402
from server.routers import gateway  # noqa: E402

_OK = {
    'id': 'x', 'object': 'chat.completion', 'created': 0, 'model': 'm',
    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                 'finish_reason': 'stop'}],
    'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
}
REJECT_BODY = {
    'error': {'code': 'no_healthy_account',
              'gateway_hint': 'upstream has no such model on this backend',
              'message': '{"code":11102,"msg":"model [m] service info not found"}'},
    'type': 'api_error',
}
OTHER_503 = {'error': {'message': 'upstream pool exhausted'}, 'type': 'api_error'}
PAYLOAD = {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 8}


class RejectionRetryIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        directory = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = directory / 'retry.db'
        config.USERS_FILE = directory / 'users.json'
        config.STATIC_DIR = directory / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        gateway._rejection_limit_cache = (0.0, -1)
        db.set_setting('upstream_rejection_retry', 2)
        self.key = keysvc.create_key(name='retry-it')['key']

    def _post(self, *, script: list, stream: bool = False) -> tuple:
        """按脚本依次返回上游响应，返回 (响应, 上游被调用次数)。"""
        calls = {'n': 0}

        def respond(request: httpx.Request) -> httpx.Response:
            if request.method != 'POST':
                return httpx.Response(200, json={'data': []})
            index = min(calls['n'], len(script) - 1)
            calls['n'] += 1
            status, body = script[index]
            if stream:
                return httpx.Response(status, json=body,
                                      headers={'content-type': 'text/event-stream'})
            return httpx.Response(status, json=body)

        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: httpx.AsyncClient(
                                   transport=httpx.MockTransport(respond))):
            payload = dict(PAYLOAD, stream=True) if stream else dict(PAYLOAD)
            response = self.c.post('/v1/chat/completions', json=payload,
                                   headers={'Authorization': f'Bearer {self.key}'})
        return response, calls['n']

    # ── 非流式 ────────────────────────────────────────────────────────
    def test_rejection_then_success_is_retried_once(self) -> None:
        r, n = self._post(script=[(503, REJECT_BODY), (200, _OK)])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(n, 2, '这类拒绝必须重发一次')

    def test_exhausted_retries_returns_last_error(self) -> None:
        r, n = self._post(script=[(503, REJECT_BODY)])
        self.assertEqual(n, 2, '默认上限是 2 次尝试')
        self.assertEqual(r.status_code, 503, r.text)

    def test_setting_one_disables_retry(self) -> None:
        db.set_setting('upstream_rejection_retry', 1)
        gateway._rejection_limit_cache = (0.0, -1)
        r, n = self._post(script=[(503, REJECT_BODY), (200, _OK)])
        self.assertEqual(n, 1, '上限设为 1 时不得重试')
        self.assertEqual(r.status_code, 503, r.text)

    def test_other_503_is_not_retried(self) -> None:
        """不属于「明确拒绝且未处理」的 503 一律不重放（可能已计费）。"""
        r, n = self._post(script=[(503, OTHER_503), (200, _OK)])
        self.assertEqual(n, 1, '无标记的 503 不得重试')
        self.assertEqual(r.status_code, 503, r.text)

    def test_2xx_never_retried(self) -> None:
        r, n = self._post(script=[(200, _OK)])
        self.assertEqual(n, 1)
        self.assertEqual(r.status_code, 200, r.text)

    # ── 流式 ──────────────────────────────────────────────────────────
    def test_stream_rejection_then_success_is_retried(self) -> None:
        r, n = self._post(script=[(503, REJECT_BODY), (200, _OK)], stream=True)
        self.assertEqual(n, 2, '流式路径也必须在写任何字节之前重试')
        self.assertEqual(r.status_code, 200, str(r.read()[:200]))

    def test_stream_success_not_buffered_twice(self) -> None:
        """流式成功响应不得被额外读取（第一版曾在 200 上调 aread()，会把整条流缓冲）。"""
        r, n = self._post(script=[(200, _OK)], stream=True)
        self.assertEqual(n, 1)
        self.assertEqual(r.status_code, 200, str(r.read()[:200]))


if __name__ == '__main__':
    unittest.main()
