"""挡住「在 async 函数里直接调同步 SQLite」的新增。

## 为什么值得一条守卫

SQLite 连接是同步的，一次写要拿全局写锁并落盘。在 `async def` 里直接调用，
阻塞的是**整个事件循环**：同一时刻在途的所有流式请求一起变慢，表现是
「偶尔整体变卡」而不是某一条慢，事后几乎无法归因到具体请求。

## 口径

只看函数体内**直接**出现的 `db.execute/executemany/query/query_one/executescript`，
并且不处于 `run_in_threadpool(...)` / `to_thread(...)` 的实参子树里。

第一版扫描器把「内层 def / async def」整段当成已 offload，于是报 0 处 ——
而 `async def gen()` 这类内层生成器本体就跑在事件循环上。这里已改掉。

## 基线

当前有 4 处，全在 `observability.py` 的中间件收尾路径上，都是**观测**写入
（不是计费）。按「文件 + 函数」冻结，不看行号 —— 行号会随重构漂移，
冻行号的测试只会教人随手改数字。

新增即失败；减少不失败（基线只做上限）。
"""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]      # server/tests/x.py -> 仓库根
SERVER = ROOT / 'server'
#: 独立服务（qtgcore），不参与本仓库事件循环。
EXCLUDED_DIRS = frozenset({'tests', 'qtgcore'})
#: 真的把同步调用丢到线程池的包装函数名。
THREAD_OFFLOAD = frozenset({'run_in_threadpool', 'to_thread'})
DB_FUNCS = frozenset({'execute', 'executemany', 'query', 'query_one', 'executescript'})

#: 已知的阻塞点上限（文件 → 函数 → 次数）。
BASELINE = {
    ('server/observability.py', '__call__'): 4,
}


def _offloaded_ids(func: ast.AsyncFunctionDef) -> set[int]:
    offloaded: set[int] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in THREAD_OFFLOAD:
            for arg in node.args:
                for inner in ast.walk(arg):
                    offloaded.add(id(inner))
    return offloaded


def scan() -> dict[tuple[str, str], int]:
    """每个 (文件, 异步函数) 里直接调 SQLite 的次数。"""
    found: dict[tuple[str, str], int] = {}
    for path in sorted(SERVER.rglob('*.py')):
        relative = path.relative_to(ROOT).as_posix()
        if EXCLUDED_DIRS & set(path.relative_to(SERVER).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'), str(path))
        except SyntaxError:      # 语法错由 compileall / 别的测试负责
            continue
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            skip = _offloaded_ids(func)
            for call in ast.walk(func):
                if not isinstance(call, ast.Call) or id(call) in skip:
                    continue
                target = call.func
                if isinstance(target, ast.Attribute) \
                        and isinstance(target.value, ast.Name) \
                        and target.value.id == 'db' and target.attr in DB_FUNCS:
                    key = (relative, func.name)
                    found[key] = found.get(key, 0) + 1
    return found


class AsyncBlockingDatabaseTest(unittest.TestCase):
    def test_no_new_sync_sqlite_calls_in_async_functions(self) -> None:
        found = scan()
        offenders = {key: count for key, count in found.items()
                     if count > BASELINE.get(key, 0)}
        self.assertEqual(offenders, {}, (
            '这些 async 函数里新增了同步 SQLite 调用，会阻塞事件循环：'
            f'{offenders}。改用 run_in_threadpool(...)，或把写入挪出响应路径。'))

    def test_baseline_entries_still_exist(self) -> None:
        # 基线只做上限，但**整条消失**说明代码已经改了 —— 那时应该顺手把基线也删掉，
        # 否则这份清单会慢慢变成"历史上曾经有过什么"的考古记录。
        found = scan()
        stale = [key for key in BASELINE if key not in found]
        self.assertEqual(stale, [], f'基线里的阻塞点已不存在，请同步删除：{stale}')

    def test_scanner_detects_a_known_shape(self) -> None:
        # 守卫自己也要被守卫：确认扫描逻辑真的能抓到，而不是恒返回空。
        source = (
            'import asyncio\n'
            'async def handler():\n'
            '    db.execute("INSERT INTO t VALUES(1)")\n'
            '    await asyncio.sleep(0)\n'
        )
        tree = ast.parse(source)
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
        skip = _offloaded_ids(func)
        hits = [n for n in ast.walk(func)
                if isinstance(n, ast.Call) and id(n) not in skip
                and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == 'db']
        self.assertEqual(len(hits), 1)


if __name__ == '__main__':
    unittest.main()
