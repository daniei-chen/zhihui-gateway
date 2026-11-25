"""部署前用新版本代码校验配置 —— 防的是 2026-11-23 那次 8 分钟崩溃重启。

## 那次发生了什么

```
23:25:34  daemon-reload && systemctl restart
23:25:38  ConfigurationError: Configuration rejected: WB_TRAFFIC_TAP: unknown_variable
23:25:41  Scheduled restart job, restart counter is at 1
23:25:44  ConfigurationError
...       StartLimitBurst=10 才停；23:33:18 人工恢复
```

**drop-in 先加了变量，代码的 schema 还不认识它。**每次启动都被拒，每 3 秒重试一次。

**这件事在重启前完全可查**：校验只依赖「当前环境」和「这一版的 schema」。

## 怎么测的（以及为什么不是 subprocess）

直接 import 那个脚本、调用 `main()`，只把 `subprocess.run` 换掉。

> 第一版是「起子进程 + 注入假 systemctl 到 PATH」。两个问题：
> 1. 先用了 `patch('subprocess.run')`，把**测试自己**的调用也 patch 了，
>    断言到的是假对象的 `returncode`，三个用例全假失败；
> 2. 改成 PATH 注入后，Windows 上根本跑不了 `#!/bin/sh`（`[WinError 2]`）。
>
> **两次都是测量工具骗我，不是代码有问题。** 第 10、11 次。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 未知变量判定为失败（退出 1） | 明明会崩溃却报通过，等于没加这一步 |
| 配置正常时退出 0 | 每次发布都被无谓拦下 |
| `systemctl` 读不到时退出 2 | 把「查不了」当成「没问题」（本章反复出现的坑） |
| 失败信息里带变量名 | 只报「校验失败」帮不到人定位 |
| 带空格的值不被切断 | 用空格硬切 `systemctl show` 的输出会得到错的变量值 |
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'deploy' / 'verify-runtime-config.py'
RELEASE = REPO          # 用仓库自身当「发布目录」，它有 server/ 与 schema

_SPEC = importlib.util.spec_from_file_location('verify_runtime_config', SCRIPT)
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)


def run_main(environment_stdout: str, returncode: int = 0, release: str | None = None):
    """调用脚本的 main()，`systemctl` 用给定的 stdout / 退出码代替。"""
    fake = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=environment_stdout, stderr='')
    buffer = io.StringIO()
    with patch('subprocess.run', return_value=fake), redirect_stdout(buffer):
        code = verify.main(['verify-runtime-config.py', release or str(RELEASE), 'workbuddy-web'])
    return code, buffer.getvalue()


class UnknownVariableTests(unittest.TestCase):
    def test_unknown_variable_exits_1_and_names_it(self):
        code, out = run_main('WB_TRAFFIC_TAP=1 WB_TOTALLY_MADE_UP_FLAG=1')
        self.assertEqual(code, 1, f'未知变量必须判定为失败——否则这一步等于没加。输出：{out}')
        self.assertIn('WB_TOTALLY_MADE_UP_FLAG', out,
                      '报错要指出是哪个变量，只报「失败」帮不到人')
        self.assertIn('没有重启', out,
                      '必须让人知道服务还在跑旧代码，没有半途而废')


class HappyPathTests(unittest.TestCase):
    def test_known_variables_pass(self):
        code, out = run_main('WB_TRAFFIC_TAP=1')
        self.assertEqual(code, 0, f'已知变量不该被拒；输出：{out}')
        self.assertIn('ok', out)


class CannotCheckTests(unittest.TestCase):
    def test_systemctl_failure_is_not_reported_as_pass(self):
        """读不到环境时必须报「查不了」，不能当成「没问题」。"""
        code, out = run_main('', returncode=1)
        self.assertEqual(code, 2,
                         'systemctl 读失败被当成通过，等于又回到「没机会出错就算没出错」')
        self.assertIn('读不到', out)

    def test_missing_schema_is_not_reported_as_pass(self):
        code, _ = run_main('WB_TRAFFIC_TAP=1', release='/tmp/definitely-not-a-release')
        self.assertEqual(code, 2)


class EnvironmentParsingTests(unittest.TestCase):
    def test_quoted_values_with_spaces_are_parsed(self):
        """`systemctl show` 的值可能带空格，不能按空格硬切。"""
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='WB_TRAFFIC_TAP=1 "WB_SOME_PATH=/a b/c"', stderr='')
        with patch('subprocess.run', return_value=fake):
            loaded = verify.unit_environment('x')
        self.assertEqual(loaded.get('WB_SOME_PATH'), '/a b/c',
                         '带空格的值被切断了')
        self.assertEqual(loaded.get('WB_TRAFFIC_TAP'), '1')


class NoSideEffectsTests(unittest.TestCase):
    def test_does_not_write_bytecode_into_the_release_tree(self):
        """检查工具绝不能改变被检查对象。

        第一版忘了关字节码缓存，import `server.configcontract` 时在
        `releases/v1.5.85/server/__pycache__` 生成了 .pyc，而发布树完整性
        校验拒绝任何 `__pycache__`：

            REJECTED: 发布树里有 __pycache__：.../server/__pycache__

        `seal` 直接失败。加这个用例防止回退。
        """
        self.assertIs(verify.sys.dont_write_bytecode, True,
                      '必须关闭字节码写入，否则会在发布树里生成 __pycache__')


if __name__ == '__main__':
    unittest.main()
