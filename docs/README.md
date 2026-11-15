# 文档索引

> 37 份文档。**这份索引按「你现在要做什么」组织**，不按字母序 ——
> 按字母排的目录等于没有目录，因为你得先知道文件名才能找到它。

## 我是新来的，想先弄懂这东西是什么

| 文档 | 讲什么 |
| --- | --- |
| [`features.md`](features.md) | 功能全貌 |
| [`usage.md`](usage.md) | 怎么用 |
| [`api-tokens.md`](api-tokens.md) | 密钥与鉴权 |
| [`BUILTIN-MODELS.md`](BUILTIN-MODELS.md) | 内置模型清单 |
| [`account-proxy-routes.md`](account-proxy-routes.md) | 账号与代理路由 |
| [`screenshots.md`](screenshots.md) | 界面截图 |

## 线上出事了

| 文档 | 什么时候看 |
| --- | --- |
| [`TROUBLESHOOTING-2026-11.md`](TROUBLESHOOTING-2026-11.md) | **先看这个** |
| [`METRICS.md`](METRICS.md) | 要判断某个指标「读数为 0 是不是故障」时 |
| [`EDGE-CASES.md`](EDGE-CASES.md) | 边界与异常路径的既有结论 |
| [`SHUTDOWN-CANCEL-RESILIENCE.md`](SHUTDOWN-CANCEL-RESILIENCE.md) | 停机、取消、优雅退出相关的现象 |
| [`RECOVERY-TIMEOUT.md`](RECOVERY-TIMEOUT.md) | 恢复过程超时 |
| [`DISASTER-RECOVERY.md`](DISASTER-RECOVERY.md) | 真出事要恢复数据 |

**读 `METRICS.md` 时特别注意**：同一个 `0` 在不同指标上含义相反（健康 / 还没触发 / 异常）。
不先弄清这一点，会在正常时报警、在异常时放过。

## 要发布一个新版本

| 文档 | 讲什么 |
| --- | --- |
| [`PRODUCT-RELEASE.md`](PRODUCT-RELEASE.md) | 版本号在哪些地方必须一致 |
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | 开发与构建 |
| [`DEPLOY-WRITER-CONTENTION.md`](DEPLOY-WRITER-CONTENTION.md) | **部署时写入者争用**，滚动切换的核心约束 |
| [`DEPLOY-SCRIPT-SECURITY-2026-11-15.md`](DEPLOY-SCRIPT-SECURITY-2026-11-15.md) | 部署脚本的安全边界 |
| [`release-signing.md`](release-signing.md) | 发布签名与信任锚 |

## 安全和凭据

| 文档 | 讲什么 |
| --- | --- |
| [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md) | 安全审计结论 |
| [`security-notes.md`](security-notes.md) | 日常注意事项 |
| [`CSRF-AUDIT-2026-11-15.md`](CSRF-AUDIT-2026-11-15.md) | CSRF 面 |
| [`FRONTEND-SESSION-SECURITY-2026-11-15.md`](FRONTEND-SESSION-SECURITY-2026-11-15.md) | 前端会话 |
| [`DEPENDENCY-AUDIT-PYTHON-2026-11-15.md`](DEPENDENCY-AUDIT-PYTHON-2026-11-15.md) | **后端 Python 依赖**的 CVE 核对 |
| [`DEPENDENCY-VERSION-AUDIT-2026-11-15.md`](DEPENDENCY-VERSION-AUDIT-2026-11-15.md) | **前端 npm 依赖**的 CVE 核对 |

**这两份依赖审计名字很像，但对象完全不同**：一份是服务器上的 Python 包，
另一份是打包进前端产物里的 npm 包。**找错一份会看到一份与你无关的结论。**

## 数据、容量、账务

| 文档 | 讲什么 |
| --- | --- |
| [`DATA-GROWTH.md`](DATA-GROWTH.md) | 数据增长与归档 |
| [`RETENTION-AND-CAPACITY-2026-11-15.md`](RETENTION-AND-CAPACITY-2026-11-15.md) | 保留策略与容量 |
| [`DISK-BUDGET-2026-11-15.md`](DISK-BUDGET-2026-11-15.md) | 磁盘预算 |
| [`LEDGER-RECONCILIATION.md`](LEDGER-RECONCILIATION.md) | 账本对账 |
| [`BUDGET-BACKLOG-TRIAGE.md`](BUDGET-BACKLOG-TRIAGE.md) | 预算积压的处理口径 |

## 跟上游打交道

| 文档 | 讲什么 |
| --- | --- |
| [`UPSTREAM-CAPABILITIES.md`](UPSTREAM-CAPABILITIES.md) | **上游已有哪些能力** —— 提改进前先看，别重复造 |
| [`UPSTREAM-LOCAL-CHANGES.md`](UPSTREAM-LOCAL-CHANGES.md) | 上游代码里**未提交但正在跑**的改动，以及重装前的检查清单 |
| [`KNOWN-GAP-UPSTREAM-RETRY-AFTER.md`](KNOWN-GAP-UPSTREAM-RETRY-AFTER.md) | 等待时间转发这条线的完整记录 |
| [`UPSTREAM-REVIEW-2026-11-14.md`](UPSTREAM-REVIEW-2026-11-14.md) | 上游评审 |

**两条最容易被忽略的**：
`UPSTREAM-CAPABILITIES.md` 能避免「建议一个已经存在的功能」；
`UPSTREAM-LOCAL-CHANGES.md` 里的检查清单是**重装前的必读**。

## 审计记录（按日期，留档用）

这些是当时的取证与结论，**不是操作手册**。要找当前做法请看上面几节。

| 文档 | 主题 |
| --- | --- |
| [`PRODUCTION-AUDIT-2026-11-14.md`](PRODUCTION-AUDIT-2026-11-14.md) | 生产审计总览 |
| [`ROUTE-AND-PERF-AUDIT-2026-11-15.md`](ROUTE-AND-PERF-AUDIT-2026-11-15.md) | 路由与性能 |
| [`LATENCY-ATTRIBUTION-2026-11-15.md`](LATENCY-ATTRIBUTION-2026-11-15.md) | 延迟归因 |
| [`REACHABILITY-AUDIT-2026-11-15.md`](REACHABILITY-AUDIT-2026-11-15.md) | 可达性 |
| [`ACCOUNT-POOL-BALANCE-2026-11-15.md`](ACCOUNT-POOL-BALANCE-2026-11-15.md) | 账号池均衡 |
| [`ACCOUNT-SCHEDULER-FINDINGS-2026-11-15.md`](ACCOUNT-SCHEDULER-FINDINGS-2026-11-15.md) | 账号调度 |

## 维护这份索引

**新增文档时请加进对应小节**，并写清「什么时候该看它」——

只写文件名的索引帮不上忙，因为读者得先知道有这个文件才找得到。
