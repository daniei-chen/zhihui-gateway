# 网关数据路径审计交接

审计基线：`4c6b4cb8cb790698e0753909745dfe8a7c78dd9f`；共享分支：`audit/strict-review-20261105`。
子任务负责人：`/root/gateway_audit`。本文件汇总本子任务的已确认问题、修复、验证和剩余范围；不能据此宣称整个仓库已形式化验证。
业务修改已冻结；未提交、推送、部署，也未使用真实供应商凭据或模型请求。

## 阅读范围

逐文件清单：`gateway-reviewed-files.txt`，明确区分 FULL / PARTIAL / AUTOMATED。
完整人工阅读的业务/辅助源码共 33 个文件，写清单时共 14,425 行；其中原分配 29 个源文件，另外完整读了 config.py、upstreambody.py、asyncwork.py、build_config_contracts.py。
keysvc/budget/DB/portalwallet 等共享边界仅针对调用链阅读，由主代理承担其完整审计。
列出的文件全部完成阅读不等于穷尽所有交错时序，也不覆盖供应商端内部实现。

## 已确认并修复

### 1. 请求与响应的资源边界

- **管理请求容量绕过/内存放大**：GET/HEAD/OPTIONS 的实际请求体可以在无 Content-Length 时绕过并发名额。现在在首次实际帧发现正文或 more_body 时抢占名额；真正无正文的页面读取仍保持可用。原先每帧放入 list 再 join，128 KiB 单字节帧导致约 11.7 MB 瞬时分配；改为 bytearray，并在立即就绪的帧循环中周期性让步，保证超时/取消可以执行。
- **上游正文无累计上限**：增加 `upstreambody.BoundedTransport`，推理响应显式启用，每响应 wire 和每层 decoded 正文独立累计，默认 64 MiB（schema 1～512）。覆盖三种生成协议、重试创建的新 client 和管理 Playground；更新包/资源下载不套用推理上限。
- **解压炸弹**：先在 HTTPX transport 层拦截原始流，使用 zlib 的输出上限增量解压，而非解压后才检查大块。gzip、zlib deflate、raw deflate、多值/重复 Content-Encoding、截断/尾随字节、精确边界、取消关闭均有回归。默认协商 identity；不支持的压缩编码明确失败。
- **SSE 事件绕过与二次复制**：完整超长行也检查上限，空 data 行计入结构开销；按索引扫描 coalesced chunk，避免每行 split 剩余大字符串。usage 只保留固定已知数值字段与两个嵌套细节字段，未知 metadata 不随事件无限累积。
- **Unicode 预验证额外图分配**：由宽度栈改为迭代器深度栈，路径不回显敏感 key 内容；字符串用 surrogate 正则检查，降低大文本逐字符 Python 开销。
- **Responses/Anthropic 输出状态无限扩展**：共享 256 输出项/块上限，文本、推理、工具共同计数，超限明确错误。Responses 用片段列表加一次 join 管理文本/工具参数，工具名称冲突编号不再每次从 2 线性重扫。

### 2. 身份、预算、账本与协议语义

- **max_ips 并发首次使用**：主代理增加 keysvc.claim_ip 原子 helper，本子任务在最终重新鉴权后、budget.forwarded 前接入。不是在 header 预检、仅查询模型列表或请求结束时才绑定。claim 及最终校验在线程内执行；取消必须等待所属工作线程退出，且取消标记阻止后续预算发送。
- **事件循环被 SQLite 锁等待阻塞**：最终 revalidate/claim_ip/budget.forwarded 整段移到有取消归属的工作线程；上游 durable dispatch header 写入也通过 joined_thread 完成。保留实际 dispatch 前的校验和所有权，未通过拆除门禁换取延迟。
- **禁止错误事务合并**：没有把 claim_ip 和 budget.forwarded 强行包在同一 main DB transaction；后者先写 portal wallet，再写 main DB，合并会改变跨库锁序/事务语义。不限 IP 的 claim 仍有只读 BEGIN IMMEDIATE 开销，无 DML 的提交通常不产生新增数据 fsync，后续需要实际部署锁竞争基准。
- **错误的平台重试合同**：_upstream_headers 原先读未设置的 forced_platform 后回退 WorkBuddy，导致 qoder/trae 也拿到 WB attempt receipt 约束。现在优先使用实际选中平台；只有 WorkBuddy 附带该协议。
- **无效 usage 被当成合法零消费**：_record 使用 metering.parse_usage 原始证据，完整度必须 complete 且非 malformed；有效部分 token 保留，非法部分不会从调用方 coercion 的 0 推断成功。NaN、inf、bool、负数、非整数 float、越界数字和非标量 credit 均保留未知消费。合法整数数值字符串继续兼容。主代理同步修了 budget.settle 及 spool/replay 相关契约。
- **Messages 交错工具参数错配**：工具状态按 OpenAI index 分开，交错 A/B 片段分别送对应 content block；不再把后续片段归到最后一个工具。每段 reasoning 的 signature 只覆盖该段，不夹带之前的推理内容。
- **错误流伪造成功终态**：Anthropic 增加 abort，只关闭已开的块并 message_stop，不在错误后追加 end_turn/tool_use 的成功 message_delta。保留客户端既有错误后关闭消息契约。Responses 超限不再静默丢掉后面的工具。
- **Playground 资源/路由不一致**：选定平台不可用时明确失败，避免静默回落 WorkBuddy；复用请求时间预算、受限 transport、增量 StreamAudit 和 ManagedStreamingResponse。非流式超限/读取错误返回 502 且记录失败，取消记录 499，关闭所属 client/response。

