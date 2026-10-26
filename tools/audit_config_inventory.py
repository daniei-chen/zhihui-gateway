"""K22：QTG/Manager 配置变量全量对账（代码读取 ↔ config-schema.json）。

产出四类差异，任何一类都意味着"启动期校验"和"实际读值"不一致：

  missing_in_schema   代码会读、schema 没登记 → 启动被 unknown_variable 拒绝（功能不可用）
  dead_in_schema      schema 登记、无人读取 → 配了也没用（误导运维）
  undeclared_reader   绕开 *_env/_abs helper 直接 os.environ 读 → 逃过启动校验
  untyped_choice      choices 里含非字符串，校验按小写比较但永远不匹配

用法：
    python -X utf8 tools/audit_config_inventory.py            # 人读
    python -X utf8 tools/audit_config_inventory.py --json out.json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QTG_DIR = ROOT / 'qtg' / 'app' / 'qtg'
QTG_SCHEMA = QTG_DIR / 'config-schema.json'
MANAGER_DIR = ROOT / 'manager' / 'server'
MANAGER_SCHEMA = MANAGER_DIR / 'config-schema.json'

QTG_HELPERS = {'_env_str', '_env_bool', '_env_int', '_env_float', '_abs', '_num'}
MANAGER_HELPERS = QTG_HELPERS | {'_env'}
MANAGER_PREFIX = re.compile(r'WB(?:2API)?_[A-Z0-9_]+\Z')
VAR_TOKEN = re.compile(r'\b(?:WB(?:2API)?|QTG)_[A-Z0-9_]{2,}\Z')


def _literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _scan(folder: Path, schema_name: str, helpers: set[str], name_prefix: str | None,
          extra_roots: tuple[Path, ...] = ()):
    """返回 (reads, direct, indirect)。

    reads:    经 helper 读取 —— 值进入启动校验与 configuration_snapshot
    direct:   直接 os.environ.get / getenv / environ[...]
    indirect: 只作为字符串常量/子进程环境变量出现（字典查表、deploy 脚本、
              向子进程注入），无法静态证明"读了"，但也不是死配置
    """
    reads: dict[str, list[str]] = {}
    direct: dict[str, list[str]] = {}
    indirect: dict[str, list[str]] = {}
    paths = sorted(folder.rglob('*.py'))
    for root in extra_roots:
        paths.extend(sorted(root.rglob('*.py')))
    seen_files: set[Path] = set()
    for path in paths:
        if path in seen_files:
            continue
        seen_files.add(path)
        parts = set(path.parts)
        if 'tests' in parts or path.name == schema_name:
            continue
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        except SyntaxError as exc:
            indirect.setdefault('__syntax_error__', []).append(f'{rel}:{exc.lineno}')
            continue
        helper_hits: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                helper = node.func.id
                if helper in helpers and node.args:
                    name = _literal(node.args[0])
                    if not isinstance(name, str):
                        continue
                    if name_prefix:
                        name = name_prefix + name.upper()
                    elif not MANAGER_PREFIX.fullmatch(name):
                        continue
                    reads.setdefault(name, []).append(f'{rel}:{node.lineno}')
                    helper_hits[name] = node.lineno
            # os.environ.get('X') / os.getenv('X') / os.environ['X']
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {'get', 'getenv'} and node.args):
                base = node.func.value
                is_env = (isinstance(base, ast.Attribute) and base.attr == 'environ') or (
                    isinstance(base, ast.Name) and base.id == 'os')
                if is_env:
                    name = _literal(node.args[0])
                    if isinstance(name, str) and VAR_TOKEN.fullmatch(name):
                        direct.setdefault(name, []).append(f'{rel}:{node.lineno}')
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
                    and node.value.attr == 'environ':
                name = _literal(node.slice)
                if isinstance(name, str) and VAR_TOKEN.fullmatch(name):
                    direct.setdefault(name, []).append(f'{rel}:{node.lineno}')
            # 裸字符串常量（字典值、子进程 env 注入、EXE_ENV 查表）
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and VAR_TOKEN.fullmatch(node.value):
                indirect.setdefault(node.value, []).append(f'{rel}:{node.lineno}')
    for name in reads:
        indirect.pop(name, None)
    for name in direct:
        indirect.pop(name, None)
    return reads, direct, indirect


def audit(service: str) -> dict:
    if service == 'qtg':
        folder, schema_path, helpers, prefix = QTG_DIR, QTG_SCHEMA, QTG_HELPERS, 'QTG_'
        extra: tuple[Path, ...] = ()
    else:
        folder, schema_path, helpers, prefix = MANAGER_DIR, MANAGER_SCHEMA, MANAGER_HELPERS, None
        # 部署脚本与子进程入口也消费同一套 WB_* 变量；漏扫它们会误判成"死配置"
        extra = tuple(p for p in (ROOT / 'manager' / 'deploy',
                                  ROOT / 'manager' / 'server' / 'services')
                      if p.is_dir())
    schema = json.loads(schema_path.read_text(encoding='utf-8'))
    specs = schema['settings']
    reads, direct, indirect = _scan(folder, 'configcontract.py', helpers, prefix, extra)

    read_any = set(reads) | set(direct) | set(indirect)
    missing = {k: v for k, v in reads.items() if k not in specs}
    # schema 登记但全仓库无任何引用：配置了也不生效，属误导运维的"死项"
    dead = sorted(k for k in specs if k not in read_any)
    # 绕过 helper、且未登记。分两类：
    #   server_*  → 真缺陷：Manager 启动即 validate_environment()，未登记会拒启动（N33）
    #   deploy_*  → 部署脚本独立进程（manager/deploy/update.py 不 import server），
    #               不经过启动校验，属"未纳入统一契约"的可观测缺口
    undeclared_all = {k: v for k, v in direct.items()
                      if k not in reads and k not in specs}
    undeclared = {k: v for k, v in undeclared_all.items()
                  if any('/deploy/' not in loc for loc in v)}
    deploy_only = {k: v for k, v in undeclared_all.items() if k not in undeclared}
    # 绕过 helper 但已登记：校验能过，但值不进入 configuration_snapshot（可观测缺口）
    direct_but_known = {k: v for k, v in direct.items()
                        if k not in reads and k in specs}
    indirect_only = {k: v for k, v in indirect.items()
                     if k not in reads and k not in direct and k in specs}

    bad_choices = {}
    for name, spec in specs.items():
        choices = spec.get('choices')
        if choices is None:
            continue
        if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
            bad_choices[name] = f'non_string_choices:{choices!r}'
        elif any(c != c.lower() for c in choices):
            # configcontract._parse 用小写比较 `str(result).lower() not in spec['choices']`，
            # 大写候选项永远匹配不到（或匹配到的是另一个语义）。
            bad_choices[name] = f'case_sensitive_choice_unreachable:{choices}'

    sensitive_without_default = sorted(
        name for name, spec in specs.items() if spec.get('sensitive') and 'default' in spec)

    return {
        'service': service,
        'schema_path': schema_path.relative_to(ROOT).as_posix(),
        'schema_sha256': __import__('hashlib').sha256(
            json.dumps(schema, sort_keys=True).encode()).hexdigest(),
        'schema_settings': len(specs),
        'code_reads': len(reads),
        'missing_in_schema': missing,
        'dead_in_schema': dead,
        'undeclared_reader': undeclared,
        'undeclared_deploy_only': deploy_only,
        'direct_read_but_registered': direct_but_known,
        'indirect_only_reference': indirect_only,
        'bad_choices': bad_choices,
        'sensitive_with_default': sensitive_without_default,
        'ok': not missing and not undeclared and not bad_choices,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='配置变量全量对账')
    ap.add_argument('--service', choices=['qtg', 'manager', 'all'], default='all')
    ap.add_argument('--json', type=Path)
    args = ap.parse_args(argv)
    services = ['qtg', 'manager'] if args.service == 'all' else [args.service]
    results = [audit(s) for s in services]
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {'schema': 'zhihui-config-inventory/v1', 'results': results},
            ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    failed = False
    for r in results:
        print(f"[{r['service']}] schema={r['schema_settings']} reads={r['code_reads']} "
              f"missing={len(r['missing_in_schema'])} dead={len(r['dead_in_schema'])} "
              f"undeclared={len(r['undeclared_reader'])} badchoices={len(r['bad_choices'])}")
        for name, where in sorted(r['missing_in_schema'].items()):
            print(f'  MISSING_IN_SCHEMA {name}  {where[0]}')
            failed = True
        for name in r['dead_in_schema']:
            print(f'  DEAD_IN_SCHEMA    {name}')
        for name, where in sorted(r['undeclared_reader'].items()):
            print(f'  UNDECLARED_READER {name}  {where[0]}')
            failed = True
        for name, why in sorted(r['bad_choices'].items()):
            print(f'  BAD_CHOICES       {name}  {why}')
            failed = True
        for name, where in sorted(r['undeclared_deploy_only'].items()):
            print(f'  (info) DEPLOY_ONLY   {name}  {where[0]}')
        if r['indirect_only_reference']:
            print(f'  (info) 仅间接引用: {len(r["indirect_only_reference"])} 项 '
                  f'{sorted(r["indirect_only_reference"])}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
