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
| [$(@{N=account-egress-routes.md; L=45}.N)](account-egress-routes.md) | 45 |
| [$(@{N=account-weight.md; L=51}.N)](account-weight.md) | 51 |
| [$(@{N=activity-task-summary.md; L=35}.N)](activity-task-summary.md) | 35 |
| [$(@{N=AI-HANDOFF-2026-11-14.md; L=77}.N)](AI-HANDOFF-2026-11-14.md) | 77 |
| [$(@{N=asset-line-endings.md; L=41}.N)](asset-line-endings.md) | 41 |
| [$(@{N=assistant-message-order.md; L=21}.N)](assistant-message-order.md) | 21 |
| [$(@{N=AUTHORIZATION.md; L=85}.N)](AUTHORIZATION.md) | 85 |
| [$(@{N=BODY-UPLOAD-TIMEOUT.md; L=54}.N)](BODY-UPLOAD-TIMEOUT.md) | 54 |
| [$(@{N=CACHE-METRICS.md; L=24}.N)](CACHE-METRICS.md) | 24 |
| [$(@{N=channel-check-console-fallback.md; L=47}.N)](channel-check-console-fallback.md) | 47 |
| [$(@{N=codex-tools-comparison.md; L=35}.N)](codex-tools-comparison.md) | 35 |
| [$(@{N=console-fallback-model-missing.md; L=43}.N)](console-fallback-model-missing.md) | 43 |
| [$(@{N=conversation-header-family.md; L=58}.N)](conversation-header-family.md) | 58 |
| [$(@{N=credit-exhausted-cooldown.md; L=53}.N)](credit-exhausted-cooldown.md) | 53 |
| [$(@{N=dsh-prompt-fingerprint.md; L=63}.N)](dsh-prompt-fingerprint.md) | 63 |
| [$(@{N=dsh-web-gui-fingerprint.md; L=47}.N)](dsh-web-gui-fingerprint.md) | 47 |
| [$(@{N=FOUR-GATEWAY-INTEGRATION.md; L=175}.N)](FOUR-GATEWAY-INTEGRATION.md) | 175 |
| [$(@{N=gateway-response-limits.md; L=53}.N)](gateway-response-limits.md) | 53 |
| [$(@{N=i18n.md; L=79}.N)](i18n.md) | 79 |
| [$(@{N=LOCAL-UPDATE-BOUNDARY.md; L=28}.N)](LOCAL-UPDATE-BOUNDARY.md) | 28 |
| [$(@{N=model-listing-compat.md; L=28}.N)](model-listing-compat.md) | 28 |
| [$(@{N=namespace-compat.md; L=39}.N)](namespace-compat.md) | 39 |
| [$(@{N=outbound-concurrency.md; L=30}.N)](outbound-concurrency.md) | 30 |
| [$(@{N=PLATFORM-POOL-REVIEW.md; L=39}.N)](PLATFORM-POOL-REVIEW.md) | 39 |
| [$(@{N=proposal-key-onclick-import.md; L=331}.N)](proposal-key-onclick-import.md) | 331 |
| [$(@{N=QODER-CLAIM-20261030.md; L=49}.N)](QODER-CLAIM-20261030.md) | 49 |
| [$(@{N=random-daily-tasks.md; L=43}.N)](random-daily-tasks.md) | 43 |
| [$(@{N=reasoning-as-content.md; L=42}.N)](reasoning-as-content.md) | 42 |
| [$(@{N=release-process.md; L=263}.N)](release-process.md) | 263 |
| [$(@{N=SERVER-ASSISTED-AUTH.md; L=83}.N)](SERVER-ASSISTED-AUTH.md) | 83 |
| [$(@{N=STEWARD-EXPERIENCE-PATCH.md; L=41}.N)](STEWARD-EXPERIENCE-PATCH.md) | 41 |
| [$(@{N=STEWARD.md; L=72}.N)](STEWARD.md) | 72 |
| [$(@{N=SYSTEMD-OPERATIONS.md; L=43}.N)](SYSTEMD-OPERATIONS.md) | 43 |
| [$(@{N=tool-discovery.md; L=27}.N)](tool-discovery.md) | 27 |
| [$(@{N=TRAE-HTTPS-CALLBACK-EXPERIMENT.md; L=43}.N)](TRAE-HTTPS-CALLBACK-EXPERIMENT.md) | 43 |
| [$(@{N=UPSTREAM-ADOPTION-v007.md; L=15}.N)](UPSTREAM-ADOPTION-v007.md) | 15 |
| [$(@{N=UPSTREAM-REVIEW-2026-11-15.md; L=129}.N)](UPSTREAM-REVIEW-2026-11-15.md) | 129 |
| [$(@{N=upstream-tool-history-fix.md; L=26}.N)](upstream-tool-history-fix.md) | 26 |
| [$(@{N=USER-PORTAL-LOCAL.md; L=109}.N)](USER-PORTAL-LOCAL.md) | 109 |
| [$(@{N=V007-OPERATIONS.md; L=51}.N)](V007-OPERATIONS.md) | 51 |
| [$(@{N=WORKBUDDY-CODEX-COMPAT.md; L=44}.N)](WORKBUDDY-CODEX-COMPAT.md) | 44 |
| [$(@{N=WorkBuddy-WebUI设计与实现方案.md; L=1161}.N)](WorkBuddy-WebUI设计与实现方案.md) | 1161 |
| [$(@{N=ZERO-DOWNTIME-EVIDENCE-2026-11-15.md; L=68}.N)](ZERO-DOWNTIME-EVIDENCE-2026-11-15.md) | 68 |

