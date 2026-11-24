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


class ShrinkTest(unittest.TestCase):
    """裁剪必须保持 JSON 合法。

    2026-11-24 上线第一天就撞上：156 条真实记录里 124 条（79%）被「截断」，
    而原实现是 `raw[:LIMIT_BYTES]` —— 把 JSON 字节串直接砍断。前端拿到的是
    半个对象，`JSON.parse` 报错，于是这个功能在最需要的长上下文场景下
    什么都看不到，只剩一个「已截断」的标记。

    核心断言因此是：**裁剪之后 `json.loads` 必须成功**。
    """

    def _prepare(self, payload: dict) -> dict:
        return json.loads(traffic._prepare(payload))

    def _base(self, **sections) -> dict:
        payload = {'request_id': 'r1', 'ts': 1, 'status': 200, 'stream': False,
                   'latency_ms': 5, 'diff': [], 'response': {}}
        payload.update(sections)
        return payload

    def test_small_payload_untouched(self) -> None:
        payload = self._base(client={'model': 'm', 'messages': [
            {'role': 'user', 'content': '你好'}]})
        out = self._prepare(payload)
        self.assertFalse(out['truncated'])
        self.assertEqual(out['truncated_fields'], [])
        self.assertEqual(out['client']['messages'][0]['content'], '你好')

    def test_long_text_keeps_head_and_tail_and_stays_valid(self) -> None:
        big = 'A' * 100_000 + '尾部标记'
        payload = self._base(client={'messages': [{'role': 'user', 'content': big}]})
        out = self._prepare(payload)
        content = out['client']['messages'][0]['content']
        self.assertTrue(out['truncated'])
        self.assertIn('此处省略', content)
        self.assertTrue(content.startswith('A'), '头部被丢了')
        self.assertIn('尾部标记', content, '尾部被丢了')
        self.assertLess(len(content.encode()), len(big.encode()))
        self.assertEqual(out['truncated_fields'],
                         ['client.messages[0].content'])

    def test_structure_survives_shrinking(self) -> None:
        """裁剪只动字符串，键、条数、其它字段都必须原样。"""
        payload = self._base(
            client={'model': 'glm', 'temperature': 0.2, 'messages': [
                {'role': 'system', 'content': 'B' * 80_000},
                {'role': 'user', 'content': '短'}]},
            gateway={'model': 'cn:glm', 'messages': [
                {'role': 'user', 'content': '短'}]})
        out = self._prepare(payload)
        self.assertEqual(out['client']['model'], 'glm')
        self.assertEqual(out['client']['temperature'], 0.2)
        self.assertEqual(len(out['client']['messages']), 2)
        self.assertEqual(out['client']['messages'][1]['content'], '短')
        self.assertEqual(out['gateway']['model'], 'cn:glm')

    def test_many_messages_are_trimmed_by_count(self) -> None:
        """条数问题靠裁条解决：保留首尾，中间插一条说明，JSON 仍然合法。"""
        messages = [{'role': 'user', 'content': 'x' * 2000} for _ in range(2000)]
        payload = self._base(client={'messages': list(messages)},
                             gateway={'messages': list(messages)})
        out = self._prepare(payload)
        kept = out['client']['messages']
        self.assertLessEqual(len(kept), traffic.LIST_HEAD + 1 + traffic.LIST_TAIL)
        placeholders = [m for m in kept if m.get('_traffic_placeholder')]
        self.assertEqual(len(placeholders), 1)
        self.assertIn('省略', placeholders[0]['content'])

    def test_both_sections_are_trimmed(self) -> None:
        """回归：曾写成「裁完 client 就 break」，于是 gateway 的几百条原样落盘，
        单条记录达到 790 KB，256 KB 上限形同虚设。

        真实数据：client 裁到 13 条（156 KB）、gateway 仍 386 条（541 KB）。
        """
        many = [{'role': 'user', 'content': 'y' * 3000} for _ in range(400)]
        payload = self._base(client={'messages': list(many)},
                             gateway={'messages': list(many)})
        raw = traffic._prepare(payload)
        self.assertLessEqual(len(raw), traffic.LIST_LIMIT,
                             f'单条记录 {len(raw)} 字节，超过 {traffic.LIST_LIMIT} 上限')
        out = json.loads(raw)
        for section in ('client', 'gateway'):
            self.assertLessEqual(len(out[section]['messages']),
                                 traffic.LIST_HEAD + 1 + traffic.LIST_TAIL)

    def test_oversize_keeps_the_last_message(self) -> None:
        """条数裁到最少仍超限时，保留**最后一条**消息，而不是整段省略。

        2026-11-24 真实数据：一条 433 条消息的长会话，四轮裁剪后仍超限，于是
        client/gateway 被整段替换成「已整体省略」——记录只剩一个 diff，排障价值
        归零。当时那条记录其实只有 19.8 KB，完全放得下「最后一条消息 + 参数」。
        """
        # 每条消息内部有大量小字符串（每段都小于单字段上限），总量极大。
        heavy = [{'role': 'user', 'content': 'z' * 3000,
                  'attachments': [{'id': f'a{i}', 'body': 'y' * 900} for i in range(60)]}
                 for _ in range(300)]
        payload = self._base(client={'model': 'glm', 'messages': heavy},
                             gateway={'model': 'cn:glm', 'messages': list(heavy)})
        raw = traffic._prepare(payload)
        self.assertLessEqual(len(raw), traffic.LIST_LIMIT)
        out = json.loads(raw)
        for section in ('client', 'gateway'):
            messages = out[section]['messages']
            self.assertTrue(messages[0].get('_traffic_placeholder'),
                            f'{section} 应该有「历史已省略」的说明在前')
            self.assertEqual(messages[-1]['role'], 'user', f'{section} 的最后一条消息被丢了')
            self.assertLessEqual(len(messages), traffic.LIST_TAIL + 1)
        self.assertNotIn('_traffic_oversize', out['client'],
                         '不该整段省略 —— 那等于什么都没留下')
        self.assertEqual(out['client']['model'], 'glm')

    def test_response_summary_survives_oversize_request(self) -> None:
        """请求侧被省略时，响应摘要必须保住。

        2026-11-24 真实数据：一条流式长会话的 `response` 被无条件牵连省略，
        记录里只剩一个 diff —— 终止原因、用量、模型实际说了什么全都没了，
        「模型为什么这么答」彻底无从判断。响应摘要是排障的核心，不能陪葬。
        """
        heavy = [{'role': 'user', 'content': 'q' * 3000,
                  'attachments': [{'id': f'a{i}', 'body': 'y' * 900} for i in range(80)]}
                 for _ in range(300)]
        payload = self._base(
            client={'messages': heavy}, gateway={'messages': list(heavy)},
            response={'kind': 'stream', 'finish_reason': 'stop', 'completed': True,
                      'usage': {'prompt_tokens': 10, 'completion_tokens': 2},
                      'text': '这是模型的回答。' * 10, 'error': None})
        raw = traffic._prepare(payload)
        self.assertLessEqual(len(raw), traffic.LIST_LIMIT)
        out = json.loads(raw)
        self.assertNotIn('_traffic_oversize', out['response'],
                         '响应摘要被省略了 —— 排障最需要的部分没了')
        self.assertEqual(out['response']['finish_reason'], 'stop')
        self.assertEqual(out['response']['usage']['completion_tokens'], 2)
        self.assertIn('这是模型的回答', out['response']['text'])

    def test_long_generated_text_is_trimmed_not_dropped(self) -> None:
        """流式生成的全文可能有几 MB，但「说了什么」正是排障要看的 ——
        裁剪保留头尾，而不是整体丢掉。"""
        payload = self._base(
            client={'messages': [{'role': 'user', 'content': '短'}]},
            gateway={'messages': [{'role': 'user', 'content': '短'}]},
            response={'kind': 'stream', 'finish_reason': 'length',
                      'text': 'A' * 500_000 + '结尾'})
        raw = traffic._prepare(payload)
        self.assertLessEqual(len(raw), traffic.LIST_LIMIT)
        out = json.loads(raw)
        text = out['response']['text']
        self.assertIn('此处省略', text)
        self.assertTrue(text.startswith('A'))
        self.assertIn('结尾', text, '生成文本的结尾被丢了')
        """连最后一条都塞不下时才整段省略，且必须留下原因。"""
        half = 200_000
        one = [{'role': 'user', 'content': 'q' * half,
                'blobs': ['w' * 30_000 for _ in range(10)]}]
        payload = self._base(client={'messages': one}, gateway={'messages': list(one)})
        raw = traffic._prepare(payload)
        self.assertLessEqual(len(raw), traffic.LIST_LIMIT)
        out = json.loads(raw)
        self.assertTrue(out['truncated'])
        self.assertTrue(out['client'].get('_traffic_oversize')
                        or len(out['client'].get('messages', [])) <= 2)

    def test_diff_is_never_shrunk(self) -> None:
        """差异是摘要，逐条都很短；裁它等于毁掉最该看的东西。"""
        payload = self._base(
            client={'messages': [{'role': 'user', 'content': 'A' * 90_000}]},
            diff=[{'path': '$.messages[0].content', 'before': 'A' * 90_000,
                   'after': 'A' * 90_000}])
        out = self._prepare(payload)
        self.assertEqual(out['diff'][0]['before'], 'A' * 90_000)

    def test_truncated_flag_is_in_the_written_payload(self) -> None:
        """回归：`truncated` 曾经在 json.dumps **之后**才赋值，所以落盘的记录里
        根本没这个字段 —— 索引里有、正文里没有，对不上。"""
        payload = self._base(client={'messages': [
            {'role': 'user', 'content': 'A' * 90_000}]})
        out = self._prepare(payload)
        self.assertIn('truncated', out)
        self.assertIs(out['truncated'], True)
        self.assertIn('truncated_fields', out)

    def test_multibyte_is_not_split(self) -> None:
        """按字节判断、按字符边界切：切坏的多字节会让解码后留下乱码。"""
        payload = self._base(client={'messages': [
            {'role': 'user', 'content': '汉' * 40_000}]})
        out = self._prepare(payload)
        content = out['client']['messages'][0]['content']
        self.assertNotIn('\ufffd', content, '裁剪切坏了多字节字符')
        self.assertIn('此处省略', content)

    def test_export_html_states_what_was_trimmed(self) -> None:
        """导出文件要写明哪些字段被裁 —— 它是要转发给别人的，不说明会误导。"""
        record = {'request_id': 'r1', 'ts': 1, 'status': 200, 'stream': False,
                  'latency_ms': 1, 'path': '/x', 'client': {}, 'gateway': {},
                  'diff': [], 'response': {},
                  'truncated_fields': ['client.messages[0].content']}
        html = traffic.render_html(record)
        self.assertIn('只保留了最近的部分', html)
        self.assertIn('client.messages[0].content', html)
        clean = dict(record)
        clean['truncated_fields'] = []
        self.assertNotIn('只保留了最近的部分', traffic.render_html(clean))