### 3. 读取与管理配置

- `/v1/usage` 现在在统计查询前执行全局 IP 策略，匿名/错误 key 也采样审计；和实际生成入口保持策略一致。
- `/v1/key` 用微单位计算 credit 剩余额度，0.3−0.2=0.1；小于 1 的额度不再因 int 截断被显示成无限额。
- `/v1/usage` 用一次按模型/日期聚合和一次实时模型聚合同时生成总计及模型窗口，避免重复扫描同一历史。排除鉴权 quota fallback 后断言只有两条统计聚合。
- metering.reconcile 先限定 request_logs 窗口，再批量取对应 request_id 的相关证据；不再把全历史 usage_events/outcomes 等整表读入 Python 字典。孤立 outcome 采用有界近期窗口+NOT EXISTS。报告增加 tables_seen_scope 与 window_limit，明确新统计口径。
- 设置保存和代理线路 CRUD 的完整 read-modify-replace 共用配置路径 RLock，避免两条管理编辑互相覆盖；原子 rename 仅保证 JSON 不撕裂，不能单独保证无丢更新。
- 删除旧 proxy_pool 字段和设置 proxy 在同一次账号文件受保护写入内完成，移除第二次无锁回写，避免覆盖刚更新的 token。
- auto_assign(include_bound=False) 只选择未绑定账号，不会因需求数量超过空闲候选而改写已有绑定。
- HTTP/HTTPS 默认端口规范化保持幂等；IPv6 URL 加回方括号，脱敏/恢复秘密及非法 port 不再意外崩溃。
- 坏 account/auth JSON 形状和非法 expiry/weight 不让一份账号文件使整个列表失败。Redis 探测拒绝声明超过 64 KiB 的 bulk reply，避免按恶意长度先读取巨量正文。

## 可重复测试证据

所有 pytest 经仓库 `tests/zhihui_harness/run_tests.py` 隔离入口，未直接在业务目录运行 pytest。
使用 Python 3.12.14；生产依赖与 requirements.lock 一致；pytest 实际 9.2.0，与 lock 9.1.1 存在 QA 工具版本差异，无 pytest-asyncio。

| 证据 | 结果 / 用途 |
| --- | --- |
| `20261105-193707-a64851e0` | baseline 请求体回归：容量绕过、超时无法调度、单字节帧内存放大确实失败；`gateway-bodyguard-baseline.log` |
| `20261105-193733-af9b088b` | 修复后 bodyguard +既有闭合合同：44 passed、11 subtests |
| `20261105-194108-95658108` | baseline 新边界回归复现 usage/IP、Unicode、SSE/无效数字等缺陷；日志 `gateway-boundaries-baseline.log` |
| `20261105-195144-de0b8520` | 83 passed、38 subtests；第一轮 response budget/fixture/config/连接池验证 |
| `20261105-200021-f696c4de` | 246 passed，3 subtest failures：发现 Anthropic 错误后仍 end_turn，以及新测试误用了 Responses 的 max_tokens 字段；均已修复 |
| `20261105-201138-c76e637b` | 161 passed + 1 collection error：manager 目标缺前端源资产，属于目标选择错误，没有放宽测试 |
| `20261105-201227-2dca396a` | manager-assets 重跑 186 passed、61 subtests，view 前后 hash 一致 |
| `20261105-201350-82ede806` | 同组增加 `--require-built-web`：186 passed、61 subtests，verification_exit=0 |
| `20261105-201615-2c478740` | 最后 Playground 非流式失败闭合/代理RMW加强：32 passed、18 subtests，`--require-built-web`，verification_exit=0 |

日志位于工作区 `gateway-*.log`；每轮精确参数、统计和 JUnit 位于 repo `evidence/runs/<run_id>/`。
这些均为 focused 运行，不能代替主代理正在执行的 scope=full 全量结果。不同轮次测试重叠，不能将数量相加宣称独立测试总数。

**隔离网络记录解释**：201350/此前 assets 组有本机 fake provider 和真实 loopback transport 测试。
pytest_plugin 的终端文字“非回环尝试”实际打印全部被观察的 socket 调用，因 `_decide` 在 is_loopback 前计数。
201227 的 13 次计数全是 `127.0.0.1`，阻断 0；最后报告抓取 `__stats` 又增加 3 个 loopback 记录，导致该 JSON 的 attempts 数组长度与预先捕获的 attempt_count 不同。
唯一越界写记录为 `/dev/null`，protected_violations 为空。不得把终端 13 直接称为外网请求，也不得笼统描述所有轮次“零网络尝试”。

