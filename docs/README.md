# 文档索引

> 这份索引收录 **63 份**按场景组织的文档。`docs/` 下实际有 170 份 Markdown
> （其余多为按日期归档的交接记录），没有全部收进来 —— 收的是「你会主动去找」的那些。
> **索引按「你现在要做什么」组织**，不按字母序 ——
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
| [`WHY-11102-IS-NO-HEALTHY-ACCOUNT.md`](WHY-11102-IS-NO-HEALTHY-ACCOUNT.md) | **看起来像 bug 但不是** —— 想改这个归类之前先读它 |
| [`NGINX-MAINTENANCE-MODE-2026-11-15.md`](NGINX-MAINTENANCE-MODE-2026-11-15.md) | **管理端 503 复盘** —— 维护开关没关，以及它指向的错误排查方向 |
| [`TLS-AND-TIMERS-2026-11-15.md`](TLS-AND-TIMERS-2026-11-15.md) | **TLS 与定时任务** —— 只接受 1.2/1.3，续期自动；机器上还有别的服务 |
| [`SERVICE-HARDENING-2026-11-15.md`](SERVICE-HARDENING-2026-11-15.md) | **服务加固现状** —— 已降权到 uid 10001，两处可再收紧 |
| [`BACKUP-RESTORE-STATUS-2026-11-15.md`](BACKUP-RESTORE-STATUS-2026-11-15.md) | **备份与恢复的现状** —— 三层结构、每份自校验、真实缺口是异地 |
| [`ERROR-BUDGET-BASELINE-2026-11-15.md`](ERROR-BUDGET-BASELINE-2026-11-15.md) | **错误预算基线** —— 判断「这次是不是异常」之前先看它 |
| [`OPS-PITFALLS.md`](OPS-PITFALLS.md) | **发布与运维里踩过的坑** —— 每条都是「我以为是 A，实际是 B」 |
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

---

## 技术说明（按主题）

这些是具体机制与兼容性的说明，回答「为什么这样实现」。**没有一句话摘要的，是还没人补过 —— 不是不重要。**

| 文档 | 行数 |
| --- | --- |
| [account-egress-routes.md](account-egress-routes.md) | 45 |
| [account-weight.md](account-weight.md) | 51 |
| [activity-task-summary.md](activity-task-summary.md) | 35 |
| [AI-HANDOFF-2026-11-14.md](AI-HANDOFF-2026-11-14.md) | 77 |
| [asset-line-endings.md](asset-line-endings.md) | 41 |
| [assistant-message-order.md](assistant-message-order.md) | 21 |
| [AUTHORIZATION.md](AUTHORIZATION.md) | 85 |
| [BODY-UPLOAD-TIMEOUT.md](BODY-UPLOAD-TIMEOUT.md) | 54 |
| [CACHE-METRICS.md](CACHE-METRICS.md) | 24 |
| [channel-check-console-fallback.md](channel-check-console-fallback.md) | 47 |
| [codex-tools-comparison.md](codex-tools-comparison.md) | 35 |
| [console-fallback-model-missing.md](console-fallback-model-missing.md) | 43 |
| [conversation-header-family.md](conversation-header-family.md) | 58 |
| [credit-exhausted-cooldown.md](credit-exhausted-cooldown.md) | 53 |
| [dsh-prompt-fingerprint.md](dsh-prompt-fingerprint.md) | 63 |
| [dsh-web-gui-fingerprint.md](dsh-web-gui-fingerprint.md) | 47 |
| [FOUR-GATEWAY-INTEGRATION.md](FOUR-GATEWAY-INTEGRATION.md) | 175 |
| [gateway-response-limits.md](gateway-response-limits.md) | 53 |
| [i18n.md](i18n.md) | 79 |
| [LOCAL-UPDATE-BOUNDARY.md](LOCAL-UPDATE-BOUNDARY.md) | 28 |
| [model-listing-compat.md](model-listing-compat.md) | 28 |
| [namespace-compat.md](namespace-compat.md) | 39 |
| [outbound-concurrency.md](outbound-concurrency.md) | 30 |
| [PLATFORM-POOL-REVIEW.md](PLATFORM-POOL-REVIEW.md) | 39 |
| [proposal-key-onclick-import.md](proposal-key-onclick-import.md) | 331 |
| [QODER-CLAIM-20261030.md](QODER-CLAIM-20261030.md) | 49 |
| [random-daily-tasks.md](random-daily-tasks.md) | 43 |
| [reasoning-as-content.md](reasoning-as-content.md) | 42 |
| [release-process.md](release-process.md) | 263 |
| [SERVER-ASSISTED-AUTH.md](SERVER-ASSISTED-AUTH.md) | 83 |
| [STEWARD-EXPERIENCE-PATCH.md](STEWARD-EXPERIENCE-PATCH.md) | 41 |
| [STEWARD.md](STEWARD.md) | 72 |
| [SYSTEMD-OPERATIONS.md](SYSTEMD-OPERATIONS.md) | 43 |
| [tool-discovery.md](tool-discovery.md) | 27 |
| [TRAE-HTTPS-CALLBACK-EXPERIMENT.md](TRAE-HTTPS-CALLBACK-EXPERIMENT.md) | 43 |
| [UPSTREAM-ADOPTION-v007.md](UPSTREAM-ADOPTION-v007.md) | 15 |
| [UPSTREAM-REVIEW-2026-11-15.md](UPSTREAM-REVIEW-2026-11-15.md) | 129 |
| [upstream-tool-history-fix.md](upstream-tool-history-fix.md) | 26 |
| [USER-PORTAL-LOCAL.md](USER-PORTAL-LOCAL.md) | 109 |
| [V007-OPERATIONS.md](V007-OPERATIONS.md) | 51 |
| [WORKBUDDY-CODEX-COMPAT.md](WORKBUDDY-CODEX-COMPAT.md) | 44 |
| [WorkBuddy-WebUI设计与实现方案.md](WorkBuddy-WebUI设计与实现方案.md) | 1161 |
| [ZERO-DOWNTIME-EVIDENCE-2026-11-15.md](ZERO-DOWNTIME-EVIDENCE-2026-11-15.md) | 68 |

