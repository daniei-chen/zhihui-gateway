# 公开引擎补丁人工补充审计（只读）

日期：2026-11-05。审阅对象为共享审计分支中 27 个公开 `.patch`，共 10,049 行（包括新增、删除、上下文与补丁内测试）；逐文件路径、行数及 SHA-256 见 `gateway-patches-reviewed.tsv`。其中 ops 五份补丁 3,764 行、manager/dev 十九份补丁 5,787 行、后补历史 enterprise-support 三份补丁 498 行。全部标为 `manual-full-patch`，不混同于完整生产 Go/Python 源文件全文审计。

**结论：本次只读补读没有形成新的、已复现的当前 P0/P1。没有修改任何补丁，没有构建、运行或调用私有引擎。公开补丁中测试的阅读不代表本轮执行了这些 Go/Python 测试，也不代表重新验证了仓库历史生产回执。**

## 来源和覆盖边界

- `ops/workbuddy-ingress/README.md` 明确 ingress 适用于留存 `20261028.u2114.1` 源码快照，不适用于同名可变 HEAD；compression 的前提又是已核验的 ingress 候选树。`ops/workbuddy-remediation-0.0.5/README.md` 同样声明差异不是完整独立引擎源码。
- 当前仓库可见补丁及来源/哈希清单，缺少它们对应的完整私有引擎基线。因此不能把 19 个历史 dev 补丁机械叠加后称为已重建 v0.0.7，也不能声称所有未出现在 hunk 中的函数、锁序、默认 timeout、依赖库或实际运行二进制都已审完。
- v0.0.7 的新增完整文件可以全文审阅；对既有文件仅能判断显示的修改和上下文。后续补丁覆盖的旧行为已按版本区分，例如 0006 的渠道拒绝软冷却与旧测试不应误当作当前行为，v0.0.5 取消共享提示词降级，v0.0.7 取消两路径后的额外原样重发。

## 关键可见控制链

### 发送次数和取消

v0.0.7 `provider_budget.go` 使用单请求共享 `spent/limit`，接受 1..8 的 `X-WB-Attempt-Budget`，每次 upstream attempt 的 `sending` observer 前授权；因此账号轮换、路径 fallback 与可见的 gzip 重试共用同一个预算。`X-WB-Remaining-Ms` 限制 1..600000，并从入站 context 派生 deadline。预算是发送授权次数，预发送构造或证据写失败可以保守消耗额度，不能当成供应商确认扣费次数。

v0.0.5 `DefinitelyUnsent` 仅将 dial/DNS 错误作为可换号的明确未发送；其余传输错误直接返回 `upstream_transport_uncertain`。v0.0.7 为 request client 设置 `CheckRedirect = http.ErrUseLastResponse`，3xx 关闭 body、取消 context 并返回未知，不让 307/308 自动重放 POST 逃逸发送预算。durable provider observer 失败同样中止后续发送；发送后证据失败保持保守未知。

`AttemptTimeout` 看门狗围绕 `http.Client.Do`，到 headers 时停止；成功 SSE 生命周期仍依赖入站总截止、monitorBody 与调用方 Close。不能把这一 watchdog 表述为整个长流的总时长限制。

### 凭据和共享刷新

v0.0.7 用跨进程文件锁、当前 token generation 检查、UID 检查、唯一临时文件、0600 权限、文件/目录 fsync 与原子 rename 防止并发刷新覆盖。JSON UseNumber 保留未知元数据的大整数；已存在源文件被删除不会被过时内存对象复活。Unix lock 使用 O_NOFOLLOW/O_NONBLOCK、普通文件与单硬链检查；Windows 分支使用 LockFileEx。文件锁只能协调遵守相同 lease 的写者，不是任意目录写权限攻击者的完整文件系统沙箱。

RefreshTokenContext 按 `*auth.Auth` 合并同进程并发调用；文件 lease 再协调不同对象/进程。等待者取消不取消共享 leader；共享错误的惩罚由 atomic CAS 最多领取一次。leader 的 `refreshContext` 为后台 60 秒，但可见调用只传到 `RefreshSerialized`，其回调仍为 `c.refreshToken(account)`，没有接收该 ctx。旧函数未完整出现在补丁，旧注释写有独立 30 秒 I/O，而命名线路非 chat client 可见 120 秒 timeout；因此**未证实 leader 的完整刷新 I/O 按 60 秒终止**，也不能据此断言它必然无限或必然等待 120 秒。应由完整私有源码和取消/关停测试确认。未把这一证据缺口升格为已复现 P1。

### 入口、连接和性能

Ingress 在鉴权后、选号前按实际字节读取并限制；单 body、总原始 payload 与并发租约都受 mutex 保护，取消/错误释放，成功则一直持有至转发结束。body slice 容量、JSON、压缩和响应聚合不属于原始 payload 预算，README 已作同样声明。compression 仅处理至少 256 KiB 请求，BestSpeed 且压后至少节省 10%；这里仅确认实现条件，没有冒称实测生产延迟收益。

0007 按线路缓存 Transport，未知线路报错，0013 的 `__direct__` 使用独立 OnceValue Transport 且 Proxy=nil。默认共享 client 和命名线路保持连接复用。可见 `TransportFor` 首次 cache miss 在锁外构造且未二次检查，两个首次请求可短暂产生重复 Transport；`ConfigureProxies` 重置表未关闭旧 idle connections。可见调用约定是启动一次，未证明线上热重载可达，故作为后续性能验证项保留，不在冻结期修改。

