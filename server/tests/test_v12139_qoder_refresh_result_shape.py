"""v1.2.139：qoder 刷新成功却被报成「结果未确认」的根因修复。

## 用户看到的现象

在面板上点 qoder 账号的「刷新」，提示：

> 刷新结果未确认，请核对账号状态

**但上游其实刷新成功了。**

## 根因

两层各自的形状对着看就清楚了。

**上游 qoder 引擎返回的是**：

```json
{"results": [{"uid": "019ee5d2-...", "ok": true, "error": ""}], "skipped": []}
```

**顶层没有 `ok`，也没有 `success`。**

**而调用方的成功判定只看顶层**：

```python
markers = [res[name] for name in ('ok', 'success') if name in res]
if not markers:
    return False          # ← 走到这里
```

**于是「刷新成功」被判成「结果未确认」。**

**这个不一致的代价不只是显示错**：用户会以为没成，反复点，
而**每一次实际上都成功了** —— 既浪费也可能触发上游的刷新频率限制。

## 修法的选择

有两条路：

| 方案 | 影响面 | 风险 |
| --- | --- | --- |
| 改通用的成功判定 | 所有平台 | 大 |
| **改 qoder 适配器的返回形状** | **只有 qoder** | **小** |

**选了后者。** 通用判定那套「只认显式确认」的设计是有意为之
（它的注释写着「只有明确、一致的终止确认才算成功」）——
不该因为一个适配器的返回形状不同就放宽它。

## 归一的规则

- 上游给了 `results` 数组、且里面有对象 → 取**匹配当前 uid 的那一条**，没有就取第一条
- 把它的 `ok` 提到顶层
- **失败时**把各条的 `error` 合并到顶层，**让界面能显示原因而不是空**
- **形状不认识就原样返回** —— 不猜
- **原始 payload 全部保留**（`results` / `skipped` 原样带回）

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| `results[0].ok` 为真 → 顶层 `ok` 为真 | 又回到「成功了却报未确认」 |
| 失败时把 `error` 提上来 | 界面只有一句笼统的「未确认」，看不到原因 |
| 形状不认识就原样返回 | 猜测会把未知情况误判成失败 |
| 原始字段不丢 | 调用方拿不到明细 |
| 只改 qoder 适配器 | 动了通用判定会波及其他平台 |
"""
from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest import mock

from server.qtgcore.adapters import qoder2


class FakeAdapter(qoder2.Qoder2Adapter):
    """只替换网络调用，保留被测方法本身。"""

    def __init__(self, payload):
        self._payload = payload           # 不调 super，避开配置加载
        self.seen = None

    async def _json(self, method, path, **kwargs):
        self.seen = (method, path, kwargs)
        return self._payload


def run(coro):
    return asyncio.run(coro)


class TheUpstreamShapeIsNormalizedTests(unittest.TestCase):
    def test_success_is_lifted_to_top_level(self):
        """上游的 results[0].ok=true 必须变成顶层的 ok=true。"""
        adapter = FakeAdapter({"results": [{"uid": "u1", "ok": True, "error": ""}],
                               "skipped": []})
        out = run(adapter.refresh_account("u1"))
        self.assertIs(out.get('ok'), True,
                      '顶层没有 ok —— 调用方会把它判成「未确认」')

    def test_it_picks_the_row_matching_the_uid(self):
        """传了 uid 就要取那一条，而不是永远取第一条。"""
        adapter = FakeAdapter({"results": [
            {"uid": "other", "ok": False, "error": "boom"},
            {"uid": "u2", "ok": True, "error": ""},
        ]})
        out = run(adapter.refresh_account("u2"))
        self.assertIs(out.get('ok'), True)

    def test_it_falls_back_to_the_first_row(self):
        """上游没回 uid 或调用方没传 uid 时，取第一条。"""
        adapter = FakeAdapter({"results": [{"ok": True, "error": ""}]})
        out = run(adapter.refresh_account(""))
        self.assertIs(out.get('ok'), True)

    def test_the_original_payload_survives(self):
        """明细要留着，调用方可能要看 skipped。"""
        payload = {"results": [{"uid": "u1", "ok": True, "error": ""}], "skipped": ["x"]}
        adapter = FakeAdapter(payload)
        out = run(adapter.refresh_account("u1"))
        self.assertEqual(out.get('results'), payload['results'])
        self.assertEqual(out.get('skipped'), payload['skipped'])


class FailuresCarryAReasonTests(unittest.TestCase):
    def test_failure_is_false_at_top_level(self):
        adapter = FakeAdapter({"results": [{"uid": "u1", "ok": False, "error": "jwt still fresh"}]})
        out = run(adapter.refresh_account("u1"))
        self.assertIs(out.get('ok'), False)

    def test_the_reason_is_lifted(self):
        """界面要能显示上游的原话，而不是笼统的「未确认」。"""
        adapter = FakeAdapter({"results": [{"uid": "u1", "ok": False, "error": "jwt still fresh"}]})
        out = run(adapter.refresh_account("u1"))
        self.assertIn('jwt still fresh', str(out.get('error', '')))


class UnrecognizedShapesPassThroughTests(unittest.TestCase):
    def test_no_results_key_is_returned_as_is(self):
        payload = {"ok": True, "message": "fine"}
        adapter = FakeAdapter(payload)
        self.assertEqual(run(adapter.refresh_account("u1")), payload)

    def test_empty_results_is_returned_as_is(self):
        payload = {"results": [], "skipped": []}
        adapter = FakeAdapter(payload)
        self.assertEqual(run(adapter.refresh_account("u1")), payload)

    def test_garbage_entries_do_not_crash(self):
        """results 里混进非对象时不能抛。"""
        adapter = FakeAdapter({"results": ["nonsense"]})
        out = run(adapter.refresh_account("u1"))
        self.assertIsInstance(out, dict)


class OnlyThisAdapterChangedTests(unittest.TestCase):
    """通用的成功判定不该被放宽 —— 它是有意严格的。"""

    def test_shared_ok_check_still_demands_explicit_marker(self):
        from server.qtgcore.routers import compat
        src = inspect.getsource(compat._res_ok)
        self.assertIn("'ok'", src)
        self.assertIn("'success'", src)
        # 仍然要求「存在且为 True」，没有退化成「有 results 就算成功」
        self.assertIn('not markers', src)


if __name__ == '__main__':
    unittest.main()
