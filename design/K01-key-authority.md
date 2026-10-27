# K01 设计稿：统一公开推理密钥权威（方案 a）

状态：设计，未实现。日期：2026-10-23。基线 `b53f36e`。
用户已确认方案 (a)：**Manager 为公开推理密钥权威；QTG 只保留 UI/平台适配，新建 key 走 Manager。**

## 1. 现状事实（源码核对）

| 项目 | Manager | QTG |
|---|---|---|
| 生成 | `keysvc.py:252` `token = 'wbk_' + secrets.token_urlsafe(32)` | `store.py:168` `plain = 'sk-qtg-' + secrets.token_hex(20)` |
| 存储 | `keysvc.py:48` `_hash = sha256(token).hexdigest()`，存 Manager DB | `store.py:84` `hash_key = sha256(plain).hexdigest()`，存 `qtg_keys` 表 |
| 校验 | `keysvc.py:342 resolve()`，`if not token.startswith('wbk_')` 直接返回 None | `store.py:186 verify_key()`，只认 `sk-qtg-` 前缀 |
| 入口 | `/v1`、`/qoder/v1`、`/trae/v1` 都经 Nginx 到 Manager | QTG `/api/keys` 创建 + 导出 base_url 指向上述入口 |
| 额度字段 | `quota`/`quota_credit`（统一前端） | `daily_limit`/`monthly_limit`（QTG 创建时读取） |

**关键事实：两边 hash 算法相同（sha256 hex）**，因此"迁移已有 key"不需要明文，直接把 `key_hash` 搬过去即可校验 —— 这是方案 (a) 可行的技术前提。

## 2. 目标

1. 新建 key：无论从哪个平台 UI 创建，都写入 Manager，返回可用的 `sk-…`/`wbk_…` 凭据；
2. 旧 key：QTG 已发出的 `sk-qtg-` key 继续可用，不要求用户换钥匙、不重置额度；
3. 所有公开入口（`/v1`、`/qoder/v1`、`/trae/v1`、`/wb/api/v1`）统一由 Manager 校验、计量、审计；
4. QTG 不再拥有"公开推理密钥"的第二权威，只做平台运营与 UI 适配。

## 3. 实施步骤（三个可独立回退的阶段）

### S1 · 让 Manager 能校验 QTG 旧 key（只读兼容）

- Manager `keysvc.resolve()`：
  ```
  1) 若 token 以 wbk_ 开头 → 走现有查询（不变）；
  2) 否则计算 sha256(token)，查 keys 表新增列 source='qtg-legacy'（或独立映射表 legacy_keys）；
     命中且 enabled → 返回与原 wbk_ 等价的 key 记录（额度/平台/模型/IP/有效期沿用迁移值）；
     未命中 → None（仍拒绝随机前缀，不能"放宽成任何前缀都查"）。
  ```
- 迁移数据来源：QTG `qtg_keys` 表导出（`id/name/key_hash/key_prefix/platforms/models/daily_limit/monthly_limit/enabled/created_at/expires_at`）；**不导出明文**（本来也没有）。
- 验收：旧 `sk-qtg-` key 在 `/qoder/v1` 上模型列表 + 流式调用成功；随机 `sk-xxx` 仍 401；禁用/过期 key 立即失效。

### S2 · 新建 key 切到 Manager

- QTG `/api/keys` POST 改为服务端调用 Manager `POST /api/keys`（内部身份，见下）；
- QTG 保留只读列表（从 Manager 拉取）+ 导出配置生成，不再写 `qtg_keys`；
- 内部身份：Manager 现有 admin token 机制（`wbt_`）或新增服务间 token，**不转发用户 Cookie**（避免把浏览器会话变成机器身份）；
- 字段映射：QTG 表单的 `daily_limit/monthly_limit` → Manager `quota` 语义（按 K02 冻结的单位），`platforms/models/ip_allowlist/expires_at` 一一对应；
- 验收：三平台创建 → 模型列表 → 流式调用 → 额度结算 → 吊销 → 旧 key 仍可用。

### S3 · 退役 QTG 本地 keys 表（保留归档）

- `qtg_keys` 标记只读并归档（不删数据，按 K24 保留策略）；
- QTG UI 的"创建"按钮在后端失败时不得假装成功（当前 K29 的假成功类问题）；
- 回退：S3 之前，QTG 仍可回退到"本地创建 + S1 兼容"模式。

## 4. 兼容与回退边界

| 场景 | 处理 |
|---|---|
| 旧 key 未被迁移 | 会 401；因此迁移必须在 S1 上线前完成并核对数量（QTG `qtg_keys` 行数 = 迁移行数） |
| 迁移后旧 key 额度 | 保留原值，不重置；单位差异按 K02 冻结表换算，无法确定的登记待决 |
| Manager 不可达（S2 期间） | QTG 创建接口返回明确错误，不落本地库、不返回假 key |
| 回退 | 代码/配置回退；不覆盖切换后新增的 keys 行；不恢复已吊销 key |

## 5. 验收矩阵（对应 02 计划 §6.2 第 10 条）

1. 创建 → 明文只返回一次；
2. 导出 → base_url 与 key 可用；
3. SDK 模型列表 → 200；
4. 流式调用 → 正常终态、usage 结算一次；
5. 额度/并发/IP/模型/平台限制生效；
6. 吊销 → 立即 401；
7. 旧 key 迁移后连续可用；
8. 随机前缀 / 伪造 key → 401（不放宽前缀检查）。

## 6. 未决项（需要产品/运维确认）

1. 新 key 的前缀：继续 `wbk_`，还是给 QTG 创建的发 `sk-qtg-` 以保持客户端配置兼容？（推荐：新 key 统一 `wbk_`，QTG 导出时照常生成配置）
2. 是否允许 QTG UI 继续展示"由 QTG 创建"的历史 key（只读）；
3. 迁移时间窗：`qtg_keys` 当前行数需在生产只读统计（本轮未取）；
4. 服务间凭据的托管方式（`/opt/qtg/secrets/*` 还是 Manager 签发短时 token）。