class StreamTextTest(unittest.TestCase):
    """流式记录必须留下「模型说了什么」。

    2026-11-24 之前 `response.text` 是个**从未被赋值**的字段：`traffic` 从审计
    对象上 `getattr(audit, 'text', None)`，而 `StreamAudit` 根本没有这个属性，
    于是面板上永远是 null。用户拿着一行 `finish_reason=length` 的记录，仍然
    不知道它断在哪一句 —— 而这恰恰是「模型为什么这么答」要回答的。
    """

    @staticmethod
    def _frame(obj) -> bytes:
        return ('data: ' + json.dumps(obj, ensure_ascii=False) + '\n\n').encode()

    def _audit(self, pieces, reasoning=(), finish='stop'):
        from server import observability
        audit = observability.StreamAudit()
        for piece in pieces:
            audit.feed(self._frame({'choices': [{'delta': {'content': piece}}]}))
        for piece in reasoning:
            audit.feed(self._frame({'choices': [{'delta': {'reasoning_content': piece}}]}))
        audit.feed(self._frame({'choices': [{'delta': {}, 'finish_reason': finish}],
                                'usage': {'completion_tokens': 3}}))
        audit.finish()
        return audit

    def test_text_is_collected_in_order(self) -> None:
        audit = self._audit(['你好', '，世界', '！'])
        self.assertEqual(audit.collected('text'), '你好，世界！')

    def test_reasoning_is_collected_separately(self) -> None:
        audit = self._audit(['答'], reasoning=['先想一想'])
        self.assertEqual(audit.collected('text'), '答')
        self.assertEqual(audit.collected('reasoning'), '先想一想')

    def test_no_text_returns_none_not_empty(self) -> None:
        """没有正文时必须是 null，不是空串 —— 面板据此区分「没生成」与「生成了空」。"""
        audit = self._audit([])
        self.assertIsNone(audit.collected('text'))

    def test_cap_stops_accumulation_and_flags(self) -> None:
        """长回答只留前 64 KB，并标记被截断 —— 几 MB 的正文不能全塞进记录。"""
        from server import observability
        audit = observability.StreamAudit()
        chunk = 'x' * 8000
        for _ in range(20):          # 160 KB > 64 KB 上限
            audit.feed(self._frame({'choices': [{'delta': {'content': chunk}}]}))
        audit.finish()
        text = audit.collected('text')
        self.assertLessEqual(len(text), audit.TEXT_CAP + len(chunk))
        self.assertTrue(audit.text_truncated)

    def test_summary_carries_text_and_reasoning(self) -> None:
        audit = self._audit(['最终回答'], reasoning=['内心活动'])

        class Trace:
            request = None
            rid = 'r1'
            started = 0.0
            status = 200
            client = None
            gateway = None

        trace = Trace()
        traffic.response_stream(trace, status=200, audit=audit, error=None)
        self.assertEqual(trace.response['text'], '最终回答')
        self.assertEqual(trace.response['reasoning'], '内心活动')
        self.assertEqual(trace.response['finish_reason'], 'stop')


