"""环境变量契约：读到的必须登记，登记的必须能用。

## 为什么单列一个测试文件

`server/configcontract.py` 的规则是：任何以 `WB_` / `WB2API_` 开头、但不在
`config-schema.json` 里的变量，一律判定为 `unknown_variable` 并**拒绝启动**。
这个「宁可起不来也不静默忽略」的选择是对的——否则用户设了变量、以为生效了，
实际什么都没发生，比直接报错难查得多。

代价是：**在代码里加一行读 `WB_XXX` 而忘记登记 schema，就等于埋了一颗
「一旦有人设置这个变量，服务立刻起不来」的雷。** 而且它不会被常规测试抓到：
默认部署不设这个变量，本地也没人设，于是门禁全绿。

2026-11-23 这颗雷真的炸了：给抓包总开关设 `WB_TRAFFIC_TAP=1` 之后，
`workbuddy-web` 连续重启 10 次后彻底放弃，全站 502。同一批还发现另一颗更早埋下
的雷 `WB_TRAE_TIMEOUT`（有人加了读取、从没登记，也从未被设置过）。

2026-11-24 复查发现**当初的修法范围不够**：本文件原来只扫 `config.py` 且只认
`_env_bool('X')` 这一类写法，于是下面 10 个「服务启动期真的会被读到」的变量
一直是盲区（`accountguard.py` 5 个、`externalplatforms.py` 5 个）——它们和
`WB_TRAFFIC_TAP` 完全同级，设置即宕机，但测试永远绿。现在改成 AST 扫全树：
`os.environ.get("X")`、`os.environ['X']`、`os.getenv('X')`、项目内 `_env_*('X')`
四种写法都算。

所以这里做三件事：
  1. 服务启动期读的每个 `WB_*` 都必须在 schema 里（防止埋雷）；
  2. 部署层变量（只被独立运行的更新器读）必须与
     `configcontract.DEPLOY_ONLY_VARIABLES` 逐一对应，且**不**登记进 schema
     ——登记等于宣称「服务会校验并让它生效」，那是更糟的静默误导；
  3. 这些部署层变量被误写进服务环境时，只能告警、不能拦住启动
     ——运维照文档写进 drop-in 是可预见的操作，而「起不来」是全站不可用。
"""
from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SERVER = Path(__file__).resolve().parents[1]

PREFIXES = ('WB_', 'WB2API_')

#: 与 `configcontract.parse_source_defaults` 保持同一套排除规则：
#: `tests` 不是配置消费者；`qtgcore` 是**另一个服务**（自带 `QTG_` schema）；
#: `deploy` 是独立运行的更新器，不经过 `validate_environment`。
_EXCLUDED_DIRS = frozenset({'tests', 'qtgcore', 'deploy'})

_ENV_GETTERS = frozenset({
    'os.environ.get', 'os.getenv', 'environ.get', 'getenv',
})

#: `config.py` 里所有读环境变量的调用形式（默认值一致性那条用例要用）。
_READ = re.compile(r"_env_(?:bool|int|str|float|list)\(\s*'([A-Z0-9_]+)'")


