# 智汇网关 0.0.5

日期：2026-11-03。内部精确构建号与产品版本分开记录；以受信发布事务和签名回执确定实际部署状态。

## 范围

本版包含 A01–A09 九项高优先级修复及 S01 内部连接密钥轮换。修复先以 `v0.0.4+audit.20261103.2` 验证上线，再发布产品版本 `0.0.5`；0.0.5 不再更换引擎、前端、账号或密钥。

## 协议边界

| 能力 | 结果 |
| --- | --- |
| Chat / Responses / Messages 系统兼容 | WorkBuddy 共用同一明确策略，可即时关闭 |
| user / assistant / tool / 图片数据 | 不做指纹文本替换 |
| Responses 显式完整历史、工具回合、SSE | 保留并回归验证 |
| previous_response_id / conversation | 当前不支持；转发前 400 `unsupported_*` |
| background=true | 当前不支持；400 `unsupported_background` |
| parallel_tool_calls=false | 上游不能保证串行；400 `unsupported_parallel_tool_calls` |
| function strict=true | 上游不能保证严格参数；400 `unsupported_strict_tools` |
| 自定义工具 grammar | 仅描述性提示，不宣称严格执行 |
| 上游拒绝、用量未知 | 保留失败及 unknown，不伪造成功或零费用 |

## 权限与取证

- 旧预算核销 `/api/budgets/{request_id}/resolve` 需要 15 分钟内登录的管理员本人会话，普通 admin API Token 不接受。
- 请求必须为 unknown，并有可信生命周期结束证明或明确已退出的归属进程。超时不构成结束证明。
- 原因和证据说明各需 8–500 字符；不要粘贴授权文件、密码或 Token。提交金额不能覆盖已经记录的供应商费用证据。
- `/api/logs/provider-attempts` 提供三层独立调用身份；`/api/logs/provider-evidence-health` 提供待采集、最老等待时间和冲突状态。
- `sending` 只证明发送意图已持久化，`headers` 证明收到响应头；均不等于供应商费用、完成或取消证明。每次调用的金额仍不推算。

## 恢复与回滚

- 每日原有本地加密恢复任务继续运行，应用包内增加配对账本及实际隔离恢复验证。
- 配对快照短暂按 main→wallet 顺序获取写锁，复制后释放锁，再做完整性和业务一致性检查。
- 恢复只写入新隔离目录，禁用客户密钥和会话，将凭据移出引擎常规读取目录；必须人工核对之后才能启用，不能恢复旧备份直接返还已消费余额。
- 发布回滚只切换签名代码及必要镜像，不恢复业务数据库，不重新启用已撤销的内部连接密钥。
- FULL 保证依赖操作系统和存储实现；已进行真实进程中止后的 WAL 恢复测试，没有进行生产断电试验。

## 验证基线

- 修复构建：Linux Manager 严格门禁 4,301 passed / 9 既有 skipped / 0 failed，3,011 subtests。
- Linux Go 引擎：18 个包，1,727 passed / 0 failed。
- 发布控制合同：30 passed，涵盖单写者、阶段中断恢复、显式回滚、新密钥保留和前端保持原样。
- 生产三协议均 HTTP 200；不支持的四类语义均稳定 400；admin Token 和过期近期会话均 403；喜鹊 `/v1/responses` 与 `/backend-api/codex/responses` 均收到 `response.completed`。
- 配对实机快照及新目录隔离恢复通过，密文真实解密校验通过，现网数据未被恢复覆盖。

0.0.5 自身的严格门禁和部署回执由受控发布流程单独生成，不能把前一构建的测试记录冒充新版签名证明。