class FilterTest(unittest.TestCase):
    """按条件筛记录。

    记录上限是 5000 条 / 24 小时。没有筛选时，想找「刚才那次失败」只能一页页翻，
    而这恰恰是排障的起点。所有条件都只读索引，不碰正文。
    """

    ROWS = [
        {'request_id': 'r1', 'ts': 1000, 'status': 200, 'model': 'glm-5', 'mapped': 'cn:glm-5',
         'key': 'prod', 'stream': True, 'truncated': False, 'size': 10, 'file': 'd/r1.json.gz'},
        {'request_id': 'r2', 'ts': 2000, 'status': 429, 'model': 'glm-5', 'mapped': 'cn:glm-5',
         'key': 'prod', 'stream': True, 'truncated': True, 'size': 20, 'file': 'd/r2.json.gz'},
        {'request_id': 'r3', 'ts': 3000, 'status': 500, 'model': 'deepseek', 'mapped': 'cn:deepseek',
         'key': 'test', 'stream': False, 'truncated': False, 'size': 30, 'file': 'd/r3.json.gz'},
    ]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = traffic.base_dir
        root = Path(self._tmp.name)
        traffic.base_dir = lambda: root           # type: ignore[assignment]
        (root / 'index.jsonl').write_text(
            ''.join(json.dumps(row) + '\n' for row in self.ROWS), encoding='utf-8')

    def tearDown(self) -> None:
        traffic.base_dir = self._orig            # type: ignore[assignment]
        self._tmp.cleanup()

    def ids(self, **kwargs) -> list[str]:
        return [row['request_id'] for row in traffic.list_records(**kwargs)]

    def test_no_filter_returns_newest_first(self) -> None:
        self.assertEqual(self.ids(limit=10), ['r3', 'r2', 'r1'])

    def test_status_error_covers_4xx_and_5xx(self) -> None:
        """只关心「失败了哪些」时，不该还要分别记住 4xx 与 5xx。"""
        self.assertEqual(self.ids(limit=10, status='error'), ['r3', 'r2'])

    def test_status_ok(self) -> None:
        self.assertEqual(self.ids(limit=10, status='ok'), ['r1'])

    def test_status_specific_code(self) -> None:
        self.assertEqual(self.ids(limit=10, status='429'), ['r2'])

    def test_status_class_split(self) -> None:
        self.assertEqual(self.ids(limit=10, status='client'), ['r2'])
        self.assertEqual(self.ids(limit=10, status='server'), ['r3'])

    def test_model_matches_requested_or_mapped(self) -> None:
        """用户记的是请求模型，索引里还有映射后的名字，两边都要能查到。"""
        self.assertEqual(self.ids(limit=10, model='cn:deepseek'), ['r3'])
        self.assertEqual(self.ids(limit=10, model='glm'), ['r2', 'r1'])

    def test_key_filter(self) -> None:
        self.assertEqual(self.ids(limit=10, key='test'), ['r3'])

    def test_stream_and_truncated_filters(self) -> None:
        self.assertEqual(self.ids(limit=10, stream=False), ['r3'])
        self.assertEqual(self.ids(limit=10, truncated=True), ['r2'])

    def test_since_window(self) -> None:
        """`24h` 之外的旧记录不该出现。"""
        now = int(time.time())
        rows = [dict(self.ROWS[0], ts=now - 60), dict(self.ROWS[1], ts=now - 90000)]
        (traffic.base_dir() / 'index.jsonl').write_text(
            ''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        self.assertEqual(self.ids(limit=10, since='1h'), ['r1'])
        self.assertEqual(self.ids(limit=10, since='24h'), ['r1'])

    def test_unknown_filters_are_ignored(self) -> None:
        """拼错的条件不该静默返回空 —— 忽略它，至少还能看到全部。"""
        self.assertEqual(self.ids(limit=10, status='bogus'), ['r3', 'r2', 'r1'])
        self.assertEqual(self.ids(limit=10, since='7d'), ['r3', 'r2', 'r1'])

    def test_limit_applies_after_filtering(self) -> None:
        self.assertEqual(self.ids(limit=1, status='error'), ['r3'])


if __name__ == '__main__':
    unittest.main()