baseline 的 PromptWireContractTest 失败并非安全断言过严，而是 fake provider 不实现 echo/refusal mode；新增真实 payload echo 与显式 403 refusal。HealthFreshnessTest 成功探针原先依赖 127.0.0.1:7863，改为显式指向 harness 提供的 ZH_FAKE_PROVIDER_BASE。原有断言保留。

## 本地资源微基准

脚本：`tests/benchmarks/gateway_resources.py`；完整结果：`gateway-resource-benchmark.json`。
通过 AST 仅加载两份 checkout 中指定纯函数/类，不导入应用、不读账号/DB、不连接网络；记录源码 SHA256。
同一进程 before/after 顺序交替，7 轮中位数；tracemalloc 峰值独立测量，不影响计时，输入已在测量外创建。

| 合成工作负载 | 基线 → 修复 CPU 中位数 | 基线 → 修复 Python 分配峰值 | 解释 |
| --- | --- | --- | --- |
| Unicode 100,000 节点宽数组 | 36.31 → 25.14 ms | 12,192,194 → 1,738 B | 约 1.44×；深度栈消除第二份节点路径图 |
| Unicode 1 MiB ASCII 字符串 | 55.48 → 3.08 ms | 876 → 1,283 B | 约 18.03×；固定小内存增加 407 B |
| 合并一块的 4,096 SSE 事件 | 40.14 → 15.27 ms | 3,668,367 → 3,668,427 B | 约 2.63×；仍保留必要的事件输出对象 |
| 128 KiB、每帧 1 字节 ASGI body | 36.01 → 63.35 ms | 11,766,574 → 274,732 B | 峰值降低 97.67%；极端分片 CPU **慢约 76%**，换取内存边界和调度公平性 |

这些是局部 CPU/堆分配结果，不是 RSS、生产 P95/P99、供应商 token/s 或用户端首 token 延迟；不能据此承诺全链路提速倍数。
管理请求通常更大帧，最后一行是刻意的对抗性分片；仍保留性能代价，不隐藏为“全面提速”。

## 部署/兼容性说明与剩余风险

运行边界见 repo `docs/gateway-response-limits.md`（root docs，不新增 manager/docs 发布白名单项目）。

1. **64 MiB SSE 是累计上限**，可能中断原本无限长的会话；identity 协商放弃压缩带宽收益，串接 gzip member/意外 br/zstd 被拒绝。这些是明确兼容边界，需要灰度结合真实响应分布核验。
2. **内存不是仅一份正文**：JSON/Python 字符串、工具参数、最终翻译输出会有倍数开销，多并发再相加；仍需进程 RSS 与并发上限压测。本轮未做真实 HTTP2 网络/恶劣网络、供应商成本或生产并发压测。
3. **剩余事件循环阻塞**：响应 JSON 解析/协议转换、某些计数估算和完成时 _record SQLite 路径仍可同步占用循环；本轮移走已确认的最终 dispatch 锁等待，不冒险重构所有事务/取消边界。
4. **管理 Upstash SSRF 防护不足（未修）**：wb2api._reject_internal_host 文档同时写 fail-closed 与 fail-open，实际 DNS 异常返回允许。预检查与 HTTPX 再解析之间存在 DNS rebinding TOCTOU，不能以“攻击者能控制 DNS 不在威胁边界”证明无风险。接口为管理权限，私有 Redis 本就有独立允许政策；稳妥修复需要将解析、允许地址和实际 socket/SNI 绑定，而不是只改一次字符串检查。本轮记录，不给出已完全修复承诺。
5. **离线冷却修改生命周期（未修）**：wb2api.force_clear_account_cooling / set cooling / clear_expired_manual_cooling 具有停引擎→改 state→重启的多步流程，取消/管理并发与进程命令超时仍需专项一致性验证；不能当作原子事务。
6. **配置锁是进程内锁**：本轮解决同一 Manager 内设置与代理CRUD丢更新；任意不遵循该协议的外部进程写 config.json 仍可能覆盖。账号凭据写有现有 credential lease，配置文件没有等价跨进程写协议。
7. **协议边界**：Responses/Messages 是 Chat 转换适配器，不保证所有原生供应商特性；现有 wbm1 reasoning credential 是 base64 包装，不是密码学加密。部分已声明不支持功能不能据此宣称完全原生保真。
8. **连接/选号策略**：全局池和身份隔离保留既有设计，未盲目增大连接/并发。config 的 least-connections 近似近期调用计数、QTG 组合面板的串行平台读取等仍有可进一步测量项。本轮没有提供足以声称它们是生产瓶颈的证据。

以上剩余项属于可见的待办或运行边界，报告应保留；“最高级审计”不应表述成不存在任何剩余风险。
