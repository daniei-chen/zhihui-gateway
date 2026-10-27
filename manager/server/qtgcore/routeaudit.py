"""最终生效路由表与重复组检测（K30）。

## 口径问题

审计包给出的两个数字**不是同一件事**，不能混用：

历史审计快照曾有 230 条声明、208 个唯一 method/path；新增身份端点后是
235/213。本轮清除了 22 个不可达处理器，保留全部 213 个生效绑定。目前实际
数量由 inventory() 计算，不能把历史声明数当成当前可用能力。本模块做的事：

1. AST 静态清单：不导入应用即可枚举声明（因此可以直接在任意 Python 环境下跑，
   不受 Windows 上 `Settings` 绝对路径校验的影响）；
2. 有效顺序：按 `create_app()` 里 `include_router` 的先后（+ `app.get` 直接注册）
   决定重复组的**生效 handler**；
3. 重复组清单与 digest：method/path、注册顺序或权威处理器变化都会触发契约回归。

用法（本地只读，不碰生产）::

    python -m qtg.routeaudit --json design/qtg/route-table.json
"""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

APP_DIR = Path(__file__).resolve().parent
ROUTERS_DIR = APP_DIR / "routers"

# `create_app()` 里的 include_router 顺序（与 main.py 实际代码一致）。
# 这个常量是「注册顺序」的第二份记录：main.py 改了顺序但没改这里，测试会失败。
REGISTRATION_ORDER: Tuple[str, ...] = (
    "ip_guard", "health", "auth", "read", "adapter", "actions", "proxy", "anthropic",
    "data_admin", "system", "compat_fixes", "compat_stats", "compat_settings",
    "compat_accounts_ext", "authz", "capabilities", "compat",
)

# 允许重复的路径（同一 method+path 出现多次是「刻意分层」的情况，需人工登记）
INTENTIONAL_DUPLICATES: Dict[str, str] = {}

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


@dataclass(frozen=True)
class Declaration:
    file: str
    line: int
    method: str
    path: str
    full_path: str
    func: str
    prefix: str
    router_name: str        # router 变量名；app 直接注册时为 "app"
    module: str             # 模块名（不含 .py），用于和 REGISTRATION_ORDER 对齐

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "method": self.method,
            "path": self.path,
            "full_path": self.full_path,
            "func": self.func,
            "prefix": self.prefix,
            "module": self.module,
        }


def _router_prefixes(tree: ast.Module) -> Dict[str, str]:
    """解析模块内 `xxx = APIRouter(prefix=...)`。"""
    out: Dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Name) and call.func.id == "APIRouter"):
            continue
        prefix = ""
        for kw in call.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                prefix = str(kw.value.value or "")
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = prefix
    return out


def scan(root: Optional[Path] = None) -> List[Declaration]:
    """AST 扫描所有路由声明（不导入模块、不建 app）。"""
    base = Path(root or APP_DIR)
    base = base.parent if base.name == "qtg" else base
    rows: List[Declaration] = []
    for path in sorted(base.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - 源码坏了应当立刻可见
            raise RuntimeError(f"无法解析 {path}: {exc}") from exc
        prefixes = _router_prefixes(tree)
        try:
            rel = path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:  # pragma: no cover - 兜底：不同盘符
            rel = path.name
        module = path.stem
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                    continue
                if func.attr.lower() not in _HTTP_METHODS:
                    continue
                owner = func.value.id
                if owner not in ("router", "app"):
                    continue
                if not dec.args or not isinstance(dec.args[0], ast.Constant):
                    continue
                sub_path = str(dec.args[0].value)
                prefix = prefixes.get(owner, "") if owner == "router" else ""
                rows.append(Declaration(
                    file=rel.replace("\\", "/"),
                    line=node.lineno,
                    method=func.attr.upper(),
                    path=sub_path,
                    full_path=_join(prefix, sub_path),
                    func=node.name,
                    prefix=prefix,
                    router_name=owner,
                    module=module,
                ))
    rows.sort(key=lambda d: (d.file, d.line))
    return rows


def _join(prefix: str, path: str) -> str:
    if not prefix:
        return path or "/"
    if not path:
        return prefix
    if prefix.endswith("/") and path.startswith("/"):
        return prefix.rstrip("/") + path
    if not prefix.endswith("/") and not path.startswith("/"):
        return prefix + "/" + path
    return prefix + path


def duplicate_groups(rows: Sequence[Declaration]) -> List[dict]:
    """同一 (method, 全路径) 的重复声明组（按首次出现顺序）。"""
    buckets: Dict[Tuple[str, str], List[Declaration]] = {}
    order: List[Tuple[str, str]] = []
    for row in rows:
        key = (row.method, row.full_path)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)
    groups: List[dict] = []
    for key in order:
        items = buckets[key]
        if len(items) < 2:
            continue
        groups.append({
            "method": key[0],
            "path": key[1],
            "count": len(items),
            "declarations": [d.as_dict() for d in items],
            "intentional": INTENTIONAL_DUPLICATES.get(f"{key[0]} {key[1]}", ""),
        })
    return groups


def registration_rank(module: str) -> int:
    try:
        return REGISTRATION_ORDER.index(module)
    except ValueError:
        return len(REGISTRATION_ORDER) + 1


