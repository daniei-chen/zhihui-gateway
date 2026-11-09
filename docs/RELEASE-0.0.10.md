# 智汇网关 0.0.10 发布说明（受限稳定性补丁二）

- 构建：`v0.0.10+stable.20261107.3`（候选 `v0.0.10+stable.20261107.2` 重打版本戳并重新签名，制品 587 文件）
- 基线：`65978a1f1dfa60caf3f90ebc1c871ffd2100ea2e`（0.0.9）
- 触发背景：延续 2026-11-06 生产审计发现与 0.0.9 发布遗留项；详见 `FINDINGS.md` 与 `PROD-30D-AUDIT.md`。

## 本版修复

1. **ZG-01**：`routers/anthropic.py` 流式工具调用重复 id/name 去重，不再拼成 `call_1call_1` / `BashBash`；单字符分片保持拼接语义。
2. **ZP-001（收口）**：门户账号锁定维度改为 来源 IP+用户名，防止第三方恶意锁死受害者账号；与 0.0.9 的来源 IP 失败限速并存。
3. **ZM-002（部分）**：`routers/portal.py` / `portalwallet.py` 对网关 `rejected` 终态的 portal 钱包占用自动释放（`WB_PORTAL_AUTO_CLOSE_REJECTED`，默认关闭）；供应商未知成本单独保留，不计 0、不清零。
4. **更新日志契约**：`/api/system/changelog` 读取发布树 Manager 更新日志；0.0.9 发版时该文件未随包分发导致控制台缺 0.0.9 条目，本版补齐并随版分发。
5. **版本/文档/配置契约**：`config-schema.json`（注册 `WB_PORTAL_AUTO_CLOSE_REJECTED`）、`product-release.json`、CHANGELOG、回归测试同步。

## 随版落地的运维配置（非代码变更，切换窗口生效）

- `WB_KEY_CONCURRENCY_QUOTA` 1→8、`WB_KEY_BUDGET_PENDING_QUOTA` 1→8：限额密钥不再被一次客户端中断锁死 15 分钟。
- `WB_UPSTREAM_TIMEOUT` 600s→300s：p99 实测约 100s，卡死请求更早释放并发槽位。
- `MemoryHigh` 1 GiB→1.5 GiB（RSS 峰值曾贴 1.0 GiB）；`vm.swappiness` 60→10。
- ufw 封禁扫描源 38.92.14.84；停用 `zh-trae-callback-probe` 实验单元。

## 未修复（如实列出）

- `session_expired` 独立错误码（internal-session-verify 401 污染 auth.denied 指标）、数据保留策略、告警通知渠道、SQLite `synchronous` 持久化级别（涉及账本持久性契约，需产品决策）、nginx upstream keepalive（受 writer-migration upstream 令牌校验约束，需先升级框架）。
- ZM-001/002 其余部分、ZR-01、ZQ-01、ZX-01、ZTZ-01、ZO-01、PRD-01/02 等 `FINDINGS.md` 其余条目未在本轮关闭。

## 发布方式与验收（实际口径）

1. 候选经 `migration_entry.py prepare v0.0.10+stable.20261107.3 v010-prod-forward` → 受控排空/激活/就绪/解除围栏，事务 `run` 记录 `phase: complete`（epoch 17），全程约 40 秒。
2. 源码↔现网制品一致性：8 个变更服务端文件 sha256 逐字节一致（CRLF/LF 归一后比对）。
3. 切换后实测：三平台数据面恢复正常鉴权响应、`/healthz` 200、`/api/readyz` ok、审计日志 `release_id=v0.0.10+stable.20261107.3`、版本接口 `manager=v0.0.10`、更新日志接口返回 0.0.10→0.0.2 完整列表、独立看门狗 `TRUSTED-PREFLIGHT-OK`。
4. 本版未执行完整 Linux 门禁（4966 项基线为 0.0.8 口径）；随版回归测试覆盖 ZG-01 / ZP-001 / ZM-002 行为。真实断电、长期 SLO、国际账号真实推理与完整新主机容灾仍未验证。
