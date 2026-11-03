# WorkBuddy × Codex：系统提示词兼容

适用于已获授权的 WorkBuddy 账号，经智汇网关和喜鹊连接 Codex 的场景。本说明记录 2026-11-03 已复现及验收的特定模板，不是对任意风控、渠道校验或供应商授权的通用绕过。

## 症状与定位

```text
Codex → 喜鹊 Responses 桥接 → 智汇网关 → WorkBuddy
                                      ↳ HTTP 400 / 11128
```

普通简短 API 请求成功；带 Codex 系统上下文的请求出现 `Illegal API invocation from an unapproved channel`。新开空会话可能正常，一旦带入相同系统模板再次失败。

将相同上下文直接调用上游也能复现，说明本次不是喜鹊凭空拦截。逐段对照将触发点缩减至 Codex 自带的身份前缀；用户消息中的 GitHub 链接不是已确认原因。

## 本次最小兼容规则

现网 `settings.prompt_defuse_rules` 增加了以下一条替换，仅在 WorkBuddy 的 system/developer 文本中生效：

```json
[
  [
    "You are a coding agent running in the Codex CLI",
    "You are a coding agent, running in the Codex CLI"
  ]
]
```

只增加一个逗号，不删除其余指令，也不将系统模板整体替换为另一套身份。已有自定义规则应保留，不能用示例数组盲目覆盖整个 settings。

- `prompt_compat_enabled=false` 可立即关闭；下一次请求起不再改写，没有分钟级缓存。
- Chat、Responses、Messages 转换后共用同一出站策略；喜鹊两条 Codex Responses 路径沿用该合同。
- Manager 已执行规则时，引擎不再叠加第二轮指纹改写。
- user、assistant、工具参数/结果、图片及非 WorkBuddy 平台保持不变。
- 不以某个用户触发的拒绝改变其他租户提示词，不自动吞掉供应商错误或猜测零费用。

**公开源码中的默认规则不自动包含这条现网配置。** 生产设置不是发布包内容，新部署需要通过自己的受信 settings 配置流程合并规则。已有生产实例不得为配置规则直接编辑签名源码、重建历史账本或覆盖 data 目录；变更前备份原值，使用比较后交换，回滚时恢复原 setting 即可。

### 配置顺序

1. 确认运行的是包含三协议公共出站阶段的受信版本，而不是只改了版本号的旧代码。
2. 管理员通过已认证的 `GET /api/settings/panel` 读取当前 `prompt_compat_enabled` 和 `prompt_defuse_rules`。普通用户不能据此获得写入权限。
3. 使用部署方已有的受信配置维护流程保存原值；不要把整份 settings、账号或数据库贴进聊天或工单。
4. 在原规则数组中合并上面的**一条**规则，检查同一匹配前缀是否已有其他替换；有冲突先人工复核。若开关原来明确为 false，不擅自启用。
5. 使用事务及比较后交换提交，确认原值没有在准备期间发生变化。当前产品没有公开的通用 settings 写入 API，也没有“导入 JSON 即启用”的按钮；不能向只读 panel 端点发送示例 JSON 当作配置完成。
6. 通过单次低配额请求确认回程终态和用户/工具内容边界。该设置每次请求读取，现有部署不需要为这条规则重启。

出问题时只撤销本次规则：仍须比较当前值与本次提交值；若管理员后来改过规则，停止自动回滚，避免覆盖新设置。也可通过同一受信流程设置 `prompt_compat_enabled=false` 暂停全部系统兼容规则，但这会同时停用其他已配置的规则，应先评估影响。

喜鹊中的模型分组是路由配置，与这条兼容 setting 分开。不要为修复 11128 删除原模型分组、轮换客户密钥或修改工具参数。

## 验证与验收

1. 使用已授权模型和测试密钥，将同一系统模板分别发送至 Chat、Responses、Messages。
2. 对照 user/tool 文本中的相同字符串及 JSON 数值 `11128`，确认不发生改写。
3. 经喜鹊测试 `/v1/responses` 和 `/backend-api/codex/responses`；读取终态，不能只看 HTTP 200。
4. 对图片和模拟工具结果回合验证，不能为验收执行真实工具命令。
5. 在隔离环境关闭规则，确认即时恢复原文；不要在公共生产流量中反复启停试验。

本次生产复验三协议均成功，两条喜鹊入口、原截图和工具回合均收到 `response.completed`。过小输出上限可能合法返回 `response.incomplete`；这与 11128 不同，不能把截断伪装成完成。

## 不适用的情况

新模板、账号授权不足、地区/模型不允许、实际渠道或内容策略拒绝、供应商策略变化，不由这条已知前缀兼容规则保证解决。保留错误码与请求编号，使用脱敏最小复现重新定位；不要扩大到全角色替换、工具 JSON 改写或多层盲重试。

相关社区案例：[magpie #182](https://github.com/yetone/magpie/issues/182)、[plugins #279](https://github.com/magpie-community/plugins/issues/279)。社区案例解释相似现象，不替代当前实例的对照验证。
