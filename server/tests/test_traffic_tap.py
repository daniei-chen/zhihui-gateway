"""调用抓包（traffic）的契约测试。

抓包的三条硬约束都在这里钉住：
  1. 默认关闭时**不碰任何正文**（关闭成本必须为零，否则等于强制开启）；
  2. 落盘内容必须脱敏（凭据与个人标识），但工具参数要留着（那是排障重点）；
  3. 写入不阻塞、不落库、按保留期清理。

用临时目录做真实落盘，不 mock 文件系统 —— 这一层最容易在「本地绿、线上不落盘」
之间出现差异（路径、gzip、权限）。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server import config, db, traffic  # noqa: E402


class _FakeRequest:
    """最小 Request 桩：只需要 path 与 client.host。"""

    def __init__(self, path: str = '/v1/chat/completions', host: str = '203.0.113.9'):
        import types
        self.url = types.SimpleNamespace(path=path)
        self.client = types.SimpleNamespace(host=host)
        self.state = types.SimpleNamespace()


class TrafficBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data = config.DATA_DIR
        self._orig_tap = config.TRAFFIC_TAP
        config.DATA_DIR = Path(self._tmp.name)
        config.TRAFFIC_TAP = True
        traffic._FLAG['value'] = None
        traffic._FLAG['expires'] = 0.0
        for name in traffic._STATS:
            traffic._STATS[name] = 0
        # 清空队列，避免上一个用例的残留影响计数
        while not traffic._QUEUE.empty():
            traffic._QUEUE.get_nowait()
            traffic._QUEUE.task_done()

    def tearDown(self) -> None:
        # 等后台线程把队列写空，再回收目录
        traffic._QUEUE.join()
        config.DATA_DIR = self._orig_data
        config.TRAFFIC_TAP = self._orig_tap
        traffic._FLAG['value'] = None
        traffic._FLAG['expires'] = 0.0
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _records(self) -> list[dict]:
        index = Path(self._tmp.name) / 'traffic/index.jsonl'
        if not index.is_file():
            return []
        return [json.loads(line) for line in index.read_text(encoding='utf-8').splitlines() if line]


class DisabledTest(TrafficBase):
    def test_disabled_by_default_returns_none_and_writes_nothing(self) -> None:
        config.TRAFFIC_TAP = False
        trace = traffic.begin({'model': 'x'}, _FakeRequest())
        self.assertIsNone(trace)
        traffic.forwarded(trace, {'model': 'x'})
        traffic.response(trace, status=200, data={'ok': True})
        self.assertEqual(traffic._QUEUE.qsize(), 0)
        self.assertFalse((Path(self._tmp.name) / 'traffic').exists())

    def test_environment_switch_overrides_database(self) -> None:
        """环境变量是总开关：它为 0 时数据库写 true 也不能落盘。"""
        config.TRAFFIC_TAP = False
        traffic._FLAG['expires'] = 0.0
        db._conn = None
        db.connect()
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('traffic.tap','true')")
        self.assertFalse(traffic.enabled())

    def test_database_switch_can_tighten_within_environment(self) -> None:
        traffic._FLAG['expires'] = 0.0
        db._conn = None
        db.connect()
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('traffic.tap','false')")
        self.assertFalse(traffic.enabled())
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('traffic.tap','true')")
        traffic._FLAG['expires'] = 0.0
        self.assertTrue(traffic.enabled())


class RedactionTest(unittest.TestCase):
    def test_credentials_replaced_everywhere(self) -> None:
        body = {
            'model': 'glm-5.3-flash',
            'metadata': {'authorization': 'Bearer sk-real-secret',
                         'x-api-key': 'wbk_xxx', 'nested': {'token': 'abc'}},
            'cookies': [{'cookie': 'sid=1'}],
        }
        scrubbed = traffic._scrub(body)
        self.assertEqual(scrubbed['metadata']['authorization'], '<已脱敏>')
        self.assertEqual(scrubbed['metadata']['x-api-key'], '<已脱敏>')
        self.assertEqual(scrubbed['metadata']['nested']['token'], '<已脱敏>')
        self.assertEqual(scrubbed['cookies'][0]['cookie'], '<已脱敏>')
        self.assertEqual(scrubbed['model'], 'glm-5.3-flash')

    def test_pii_in_text_masked(self) -> None:
        scrubbed = traffic._scrub({'messages': [{'content': '我的手机 13800138000 邮箱 a.b@c.com'}]})
        text = scrubbed['messages'][0]['content']
        self.assertIn('<手机号>', text)
        self.assertIn('<邮箱>', text)
        self.assertNotIn('13800138000', text)

    def test_tool_arguments_kept_intact(self) -> None:
        """工具参数是排障重点，不能被当成自由文本打码。"""
        body = {'tools': [{'function': {'name': 'lookup',
                                        'parameters': {'type': 'object'}}}]}
        self.assertEqual(traffic._scrub(body), body)


class DiffTest(unittest.TestCase):
    def test_added_and_removed(self) -> None:
        before = {'model': 'a'}
        after = {'model': 'a', 'metadata': {'user_id': 'u1'}}
        diff = traffic._diff(before, after)
        paths = {(item['path'], item.get('change')) for item in diff}
        self.assertIn(('$.metadata', 'added'), paths)

    def test_changed_scalar(self) -> None:
        diff = traffic._diff({'max_tokens': 100}, {'max_tokens': 4096})
        self.assertEqual(diff, [{'path': '$.max_tokens', 'before': 100, 'after': 4096}])

    def test_identical_produces_empty(self) -> None:
        body = {'messages': [{'role': 'user', 'content': 'hi'}]}
        self.assertEqual(traffic._diff(body, dict(body)), [])

    def test_array_length_change_reported(self) -> None:
        diff = traffic._diff({'messages': []}, {'messages': [{'role': 'system'}]})
        self.assertIn({'path': '$.messages.length', 'before': 0, 'after': 1}, diff)


class WriteTest(TrafficBase):
    def test_request_response_roundtrip(self) -> None:
        request = _FakeRequest()
        trace = traffic.begin({'model': 'glm-5.3-flash', 'messages': []}, request)
        traffic.key_for(request, {'name': '测试密钥', 'prefix': 'wbk_abc'})
        traffic.forwarded(trace, {'model': 'glm-5.3-flash', 'messages': [],
                                  'metadata': {'user_id': 'u1'}})
        traffic.response(trace, status=200, data={'choices': [], 'usage': {'total_tokens': 3}})
        traffic._QUEUE.join()

        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], 200)
        self.assertEqual(records[0]['model'], 'glm-5.3-flash')
        self.assertFalse(records[0]['truncated'])

        stored = traffic.read_record(records[0]['request_id'])
        self.assertIsNotNone(stored)
        self.assertEqual(stored['client']['model'], 'glm-5.3-flash')
        self.assertEqual(stored['gateway']['metadata']['user_id'], 'u1')
        self.assertEqual(stored['key']['name'], '测试密钥')
        self.assertTrue(stored['diff'])
        self.assertEqual(stored['response']['body']['usage']['total_tokens'], 3)

    def test_stream_summary_only(self) -> None:
        class Audit:
            finish_reason = 'stop'
            completed = True
            usage = {'prompt_tokens': 5, 'completion_tokens': 7}
            error = None
            error_code = None
            saw_visible_text = True
            saw_tool_delta = False
            text = 'hello'

        trace = traffic.begin({'model': 'm', 'stream': True}, _FakeRequest())
        traffic.forwarded(trace, {'model': 'm', 'stream': True})
        traffic.response_stream(trace, status=200, audit=Audit())
        traffic._QUEUE.join()

        stored = traffic.read_record(self._records()[0]['request_id'])
        self.assertEqual(stored['response']['kind'], 'stream')
        self.assertEqual(stored['response']['finish_reason'], 'stop')
        self.assertEqual(stored['response']['usage']['completion_tokens'], 7)
        self.assertTrue(stored['stream'])

    def test_oversize_truncated_and_marked(self) -> None:
        huge = {'messages': [{'role': 'user', 'content': 'x' * (traffic.LIMIT_BYTES + 5000)}]}
        trace = traffic.begin(huge, _FakeRequest())
        traffic.forwarded(trace, huge)
        traffic.response(trace, status=200, data={'ok': True})
        traffic._QUEUE.join()

        records = self._records()
        self.assertTrue(records[0]['truncated'])
        self.assertLessEqual(records[0]['size'], traffic.LIMIT_BYTES)

    def test_request_id_cannot_traverse(self) -> None:
        """request_id 来自 URL，必须挡住路径穿越。"""
        for bad in ('../etc/passwd', 'a/b', '..', ''):
            with self.subTest(bad=bad):
                self.assertIsNone(traffic.read_record(bad))

    def test_unknown_id_returns_none(self) -> None:
        self.assertIsNone(traffic.read_record('deadbeefdeadbeef'))


class SweepTest(TrafficBase):
    def _write(self, ts: int, rid: str) -> None:
        traffic._write_payload({
            'request_id': rid, 'ts': ts, 'status': 200, 'stream': False,
            'model': {'requested': 'm', 'mapped': None}, 'key': {'name': 'k'},
            'latency_ms': 1, 'client': {}, 'gateway': {}, 'diff': [],
            'response': {}, 'truncated': False,
        })

    def test_expired_removed_and_index_rebuilt(self) -> None:
        now = int(time.time())
        self._write(now - 48 * 3600, 'old1')
        self._write(now - 30 * 3600, 'old2')
        self._write(now - 60, 'fresh')
        result = traffic.sweep()
        self.assertEqual(result['removed'], 2)
        self.assertEqual(result['kept'], 1)
        remaining = [row['request_id'] for row in self._records()]
        self.assertEqual(remaining, ['fresh'])
        self.assertIsNotNone(traffic.read_record('fresh'))
        self.assertIsNone(traffic.read_record('old1'))

    def test_sweep_on_missing_dir_is_noop(self) -> None:
        self.assertEqual(traffic.sweep(), {'removed': 0, 'kept': 0})


class RenderTest(unittest.TestCase):
    def test_html_contains_three_views(self) -> None:
        record = {
            'request_id': 'r1', 'ts': 1, 'status': 200, 'stream': False,
            'latency_ms': 12, 'path': '/v1/chat/completions',
            'client': {'model': 'a'}, 'gateway': {'model': 'b'},
            'diff': [{'path': '$.model', 'before': 'a', 'after': 'b'}],
            'response': {'kind': 'json', 'body': {}},
        }
        html = traffic.render_html(record)
        self.assertIn('客户端请求', html)
        self.assertIn('网关发往上游', html)
        self.assertIn('$.model', html)
        self.assertIn('上游响应', html)
        self.assertIn('<!doctype html>', html)

    def test_html_escapes_nothing_dangerous(self) -> None:
        """内容里有脚本片段时不能变成可执行 HTML。"""
        record = {'request_id': 'r1', 'ts': 1, 'status': 200, 'stream': False,
                  'latency_ms': 1, 'path': '/x',
                  'client': {'content': '<script>alert(1)</script>'},
                  'gateway': {}, 'diff': [], 'response': {}}
        html = traffic.render_html(record)
        self.assertNotIn('<script>alert(1)</script>', html)


if __name__ == '__main__':
    unittest.main()
