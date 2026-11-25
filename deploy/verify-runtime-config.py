#!/usr/bin/env python3
"""用**待发布那一版**的代码校验当前服务的环境，重启之前就发现配置会被拒。

## 为什么需要这个

2026-11-23 23:25 真实发生过一次：有人 `daemon-reload && systemctl restart`，
而那个 drop-in 里加了 `WB_TRAFFIC_TAP=1`，**当时代码的 schema 还不认识这个变量**。

于是每 3 秒一轮：

```
23:25:38  ConfigurationError: Configuration rejected: WB_TRAFFIC_TAP: unknown_variable
23:25:41  Scheduled restart job, restart counter is at 1
23:25:44  ConfigurationError
23:25:47  restart counter is at 2
...       一直到 StartLimitBurst=10 才停
```

**约 8 分钟完全不可用**（23:25 → 23:33）。

关键点：**这件事在重启前就能查出来。**配置校验读的是环境变量和当前版本的
schema，两者在重启前都是已知的。原来的流程只是没人去比。

## 用法

    verify-runtime-config.py <release-dir> <unit>

它会：
  1. 从 `systemctl show -p Environment` 取该 unit 的**实际**环境（含所有 drop-in）；
  2. 用 `<release-dir>/server/configcontract.py`（即待发布那一版）校验；
  3. 通过则退出 0 并打印 ok；不通过则退出 1 并打印**具体哪一项**被拒。

## 为什么不直接用当前进程的环境

`rollout-rolling.sh` 是被 sudo 拉起的，它的 `os.environ` 是调用者的，
**不是 systemd 给服务准备的那一份**。用错环境会得到一个假的「通过」。
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path


def unit_environment(unit: str) -> dict[str, str]:
    """取 unit 的实际环境（`systemctl show` 会把所有 drop-in 合并进来）。"""
    # 用 PATH 查找而不是写死 /bin/systemctl：测试要靠覆盖 PATH 注入一个假的
    # systemctl，写死路径就没法在不 patch subprocess 的前提下测了。
    result = subprocess.run(
        ['systemctl', 'show', '--property=Environment', '--value', unit],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        # 这里抛 RuntimeError 而不是 SystemExit：调用方要能把它归为
        # 「查不了」（退出 2），而不是「配置被拒」（退出 1）。
        raise RuntimeError(f'读不到 {unit} 的环境：{result.stderr.strip()}')
    # 输出形如 `A=1 B=2 "C=x y"`，用 shlex 切才不会被带空格的值骗到。
    tokens = shlex.split(result.stdout)
    environment: dict[str, str] = {}
    for token in tokens:
        if '=' in token:
            name, _, value = token.partition('=')
            environment[name] = value
    return environment


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print('用法: verify-runtime-config.py <release-dir> <unit>', file=sys.stderr)
        return 2
    release = Path(argv[1]).resolve()
    unit = argv[2]

    schema = release/'server'/'config-schema.json'
    if not schema.is_file():
        # 版本目录不完整时不要伪装成「通过」——直接说不出来。
        print(f'!! {release} 下没有 server/config-schema.json，无法校验')
        return 2

    sys.path.insert(0, str(release))
    try:
        from server.configcontract import ConfigurationError, validate_environment
    except Exception as exc:                       # noqa: BLE001
        print(f'!! 无法导入 {release} 的 configcontract：{type(exc).__name__}')
        return 2

    try:
        environment = unit_environment(unit)
    except Exception as exc:                       # noqa: BLE001
        # 「查不了」必须和「配置被拒」区分开：前者是环境问题（退出 2），
        # 后者是这一版真的起不来（退出 1）。混成一个数字会误导。
        print(f'!! 读不到 {unit} 的环境：{exc}')
        return 2

    # `validate_environment` 在校验失败时**抛异常**（不是返回错误列表），
    # 成功时返回快照 dict。这个差别踩过一次，别改成返回值判断。
    try:
        validate_environment(environment)
    except ConfigurationError as exc:
        print('!! 这一版会拒绝当前配置，重启必定失败：')
        # ConfigurationError 上有 `.errors`（原始的结构化列表），
        # `args[0]` 只是拼好的一句话 —— 迭代它会把字符串逐字拆开。
        for item in getattr(exc, 'errors', None) or []:
            if isinstance(item, dict):
                print(f"   {item.get('name', '?')}: {item.get('code', '?')}")
            else:
                print(f'   {item}')
        print('   服务仍在跑旧代码，没有重启。修好配置再发布。')
        return 1
    except Exception as exc:                       # noqa: BLE001
        print(f'!! 校验本身没跑成：{type(exc).__name__}')
        return 2

    print(f'   ok 配置校验通过（{unit}，{len(environment)} 个变量）')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
