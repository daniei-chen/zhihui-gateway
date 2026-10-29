# K01-S2 设计：QTG 新建密钥改为调用 Manager

状态：设计完成，待实施。日期：2026-10-23。基线 `b53f36e`。
前置：用户已确认"统一 `wbk_` 前缀""QTG 页面保留完整 key 管理能力（不是只读）""服务间凭据用方案 A + 最小 scope"。

## 1. 目标

1. QTG 页面上的创建/修改/吊销全部转成对 Manager API 的调用，QTG 不再持有公开推理密钥的权威；
2. 新 key 统一 `wbk_` 前缀；旧 `sk-qtg-` key 由 K01-S1 的迁移兼容层继续可用；
3. QTG 侧失败必须明确报错，不允许"页面显示成功但没生效"。

## 2. 服务身份（方案 A）

**问题**：Manager 的 `POST /api/keys` 用 `Depends(security.require_admin)`，只认 admin 会话或 admin token。QTG 是后端程序，需要一种"可信内部服务"身份。

**方案**：给 Manager 增加一个受限 scope，只覆盖密钥管理端点。

| 层 | 改动 |
|---|---|
| `manager/server/tokensvc.py` | `SCOPES` 增加 `'keys'`；`scope_to_role('keys') → 'keys_service'`；`normalize_scope` 仍把非法值降级为 readonly |
| `manager/server/security.py` | 新增依赖 `require_keys_writer`：接受 role ∈ {`admin`, `keys_service`}；其余角色返回 401/403 |
| `manager/server/routers/keys.py` | 写端点（创建/更新/删除/重置用量）从 `require_admin` 换成 `require_keys_writer`；读端点保持 `current_user` |
| 其它路由器 | **不动**，`keys_service` token 访问 accounts/settings/auth 等一律 401 |

约束：
- 不允许 `keys_service` token 走 `require_session_admin`（高危接口），保持真人会话要求；
- token 存 `api_tokens`（只存哈希），由管理员在"设置 → 管理 Token"创建，scope 选 `keys`；
- token 文件放 `/opt/qtg/secrets/manager_service_token`（600、root 可读），轮换周期 90 天，纳入 K17。

## 3. QTG 侧

### 3.1 配置

`qtg/app/qtg/config.py` 新增：

```
manager_service_token_file: str = "/opt/qtg/secrets/manager_service_token"
manager_api_timeout_sec: float = 10.0
```

复用已有 `wb_panel_base`（默认 `http://127.0.0.1:7864`）作为 Manager 地址。

### 3.2 客户端

新增 `qtg/app/qtg/manager_api.py`：

- `read_token(path)`：读文件，去掉首尾空白；文件缺失/空 → 抛 `ConfigMissing`（明确"未配置服务凭据"，不要静默回退到本地创建）；
- `async def create_key(payload) -> dict`：`POST {wb_panel_base}/api/keys`，带 `Authorization: Bearer <service token>` 与 `X-Internal-Actor: qtg-service`；
- 错误映射：Manager 401/403 → 明确 `upstream_error/权限不足`；5xx/超时 → `上游不可用`；均不透传原始响应体中的敏感字段。

### 3.3 改造 `compat_fixes.fixed_keys_create`

- 不再 `rt.store.create_key(...)`；
- 把前端字段映射为 Manager 字段后调用 `manager_api.create_key`；
- 成功：返回与前端期望一致的结构（含一次性明文 key）；
- 失败：返回 4xx/5xx 明确错误（不写本地库、不返回假 key）。

### 3.4 字段映射（以前端 form 为准）

| 前端 form | 发往 Manager | 说明 |
|---|---|---|
| `name` | `name` | 同名 |
| `models` | `models`（数组） | 前端字符串按逗号拆 |
| `quota` | `quota`（int） | token 总量，0=不限 |
| `quotaCredit` | `quota_credit`（float） | 积分额度 |
| `realm` | `realm`（''/'cn'/'global'） | 版本归属 |
| `upstream_id` | `upstream_id` | 账号池分组 |
| `expiresDays` + `expiryMode` | `expires_at`（epoch） | 前端换算；`never` → null |
| `maxIps` | `max_ips` | 同名 |
| `ipAllowlist` | `ip_allowlist`（数组） | 前端换行拆分 |

不再使用 QTG 独有的 `daily_limit`/`monthly_limit`/`max_concurrency`（Manager 无对应字段）：
- 读取/展示历史 key 时，这几个字段从 `legacy_qtg_keys` 读取并只读展示；
- 新建 key 不提供这几个字段（前端在 QTG 平台下隐藏或标注"由 Manager 统一定义"）。

## 4. 审计

- QTG 侧记录 `action=manager_key_create actor=qtg-service user=<原始用户> request_id`；
- Manager 侧记录 `create_key` 并带 `auth_method=token`、`token_scope=keys`；
- 两边用同一个 `request_id` 关联（QTG 生成并放入 `X-Request-Id`）。

## 5. 回退

| 场景 | 处理 |
|---|---|
| Manager 不可达 | QTG 创建返回明确 502/503，不写本地、不返回假 key；页面提示稍后重试 |
| Token 失效/未配置 | 返回 503 + 配置提示；运维按 K17 轮换/补配 |
| 需要整体回退 | 代码回退 + 关闭 QTG 创建入口（前端只读），不影响已有 key 调用 |

## 6. 测试计划

Manager：
- `keys_service` token 能创建/更新/删除 key；
- 该 token 访问 `/api/accounts`、`/api/settings/*`、`/api/auth/*` 一律 401/403；
- viewer/admin 现有行为不变；
- 非法 scope 仍降级 readonly。

QTG：
- mock Manager：创建成功返回明文 key；Manager 401 → 明确错误；超时 → 503；文件缺失 → 503 配置提示；
- 字段映射：quota/quotaCredit/realm/upstream_id/expiresDays/maxIps/ipAllowlist 全部落到请求体；
- 失败时不写 `qtg_keys`。

## 7. 未决项

1. Manager 的 `keys` scope 是否也允许 `GET /api/keys`（列表）——建议允许，便于 QTG 代理列表；
2. 历史 key 的 `daily_limit/monthly_limit` 在 QTG 页面如何展示（只读且标注"迁移遗留"）；
3. 服务 token 的创建方式：由管理员在 UI 创建后手工放到 QTG 机器，还是安装脚本生成。
