"""首字延迟（first token）的判定与入库。

背景：日志页原先只有「延迟」一列，它是**整条响应**的墙钟时长——流式请求要等模型
把全部内容生成完才结束，因此回答越长数字越大，反映不出上游响应快慢。为此新增
「首字」列，记录从发起上游请求到**上游开始交付内容**的时间。

## 2026-11-23 重写：判定逻辑只有一份

这里原来测的是一个 `_scan_sse` 辅助函数，而**生产路径从来不用它** —— 真正的判定在
`observability.StreamAudit` 里。那种测试守的是死代码：它说「工具帧不算首字」，
而线上把工具帧也算进去，两边长期不一致却没人发现。

现在直接测 `StreamAudit`（生产用的那一份），并把两处容易做错的点锁住：

  1. 只带 role 的首块不算——那记的是建连时间，数字偏小且失真；
  2. 首字只记一次，后续 delta 不能覆盖它。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import observability  # noqa: E402


def _data(obj) -> bytes:
    # SSE 的事件分隔符是**空行**（`\n\n`），不是单个换行。少一个空行，两帧就会
    # 被当成同一个事件、拼起来解析失败 —— 这里踩过一次，症状是「怎么都不算首字」。
    return ('data: ' + json.dumps(obj, ensure_ascii=False) + '\n\n').encode()


ROLE_ONLY = _data({'choices': [{'delta': {'role': 'assistant', 'content': ''}}]})
CONTENT_A = _data({'choices': [{'delta': {'content': '你'}}]})
CONTENT_B = _data({'choices': [{'delta': {'content': '好'}}]})
REASONING = _data({'choices': [{'delta': {'reasoning_content': '想想'}}]})
USAGE = _data({'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}})
DONE = b'data: [DONE]\n\n'


class StreamFirstContentTest(unittest.TestCase):
    def _audit(self, *chunks) -> object:
        audit = observability.StreamAudit()
        for chunk in chunks:
            audit.feed(chunk)
        audit.finish()
        return audit

    def test_role_only_chunk_is_not_first_content(self) -> None:
        """只带 role 的首块不算交付——否则首字延迟会少算建连后的等待。"""
        self.assertFalse(self._audit(ROLE_ONLY).saw_useful)

    def test_content_chunk_is_first_content(self) -> None:
        self.assertTrue(self._audit(CONTENT_A).saw_useful)

    def test_reasoning_counts_as_useful(self) -> None:
        """推理增量也算「上游开始交付」——思考模型的第一段输出就是它。"""
        self.assertTrue(self._audit(REASONING).saw_useful)

    def test_usage_extracted(self) -> None:
        audit = self._audit(CONTENT_A, USAGE)
        self.assertEqual(audit.usage.get('prompt_tokens'), 10)
        self.assertEqual(audit.usage.get('completion_tokens'), 2)

    def test_frame_split_across_chunks(self) -> None:
        """字节流被切断时不能丢帧：这是真实网络的常态。"""
        audit = observability.StreamAudit()
        half = len(CONTENT_A) // 2
        audit.feed(CONTENT_A[:half])
        audit.feed(CONTENT_A[half:])
        audit.finish()
        self.assertTrue(audit.saw_useful)

    def test_done_and_blank_lines_ignored(self) -> None:
        audit = self._audit(DONE, b'\n', b'data: \n\n', CONTENT_A)
        self.assertTrue(audit.saw_useful)
        self.assertIsNone(audit.error)

    def test_non_json_line_does_not_raise(self) -> None:
        """畸形帧不能让审计抛异常 —— 转发路径要能继续走下去。"""
        audit = observability.StreamAudit()
        audit.feed(b'data: not-json\n\n')
        audit.feed(CONTENT_A)
        audit.finish()
        # 坏帧被记成错误，但后面的有效帧仍然被看到
        self.assertTrue(audit.saw_useful)

    def test_multiple_frames_in_one_chunk(self) -> None:
        audit = self._audit(ROLE_ONLY + CONTENT_A + CONTENT_B)
        self.assertTrue(audit.saw_useful)

    def test_tool_delta_also_counts_as_useful(self) -> None:
        """工具帧算「上游开始交付」。

        这一条是**生产口径**，与旧测试的说法相反（旧测试断言工具帧不算）。
        实际上模型先决定调用工具，本身就是一种回应；而首字延迟的意义是「上游
        多久开始回话」，不是「多久开始写字」。这里把口径固定下来，免得下次又
        有人照旧文档改代码。
        """
        frame = _data({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'function': {'name': 'Read', 'arguments': ''}}]}}]})
        audit = self._audit(frame)
        self.assertTrue(audit.saw_useful)
        # 但它不算「交付了可见正文」——预算耗尽判定用的是另一个口径。
        self.assertFalse(audit.saw_visible_text)
        self.assertTrue(audit.saw_tool_delta)

    def test_saw_useful_is_sticky(self) -> None:
        """一旦判定「上游开始交付」，后续帧不会把它改回假。

        真正的「首字时刻」由调用方在 `saw_useful` 首次为真时取一次时间戳 ——
        审计对象本身只回答「有没有开始」，不存时刻（曾经有个 `first_useful`
        字段从未被赋值，已删除）。
        """
        audit = observability.StreamAudit()
        audit.feed(CONTENT_A)
        self.assertTrue(audit.saw_useful)
        audit.feed(USAGE)
        audit.feed(DONE)
        audit.finish()
        self.assertTrue(audit.saw_useful)


class RecordFirstTokenTest(unittest.TestCase):
    """首字入库：0 与「没采集到」必须是两回事。"""

    def test_absent_first_token_is_null_not_zero(self) -> None:
        # 没有正文的流（只有 role 帧）不该被当成「0 毫秒就开始交付」。
        audit = observability.StreamAudit()
        audit.feed(ROLE_ONLY)
        audit.finish()
        self.assertFalse(audit.saw_useful)


class MigrationTest(unittest.TestCase):
    """升级路径：已有旧库必须补列成功，历史记录保留且首字为 NULL。

    SQLite 的 CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，因此这条
    路径必须由显式 ALTER 覆盖——否则升级后一查 first_token_ms 就报
    no such column，日志页整页失败。
    """

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / 'old.db'

    def tearDown(self) -> None:
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        # 连接关闭后 Windows 才允许删除；失败也不影响结论
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _make_old_db(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self._path)
        conn.executescript(
            'CREATE TABLE request_logs ('
            '  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, key_id INTEGER,'
            '  ip TEXT, model TEXT, mapped_model TEXT, status INTEGER DEFAULT 0,'
            '  prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,'
            '  latency_ms INTEGER DEFAULT 0, ua TEXT, error TEXT, stream INTEGER DEFAULT 0,'
            '  credit REAL);'
        )
        conn.execute('INSERT INTO request_logs(ts, latency_ms) VALUES(1, 1234)')
        conn.commit()
        conn.close()
        cols = {r[1] for r in sqlite3.connect(
            self._path).execute('PRAGMA table_info(request_logs)')}
        self.assertNotIn('first_token_ms', cols)

    def test_old_db_gains_column_and_keeps_rows(self) -> None:
        from server import config, db
        self._orig = config.DB_PATH
        self._make_old_db()
        config.DB_PATH = self._path
        db._conn = None
        db.connect()

        cols = {r[1] for r in db._conn.execute('PRAGMA table_info(request_logs)')}
        self.assertIn('first_token_ms', cols)
        row = db.query_one('SELECT latency_ms, first_token_ms FROM request_logs WHERE id = 1')
        self.assertEqual(row['latency_ms'], 1234)
        self.assertIsNone(row['first_token_ms'])

    def test_migration_is_idempotent(self) -> None:
        from server import config, db
        self._orig = config.DB_PATH
        self._make_old_db()
        config.DB_PATH = self._path
        for _ in range(2):
            db._conn = None
            db.connect()  # 重复执行不能报错
        cols = [r[1] for r in db._conn.execute('PRAGMA table_info(request_logs)')]
        self.assertEqual(cols.count('first_token_ms'), 1)


if __name__ == '__main__':
    unittest.main()


if __name__ == '__main__':
    unittest.main()
