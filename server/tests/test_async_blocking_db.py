"""挡住「在 async 函数里直接调同步 SQLite」的新增。

## 为什么值得一条守卫

SQLite 连接是同步的，一次写要拿全局写锁并落盘。在 `async def` 里直接调用，
阻塞的是**整个事件循环**：同一时刻在途的所有流式请求一起变慢，表现是
「偶尔整体变卡」而不是某一条慢，事后几乎无法归因到具体请求。

## 口径

只看函数体内出现的 `db.execute/executemany/query/query_one/executescript`。
只有可信导入的线程执行器收到的同步 callable 本体可以豁免；调用前求值的实参、
默认参数以及另外被直接调用或逃逸的具名 callback 都仍检查。

第一版扫描器把「内层 def / async def」整段当成已 offload，于是报 0 处 ——
而 `async def gen()` 这类内层生成器本体就跑在事件循环上。这里已改掉。

## 基线

原 4 处已移入 `persist_completion`，唯一使用为 `persistence.run(callback)`；
该入口经 `PersistencePool.submit` 交给专用 `ThreadPoolExecutor`。基线已清空。
扫描器不追踪任意函数别名或动态重绑定；无法证明只在工作线程执行时保守报错。
"""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
from pathlib import Path
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]      # server/tests/x.py -> 仓库根
SERVER = ROOT / 'server'
#: 独立服务（qtgcore），不参与本仓库事件循环。
EXCLUDED_DIRS = frozenset({'tests', 'qtgcore'})
#: 完整导入来源，不能仅因某个对象碰巧有 .to_thread/.run 就信任它。
THREAD_OFFLOAD = frozenset({'asyncio.to_thread', 'starlette.concurrency.run_in_threadpool',
                           'server.services.persistence.run'})
DB_FUNCS = frozenset({'execute', 'executemany', 'query', 'query_one', 'executescript'})

#: 已知的阻塞点上限（文件 → 函数 → 次数）。
BASELINE = {}


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return prefix + '.' + node.attr if prefix else ''
    return ''