## 历史归档

早期版本与那几次发布的记录。**保留它们是为了查「当时为什么这么做」**，其中描述的做法不一定还适用于现在。

| 文档 | 行数 |
| --- | --- |
| [AUDIT-0.0.2.md](AUDIT-0.0.2.md) | 29 |
| [AUDIT-0.0.3.md](AUDIT-0.0.3.md) | 45 |
| [RELEASE-0.0.10.md](RELEASE-0.0.10.md) | 23 |
| [RELEASE-0.0.3.md](RELEASE-0.0.3.md) | 57 |
| [RELEASE-0.0.4-RC.1.md](RELEASE-0.0.4-RC.1.md) | 28 |
| [RELEASE-0.0.4.md](RELEASE-0.0.4.md) | 14 |
| [RELEASE-0.0.5.md](RELEASE-0.0.5.md) | 59 |
| [RELEASE-0.0.6.md](RELEASE-0.0.6.md) | 23 |
| [RELEASE-0.0.7.md](RELEASE-0.0.7.md) | 33 |
| [RELEASE-0.0.8.md](RELEASE-0.0.8.md) | 11 |
| [RELEASE-0.0.9.md](RELEASE-0.0.9.md) | 20 |
| [RELEASE-20261030-FLOW.md](RELEASE-20261030-FLOW.md) | 51 |
| [RELEASE-20261030-ICE.md](RELEASE-20261030-ICE.md) | 39 |
| [RELEASE-20261030-ROLES.md](RELEASE-20261030-ROLES.md) | 44 |
| [RELEASE-20261030-THREE.md](RELEASE-20261030-THREE.md) | 35 |
| [RELEASE-20261101-SPEED.md](RELEASE-20261101-SPEED.md) | 24 |
| [RELEASE-20261101-STABILITY.md](RELEASE-20261101-STABILITY.md) | 105 |
| [SECURITY-AUDIT-2026-09-15.md](SECURITY-AUDIT-2026-09-15.md) | 115 |
| [UPGRADE-20261106.md](UPGRADE-20261106.md) | 43 |
| [UPSTREAM-REVIEW-0.0.4.md](UPSTREAM-REVIEW-0.0.4.md) | 30 |