def effective_order(rows: Sequence[Declaration], *, static_app_routes: Sequence[str] = ()) -> List[Declaration]:
    """按注册顺序排出的唯一路由表（每个 method+路径只留生效的那条）。

    规则（与 Starlette 行为一致）：
    1. `app.xxx(...)` 直接注册的路由排在所有 `include_router` 之后（main.py 里
       `include_router` 在前、`app.get` 在后）；`main.py` 内的 `_spa` 兜底也是直接注册。
    2. 同名 module 在 `REGISTRATION_ORDER` 里靠前者先注册；
       不在表里的模块排在最后（并保持文件序，稳定可复现）。
    3. 同一 (method, path) 首个出现的生效，其余为 dead code。
    """
    def sort_key(item: Tuple[int, Declaration]):
        idx, row = item
        direct = 0 if row.router_name == "router" else 1
        # main.py 内的 app 直接注册还要区分「include_router 之前/之后」：
        # 这里的直接注册全部视为之后（当前源码只有 /api/v1/meta、/、/favicon.svg、/{path:path}）。
        return (direct, registration_rank(row.module), row.file, row.line, idx)

    ordered = [row for _, row in sorted(enumerate(rows), key=sort_key)]
    seen: Dict[Tuple[str, str], Declaration] = {}
    out: List[Declaration] = []
    for row in ordered:
        key = (row.method, row.full_path)
        if key in seen:
            continue
        seen[key] = row
        out.append(row)
    return out


def inventory(*, root: Optional[Path] = None) -> dict:
    """完整清单：声明数 / 唯一数 / 重复组 / 生效表 / digest。"""
    rows = scan(root)
    groups = duplicate_groups(rows)
    unique = {(r.method, r.full_path) for r in rows}
    effective = effective_order(rows)
    effective_keys = [(r.method, r.full_path, r.module, r.func) for r in effective]
    digest = hashlib.sha256(
        json.dumps(effective_keys, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "declarations": len(rows),
        "unique_method_paths": len(unique),
        "duplicate_groups": len(groups),
        "duplicate_extra_declarations": sum(g["count"] - 1 for g in groups),
        "registration_order": list(REGISTRATION_ORDER),
        "effective_digest": digest,
        "effective_digest_schema": "method-path-module-handler/v2",
        "effective": [r.as_dict() for r in effective],
        "duplicates": groups,
        "declared_rows": [r.as_dict() for r in rows],
    }


@dataclass(frozen=True)
class RuntimeRoute:
    path: str
    methods: frozenset
    endpoint: object
    name: str


def runtime_routes(app) -> List[RuntimeRoute]:
    """Flatten both eager and lazy FastAPI router inclusion without mutation.

    Modern FastAPI keeps an included router as a branch. A small app.routes list
    is not evidence that endpoints are missing. Include prefixes apply before
    the original APIRouter's already-prefixed route paths.
    """
    out = []

    def walk(routes, prefix=''):
        for route in routes:
            original = getattr(route, 'original_router', None)
            if original is not None:
                context = getattr(route, 'include_context', None)
                walk(original.routes, prefix + str(getattr(context, 'prefix', '') or ''))
                continue
            path = getattr(route, 'path', None)
            methods = getattr(route, 'methods', None)
            if path is not None and methods:
                out.append(RuntimeRoute(prefix + path, frozenset(methods),
                                        getattr(route, 'endpoint', None),
                                        getattr(route, 'name', '')))
    walk(getattr(app, 'routes', []))
    return out


def effective_from_app(app) -> List[dict]:
    """从真实 app 对象取生效顺序（测试用；需在隔离环境里 create_app）。"""
    out: List[dict] = []
    for route in runtime_routes(app):
        methods = sorted(getattr(route, "methods", None) or [])
        out.append({
            "path": getattr(route, "path", ""),
            "methods": methods,
            "name": getattr(route, "name", ""),
            "handler": getattr(getattr(route, "endpoint", None), "__qualname__", ""),
        })
    return out


def compare_effective_with_app(inv: dict, app) -> List[dict]:
    """对比「静态推演的生效表」与「真实 app.routes」，返回差异。"""
    actual = {(m, item["path"]) for item in effective_from_app(app)
              for m in (item["methods"] or ["GET"])}
    static = {(row["method"], row["full_path"]) for row in inv["effective"]}
    diffs: List[dict] = []
    for key in sorted(static - actual):
        diffs.append({"kind": "static_only", "method": key[0], "path": key[1]})
    for key in sorted(actual - static):
        # 隔离环境里 main.py 的静态路由（/、/{path:path}）依赖 static_dir 是否存在；
        # 这类差异按 kind=app_only 报出，由测试决定是否属于预期。
        diffs.append({"kind": "app_only", "method": key[0], "path": key[1]})
    return diffs


def _main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - CLI
    import argparse

    ap = argparse.ArgumentParser(description="QTG 路由清单与重复组检测（K30，只读）")
    ap.add_argument("--json", default="", help="把完整清单写到该 JSON 文件")
    args = ap.parse_args(list(argv) if argv is not None else None)
    inv = inventory()
    summary = {k: inv[k] for k in (
        "declarations", "unique_method_paths", "duplicate_groups",
        "duplicate_extra_declarations", "effective_digest",
    )}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出: {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
