"""控制台静态资源规则（R12）。

背景是 2026-11-24 的真实故障：页面 HTML 更新了，但页内 `_next/static` 没同步进
nginx 的静态池，用户白屏，而巡检脚本当时报的是 `ok(probe=200 svc=active/healthy)`。
**"接口 200" 和 "控制台能用" 是两件事**，这条规则就是补这个盲区。

测试只覆盖判定逻辑（纯函数），不打真网络：`console_static_health()` 的结果作为
`evaluate()` 的输入。探测本身是否工作由线上日志验证，不适合放进单元测试——
否则网络抖一下门禁就红。
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'ops' / 'wb-gateway-alert.py'


def _load():
    spec = importlib.util.spec_from_file_location('wb_gateway_alert_r12', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metrics(console_static: dict | None) -> dict:
    base = {
        'service': 'active', 'health': 'healthy', 'probe': '200 OK',
        'timeout_n': 0, 'cap_n': 0, 'pool_n': 0, 's503_n': 0,
        'soft_rate_n': 0, 'risk_1128_n': 0,
        'client': {'total': 0}, 'backup': {}, 'res': {'disk_pct': 60.0},
    }
    if console_static is not None:
        base['console_static'] = console_static
    return base


class ConsoleStaticRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_healthy_console_is_silent(self) -> None:
        fired = self.mod.evaluate(_metrics({'page': '200', 'checked': 6, 'bad': []}))
        self.assertNotIn('R12', fired)

    def test_missing_page_reading_is_silent(self) -> None:
        # 页面都取不到说明是更大故障，交给 R1；这里不能跟着喊第二遍。
        fired = self.mod.evaluate(_metrics({'page': '', 'checked': 0, 'bad': []}))
        self.assertNotIn('R12', fired)

    def test_no_reading_at_all_is_silent(self) -> None:
        fired = self.mod.evaluate(_metrics(None))
        self.assertNotIn('R12', fired)

    def test_partial_failure_is_warn(self) -> None:
        fired = self.mod.evaluate(_metrics(
            {'page': '200', 'checked': 6, 'bad': ['404 /_next/static/chunks/app/(main)/layout-x.js']}))
        self.assertIn('R12', fired)
        self.assertEqual(fired['R12'][0], 'WARN')

    def test_all_assets_missing_is_critical(self) -> None:
        fired = self.mod.evaluate(_metrics({'page': '200', 'checked': 3, 'bad': ['404 a', '404 b', '404 c']}))
        self.assertEqual(fired['R12'][0], 'CRITICAL')

    def test_message_names_the_real_cause_and_the_fix(self) -> None:
        fired = self.mod.evaluate(_metrics({'page': '200', 'checked': 6, 'bad': ['404 x.js']}))
        text = fired['R12'][1]
        self.assertIn('静态池', text)          # 说清根因
        self.assertIn('publish-console.sh', text)  # 给出可执行的修复手段
        self.assertIn('404 x.js', text)        # 带上具体哪个资源

    def test_collect_exposes_the_field(self) -> None:
        # collect() 依赖 docker/journalctl，不在这里跑；只确认结构上有这个键，
        # 避免有人后续重构时把这条规则悄悄弄丢。
        source = SCRIPT.read_text(encoding='utf-8')
        self.assertIn("'console_static': console_static_health()", source)


if __name__ == '__main__':
    unittest.main()
