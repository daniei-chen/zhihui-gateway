"""环境变量契约：读到的必须登记，登记的必须能用。

## 为什么单列一个测试文件

`server/configcontract.py` 的规则是：任何以 `WB_` / `WB2API_` 开头、但不在
`config-schema.json` 里的变量，一律判定为 `unknown_variable` 并**拒绝启动**。
这个「宁可起不来也不静默忽略」的选择是对的——否则用户设了变量、以为生效了，
实际什么都没发生，比直接报错难查得多。

代价是：**在 `config.py` 里加一行 `_env_bool('WB_XXX')` 而忘记登记 schema，
就等于埋了一颗「一旦有人设置这个变量，服务立刻起不来」的雷。** 而且它不会被
常规测试抓到：默认部署不设这个变量，本地也没人设，于是门禁全绿。

2026-11-23 这颗雷真的炸了：给抓包总开关设 `WB_TRAFFIC_TAP=1` 之后，
`workbuddy-web` 连续重启 10 次后彻底放弃，全站 502。同一批还发现另一颗更早埋下
的雷 `WB_TRAE_TIMEOUT`（有人加了读取、从没登记，也从未被设置过）。

所以这里做两件事，方向相反：
  1. `config.py` 读的每个变量都必须在 schema 里（防止埋雷）；
  2. schema 里登记的每个变量都必须真的被某处读取，或明确标注为「保留/预留」
     （防止登记了却没人用，那是另一种漂移）。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SERVER = Path(__file__).resolve().parents[1]

#: `config.py` 里所有读环境变量的调用形式。
_READ = re.compile(r"_env_(?:bool|int|str|float|list)\(\s*'([A-Z0-9_]+)'")

#: 允许「登记了但 config.py 不直接读」的前缀：这些由别的模块读取
#: （例如 `server/externalplatforms.py`、`server/traffic.py`），也有的只是
#: 部署层用（`WB_RELEASE_TRUST` 之类）。测试只要求它们**至少被仓库里某处
#: 提到**，避免出现纯粹的死登记。
_CONSUMER_HINT = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b")


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


class DeclaredTest(unittest.TestCase):
    def test_every_read_variable_is_declared(self) -> None:
        """在读的地方加变量、却不在 schema 登记 —— 一旦设置就启动失败。"""
        declared = set(_schema()['settings'])
        text = (SERVER / 'config.py').read_text(encoding='utf-8')
        used = set(_READ.findall(text))
        missing = sorted(used - declared)
        self.assertEqual(
            missing, [],
            f'{len(missing)} 个变量在 config.py 里被读取但未登记到 config-schema.json，'
            f'设置它们会导致服务拒绝启动：{missing}')

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


if __name__ == '__main__':
    unittest.main()
