"""v1.2.146：把「当前用哪个数据面引擎」暴露成指标。

## 为什么要做

qoder 和 trae 各有两代适配器，由 `QTG_QODER_ENGINE` / `QTG_TRAE_ENGINE`
或 `/opt/workbuddy-manager/engine/var/` 下的一个文件决定。

**文件缺失时回落到 `bff`** —— 而注释里写明那个引擎「源码已丢失，仅作回滚」。
所以一次误删、一次恢复备份没带上那个文件，服务会**静默**切到一个
再也升不了级的实现上。

2026-11-15 查引擎时确认了两件事：

| 位置 | 状态 |
| --- | --- |
| 启动日志 | **有**（`qtg 启动：qoder=qoder2(...)`） |
| `/metrics` | **一条引擎相关的序列都没有** |

**日志有、指标没有，等于「出事只能等有人去翻日志」。** 告警配不了，
看板看不到，而这条回退路径是**静默**的。

## 这一版加了什么

`workbuddy_engine_active{platform,engine}`，三个平台各一条：

| platform | 可能的 engine |
| --- | --- |
| `qoder` | `qoder2` / `bff` / `absent` / `unknown` |
| `trae` | `aiwork` / `bff` / `absent` / `unknown` |
| `workbuddy` | `workbuddy` / `off` |

**取值不是 1/0，而是引擎名本身** —— 因为「现在是哪个」比「是不是某个」信息量大：
一次 `count by (engine)` 就能看出是否发生了回退。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 指标存在 | 引擎静默回退不可观测 |
| 三个平台都有 | 漏掉的那个平台回退时没人知道 |
| 适配器类名映射到配置值 | 指标里的名字和能写进配置的名字对不上，看板没法关联 |
| 未知类名归一成 `unknown` | 新增适配器时吐出任意类名，序列基数不可控 |
| runtime 缺失时不崩 | 纯后端/过渡实例形态下 metrics 直接 500 |
"""
from __future__ import annotations

import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8')


class TheMetricExistsTests(unittest.TestCase):
    def test_it_emits_the_engine_metric(self):
        self.assertIn('workbuddy_engine_active', SOURCE,
                      '没有引擎指标 —— 静默回退到 bff 时监控看不到')

    def test_it_labels_by_platform_and_engine(self):
        self.assertIn('platform="%s",engine="%s"', SOURCE)


class ItCoversEveryPlatformTests(unittest.TestCase):
    def test_qoder_and_trae_are_both_covered(self):
        idx = SOURCE.find("('qoder', 'qoder', 'bff')")
        self.assertGreater(idx, 0, 'qoder 没被覆盖')
        idx2 = SOURCE.find("('trae', 'trae', 'bff')")
        self.assertGreater(idx2, 0, 'trae 没被覆盖')

    def test_workbuddy_is_covered(self):
        self.assertIn('platform="workbuddy"', SOURCE)


class ItMapsAdapterNamesToConfigValuesTests(unittest.TestCase):
    """适配器类名形如 Qoder2Adapter，但配置里写的是 qoder2 —— 必须对得上。"""

    def test_the_mapping_covers_all_four_adapters(self):
        for pair in ("'Qoder2Adapter': 'qoder2'", "'QoderAdapter': 'bff'",
                     "'Trae2Adapter': 'aiwork'", "'TraeAdapter': 'bff'"):
            self.assertIn(pair, SOURCE, '缺少映射：' + pair)

    def test_unknown_names_are_collapsed(self):
        self.assertIn("_engine = 'unknown'", SOURCE,
                      '新适配器会吐出任意类名，序列基数不可控')


class ItDegradesSafelyTests(unittest.TestCase):
    def test_missing_runtime_does_not_break_metrics(self):
        idx = SOURCE.find('workbuddy_engine_active')
        window = SOURCE[max(0, idx - 1600):idx]
        self.assertIn("getattr(getattr(request.app, 'state', None), 'rt', None)", window,
                      '拿 runtime 用了硬取属性，过渡实例形态下 metrics 会 500')
        self.assertIn('if _qtg_rt is not None', window)

    def test_absent_adapter_is_reported_not_skipped(self):
        self.assertIn("'absent'", SOURCE,
                      '适配器缺失时应当报 absent，而不是什么都不输出')


if __name__ == '__main__':
    unittest.main()
