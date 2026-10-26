"""三合一（qoder / trae / workbuddy）的回归测试。

2026-10-23 第二轮审计发现并修复的跨平台问题，全部固化在这里：

1. 账号级写操作只写了 qoder 分支，**其余全部落到 trae**
   → 拿 workbuddy 的 uid 去 aiwork 查，返回了 trae 账号「前程如风」的签到结果。
2. 启停接口恒回 `success: true`，即使适配器明确 `ok:false`
   → 界面显示「已禁用」，实际什么都没发生（比报错更糟）。
3. 批量签到时 workbuddy 被整个跳过，用户看不到「为什么不支持」。
"""
import unittest
from types import SimpleNamespace

from qtg.adapters.workbuddy import WorkBuddyAdapter
from qtg.routers.compat import _adapter_for, _res_ok, _split_file
from qtg.routers.compat_fixes import (
    _PLAT_TO_UPSTREAM,
    _resolve_upstream_id,
    _rows_of,
)


def _agg():
    return SimpleNamespace(
        qoder_accounts=["q1"],
        trae_accounts=["t1"],
        wb_accounts=["w1", "w2", "w3"],
    )


class AdapterRoutingTest(unittest.TestCase):
    def test_three_platforms_map_to_their_own_adapter(self) -> None:
        rt = SimpleNamespace(qoder="Q", trae="T", wb="W")
        self.assertEqual(_adapter_for(rt, "qoder"), "Q")
        self.assertEqual(_adapter_for(rt, "trae"), "T")
        self.assertEqual(_adapter_for(rt, "workbuddy"), "W")

    def test_workbuddy_off_returns_none(self) -> None:
        rt = SimpleNamespace(qoder="Q", trae="T", wb=None)
        self.assertIsNone(_adapter_for(rt, "workbuddy"))

    def test_unknown_platform_returns_none(self) -> None:
        rt = SimpleNamespace(qoder="Q", trae="T", wb="W")
        self.assertIsNone(_adapter_for(rt, "nope"))

    def test_split_file(self) -> None:
        self.assertEqual(_split_file("workbuddy:abc"), ("workbuddy", "abc"))
        self.assertEqual(_split_file("qoder:x:y"), ("qoder", "x:y"))


class ResOkTest(unittest.TestCase):
    def test_upstream_ok_false_is_failure(self) -> None:
        self.assertFalse(_res_ok({"ok": False, "error": "只读"}))
        self.assertFalse(_res_ok({"success": False}))

    def test_upstream_ok_true_is_success(self) -> None:
        self.assertTrue(_res_ok({"ok": True}))
        self.assertTrue(_res_ok({"success": True}))

    def test_silent_upstream_keeps_old_behaviour(self) -> None:
        self.assertTrue(_res_ok({}))

    def test_non_dict_is_failure(self) -> None:
        self.assertFalse(_res_ok(None))
        self.assertFalse(_res_ok("ok"))


class RowsIsolationTest(unittest.TestCase):
    def test_each_upstream_id_stays_isolated(self) -> None:
        agg = _agg()
        self.assertEqual(_rows_of(agg, 1), ["q1"])
        self.assertEqual(_rows_of(agg, 2), ["t1"])
        self.assertEqual(_rows_of(agg, 3), ["w1", "w2", "w3"])

    def test_none_means_all_three(self) -> None:
        rows = _rows_of(_agg(), None)
        self.assertEqual(len(rows), 5)


class PlatformHeaderTest(unittest.TestCase):
    def test_header_maps_to_upstream_id(self) -> None:
        for plat, uid in _PLAT_TO_UPSTREAM.items():
            req = SimpleNamespace(headers={"x-qtg-platform": plat})
            self.assertEqual(_resolve_upstream_id(req, None), uid)

    def test_explicit_param_wins(self) -> None:
        req = SimpleNamespace(headers={"x-qtg-platform": "workbuddy"})
        self.assertEqual(_resolve_upstream_id(req, 1), 1)

    def test_no_header_no_param_is_none(self) -> None:
        self.assertIsNone(_resolve_upstream_id(SimpleNamespace(headers={}), None))