## 历史归档

早期版本与那几次发布的记录。**保留它们是为了查「当时为什么这么做」**，其中描述的做法不一定还适用于现在。

| 文档 | 行数 |
| --- | --- |
| [$(@{N=AUDIT-0.0.2.md; L=29}.N)](AUDIT-0.0.2.md) | 29 |
| [$(@{N=AUDIT-0.0.3.md; L=45}.N)](AUDIT-0.0.3.md) | 45 |
| [$(@{N=RELEASE-0.0.10.md; L=23}.N)](RELEASE-0.0.10.md) | 23 |
| [$(@{N=RELEASE-0.0.3.md; L=57}.N)](RELEASE-0.0.3.md) | 57 |
| [$(@{N=RELEASE-0.0.4-RC.1.md; L=28}.N)](RELEASE-0.0.4-RC.1.md) | 28 |
| [$(@{N=RELEASE-0.0.4.md; L=14}.N)](RELEASE-0.0.4.md) | 14 |
| [$(@{N=RELEASE-0.0.5.md; L=59}.N)](RELEASE-0.0.5.md) | 59 |
| [$(@{N=RELEASE-0.0.6.md; L=23}.N)](RELEASE-0.0.6.md) | 23 |
| [$(@{N=RELEASE-0.0.7.md; L=33}.N)](RELEASE-0.0.7.md) | 33 |
| [$(@{N=RELEASE-0.0.8.md; L=11}.N)](RELEASE-0.0.8.md) | 11 |
| [$(@{N=RELEASE-0.0.9.md; L=20}.N)](RELEASE-0.0.9.md) | 20 |
| [$(@{N=RELEASE-20261030-FLOW.md; L=51}.N)](RELEASE-20261030-FLOW.md) | 51 |
| [$(@{N=RELEASE-20261030-ICE.md; L=39}.N)](RELEASE-20261030-ICE.md) | 39 |
| [$(@{N=RELEASE-20261030-ROLES.md; L=44}.N)](RELEASE-20261030-ROLES.md) | 44 |
| [$(@{N=RELEASE-20261030-THREE.md; L=35}.N)](RELEASE-20261030-THREE.md) | 35 |
| [$(@{N=RELEASE-20261101-SPEED.md; L=24}.N)](RELEASE-20261101-SPEED.md) | 24 |
| [$(@{N=RELEASE-20261101-STABILITY.md; L=105}.N)](RELEASE-20261101-STABILITY.md) | 105 |
| [$(@{N=SECURITY-AUDIT-2026-09-15.md; L=115}.N)](SECURITY-AUDIT-2026-09-15.md) | 115 |
| [$(@{N=UPGRADE-20261106.md; L=43}.N)](UPGRADE-20261106.md) | 43 |
| [$(@{N=UPSTREAM-REVIEW-0.0.4.md; L=30}.N)](UPSTREAM-REVIEW-0.0.4.md) | 30 |
