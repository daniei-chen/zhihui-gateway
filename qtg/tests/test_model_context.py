"""模型目录的上下文档位（2026-10-23 用户反馈「上下文不要这么低」）。

上游 qoder2 /models 每个模型给的是**多档**窗口：
    context_windows       [1000000, 200000, 400000]
    context_window_labels ['1M', '200K', '400K']
    context_length        180000        ← 只是当前默认档
qtg 原先直接取 180000，前端再按 1024 除一次 → 页面显示 176K，
比模型真实能力（1M）低了一个数量级。
"""
import unittest

from qtg.routers.compat_fixes import _ctx_best, _ctx_label


class ModelContextTest(unittest.TestCase):
    def test_takes_highest_window_not_default(self) -> None:
        m = {
            "context_length": 180000,
            "context_windows": [1000000, 200000, 400000],
            "context_window_labels": ["1M", "200K", "400K"],
        }
        self.assertEqual(_ctx_best(m), 1000000)
        self.assertEqual(_ctx_label(m), "1M")

    def test_single_window_stays(self) -> None:
        m = {
            "context_length": 200000,
            "context_windows": [200000],
            "context_window_labels": ["200K"],
        }
        self.assertEqual(_ctx_best(m), 200000)
        self.assertEqual(_ctx_label(m), "200K")

    def test_trae_round_number_label(self) -> None:
        """trae 的 /v1/models 只给 context_length：128000 要显示 128K，
        不能被 1024 除成 125K。"""
        self.assertEqual(_ctx_best({"context_length": 128000}), 128000)
        self.assertEqual(_ctx_label({"context_length": 128000}), "128K")
        self.assertEqual(_ctx_label({"context_length": 200000}), "200K")
        self.assertEqual(_ctx_label({"context_length": 1000000}), "1M")

    def test_power_of_two_uses_binary(self) -> None:
        """131072 是真正的 128K（2^17），不能按 1000 进制说成 131K。"""
        self.assertEqual(_ctx_label({"context_length": 131072}), "128K")

    def test_zero_is_empty(self) -> None:
        self.assertEqual(_ctx_best({}), 0)
        self.assertEqual(_ctx_label({}), "")
        self.assertEqual(_ctx_label({"context_length": 0}), "")


if __name__ == "__main__":
    unittest.main()
