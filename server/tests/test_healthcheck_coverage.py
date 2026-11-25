"""健康巡检必须覆盖「配置能否被当前版本接受」和「发布树有没有字节码污染」。

## 为什么加这两项

**配置校验**：2026-11-23 23:25 有一个 drop-in 先加了 `WB_TRAFFIC_TAP`，
而当时代码的 schema 还不认识它。服务在「启动 → 被拒 → 3 秒后重启」之间
循环了约 8 分钟（`StartLimitBurst=10` 才停）。

规范 §5.1 说 `healthcheck.sh` 要「一条命令给出全局结论」。原来这条结论里
**没有「配置会不会被拒」** —— 而这正是那次故障的唯一原因。

**字节码污染**：发布树的完整性校验拒绝任何 `__pycache__`，而检查工具自己
import 发布树代码时就会生成它。`deploy/verify-runtime-config.py` 顶部那句
`sys.dont_write_bytecode = True` 就是为此。巡检里加一项能看见历史残留。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 巡检调用 `verify-runtime-config.py` | 配置会被拒这件事又只能在重启时发现 |
| 配置被拒时报 FAIL 而不是 warn | 会被当成"待观察"，而它实际上会让服务起不来 |
| 巡检会数发布树里的 `__pycache__` | 下一次 seal 失败时又要重新查一遍 |
| 巡检脚本仍然只读 | 检查工具绝不能改变被检查对象 |
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[2]/'deploy'/'healthcheck.sh').read_text(
    encoding='utf-8')


class ConfigSectionTests(unittest.TestCase):
    def test_calls_the_preflight_checker(self):
        self.assertIn('verify-runtime-config.py', SOURCE,
                      '巡检没有调用配置校验——那 8 分钟崩溃重启的成因就又看不见了')

    def test_config_rejection_is_fail_not_warn(self):
        """配置会被拒是「服务起不来」，不是「值得关注」。"""
        block = SOURCE.split("section '配置与发布树'", 1)
        self.assertEqual(len(block), 2, '「配置与发布树」这一节不见了')
        section = block[1].split('section ', 1)[0]
        self.assertIn('broken', section,
                      '配置被拒必须报 FAIL；报 warn 会被当成"待观察"')

    def test_counts_bytecode_leftovers(self):
        self.assertIn('__pycache__', SOURCE,
                      '巡检看不到发布树里的字节码残留')


class ReadOnlyTests(unittest.TestCase):
    def test_does_not_delete_or_modify_release_files(self):
        """巡检只读：只允许删它自己建的临时文件。"""
        dangerous = [line.strip() for line in SOURCE.splitlines()
                     if re.search(r'\brm\s+-[rf]', line)
                     and 'hc-' not in line]
        self.assertEqual(dangerous, [],
                         '巡检里出现了删文件的动作——它是只读检查工具')


class ThresholdSourceTests(unittest.TestCase):
    """判警告用的阈值必须来自线上，不能是代码里的魔数。

    ## 为什么

    2026-11-25 发现巡检每天报「3 次流式请求首字节超过 60 秒」，而线上实际
    `WB_FIRST_BYTE_TIMEOUT_S=240`。

    那个 60 是个**没有出处的魔数**：既不等于线上超时，也不等于任何配置值。
    在 240 秒预算下，60~240 秒之间给出首字节是**正常**的 —— 但每天都被算成
    一条警告。运维看到它会以为接近故障，实际还有 3 分钟余量。

    `server/config.py` 里也有一个 60 的默认值，同一个数字在三个地方含义不同
    （见 docs/OPS-PITFALLS.md 第十八条「代码里的默认值不是线上值」）。

    ## 本测试守什么

    | 不变量 | 破坏后的表现 |
    | --- | --- |
    | 阈值从 `systemctl show` 读 | 又变回魔数，警告失去语义 |
    | 读不到时有非数字兜底 | 读到异常值会让算术展开报错，巡检崩掉 |
    | 首字节比较不再硬编码 60000 | 240 秒预算下的正常请求继续被报成警告 |
    """

    def test_reads_threshold_from_running_unit(self):
        self.assertIn('systemctl show', SOURCE,
                      '阈值必须来自运行中的服务，不是代码里的常量')

    def test_first_byte_budget_is_not_hardcoded(self):
        self.assertNotIn('v > 60000', SOURCE,
                         '首字节阈值又硬编码成 60 秒了——线上预算是 240')
        self.assertIn('unit_env workbuddy-web WB_FIRST_BYTE_TIMEOUT_S', SOURCE,
                      '首字节预算没有从 WB_FIRST_BYTE_TIMEOUT_S 读')

    def test_unit_env_has_a_non_numeric_fallback(self):
        """读到空值或非数字时必须退回默认值，否则 `$((x * 1000))` 会报错。

        巡检是「一条命令给出全局结论」的那个命令，它自己不能崩。
        """
        self.assertIn('unit_env()', SOURCE, '缺少 unit_env 定义')
        block = SOURCE.split('unit_env()', 1)[1].split('}\n', 1)[0]
        self.assertIn('[!0-9]', block,
                      'unit_env 没有非数字兜底：读到异常值会让算术展开失败')
        self.assertIn('"$3"', block,
                      'unit_env 没有用调用方给的默认值')


if __name__ == '__main__':
    unittest.main()