### 协议保真与账务观测

工具错配/空消息等确定请求级错误已有 fail-fast 路径；fallback 路径模型缺失不会直接写主路径模型负缓存。v0.0.5 不再以一个租户的拒绝触发全局提示词降级；用户/assistant/tool 历史不再被 sanitizer 改写；v0.0.7 删除裸数字 11128 改写。Manager 的 managed compatibility 标记还关闭引擎第二次 sanitizer，避免双重变化。

0010 将 reasoning fallback 内容置于 finish/usage 前；0014 在真实缺少 finish/DONE 时加入错误帧后再发兼容 DONE，并对日志标 502。v0.0.7 小输出预算不隐式注入 thinking，高显式 effort 保持。v0.0.6 不再将未知模型窗口伪造为 1M，并区分 native/default/options/conflict。

## 必须保留的后续问题和不确定性

1. **Go 内部累计响应内存仍未证明有界。** 0009/0010 可见 StreamHint 的 `reasoningBuf strings.Builder` 与 `tailBuf []string` 没有累计字节上限；公开后续补丁未显示新增限额。Manager 本轮 64 MiB 预算约束引擎向 Manager 已吐出的 wire/decoded 字节，不能约束 Go 尚未吐出的 tail。总截止和取消提供时间边界，但不是全进程 heap/RSS 上限。完整私有实现可能有其他保护；需补充源码或合成 provider 的运行验证才能定量评级。
2. **日志脱敏不是完整引擎合同。** Ingress 已删除原始请求 dump，provider_audit 用 JSON 元数据而非 body/token；但历史 client hunk 仍有上游错误 body 前 200 字符、transport error `%v`，代理地址 parse/no-host 的错误中亦可能包含原配置字符串。未在公开补丁中看到覆盖全部这些路径的 secret redaction。只可确认具体可见日志形状，不能保证凭据/被上游回显的内容绝不会进入私有日志。未使用真实凭据验证。
3. **后台自动任务与主推理预算分开。** 0012 的 urllib lighthouse chat 使用每次 socket timeout=90，逐行寻找 ID，未在可见函数中设置累计响应字节或全调用 deadline；找到 ID 后 with 块关闭连接。0015 global balance 查询插在旧 continue 之前，未显示与后续 accountGap/ctx 检查共享。自动任务是否完全遵循退出/限速需完整调用链验证。
4. **会话隔离的证明不完整。** 0005 未提供显式 conversation ID 时从首条 user 文本派生 ID；同首条文本的独立会话有相同派生值。Manager 也有显式/稳定前缀 session 注入。可见部分没有足以证明供应商会话状态能越租户读取的证据，故不宣称跨租户泄露；但不能把该散列单独当作租户隔离边界，隔离仍需认证、上游账户/路由与完整 session 实现共同验证。
5. **元数据与产品能力声明是历史兼容事实。** 0016 在 global DeepSeek 档位上额外补 max，0017/0019 的指纹规则来自仓库历史实测描述。本次审计没有真实供应商请求，不能替这些历史兼容假设重新背书当前供应商产品能力。

## 历史 enterprise-support 可选补丁补读

追加全文阅读 `manager/docs/enterprise-support/manager-tencent.py.patch`（103 行）、`manager-wb2api.py.patch`（14 行）、`workbuddy2api-enterprise-support.patch`（381 行），共 498 行，包括新增 Go 文件及补丁内解析测试。它们是历史可选方案，不代表当前完整私有引擎实现，也没有在本轮应用、编译或执行。

- Python 额度分支用 enterprise_id 选择企业端点，并明确 `remain = limitNum - credit`；周期按 UTC+8 墙钟转 epoch，读取失败返回未知。可见分支把任意负 limit 视为无限额、缺 limit/credit 分别缺省为 0，未形成严格字段完整性/有限范围证明。这是余额快照语义，不能与本轮收费 ledger 的严格 usage 解析混同。
- Go 结构体直接解 data 而非重复解信封，循环时间解析保留时区。额度先对 limit/used 分别取整再作 int64 减法，与 Python 剩余浮点口径不完全相同；缺字段/极大值的转换界限、无限额返回 remain=0 后是否被 Pool 当作耗尽，需要完整实现验证。补丁内测试验证解析形状，余额数学测试只计算常量，不证明完整 HTTP/Pool 调用链。
- 可见 `RefreshBalances()` 顺序遍历全池，返回 UID/nickname/余额与错误文本；handler 只接 `withAuth`，不在 AdminEnabled 注册块内，没有把 `r.Context()` 传入、单请求账户预算、并发去重或总体 deadline。若选择应用该历史方案且把引擎调用 key 当作面向低权限用户的密钥，应单独封闭该管理动作；本仓库公开片段不足证明当前私有引擎已经采用该入口或公网可达，未上升为当前已证实 P1。
- `manager-wb2api.py.patch` 只改列表中的 checkin_supported 标志。注释声称定时任务不再请求，单靠这个 hunk 不能证明所有后台执行路径都强制检查该标志，仍需读取实际调用方。Python `.strip()` 与 Go `EnterpriseID != ""` 对空白 ID 的判断也不完全一致。

本补读没有形成新的已复现当前 P0/P1。业务代码和补丁均保持冻结；发现清单与覆盖边界已交主审计报告整合。
