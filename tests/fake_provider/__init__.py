"""假上游包：标准库实现，禁止引入第三方依赖。"""
from .server import MODES, FakeProvider, Stats  # noqa: F401

__all__ = ["FakeProvider", "Stats", "MODES"]
