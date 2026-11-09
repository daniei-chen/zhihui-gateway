# 智汇网关 0.0.9 发布说明（受限稳定性补丁）

- 构建：`v0.0.9+stable.20261107.1`
- 基线：`dfb3f31e0ee93deb6c57244d71b4ffa95671dc94`（0.0.8）
- 触发背景：2026-11-06 生产 30 日日志审计发现上游流截断导致 220 次 502（当日 2.41%）、30 日 p95 延迟 50.5s；详见 `deliverables/PROD-30D-AUDIT.md`。

## 本版修复

1. **ZE-01**：`server/deadline.py` 的 `decoded_bytes()` 不再使用 `aiter_raw(chunk_size=16384)`，短 SSE 响应不再被缓冲到 EOF 才下发。
2. **ZE-02**：`server/observability.py` 的 `sse_error()` 支持 `leading_separator`；`routers/gateway.py` 两处流中错误帧前强制空行，避免拼在未完成的 `data:` 残行后。
3. **ZG-02**：`/qoder/v1/models`、`/trae/v1/models` 不再套用 workbuddy 的 realm/腾讯目录裁剪；白名单维度保留。
4. **ZP-001（部分）**：门户登录新增来源 IP 维度失败限速（10 次/10 分钟，成功即清）；未知用户名不再能无限探测。账号级 5 次锁定仍保留（审计建议的“禁用他人账号”DoS 需产品决策，未在本版关闭）。
5. **ZB-02（部分）**：删除/移动/重启账号、清空签到/任务日志补写 `audit_logs`。
6. 版本/文档：`0.0.9`、CHANGELOG、PRODUCT-RELEASE、README 徽标与状态同步；新增回归测试覆盖上述行为。

## 未修复（明确列出，禁止宣称“零问题”）

- **Open High**：ZM-001/002（预算/钱包冻结）、ZR-01（租约永久 draining）、ZQ-01（签到 uncertain）、ZX-01（读写串行）、ZTZ-01（非 +08 日界）、ZO-01（发布链供应链）、PRD-01（上游流截断/502 熔断与回滚门禁）、PRD-02（SLO/大请求准入）等。
- **Medium/Low**：FINDINGS.md 中其余条目（权限边界、统计口径、测试证据、文档契约等）尚未在本版关闭。
- 因此本版是**受限稳定性补丁**，不是“全部审计问题已解决”的成熟版本；残余风险与验收口径以 `FINDINGS.md`、`NEXT_ITERATION.md`、`PROD-30D-AUDIT.md` 为准。

## 发布方式（必须）

1. 候选目录经 `cut-local-release` 的严格 Linux 门禁（零失败、跳过集合冻结）→ Ed25519 manifest 签名 + 测试回执。
2. `migration_entry.py prepare <label> <candidate>` → 受控排空/配对备份/激活 → `run <label>` → ready 验证。
3. 观察 15 分钟：`/api/readyz`、502 率、unknown 增速、journal 新错误；异常立即用迁移控制器回退到 `v0.0.8+stable.20261106.1`。
4. 不在 05:00 前切换；不在门禁未过时切换；不手工 `ln -sfn` 绕过 writer guard。
