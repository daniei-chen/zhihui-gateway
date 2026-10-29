# K29 错误契约（正式版 v1.0）

- 状态：**已定稿**（2026-10-24 接管期；机器矩阵 + 逐条裁定）
- 适用范围：Manager（WorkBuddy 平台）与 QTG（Qoder / Trae 平台）全部 `/api/*` 与推理入口
- 生产连接：**否**（本契约全部数据来自本地隔离 fixture，源 `09ffa9e2` / `1d358c1e`）
- 机器证据：
  - `evidence/k29-error-contract-2026-10-24-ba3f20/matrix.json` — 480 探针（~102 端点 × 2 平台路由 × 匿名/只读/管理员）
  - `evidence/k29-csrf-recheck.json` — 带 Origin 复测 20 探针
  - `evidence/browser/ui1719fn-*/report.json` — N23/N24 现行行为
  - 探针脚本：`tests/linux_lab/probe_error_contract.mjs`（**动作类端点从不携带管理员凭证**，写端点仅用空 body 探测）

---

## 1. 信封总表（客户端必须按此解析）

| HTTP | 信封形状 | 出现位置 | 客户端要求 | 裁定 |
|---|---|---|---|---|
| **401** | **HTML**（nginx `auth_request` 拦截） | 匿名访问全部 `/api/*`（`/api/me` 除外） | **只看状态码**，不得尝试 JSON 解析 | **接受**：边缘拦截无法返回应用层 JSON；文档化，客户端以状态码为准 |
| 401 | `{"detail": "..."}` | `/api/me`（应用层） | 按 detail 展示 | 接受（两层 401 并存，均以状态码为准） |
| 403 | `{"detail","code","reason"}` | 写操作缺 `Origin`（CSRF 拒绝） | 展示 detail；`code` 标识 CSRF | **K07 契约**：写面全量强制，带 Origin 后转为真实响应 |
| 403 | `{"detail": "..."}` | 角色不足（viewer 写 / 敏感读） | 展示 detail | 接受 |
| 403 | `{"detail": "该接口不接受 API Token，请用会话登录后操作"}` | `require_session_admin` 覆盖的高危端点 | 提示改用会话登录 | **定稿**：高危/不可逆端点（一键更新、删账号、清日志、改安全策略、管理 token 自身）一律 session-only |
| 404 | `{"detail": "..."}` | 资源不存在 / 未知路径 | 展示 detail | 接受 |
| 405 | `{"detail": "..."}` | 方法不允许 | 展示 detail | 接受 |
| 409 | `{"detail": "<可操作中文>"}` | 平台能力缺失（qoder/trae 的 tokens/users/proxy-routes）或部署前置缺失（无 upstream.json） | 原样展示（文案本身可照做） | **平台能力声明信封**；`detail` 必须可操作 |
| 410 | `{"available":false,"code":"unsupported","detail":"..."}` | qoder/trae 红包 | 展示 detail | **平台不实现**（营销功能仅 WorkBuddy） |
| 422 | `{"detail": [ {...} ]}`（FastAPI 校验数组） | Manager 入参校验失败 | `errText()` 已兜底（N14） | 接受 |
| 501 | `{"available":false,"code":"not_implemented","detail":"..."}` | qoder/trae 未实现路径 | 展示 detail | **有意区分**："未实现"≠"没有数据"（N20） |
| 502 | `{"error":{"code","message","type"}}` | 引擎控制面不可用 | `backendErrorText()` 取 `error.message`（N14 修复） | 接受 |

**统一规则（客户端）**：
1. 先看状态码分级（401/403/404/409/410/422/5xx）；
2. body 解析顺序：`typeof body === 'string'` → 直接展示；否则 `detail`（字符串或数组首项的 `msg`）→ `error.message` → 状态码兜底文案；
3. **永不**把结构化对象直接渲染成 React 子节点（N14 事故根因）。

实现位置：`src/three-platform/web/lib/err-envelope.ts`（纯函数 + 21 单测）、`lib/api.ts:errText()`。

---

## 2. 逐条裁定（历史不一致项）

### 2.1 DELETE 不存在资源：qoder 幂等 200 vs Manager 404 —— **已修（N21）**

- 原状：qoder `DELETE /api/keys/999999` → 200 `{"ok":true}`（把"不存在"当成功）；Manager → 404。
- 裁定：**统一为 404**（显式语义优先，客户端才能区分"删掉了"与"本来就没有"）。
- 修复：`qtg/app/qtg/manager_api.py:delete_key` 改为返回真实结果（原为无条件 `return True`）；
  `qtg/app/qtg/routers/compat.py:compat_keys_delete` 在 `not ok` 时返回 404 `{"detail":"未找到该密钥"}`。
- 验证：`qtg/tests/test_keys_manager.py::ManagerKeysN21ContractTest`（`test_delete_missing_returns_404`）+ Linux 全量 310/310。

### 2.2 空 body 创建密钥：qoder 200"未命名" vs Manager 422 —— **已修（N21）**

- 裁定：**统一 422**（名称是必填语义字段，静默默认值会制造无法识别的资源）。
- 修复：`qtg/app/qtg/routers/compat_fixes.py:fixed_keys_create` 空/空白名称返回 422 `{"detail":"名称不能为空"}`。
- 验证：`test_create_rejects_empty_name` + Linux 全量 310/310。

### 2.3 401 返回 HTML —— **文档化，不改**

理由：拦截发生在 nginx `auth_request` 层，早于应用；改成 JSON 需要把认证下沉到应用，扩大攻击面与改动面。
契约要求：**客户端一律以状态码判断认证失败，不解析 body**（现有 `errText()` 对 HTML body 走状态码兜底文案，已验证）。