def _bindings(nodes, *, include_imports=False):
    """Conservatively reject shadowing, including nested scopes and attributes."""
    names = set()
    for node in nodes:
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            name = _dotted(node).split('.')[0]
            if name:
                names.add(name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif include_imports and isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(item.asname or (item.name.split('.')[0] if isinstance(node, ast.Import)
                                        else item.name) for item in node.names)
    return names


def _trusted_imports(tree, relative='server/fixture.py'):
    nodes = list(ast.walk(tree))
    imports = defaultdict(set)
    package = Path(relative).parent.parts
    for node in nodes:
        if isinstance(node, ast.Import):
            for item in node.names:
                imports[item.asname or item.name.split('.')[0]].add(item.name if item.asname else item.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level > len(package):
                continue
            prefix = '.'.join(package[:len(package) - node.level + 1]) if node.level else ''
            module = '.'.join(part for part in (prefix, node.module) if part)
            for item in node.names:
                imports[item.asname or item.name].add(module + '.' + item.name)
    rebound = _bindings(nodes)
    return {name: next(iter(origins)) for name, origins in imports.items()
            if len(origins) == 1 and name not in rebound}


def _lazy_body_ids(node):
    """Do not excuse nested functions, async generators, or lazy generator expressions."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.GeneratorExp)):
        return set()
    result = {id(node)}
    for child in ast.iter_child_nodes(node):
        result.update(_lazy_body_ids(child))
    return result


def _offloaded_ids(func: ast.AsyncFunctionDef, imports=None) -> set[int]:
    imports = _trusted_imports(func) if imports is None else imports
    nodes = list(ast.walk(func))
    parents = {id(child): node for node in nodes for child in ast.iter_child_nodes(node)}

    def trusted(call):
        if not isinstance(call, ast.Call) or not call.args:
            return False
        name = _dotted(call.func).split('.')
        return '.'.join([imports.get(name[0], ''), *name[1:]]) in THREAD_OFFLOAD

    callback_args = {id(node.args[0]) for node in nodes if trusted(node)}
    offloaded = set()
    # A lambda's body is deferred; its defaults and all other call arguments are
    # evaluated on the caller and must remain visible to the scanner.
    for node in nodes:
        if (isinstance(node, ast.Lambda) and id(node) in callback_args
                and not any(isinstance(inner, (ast.Yield, ast.YieldFrom)) for inner in ast.walk(node.body))):
            offloaded.update(_lazy_body_ids(node.body))
    definitions = Counter(node.name for node in nodes
                          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    nondefinitions = [node for node in nodes
                      if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    rebound = _bindings(nondefinitions, include_imports=True)
    for node in nodes:
        if (not isinstance(node, ast.FunctionDef) or node.decorator_list
                or definitions[node.name] != 1 or node.name in rebound
                or any(isinstance(inner, (ast.Yield, ast.YieldFrom)) for inner in ast.walk(node))):
            continue
        references = [inner for inner in nodes if isinstance(inner, ast.Name)
                      and isinstance(inner.ctx, ast.Load) and inner.id == node.name]
        if not references or not all(id(reference) in callback_args and
                                     trusted(parents[id(reference)]) for reference in references):
            continue
        for statement in node.body:
            offloaded.update(_lazy_body_ids(statement))
    return offloaded


def _scan_tree(tree, relative):
    found = {}
    imports = _trusted_imports(tree, relative)
    for func in [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)]:
        skip = _offloaded_ids(func, imports)
        for call in ast.walk(func):
            if not isinstance(call, ast.Call) or id(call) in skip:
                continue
            target = call.func
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == 'db' and target.attr in DB_FUNCS):
                key = (relative, func.name)
                found[key] = found.get(key, 0) + 1
    return found


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
        found.update(_scan_tree(tree, relative))
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


class ScannerOffloadProofTests(unittest.TestCase):
    def check(self, source, *, safe=False):
        found = _scan_tree(ast.parse(textwrap.dedent(source)), 'server/fixture.py')
        if safe:
            self.assertEqual(found, {})
        else:
            self.assertTrue(found, 'The scanner must retain uncertain or caller-executed SQLite calls')
        return found

    def test_named_callback_exclusively_submitted_to_the_real_persistence_pool(self):
        self.check('''
            async def handler():
                from .services import persistence
                def persist():
                    db.execute('write')
                    db.query_one('read')
                await persistence.run(persist)
        ''', safe=True)

    def test_asyncio_and_starlette_import_aliases_are_recognized(self):
        for imported, offloader in (('import asyncio as aio', 'aio.to_thread'),
                                   ('from asyncio import to_thread as worker', 'worker'),
                                   ('from starlette.concurrency import run_in_threadpool', 'run_in_threadpool')):
            with self.subTest(imported=imported):
                self.check(f'''\
                    {imported}
                    async def handler():
                        def persist():
                            db.execute('write')
                        await {offloader}(persist)
                ''', safe=True)

    def test_inline_lambda_body_is_offloaded_but_eager_arguments_are_not(self):
        self.check('''
            import asyncio
            async def handler():
                await asyncio.to_thread(lambda: db.query('read'))
        ''', safe=True)
        for expression in ("db.execute('write')", "lambda value=db.query('default'): value",
                           "lambda: None, db.query('argument')"):
            with self.subTest(expression=expression):
                self.check(f'''\
                    import asyncio
                    async def handler():
                        await asyncio.to_thread({expression})
                ''')

    def test_offloaded_callback_also_called_directly_remains_an_offender(self):
        self.check('''
            import asyncio
            async def handler():
                def persist():
                    db.execute('write')
                await asyncio.to_thread(persist)
                persist()
        ''')

    def test_unused_or_escaped_nested_callback_is_not_assumed_safe(self):
        for extra in ('', 'elsewhere = persist\n    await asyncio.to_thread(persist)',
                      'unknown_executor(persist)\n    await asyncio.to_thread(persist)'):
            source = ('import asyncio\nasync def handler():\n    def persist():\n'
                      '        db.execute("write")\n    ' + extra + '\n')
            with self.subTest(extra=extra):
                self.check(source)

    def test_lookalike_or_shadowed_offloaders_do_not_grant_exemptions(self):
        for header, signature, rebind in (
            ('import unrelated as persistence', '', ''),
            ('from .services import persistence', 'persistence', ''),
            ('from .services import persistence', '', 'persistence.run = direct'),
            ('from unrelated import to_thread', '', ''),
        ):
            call = 'to_thread' if 'import to_thread' in header else 'persistence.run'
            with self.subTest(header=header, signature=signature, rebind=rebind):
                self.check(f'''\
                    {header}
                    async def handler({signature}):
                        {rebind or 'pass'}
                        def persist():
                            db.execute('write')
                        await {call}(persist)
                ''')

    def test_callback_defaults_and_decorators_are_not_deferred(self):
        for declaration in ("def persist(value=db.query('default')):",
                            "@decorate\n    def persist():"):
            source = ('import asyncio\nasync def handler():\n    ' + declaration + '\n'
                      '        db.execute("write")\n    await asyncio.to_thread(persist)\n')
            with self.subTest(declaration=declaration):
                self.check(source)

    def test_sync_generator_body_is_not_executed_by_calling_its_factory_in_a_thread(self):
        self.check('''
            import asyncio
            async def handler():
                def persist():
                    yield db.query('read')
                rows = await asyncio.to_thread(persist)
                list(rows)
        ''')

    def test_nested_async_generator_remains_checked_even_inside_an_offloaded_callback(self):
        found = self.check('''
            import asyncio
            async def handler():
                def persist():
                    async def gen():
                        yield db.query('read')
                    return gen()
                iterator = await asyncio.to_thread(persist)
                async for item in iterator:
                    pass
        ''')
        self.assertIn(('server/fixture.py', 'gen'), found)

    def test_lazy_generator_expression_inside_an_offloaded_lambda_is_not_excused(self):
        for expression in ("(db.query('read') for _ in range(2))", "(yield db.query('read'))"):
            with self.subTest(expression=expression):
                self.check(f'''\
                    import asyncio
                    async def handler():
                        rows = await asyncio.to_thread(lambda: {expression})
                        list(rows)
                ''')


if __name__ == '__main__':
    unittest.main()
