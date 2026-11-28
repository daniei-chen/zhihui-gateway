# 门禁契约：什么才算绿

这份文档写给「要改门禁的人」。`tools/ci_regression.py` 是 Python 侧唯一的门禁入口，
`quality.yml` 在 PR 上跑它、`release.yml` 复用同一提交的产物。改它之前请读完，
尤其是第一节——**这条门禁过去出现过「看起来绿、实际一条都没跑」的形态**。

## 一、绿的判据只有一个：真的跑了

以下四种都曾经或可以静默变绿，现在一律按「没跑」处理：

| 形态 | 以前的判据 | 现在的判据 |
|---|---|---|
| 整模块全 skip | `unittest` 的 `wasSuccessful()` 对 skip 返回 `True`，`testsRun > 0` 只看「加载出了几条」 | 按模块统计 `tests - skips`，**等于 0 判失败** |
| 条件式跳过（缺依赖 / 缺平台） | 同上，`skipUnless` 让用例安静消失 | 同上：整模块消失就是红 |
| 清单/glob 零命中 | 只看加载出 0 条 | 只看**执行了 0 条** |
| 脚本里 `|| true` 或 `exit 0` 兜住 | 命令退出码 0 | 不许整步放行；要放行只能逐条、带到期日（见第五节） |

两条赛道（`MODULES` 走 unittest、`PYTEST_MODULES` 走 pytest）**共用同一套判据与同一份白名单**。
此前 unittest 只看 `wasSuccessful()`、pytest 要求 `passed > 0`，同一个门禁两种标准——
那本身就是漏洞：整模块全 skip 在 unittest 赛道上会绿。

## 二、全 skip 的白名单：例外可见、有人签过字

有些模块的用例**本来**就只在特定平台或特定外部依赖下才有意义（例如必须在 POSIX 上跑的
权限位契约、需要真实 symlink 的用例）。这类模块允许整模块全 skip，但必须写进
`tools/skip_allowlist.json`：

```json
{
  "server.tests.test_example_posix_only": {
    "reason": "断言的是 POSIX 权限位；Windows 上 chmod 只切只读位，mode 恒为 0o666",
    "platform": "ubuntu-24.04（quality.yml 的 manager / gateway job）"
  }
}
```

规则：

1. 每条必须有 `reason`（为什么整模块不能跑）与 `platform`（谁来兜这个模块）。
2. **没有白名单条目就是红的**，不存在「默认容忍」。白名单的意义是让例外出现在 diff 里、
   有人为它签字，不是把口子换个地方藏。
3. 放行时会打印一行 `全 skip 但已签名放行：<模块>（<原因>；兜底：<平台>）`——
   这行必须能在门禁日志里搜到，否则等于没放行记录。
4. 新增条目要问一句：「这个模块真的没有 Windows/Linux 之外的第三种兜底吗？」

## 三、新增测试文件必须登记

新测试文件要同时进 `tools/ci_regression.py` 的 `MODULES`（unittest 风格）或
`PYTEST_MODULES`（pytest 风格函数）。`GateManifestCoverageTests` 会守住
「磁盘上有、清单里没有」的新文件；不登记等于 CI 根本不执行它。

## 四、`needs:` 与必检集合绑定

`quality.yml` 里 `manager` 与 `gateway` 都是 `needs: frontend`。后果是：

- frontend 红 → 这两个 job 的状态是 **skipped**；
- **GitHub 把 required check 的 skipped 计为通过**。

所以今天后端两门实际共用一道总闸：frontend。这在「frontend 自己也在必检集合里」时是可接受的，
但它是**结构性的**——只要有人动 `needs:`、或把 frontend 从必检集合里拿掉、或给 frontend 加
`if:`/path 过滤，后端两门就会在没有总闸的情况下「全部通过」。

因此：

> 动 `quality.yml` 的 `needs:`、动 frontend 的必检状态、或给任何必需 job 加 `if:` / `paths`
> 过滤，**必须在同一个 PR 里写明后果，并通知大队长（HOME-3）同步修改 main 的规则集
> `28284164`**。只写在 PR 描述里不够，规则集不改就等于没改。

同理，`.github/workflows/quality.yml` 的 `pull_request` 触发器**不允许**加
`paths` / `paths-ignore`：那会让 docs-only PR 的三个 required 全部 skip，
而 skip 计为通过 = **零验合并**，且没人会发现。`server/tests/test_ci_required_gates.py`
里有一条守卫测试盯着这件事。

## 五、只对一条命令放行，且豁免必须有到期日

任何「放行」都必须满足：放行的范围是**一条具体命令**（不是整个 step）、放行后立刻有一步
**断言产物可解析**（否则一次网络失败会被当成「没有问题」），且豁免条目带 `expires_on`。
没有到期日的豁免等于永久后门。