### 2.4 `GET /api/keys/import-local/detect` 管理员也 403 —— **设计如此**

非面板所在机器访问本机导入能力时必须拒绝；UI 侧有对应提示文案（PB0052 `verified_unsupported_in_lab`）。

### 2.5 viewer 读不存在资源得 404（先存在性后角色）—— **接受**

常见取舍：不泄露"资源是否存在"给无权限者本可更严格，但本项目 UI 依赖 404 做空态；记录不改。

### 2.6 用户管理：读用 `current_user`，写用 `require_session_admin`（N24 裁定）—— **设计如此**

- 实测：`scope=readonly` 的 API Token `GET /api/users` → **200**（返回用户名与角色列表）。
- 源码：`settings.py:209 list_users` 依赖 `current_user`；`POST/PATCH/DELETE /users*` 全部依赖 `require_session_admin`。
- 裁定：**不是缺陷**。`session-only` 的设计目标写在 `security.py:require_session_admin` 注释里——
  "能改代码、能扩大权限、或能抹掉痕迹的入口只对真人开放"；只读列举不在此列。
- 依据：`security.py` 的 `scope=keys` 分支同样只放行密钥相关端点，说明 scope 是**服务边界**而非"只读即安全"。
- 遗留建议（产品决策，不阻断）：若担心 token 泄露后账号枚举，可把用户列表也纳入 session-only；当前不改。

### 2.7 用户自删/自降级保护（N23 裁定）—— **已实现，无需修复**

实测（源 `1d358c1e`，即当前工作树）：

| 场景 | 结果 | 依据 |
|---|---|---|
| 唯一 admin 自删 | 400 `至少保留一个最高管理员` | `settings.py:314-316` |
| 唯一 admin 自降级 | 400 `至少保留一个最高管理员` | `settings.py:269-272` |
| **有第二个 admin 时自删** | **400 `不能删除当前登录用户`** | `settings.py:317-318` |
| 删除他人 | 200 | 预期 |

历史现象：旧 fixture `575d94d2`（更早源码包）上自删曾返回 200——该包不含 317-318 行的"本人保护"。
**教训入档**：跨 fixture 比较证据前必须先核对 `source_sha256` 与目标函数字节一致（本轮通过 grep 快照 `settings.py` 定案）。

### 2.8 错误后无重试控件（N22 / PB0068）—— **N22 已修，PB0068 记录为产品决策**

- N22（changelog）：失败态只有琥珀色说明，无重试，只能整页刷新 → **已修**：
  `ChangelogPanel.tsx` 增加 `attempt` 计数驱动 effect 重跑 + 失败态"重试"按钮；5 语言包补 `changelog.retry`。
- PB0068（playground 错误后无重试）：**不修**，记产品决策——重试等于重发一次计费请求，应由用户决定是否改参数重发，而不是给一个"再烧一次"的按钮。
- N25 更正：playground 对真实 502/401/SSE 错误帧**都能正确显示文案**（`上游不可用（QA 注入）` / `登录已过期` / `上游炸了 (500)`）；
  早前"静默"结论源于测试用 `route.abort()`，被 `chat-stream.ts` 正确识别为用户取消（`aborted` 分支刻意不报错）。**N25 撤回**。

---

## 3. 平台能力矩阵（qoder / trae vs WorkBuddy）

| 能力 | WorkBuddy (Manager) | Qoder / Trae (QTG) | 信封 |
|---|---|---|---|
| 密钥 CRUD | 200 / 404 / 422 | 同（N21 修复后） | JSON `detail` |
| 红包（创建/领取/收回） | 完整实现 | **410 不实现** | `{available:false,code:unsupported}` |
| 管理 Token | 完整实现 | **409 平台不支持** | 可操作中文 detail |
| 用户管理 | 完整实现 | **409 平台不支持** | 同上 |
| 代理线路 | 完整实现（需 upstream.json） | **409 平台不支持** | 同上 |
| 账号池 / 任务 / 签到 | 完整实现 | **409/501 不支持** | 同上 |
| 转发代理（推理） | `/v1/responses`、`/v1/messages` | 由 QTG 转发 | — |
| 未实现路径 | 404 | **501 `not_implemented`** | 有意区分 |

---

## 4. 验收清单（本契约的可检验项）

| # | 事项 | 状态 | 证据 |
|---|---|---|---|
| 1 | 11 类信封机器化归类 | ✅ | matrix.json（480 探针） |
| 2 | CSRF 写面全量生效 | ✅ | k29-csrf-recheck.json（20 探针） |
| 3 | N21 两处跨平台不一致修复 | ✅ | 专项 11/11 + Linux 310/310 |
| 4 | N14 客户端信封解析 | ✅ | err-envelope 21 单测 + 7 场景浏览器复跑 |
| 5 | N23 本人保护 | ✅ 设计正确 | 4 场景实测（本节 2.7） |
| 6 | N24 readonly token 读用户表 | ✅ 设计如此（建议留档） | 本节 2.6 |
| 7 | N25 playground 错误呈现 | ✅ 撤回（测试方法问题） | n25ix（3 场景） |
| 8 | N22 changelog 重试 | ✅ 已修 | 本节 2.8 + 构建通过 |
| 9 | 401-HTML 文档化 | ✅ | 本节 2.3 |
| 10 | 逐端点契约表（端点级，非类别级） | ⏳ 未做 | 需要按 route-table.json 逐条展开 |

**剩余工作**：第 10 项（端点级明细）依赖 `design/qtg/route-table.json`，属 S1 收尾；本契约的类别级与裁定级已可用于客户端开发与验收。
