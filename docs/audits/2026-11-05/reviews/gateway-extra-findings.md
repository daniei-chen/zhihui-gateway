# 末轮只读复核与生产源补读

范围：主代理指定的原子 IP 绑定/预算收费调用链，以及追加的 31 个生产文件。
本轮不修改业务源码，不扩展 pytest 测试，不运行网络请求或生产数据操作。
追加覆盖见 `gateway-extra-reviewed.tsv`：**31 / 31 个文件完整人工阅读，共 4,937 行**，附当前源码 SHA256。
这里的 FULL 是源码阅读与控制/数据流分析，不代表所有可能状态已经动态验证。

## 唯一立即报告的财务一致性遗漏

### G-REVIEW-01：主账拒绝的非法 token 回执，钱包实时路径仍可能按零费用结清

严重度建议：P1（财务未知证据被当成可结清回执）。状态：主代理已确认并修复 portalwallet.receipt，本子任务只读复核其 diff；主代理的真实钱包合并回归正在执行。本子任务没有修改该文件。

触发证据：

```json
{"prompt_tokens":9007199254740992,"completion_tokens":0,"credit":0}
```

1. 本轮 `metering.parse_usage` 对 `2**53` token 返回非法/未知，`_record` 写入 `usage_complete=0`，`budget.settle` 将已发送预留保留为 `unknown`。
2. 复核时 `portalwallet.receipt` 仍使用旧条件：两个 token 都是 Python int 且 `>=0`。上述回执因此通过，写 `portal_spend_receipts.payload={"amount_units":0}`。
3. `portalwallet.apply` 看到整数非负 amount 后调用 `_release(...,0)`，把钱包记为 settled 并退回冻结余额；这与同一请求主账的 unknown 结论矛盾。
4. 相同事实若走 `portalwallet.replay_receipt`，由于它要求 `facts.usage_complete==1`，得到 `amount_units=null`，钱包保持 unknown。

因此故障/重放时序会改变用户钱包的财务结论。问题不是新引入了一条对外收费 API，而是本轮主账采用更严格证据后，旧钱包实时入口没有同步收敛。
主代理已将 live receipt 与 _record/budget 的 `parse_usage.complete && !malformed` 判断统一，并保留 credit 精度约束；新增真实钱包 live/replay 两个回归方法，主代理报告修前均失败（超大 token 放行零费用、合法字符串数字错误冻结），修后合并组正在运行。最终测试结果以其交接为准。

## 原子性与锁顺序复核结果

对以下代码与相关 helper/事务实现进行了只读追踪：keysvc.claim_ip、keysvc.validate/touch、gateway._prepare_forward_async/_record、budget.prepare/forwarded/settle/finish、portalpolicy、portalwallet.reserve/forwarded/receipt/apply/replay_receipt、db.transaction、portalstore.transaction。

- `claim_ip` 的当前限制读取、已绑定查询、计数、插入在同一个 `BEGIN IMMEDIATE` 内；同进程有 DB RLock，跨 SQLite 连接由写事务串行化，`PRIMARY KEY(key_id,ip)` 保护重复绑定。检查 enabled、expiry、max_ips 时不使用旧快照的这些字段。
- 最终完整 validate 结束后才 claim；不会仅通过 header 预检或模型列表查询就绑定新 IP。
- claim 的 main DB 事务已退出，才调用 budget.forwarded；forwarded 的 portal DB 事务也已退出，才进入 main DB 更新。没有为了节省事务把它们变成反向嵌套，没有发现本轮引入的 P0/P1 交叉锁死锁。
- 最终工作线程复制 contextvars 并受所属 task 管理；取消检查位于 claim 前后，异常收尾等待 worker join，避免准入/预算归属先释放、后台工作仍在推进。取消刚好发生在最后检查后，可能留下保守的 forwarded/unknown marker，但后续 coroutine 不继续发网络；这不是证明消费为零的依据。
- _record、budget.settle、request_logs、usage_events、key touch 在同一个主 DB 事务内。有效部分 token 保留，非法部分不编造成完整零回执；更新失败会整体回滚并走 spool。没有发现另一条新引入的 P0/P1 部分提交/重复收费回归。
- 原子 IP 绑定事务不等于把授权到真实 socket send 的全过程变成数据库事务；配置在准入后被管理员更改仍遵循既有在途语义。本轮没有通过跨网络持锁来声称绝对即时撤销。

## 补读范围结论

追加 31 个文件涵盖密钥/代理/只读 API/入站安全/上游/服务 token/预算路由，realm/scope/余额/模型目录服务，以及身份桥、metrics、诊断、统计、发布元数据、供应商回执、QTG 适配器、用户文件锁、正文编码与预算复核 helper。

没有发现第二个能够在本轮限制下确认、需要打破代码冻结的 P0/P1。未把以下源码中可见的既有运行边界误报成已复现阻断缺陷：

- read-api usage-matrix 保留无上界的全历史契约；大数据量资源成本需单独测量。
- 模型元数据/realm 读取仍有缓存期限、供应商字段有效性及同步文件读取等边界；没有伪称已验证最新供应商实际能力。
- 代理设置多字段管理写入及归档文件操作不等于跨进程事务；调用权限、现有文件协议和部署形态仍是前提。
- iputil 的转发头信任依赖边缘代理覆盖 header 与可信网段配置；仅源码阅读不能证明实际 Nginx/网络路径配置。

主审计报告应结合主代理的最终完整 harness、其它子任务证据和未覆盖范围；这里未新增动态测试数量。
