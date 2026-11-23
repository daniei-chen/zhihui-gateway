"""网关流式转发的透传性回归测试（含 tool_calls 帧形态）。

背景：上游 2026-09-14 修了流式 `tool_calls` 的 name 语义（issue #82）——把
「逐帧回填 name」改成「每个 index 只发一次 name」（对齐 OpenAI 官方流）。
辅助函数 `backfillToolCallNames` 被重写为 `stripToolCallNames`。

对管理端的判断：**不需要适配**。我们的网关是 `yield chunk` 原样透传上游字节，
只在旁边用 `_scan_sse` 读一份做用量与首字统计（不改写、不回写、不缓冲），
且全仓库完全不碰 tool_calls。所以上游这个修复会通过转发路径自动生效。

本文件把这个「结论」变成断言：若将来有人在转发热路径里开始改写 SSE
（例如为了统一 tool_calls 形态），这些用例会失败，逼迫他先想清楚
「上游已经修过的语义，我们是否要再改一遍」——那正是重复且易错的。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import observability  # noqa: E402
from server.routers import gateway  # noqa: E402


def _data(obj) -> str:
    # 事件分隔符是空行（`\n\n`）。只有单个换行时，多帧会被拼成同一个事件而解析
    # 失败 —— 症状是「怎么都不算首字」。
    return 'data: ' + json.dumps(obj, ensure_ascii=False) + '\n\n'


# 上游新形态：首帧带 name，后续帧**没有 name 键**只有 arguments 片段
TOOL_FIRST = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 0, 'id': 'call_1', 'type': 'function',
     'function': {'name': 'Bash', 'arguments': ''}}]}}]})
TOOL_ARG_FRAME = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 0, 'function': {'arguments': '{"cmd"'}}]}}]})
TOOL_ARG_FRAME2 = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 0, 'function': {'arguments': ': "ls"}'}}]}}]})
TOOL_PARALLEL = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 1, 'id': 'call_2', 'type': 'function',
     'function': {'name': 'Read', 'arguments': ''}}]}}]})


class StreamAuditToolCallsTest(unittest.TestCase):
    """审计对象不得依赖工具调用的形态 —— 它只做统计，不参与转发。

    2026-11-23 之前这里测的是一个 `_scan_sse` 辅助函数，而生产路径从来不用它
    （真正的判定在 `observability.StreamAudit`）。那种测试守的是死代码，且它与
    生产口径相反：它说「工具帧不算首字」，线上把工具帧也算进去。现在直接测
    生产用的那一份，并把这个口径写在断言里。
    """

    def _audit(self, *frames) -> object:
        audit = observability.StreamAudit()
        for frame in frames:
            audit.feed(frame.encode() if isinstance(frame, str) else frame)
        audit.finish()
        return audit

    def test_tool_frames_parse_without_error(self) -> None:
        audit = self._audit(TOOL_FIRST, TOOL_ARG_FRAME, TOOL_ARG_FRAME2, TOOL_PARALLEL)
        self.assertIsNone(audit.error)
        self.assertTrue(audit.completed is False or audit.completed is True)

    def test_tool_frame_counts_as_useful_but_not_visible_text(self) -> None:
        """工具帧算「上游开始交付」，但不算「交付了可见正文」。

        两个口径服务不同目的：首字延迟想知道「上游多久开始回话」（工具调用也算
        回话）；预算耗尽判定要求「真的给出正文或工具增量」，两者不能混用。
        """
        audit = self._audit(TOOL_FIRST)
        self.assertTrue(audit.saw_useful)
        self.assertFalse(audit.saw_visible_text)
        self.assertTrue(audit.saw_tool_delta)

    def test_tool_frame_with_empty_name_is_harmless(self) -> None:
        """上游后续帧若带空串 name（旧形态），也不能影响统计。"""
        frame = _data({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'function': {'name': '', 'arguments': 'x'}}]}}]})
        audit = self._audit(frame)
        self.assertIsNone(audit.error)
        self.assertTrue(audit.saw_tool_delta)

    def test_usage_extracted_from_tool_stream(self) -> None:
        """工具调用流的末帧同样要能取到 usage（否则用量统计漏记）。"""
        audit = self._audit(TOOL_FIRST, _data(
            {'choices': [], 'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}))
        self.assertEqual(audit.usage.get('prompt_tokens'), 7)
        self.assertEqual(audit.usage.get('completion_tokens'), 3)

    def test_feed_return_value_is_never_used(self) -> None:
        """`audit.feed()` 的返回值**不得**被用到转发路径上。

        审计内部会解析出工具调用等结构用于统计，它的返回值只对记账有意义。
        一旦有人写 `x = audit.feed(chunk)` 再把 x 发出去，就等于让审计层参与
        改写 —— 那是这套透传约束最怕的形态。这里直接查源码钉住。
        """
        src = (Path(__file__).resolve().parents[2] / 'server' / 'routers' / 'gateway.py'
               ).read_text(encoding='utf-8')
        for pattern in ('= audit.feed(', 'if audit.feed(', '=audit.feed(', 'yield audit.feed('):
            self.assertNotIn(pattern, src, f'审计返回值被用到了：{pattern}')


class GatewayPassthroughTest(unittest.TestCase):
    """确认网关没有「改写 SSE」的代码路径 —— 这是上面结论的前提。"""

    def test_gateway_source_does_not_rewrite_sse(self) -> None:
        import inspect
        src = (Path(__file__).resolve().parents[2] / 'server' / 'routers' / 'gateway.py'
               ).read_text(encoding='utf-8')
        # 转发热路径只 yield 上游 chunk；不应出现「重新构造/替换帧」的迹象
        for bad in ('yield _data(', 'yield json.dumps(', 'yield _rewrap('):
            self.assertNotIn(bad, src, f'网关似乎在重写 SSE：{bad}')
        # 所有 yield 出来的都必须是上游原样的 chunk（不能有第二个变量名混进来）
        # 注意：这里按**出现次数**断言，只查包含太弱——文件里有两处 yield，
        # 改掉其中一处仍会通过（这个弱点是被反证试出来的）。
        # Inspect the complete forwarding handler, including its async stream.
        # The module also has a synchronous Unicode-tree iterator whose yields
        # are unrelated to response bytes and must not count as SSE rewrites.
        forwarding_src = inspect.getsource(gateway._chat)
        yields = [ln.strip() for ln in forwarding_src.splitlines() if ln.strip().startswith('yield ')]
        # 三处，且只有三处：
        #   · `yield chunk` —— 上游字节原样出去；
        #   · 统一终止分支里的错误帧（两条路径：流内失败、收尾判定失败）。
        # 判定「预算耗尽」时**不重写任何一帧**：直接跳过上游尾部（`continue`），
        # 收尾时补一条错误事件。所以 yield 的形态没有增加，正文与工具分片仍然
        # 一个字节都不改。
        expected = [
            'yield chunk',
            'yield observability.sse_error(error_text, leading_separator=True,',
            'yield observability.sse_error(error_text, leading_separator=True,',
        ]
        self.assertEqual(yields, expected,
                         f'转发热路径出现了非透传的 yield：{yields}')

    def test_no_tool_calls_handling_anywhere(self) -> None:
        """转发热路径不碰 tool_calls —— 上游的修复通过透传自动生效。

        若将来需要在此处加逻辑（例如为了兼容某个客户端），请先确认
        **上游是否已经修过同一个问题**：重复修一遍往往引入新的不一致。

        两个例外都是**协议转换层**（不是转发热路径）：
          · **Anthropic 兼容层**（`routers/anthropic.py`）：它**必须**处理
            tool_calls，因为那正是协议转换本身——OpenAI 把工具参数作为
            **分片字符串**下发（`{"loc` + `ation":…}`），而 Anthropic 的客户端要的是
            `input_json_delta` 事件、且参数以对象形式呈现。
          · **Responses 兼容层**（`routers/responses.py`）：同理——Chat
            Completions 的 `tool_calls[].index` 要重建成 Responses 的
            `function_call` 输出项与 `function_call_arguments.delta` 事件。

        这与「替上游兜底」是两回事：它们不改动转发给上游的内容，只改变回给
        客户端的表达形式；「转发热路径必须原样透传」这条约束由上面几个用例
        独立守着（`gateway.py` 里不得出现非透传的 yield）。

        新增此类转换层时**显式**登记在此，而不是放宽匹配——豁免名单越长越该
        有人复核，静默放行会让这条约束慢慢失效。
        """
        # 例外：协议转换层（不是转发热路径）
        allow = {'routers/anthropic.py', 'routers/responses.py', 'observability.py',
                 # The metered steward is a tool-calling CLIENT, not a proxy
                 # transformer. Its two-round/tool budget has dedicated tests.
                 'steward_ai.py',
                 # These three expose model capability metadata; they do not
                 # process or rewrite generation payloads or SSE deltas.
                 'qtgcore/models.py', 'qtgcore/service.py', 'qtgcore/routers/compat_fixes.py'}
        # Observation reads tool deltas for timing; it never mutates forwarded chunks.
        root = Path(__file__).resolve().parents[2] / 'server'
        hits = []
        for f in root.rglob('*.py'):
            if 'tests' in str(f):
                continue
            rel = str(f.relative_to(root)).replace('\\', '/')
            if rel in allow:
                continue
            if 'tool_calls' in f.read_text(encoding='utf-8'):
                hits.append(str(f.relative_to(root)))
        self.assertEqual(hits, [], f'以下文件开始处理 tool_calls：{hits}')

    def test_steward_is_not_imported_into_proxy_forwarding(self) -> None:
        import ast
        tree = ast.parse(Path(gateway.__file__).read_text(encoding='utf-8'))
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertFalse(any('steward' in (getattr(node, 'module', '') or '')
                            or any('steward' in alias.name for alias in node.names) for node in imports))


if __name__ == '__main__':
    unittest.main()