class WorkBuddyWriteRefusalTest(unittest.IsolatedAsyncioTestCase):
    async def test_checkin_never_reports_success(self) -> None:
        a = WorkBuddyAdapter.__new__(WorkBuddyAdapter)
        out = await a.checkin()
        self.assertFalse(out["ok"])
        self.assertFalse(out["results"][0]["ok"])
        self.assertIn("定时任务", out["results"][0]["message"])

    async def test_refresh_refused(self) -> None:
        a = WorkBuddyAdapter.__new__(WorkBuddyAdapter)
        out = await a.refresh_account("u1")
        self.assertFalse(out.get("ok"))

    async def test_set_account_refused(self) -> None:
        a = WorkBuddyAdapter.__new__(WorkBuddyAdapter)
        out = await a.set_account("u1", False)
        self.assertFalse(out.get("ok"))
        self.assertIn("原面板", out.get("error", ""))

    async def test_add_account_refused(self) -> None:
        a = WorkBuddyAdapter.__new__(WorkBuddyAdapter)
        self.assertFalse((await a.add_account("n", "jwt")).get("ok"))

    async def test_save_keys_refused(self) -> None:
        a = WorkBuddyAdapter.__new__(WorkBuddyAdapter)
        self.assertFalse((await a.save_keys([])).get("ok"))


if __name__ == "__main__":
    unittest.main()


class CapabilityMatrixTest(unittest.TestCase):
    """/api/platforms 的能力矩阵：只读平台不能有任何写能力。"""

    def test_workbuddy_has_no_write_capability(self) -> None:
        from qtg.routers.compat_fixes import _CAP_MATRIX

        wb = _CAP_MATRIX["workbuddy"]
        self.assertTrue(wb["readonly"])
        for key, value in wb.items():
            if key in ("readonly", "note", "alerts"):
                continue
            self.assertFalse(value, f"workbuddy.{key} 不该是 True（qtg 不代它写）")

    def test_qoder_trae_are_writable(self) -> None:
        from qtg.routers.compat_fixes import _CAP_MATRIX

        for pid in ("qoder", "trae"):
            cap = _CAP_MATRIX[pid]
            self.assertFalse(cap["readonly"], pid)
            self.assertTrue(cap["checkin"], pid)

    def test_matrix_covers_all_platforms(self) -> None:
        from qtg.routers.compat_fixes import _CAP_MATRIX, PLATFORMS

        self.assertEqual(set(_CAP_MATRIX), set(PLATFORMS))


class TraePoolSetKeyTest(unittest.IsolatedAsyncioTestCase):
    """aiwork 的 pool_set 只认 `uids` 键。

    写成 pool_list 里的键名 `enabled_uids`，aiwork 会照回 {"ok":true}
    但池子一个字都不动 —— 2026-10-23 实测踩过，表现为「启用按钮说成功、
    数据面继续 503」。这个断言防止有人「顺手对齐字段名」改回去。
    """

    async def test_pool_set_must_use_uids_key(self) -> None:
        from qtg.adapters.trae2 import Trae2Adapter

        a = Trae2Adapter.__new__(Trae2Adapter)
        calls = []

        async def fake(name, body=None, required=True):
            calls.append((name, body))
            if name == "pool_list":
                return {"data": {"enabled_uids": []}}
            return {"ok": True}

        a._cmd = fake
        await a.set_account("u1", True)
        name, body = calls[-1]
        self.assertEqual(name, "pool_set")
        self.assertIn("uids", body)
        self.assertNotIn("enabled_uids", body)
        self.assertEqual(body["uids"], ["u1"])


class EngineSwitchWiringTest(unittest.TestCase):
    """三个数据面引擎都必须有「开关文件」这条回滚路径。

    2026-10-23 交付审计发现：qoder/trae 都有 `_file_switch`，唯独 workbuddy
    只写了硬编码默认值 "on" —— 于是文档里那条回滚命令
    （`echo off | sudo tee /opt/qtg/var/wb_engine`）**根本不起作用**。
    """

    def test_wb_engine_reads_switch_file(self) -> None:
        from pathlib import Path

        from qtg.config import _wb_engine_default

        value = _wb_engine_default()
        self.assertIn(value, ("on", "off"))
        # 文件存在时，函数返回值必须与文件一致（证明真的读了文件，而不是硬编码）
        p = Path("/opt/qtg/var/wb_engine")
        if p.exists():
            self.assertEqual(value, p.read_text(encoding="utf-8").strip().lower())

    def test_all_three_engines_are_wired(self) -> None:
        import inspect

        from qtg import config

        src = inspect.getsource(config)
        for fn in ("_engine_default", "_trae_engine_default", "_wb_engine_default"):
            self.assertIn(fn, src, f"{fn} 缺失")
        # from_env 里三个引擎都要通过对应函数取值
        self.assertIn("_wb_engine_default()", inspect.getsource(config))