def _literal_name(node: ast.AST) -> str | None:
    """若这个 AST 节点是「用字面量名字读环境变量」，返回该变量名。"""
    if isinstance(node, ast.Subscript):
        if ast.unparse(node.value) in ('os.environ', 'environ'):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                return key.value
        return None
    if not isinstance(node, ast.Call):
        return None
    target = ast.unparse(node.func)
    if target not in _ENV_GETTERS and not target.startswith('_env_'):
        return None
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _env_reads(root: Path, *, exclude: frozenset) -> dict[str, list[str]]:
    """扫 `root` 下的 .py，返回 `{变量名: ['server/x.py:12', ...]}`。"""
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob('*.py')):
        if exclude & set(path.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        rel = path.relative_to(SERVER.parent).as_posix()
        for node in ast.walk(tree):
            name = _literal_name(node)
            if name and name.startswith(PREFIXES):
                found.setdefault(name, []).append(f'{rel}:{node.lineno}')
    return found


def _declared() -> set[str]:
    return set(_schema()['settings'])


def _schema() -> dict:
    return json.loads((SERVER / 'config-schema.json').read_text(encoding='utf-8'))


def _errors_for(environment: dict) -> list:
    """`validate_environment` 抛异常而不是返回错误列表 —— 这里统一取出来。"""
    from server import configcontract
    try:
        configcontract.validate_environment(environment)
    except configcontract.ConfigurationError as exc:
        return exc.errors
    return []


def _notices_for(environment: dict) -> list:
    from server import configcontract
    return list(configcontract.validate_environment(environment).get('notices') or [])


class DeclaredTest(unittest.TestCase):
    def test_every_service_env_read_is_declared(self) -> None:
        """服务启动期读的变量却没登记 —— 一旦设置就启动失败。

        2026-11-24 之前这条只扫 `config.py`，所以 `accountguard.py` 与
        `externalplatforms.py` 里的 10 个变量一直是盲区。
        """
        reads = _env_reads(SERVER, exclude=_EXCLUDED_DIRS)
        missing = sorted(set(reads) - _declared())
        detail = [(name, reads[name][:3]) for name in missing]
        self.assertEqual(
            missing, [],
            f'{len(missing)} 个变量在服务启动期被读取但未登记到 config-schema.json，'
            f'设置它们会导致服务拒绝启动：{detail}')

    def test_every_test_env_read_is_declared(self) -> None:
        """测试里也不许自创 `WB_*` 变量：那会把契约测试变成永真的摆设。"""
        reads = _env_reads(SERVER / 'tests', exclude=frozenset())
        missing = sorted(set(reads) - _declared())
        self.assertEqual(
            missing, [],
            '测试读了自己发明的 WB_* 变量。要么登记进 schema（说明它是真配置），'
            f'要么改用不占用 WB_ 命名空间的变量名：{missing}')

    def test_declared_defaults_match_config_fallback(self) -> None:
        """schema 里的默认值必须与 `config.py` 的兜底值一致。

        两边不一致时，不设变量与设成默认值会得到不同行为 —— 这种差异只在
        切换部署方式时才暴露出来，最难查。
        """
        schema = _schema()['settings']
        text = (SERVER / 'config.py').read_text(encoding='utf-8')
        mismatches = []
        for name, spec in schema.items():
            if 'default' not in spec:
                continue
            match = re.search(rf"_env_\w+\(\s*'{re.escape(name)}'\s*,\s*([^)]+)\)", text)
            if not match:
                continue
            fallback = match.group(1).strip()
            declared = spec['default']
            if spec['type'] == 'boolean':
                expected = 'True' if declared else 'False'
                if fallback not in ('True', 'False') or fallback != expected:
                    mismatches.append((name, declared, fallback))
            elif spec['type'] == 'integer':
                try:
                    if int(fallback) != int(declared):
                        mismatches.append((name, declared, fallback))
                except ValueError:
                    mismatches.append((name, declared, fallback))
        self.assertEqual(mismatches, [],
                         f'{len(mismatches)} 处默认值与 schema 不一致（name, schema, config）：'
                         f'{mismatches[:8]}')

    def test_unknown_prefix_is_rejected(self) -> None:
        """确认守卫本身是活的：随便一个未登记的 WB_ 变量必须被拒。"""
        errors = _errors_for({'WB_DEFINITELY_NOT_REGISTERED': '1'})
        codes = [item for item in errors if item.get('code') == 'unknown_variable']
        self.assertTrue(codes, '未登记变量没有被拒绝 —— 守卫失效了（会假通过）')
        self.assertEqual(codes[0]['name'], 'WB_DEFINITELY_NOT_REGISTERED')

    def test_registered_traffic_flag_is_accepted(self) -> None:
        """回归：`WB_TRAFFIC_TAP` 曾经是未登记的，设置即宕机。"""
        names = [item.get('name') for item in _errors_for({'WB_TRAFFIC_TAP': '1'})]
        self.assertNotIn('WB_TRAFFIC_TAP', names,
                         '抓包总开关又被判定为未知变量 —— 设置它会直接宕机')

    def test_registered_trae_timeout_is_accepted(self) -> None:
        """回归：`WB_TRAE_TIMEOUT` 埋了更久的雷，同样从未被设置过。"""
        names = [item.get('name') for item in _errors_for({'WB_TRAE_TIMEOUT': '90'})]
        self.assertNotIn('WB_TRAE_TIMEOUT', names)

    def test_late_registered_account_and_trae_hub_variables_are_accepted(self) -> None:
        """回归：2026-11-24 前这 10 个都是「设置即宕机」的盲区。"""
        samples = {
            'WB_ACCOUNT_ACTION_MIN_INTERVAL': '2.5',
            'WB_ACCOUNT_ACTION_PER_MINUTE': '30',
            'WB_ACCOUNT_AUTOMATION_TOKEN': 'tok',
            'WB_ACCOUNT_BATCH_GAP': '5.0',
            'WB_ACCOUNT_BATCH_MAX': '4',
            'WB_TRAE_ENGINE_SWITCH': '/tmp/trae_engine',
            'WB_TRAE_HUB_ACCOUNTS': '/tmp/trae-hub/accounts',
            'WB_TRAE_HUB_BASE': 'http://127.0.0.1:8791',
            'WB_TRAE_HUB_HEALTH': 'http://127.0.0.1:8791/health',
            'WB_TRAE_HUB_PASSWORD': '/tmp/trae_hub_password',
        }
        for name, sample in samples.items():
            with self.subTest(name=name):
                spec = _schema()['settings'].get(name)
                self.assertIsNotNone(spec, f'{name} 没登记进 schema')
                errors = [item for item in _errors_for({name: sample})
                          if item.get('name') == name]
                self.assertEqual(errors, [], f'{name} 仍被判定为未知变量')


class DeployOnlyTest(unittest.TestCase):
    """部署层变量：只属于独立运行的更新器，绝不能让服务起不来。"""

    def _deploy_reads(self) -> dict[str, list[str]]:
        return _env_reads(SERVER / 'deploy', exclude=frozenset())

    def _deploy_only(self) -> frozenset:
        from server import configcontract
        return configcontract.DEPLOY_ONLY_VARIABLES

    def test_deploy_only_list_matches_actual_deploy_reads(self) -> None:
        """allow-list 必须与 `server/deploy/` 的真实读取完全一致。

        两个方向都要钉：漏一个 ⇒ 运维设置它还是会让服务起不来；
        多一个 ⇒ 清单腐烂，下一个人会以为它已经被处理过。
        """
        reads = self._deploy_reads()
        expected = set(reads) - _declared()
        actual = set(self._deploy_only())
        self.assertEqual(
            sorted(expected - actual), [],
            '这些变量被更新器读取、未登记 schema，却不在 DEPLOY_ONLY_VARIABLES 里'
            '（运维设置它们会让服务启动失败）')
        self.assertEqual(
            sorted(actual - expected), [],
            'DEPLOY_ONLY_VARIABLES 里有条目既没被更新器读取、也不该豁免了')

    def test_deploy_only_variables_are_not_service_variables(self) -> None:
        """同一个变量不能既是部署层又是服务配置 —— 那会让豁免掩盖真问题。"""
        service_reads = set(_env_reads(SERVER, exclude=_EXCLUDED_DIRS))
        overlap = sorted(service_reads & set(self._deploy_only()))
        self.assertEqual(overlap, [],
                         f'这些变量既在服务启动期被读、又被当成部署层豁免：{overlap}')
        self.assertEqual(sorted(set(self._deploy_only()) & _declared()), [],
                         'DEPLOY_ONLY_VARIABLES 不该登记进 schema（登记＝宣称服务会用它）')

    def test_deploy_only_variable_does_not_block_startup(self) -> None:
        """核心：把它写进服务环境只产出一条 notice，不拦住启动。"""
        for name in sorted(self._deploy_only()):
            with self.subTest(name=name):
                errors = [item for item in _errors_for({name: '1'})
                          if item.get('name') == name]
                self.assertEqual(errors, [], f'{name} 仍然会拦住启动 —— 全站不可用')
                notices = [item for item in _notices_for({name: '1'})
                           if item.get('name') == name]
                self.assertEqual(len(notices), 1, f'{name} 没有产出 notice')
                self.assertEqual(notices[0]['code'], 'deploy_only_variable')
                self.assertIn('WB2API_BASE', notices[0]['hint'],
                              'notice 要告诉运维服务到底看哪个变量')

    def test_deploy_only_value_is_never_validated(self) -> None:
        """连值都不校验：这些变量对服务没有语义，写错也不该拦住启动。"""
        for name in sorted(self._deploy_only()):
            with self.subTest(name=name):
                errors = _errors_for({name: 'not-a-port-@@@'})
                self.assertEqual(
                    [item for item in errors if item.get('name') == name], [])

    def test_notices_are_absent_by_default(self) -> None:
        """默认部署不该每次都刷一条告警。"""
        from server import configcontract
        result = configcontract.validate_environment({})
        self.assertEqual(result.get('notices'), [])


if __name__ == '__main__':
    unittest.main()
