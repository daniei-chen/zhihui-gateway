# v1.5.85：发布前先确认新版本能接受当前配置（第 55 轮）

## 1. 起因：一次 8 分钟的崩溃重启

查日志时发现 2026-11-23 有一段：

```
23:25:34  systemd: Stopping workbuddy-web.service
23:25:34  systemd: Started workbuddy-web.service
23:25:38  server.configcontract.ConfigurationError:
          Configuration rejected: WB_TRAFFIC_TAP: unknown_variable
23:25:38  Main process exited, code=exited, status=1/FAILURE
23:25:41  Scheduled restart job, restart counter is at 1
23:25:44  ConfigurationError（又一次）
23:25:47  restart counter is at 2
...
23:26:38  restart counter is at ...
23:33:18  终于起来了
```

**约 8 分钟完全不可用。**

根因：那个 drop-in 里先加了 `WB_TRAFFIC_TAP=1`，而**当时代码的配置 schema 还不认识这个变量**。
于是每次启动都被拒，`Restart=always` + `RestartUSec=3s` 让它每 3 秒重试一次。

`StartLimitBurst=10` 最终把它拦下来（这是好事，否则会无限重启），
但**那 10 次尝试已经花掉了 8 分钟**。

## 2. 关键观察：这件事在重启前完全可查

配置校验只依赖两样东西：

1. **当前环境**（`systemctl show -p Environment` 能拿到，含所有 drop-in）
2. **这一版的 schema**（`releases/current/server/config-schema.json`）

**两者在重启前都是已知的。**原流程只是没人去比。

## 3. 改法

新增 `deploy/verify-runtime-config.py`：

```python
environment = unit_environment(unit)          # systemctl show 拿实际环境
validate_environment(environment)             # 用待发布那一版的 schema 校验
```

**三种结果要分开**（这是设计上最要紧的一点）：

| 情况 | 退出码 | 为什么不能合并 |
|---|---|---|
| 配置被拒 | **1** | 这一版真的起不来，必须停 |
| 读不到环境 | **2** | 「查不了」不等于「有问题」，也不等于「没问题」 |
| 通过 | 0 | — |

接入 `rollout-rolling.sh` 的 `restart_gateway` 之前，`--check` 模式也跑。
失败时**服务仍在跑旧代码**，不会留下半成品。

## 4. 踩的两个坑（都是测量工具骗人）

### 4.1 忘了关字节码缓存，污染了发布树

第一次在生产试跑：

```
REJECTED: 发布树里有 __pycache__：/opt/workbuddy-manager/releases/v1.5.85/server/__pycache__
```

脚本 `sys.path.insert` 到发布目录后 import `server.configcontract`，
Python 默认会写 `.pyc` 到 `server/__pycache__`，而发布树的完整性校验**拒绝任何 `__pycache__`**。

**seal 直接失败。**

修法是在任何 import 之前设 `sys.dont_write_bytecode = True`。并加了一个测试守住：

```python
def test_does_not_write_bytecode_into_the_release_tree(self):
    self.assertIs(verify.sys.dont_write_bytecode, True, ...)
```

**教训**：**检查工具绝不能改变被检查对象。** 这句话值得当规则记下来。

### 4.2 测试本身写了三遍才对

| 版本 | 错在哪 |
|---|---|
| 第 1 版 | `patch('subprocess.run')` 把**测试自己**调用 `subprocess.run` 也 patch 了，断言到的是假对象的 `returncode`，三个用例全假失败 |
| 第 2 版 | 改成 PATH 注入假 `systemctl`，但 Windows 上跑不了 `#!/bin/sh`（`[WinError 2]`） |
| 第 3 版 | 直接 import 模块调 `main()`，只替换 `subprocess.run` —— 跨平台通过 |

**两次都是测量工具骗我，不是代码有问题。**这是本章记录的第 10、11 次。

顺带还发现 `ConfigurationError` 有 `.errors` 属性，`args[0]` 只是拼好的一句话 ——
迭代它会把字符串逐字打印成 `C\no\nn\nf\ni\ng...`。

## 5. 部署验证

| 项 | 结果 |
|---|---|
| 版本 | **v1.5.85** |
| 构建提交 | `bff474ec7ca4eb1c5615743d4013df6f962d304e` |
| **部署窗口** | **74 个请求，0 个 5xx** |
| 门禁 | 1,122 后端 + 245 前端 + F821=0 + F811=0 |
| 新检查实测 | `ok 配置校验通过（workbuddy-web，38 个变量）` |
| 发布树残留 `__pycache__` | **none** |

**部署日志里能看到新步骤真的执行了：**

```
== 用这一版的代码校验当前配置
   ok 配置校验通过（workbuddy-web，38 个变量）

== 重启网关（空窗期间由中继重试兜住）
```

## 6. 顺带发现：CI 用的是显式测试列表

改完测试跑门禁，发现测试数还是 **1,117**，而我新增了 5 个。

查 `tools/ci_regression.py` —— 它不是自动发现，而是**显式列出的模块名**。
新文件不在列表里就永远不会被跑。

加入后变成 **1,122**（6 个新用例，含字节码那个）。

**这个坑值得单独记住**：新增测试文件后，如果门禁数字没变，说明它根本没跑。
「测试通过」和「测试被执行」是两件事。

## 7. 待办

| 项 | 状态 |
|---|---|
| v1.5.84 / v1.5.85 Release | CI 构建中 |
| 归档首次真实运行 | 约 2026-12-13 |
| age 私钥离线保管 | 运维 |
| 异地备份 | 运维 |