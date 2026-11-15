"""v1.2.57：收尾阶段必须留下一个可用的执行器。

## 这个修复在修什么

asyncio 的默认执行器是**懒创建**的。如果整个 lifespan 从没排过阻塞任务，它就从未
存在，于是「循环关闭时关闭默认执行器」那一步是空操作 —— 之后收尾再提交时会新建
一个，一切正常。

从引入自定义工作线程池那一版（1.2.44）起，lifespan 一开始就把默认执行器换成了自己
的池：它**必然存在**，于是收尾时被关闭，之后任何提交都抛「已关闭后仍要提交」。

那个失败又打断归档线程的收尾，让后续检查连带报「归档线程已在运行」。

## 证据（2026-11-14 按版本二分）

| 版本 | 同一组检查 |
| --- | --- |
| 1.2.43 | 11 通过 |
| 1.2.44 起 | 5 通过 / 7 报错 |
| 1.2.44 起 + 本次改动 | **11 通过** |

## 修法

在清理末尾装一个只有 2 个线程的新执行器。它不显式关闭：这个阶段之后进程很快退出，
线程由操作系统回收，存在窗口只有毫秒级。

**不能用 `set_default_executor(None)`** —— CPython 要求参数是 ThreadPoolExecutor
实例，传 None 会抛 TypeError，反而在收尾多一处异常。这条也写进断言里，防止有人
「简化」成 None。
"""
from __future__ import annotations

import unittest
from pathlib import Path

SOURCE = (Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8')


def shutdown_tail() -> str:
    """取清理阶段的尾部（关客户端之后到函数结束）。"""
    start = SOURCE.index('await config.close_clients()')
    tail = SOURCE[start:]
    end = tail.find('\ndef ')
    return tail if end == -1 else tail[:end]


class ShutdownLeavesAUsableExecutorTests(unittest.TestCase):
    def test_it_installs_an_executor_at_the_end_of_cleanup(self):
        tail = shutdown_tail()
        self.assertIn('set_default_executor', tail,
                      '收尾没有再装执行器，收尾提交会撞上「已关闭」')

    def test_it_is_a_real_executor_not_none(self):
        """CPython 要求 ThreadPoolExecutor 实例，None 会抛 TypeError。

        只检查**代码行**：注释里为了说明为什么不能用 None，专门提到了它，
        整段做字符串匹配会把解释文字当成代码。
        """
        tail = shutdown_tail()
        code = '\n'.join(line for line in tail.splitlines()
                         if not line.lstrip().startswith('#'))
        self.assertIn('ThreadPoolExecutor(max_workers=2', code)
        self.assertNotIn('set_default_executor(None)', code,
                         '又改成传 None 了 —— 那会在收尾抛 TypeError')

    def test_the_pool_is_small_and_bounded(self):
        """这个池只服务于收尾的零星提交，不需要大。"""
        tail = shutdown_tail()
        self.assertIn('max_workers=2', tail)

    def test_the_original_custom_pool_is_still_installed_at_startup(self):
        """启动时换池这件事不能因为这个修复被删掉 —— 那是并发能力的来源。"""
        head = SOURCE[:SOURCE.index('await config.close_clients()')]
        self.assertIn('set_default_executor(threadpools.blocking_pool())', head)

    def test_the_reason_is_recorded(self):
        tail = shutdown_tail()
        self.assertIn('1.2.44', tail, '把二分结论写进代码，便于日后对照')
        self.assertIn('懒创建', tail, '要说清为什么以前没问题')


class ProbeHygieneTests(unittest.TestCase):
    def test_no_probe_leftovers(self):
        """排查时留的临时标记必须清干净。"""
        self.assertNotIn('PROBE', SOURCE)


if __name__ == '__main__':
    unittest.main()
