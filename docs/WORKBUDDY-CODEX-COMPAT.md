# WorkBuddy × Codex / Claude：系统模板兼容

0.0.6 已内置六条已验证规则。适用于经智汇网关和喜鹊接入已授权 WorkBuddy 账号的场景；不保证解决所有渠道、内容或账号策略拒绝。

## 为什么普通 API 正常，编码客户端却失败

```text
Codex / Claude → 喜鹊或兼容 API → 智汇 → WorkBuddy
                                         ↳ HTTP 400 / 11128
```

相同完整系统模板直连上游也曾复现 `Illegal API invocation from an unapproved channel`。逐段对照将 Codex 触发点缩减至身份前缀；Claude 的身份句、计费头提示、反馈句和分支提示也有已验证样本。新建空会话可能暂时正常，重新带入同一模板后再次失败。用户消息中的 GitHub 链接和喜鹊模型分组不是本次已确认原因。

## 0.0.6 的内置规则

[规则包](../manager/server/workbuddy-compat.json)版本为 `workbuddy-prompts-20261103.1`，与签名源码一起交付。

| 规则 ID | 适配内容 |
| :--- | :--- |
| `referenced-paths` | 已有的 `@` 路径引用说明措辞 |
| `codex-cli-identity` | 在 `You are a coding agent running in the Codex CLI` 的 `agent` 后加逗号 |
| `claude-cli-identity` | Claude CLI 身份句的最小措辞调整 |
| `claude-billing-metadata` | 系统文本中的计费头名称措辞 |
| `claude-feedback-template` | 固定反馈模板措辞 |
| `claude-branch-template` | 固定主分支说明措辞 |

**只处理 WorkBuddy 的 system/developer 文本。** 用户消息、助手消息、工具调用参数、工具结果、图片及非 WorkBuddy 平台保持原文。Chat、Responses、Messages 共用同一出站策略；Manager 执行后，引擎不再叠加第二轮改写。计费头规则只改系统文本中的模板名称，不改网络计费、账号凭据或真实授权。

每次请求记录规则包版本和命中 ID，不记录提示词原文。不因某个用户的失败改变其他会话；发送已被接受或是否被接受未知时，不为兼容规则重新发送。明确拒绝仍返回失败。

## 设置、覆盖与关闭

- 新部署默认包含六条规则，**无需再手动注入 Codex 临时规则**。0.0.5 的旧说明仅适用于当时的临时设置方式。
- 已有 `settings.prompt_defuse_rules` 保留，并对相同匹配文本优先。它不替换整个内置包。
- `prompt_compat_enabled=false` 即时关闭全部系统兼容规则，随后请求生效；不要在公共流量中反复开关做对照。
- 不直接修改签名源码，也不覆盖 data 目录、重建账本或轮换客户密钥。

维护自定义规则时，管理员先经已认证的 `GET /api/settings/panel` 读取当前值，通过部署方的受信配置维护流程备份并合并规则，用事务和比较后交换提交。该 panel 是只读端点，不能向它 POST 示例 JSON 当作配置完成；产品没有通用 settings 导入接口。已有明确关闭的开关不擅自开启。

撤销自定义变更时仍须比较当前值，避免覆盖其他管理员后续修改；代码回退保留业务数据及已轮换的内部密钥。

## 出错时怎样判断

1. 保留请求编号、实际 HTTP 状态、上游错误码、规则包版本和命中 ID，不贴密钥或完整会话。
2. 比较同一模板直连智汇、经喜鹊以及新空会话的结果；不能用不同提示词的成功请求证明原请求正常。
3. 上游预流式 400/401/403/429 保留实际状态。HTTP 200 后仍要看 SSE 终态；断流、`response.failed` 和截断不是完成。
4. 历史“记录 502、实际上游 HTTP 400”的行按真实状态展示，同时保留原始记录状态；不改写旧消费事实。
5. 新模板需要脱敏最小复现。不要扩大到全角色替换、工具 JSON 改写、整段系统提示词降级或多层盲重试。

## 验证与边界

隔离回归验证三协议的规则作用域、关闭即生效、覆盖、用户/工具原文和数值 `11128` 保真。生产验收核对国内账号的 Chat / Responses / Messages、喜鹊两条 Responses 入口、图片和模拟工具结果回合；具体结果见 [0.0.6 验收回执](../ops/workbuddy-stability-0.0.6/production-verification.json)。模拟工具验收不执行真实命令。

`previous_response_id`、`conversation`、后台 Responses、串行工具保证和严格工具模式继续明确拒绝；本版本没有新增服务端状态服务。新模板、授权不足、模型/地区限制、真实内容拒绝和供应商策略变化均不由这六条规则保证解决。

相关社区案例：[magpie #182](https://github.com/yetone/magpie/issues/182)、[plugins #279](https://github.com/magpie-community/plugins/issues/279)。案例解释相似现象，不能替代当前实例验证。更多配置见 [0.0.6 发布说明](RELEASE-0.0.6.md)。
