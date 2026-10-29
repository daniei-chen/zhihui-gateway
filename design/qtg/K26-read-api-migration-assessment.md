# K26 只读直连耦合迁移 · 可行性评估与实施方案

- 状态：**评估稿（2026-10-24 接管期）**。**读通道已于 2026-10-25（R1-A）实施并验证**：
  当前契约、两种 token 与端点权限矩阵见 `design/qtg/K26-read-token-split.md`。
  本文保留评估过程与历史结论；§1–§3 的"行数/待实施"数字是当时快照，
  §6 的"scope 阻塞"已被解决——**以新文档为准**。
- 任务卡要求（`tasks/03-52项逐条执行卡.md:280`）：
  1. 先定合同与版本探测，再迁稳定只读 API/快照视图；
  2. 连接及时关闭、限制长事务、保留现有只读边界；
  3. 迁移 QTG 对 Manager SQLite 的直接读取到**鉴权、版本化、分页**的只读 API；
  4. 列表、统计、日志、任务、审计、设置逐方法迁移；
  5. **API 不可用不得偷偷回退直读 DB**。

---

## 1. 现状盘点（机器化）

### 1.1 直连点

| 项 | 值 |
|---|---|
| 直连文件 | `qtg/app/qtg/adapters/workbuddy.py`（820 行） |
| 打开方式 | `sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)` + `PRAGMA query_only=1`（第 110-111 行） |
| 连接获取点 | `self._db()` 共 **13 处** |
| 表结构探测 | `_schema_ok()` 读 `sqlite_master` |
| 配置项 | `settings.wb_db`（默认 `/opt/workbuddy-manager/data/manager.db`） |

### 1.2 需要迁移的读取方法（按 `_db()` 调用分类）

| 方法 | 行 | 读什么 | Manager 侧对应 API | 覆盖度 |
|---|---|---|---|---|
| `overview` | 162 | 账号数与状态聚合 | `/api/stats/summary` | ✅ 直接可用 |
| `_checkin_rows` | 181 | 签到记录 | `/api/checkin-logs` | ✅ 直接可用 |
| `usage_summary` | 241 | 用量汇总（active/top） | `/api/stats/summary` | ✅ 直接可用 |
| `usage_hourly` | 292 | 分时用量 | `/api/stats/hourly` | ✅ 直接可用 |
| `credits_flow` | 349 | 积分流水 | `/api/stats/credits` | ✅ 直接可用 |
| `usage_by_model` | 424 | 按模型用量 | `/api/stats/by-model` | ✅ 直接可用 |
| `usage_by_key` | 462 | 按密钥用量 | `/api/stats/by-key` | ✅ 直接可用 |
| `usage` | 496 | 组合（14 天） | `/api/stats/daily` | ✅ 直接可用 |
| `usage_analytics` | 562 | 分析聚合 | `/api/stats/{daily,by-model,by-key}` 组合 | ⚠️ 需 3 次调用合并 |
| `keys` | 582 | 密钥列表 | `/api/keys` | ✅ 直接可用 |
| `logs` | 623 | 请求日志 | `/api/logs`（分页） | ✅ 直接可用 |
| `task_logs` | 650 | 任务日志 | `/api/task-logs` | ✅ 直接可用 |
| `audit_logs` | 665 | 审计日志 | `/api/audit-logs` | ✅ 直接可用 |
| `settings` | 687 | 管理端设置 | `/api/settings/*`（多端点） | ⚠️ 需按 key 拆分 |
| `models` | 681 | 模型目录 | `/api/models` | ✅ 直接可用 |

**结论：13 处直连中 11 处有现成 Manager API 可直接对接，2 处需组合/拆分。**

### 1.3 关键约束（迁移时必须保持）

1. **单位与口径**：额度（credit）为浮点、token 为整数；`realm`（cn/global）过滤条件必须透传；
2. **时区**：Manager 侧按 `Asia/Shanghai` 聚合日边界（`_stats_since`）；QTG 不能自行换时区；
3. **时间戳精度**：部分字段是 epoch 秒（`expires_at`）而页面曾误用毫秒（本次会话踩过）；
4. **鉴权**：QTG 访问 Manager API 用 `qtg-admin-token`（bridge 已验证）；
5. **失败语义**：**不得回退直读 DB**——Manager API 不可用时必须报错（409/503），而不是静默读本地文件；
6. **async 边界**：现有 `usage_summary` 等是**同步方法**（`def`，非 `async def`），在 ASGI 主循环里调用 SQLite 会阻塞。迁移成 HTTP 后必须变成 `async`，且调用点要 `await`——这是最容易漏的改动。

---

## 2. 迁移方案（分三步，可独立验收）

### 步骤 A：契约与版本探测（先做，风险最低）

1. Manager 增加 `GET /api/read-api/version`：
   ```json
   {"version":"1","capabilities":{"stats":true,"logs":true,"tasks":true,"audit":true,"keys":true},
    "timezone":"Asia/Shanghai","credit_unit":"float","min_qtg_version":"0.9"}
   ```
2. QTG 启动时探测；版本不匹配则**显式拒绝启动**（不静默降级）。
3. 契约文档挂到 `design/qtg/K26-read-api-contract.md`（字段、单位、时区、分页、错误码）。

**验收**：版本探测正/负例（不存在、版本过旧、能力缺失）；无回退行为。

### 步骤 B：逐方法迁移（按依赖顺序）

顺序（先纯读、后聚合、最后设置）：

1. `keys` / `logs` / `task_logs` / `audit_logs`（分页，最简单）
2. `usage_summary` / `usage_hourly` / `credits_flow` / `usage_by_model` / `usage_by_key`
3. `overview` / `_checkin_rows` / `models`
4. `usage` / `usage_analytics`（组合调用）
5. `settings`（需先拆出 Manager 的细粒度设置端点）

每个方法的迁移模板：

```python
# 迁移前（同步读 SQLite，阻塞事件循环）
def usage_summary(self, realm=""):
    conn = self._db()
    ...
# 迁移后（异步 HTTP，失败即报错）
async def usage_summary(self, realm=""):
    r = await self._read_api.get("/api/stats/summary", params={"realm": realm})
    if r.status != 200:
        raise ReadApiUnavailable(f"stats summary unavailable: {r.status}")
    return self._normalize_summary(r.json)
```

**必改项**：所有调用方要加 `await`。用 `grep -n "wb.usage_summary\|wb.overview"` 逐个核对。

### 步骤 C：移除直连（收尾）

1. 删除 `_db()` / `_schema_ok()` / `sqlite3` 导入；
2. 删除 `settings.wb_db`（改为 `settings.manager_base_url`）；
3. 增加负向测试：**篡改 `wb_db` 指向无效路径，功能仍必须正常**（证明不再依赖本地 DB）；
4. 增加故障测试：Manager API 返回 503 时，QTG 必须报错而不是返回空数据。

---

## 3. 工作量与风险评估

| 项 | 评估 |
|---|---|
| 代码改动 | QTG 侧 `workbuddy.py` 820 行中约 500 行读取逻辑替换；调用点约 20 处加 `await` |
| Manager 侧 | 仅新增版本探测端点（现有 7 个 stats 端点已够用） |
| 测试改动 | `qtg/tests/` 中所有 mock `_db()` 的用例需改为 mock HTTP |
| 风险点 1 | **同步→异步传染**：`usage_summary` 等被同步调用方引用，需逐个追调用链 |
| 风险点 2 | **口径漂移**：Manager API 的聚合口径与 QTG 直读 SQL 未必完全一致（尤其 `active` 判定），必须做对比测试 |
| 风险点 3 | **性能**：原先是本地 SQLite 单次查询，改后是 HTTP 往返；统计页会从 1 次变多次 |
| 预计工期 | 步骤 A：0.5 天；步骤 B：3–4 天（15 个方法 + 调用链）；步骤 C：1 天；合计 **4–5 天** |

**结论：不宜在本次接管窗口内整体完成**（会引入大面积未验证改动，违背"每次做可验收的小批次"）。建议按步骤 A → B(1) → B(2)… 分批交付。

---

## 4. 评估阶段已完成的准备（历史快照）

1. ✅ 直连点全量盘点（当时 13 处 `_db()`、15 个读取方法、Manager 侧覆盖度矩阵）；
2. ✅ 兼容性约束清单（单位/时区/时间戳/鉴权/失败语义/async 边界）；
3. ✅ 迁移模板与顺序；
4. ✅ 工作量与风险评估（4–5 天，分批可验收）。

## 5. 实施结果（2026-10-25，R1-A；取代上文"未实施"的结论）

- ✅ 步骤 A：`GET /api/read-api/version` 上线并升到 **v2**（新增 usage 族）；
  QTG 在**实际读路径**上校验版本/能力，未知版本或缺能力显式失败
  （`UpstreamError(code="read_api_unsupported")`，不是"读个大概"）。
- ✅ 步骤 B：**全部 16 条读方法**都有 HTTP 通道（14 条既有 + `usage` / `usage_analytics`）。
  `usage()`（按天×模型）与 `usage_analytics()`（全表拆分）由 Manager v2 的
  `/api/read-api/usage-matrix` / `/usage-totals` 提供；端点返回**原始行**，让 QTG 两种
  读模式共用同一段映射代码，切换时逐位对账。
- ✅ 步骤 C（部分）：`_db()` 仍在（sqlite 模式需要），但 **http 模式下 `_db()` 直接抛
  RuntimeError**；`wb_db` 指向不存在路径时 16 条读全部返回真实数据、`_db()` 调用 0 次
  （`evidence/r1-k26/http-no-db-read.json`）。
- ✅ 读写凭据分离：`manager_read_token_file`（scope=read）与
  `manager_service_token_file`（scope=keys）分开调用、互不回退；`/api/audit-logs`
  对 scope=read 打开（新增 `require_admin_or_read_scope`，viewer/readonly 仍拒绝）。
- ✅ 失败语义（RV05）：`_request` **默认严格**——404/4xx/5xx/超时/连接失败/非 JSON/
  空 body/JSON null/`{}` 一律抛可区分异常；只有 delete/update/reset-usage 显式
  `allow_not_found=True`。历史"404 → None → 回退 sqlite"的路径已删除。
- ⏳ 未做（属 R2/发布步骤）：默认值切换与撤 DB 挂载、日志/设置族的响应形状统一、
  `overview()` 的 accounts 段迁移、failures 日边界统一 —— 见
  `design/qtg/K26-read-token-split.md` §6/§7。

## 6. 2026-10-25 复核提出的"硬阻塞"：已解决

复核 RV05 §1 当时指出：`manager_api._headers` 统一读 `manager_service_token_file`
（scope=keys），而 keys 令牌读不到 stats/logs/task/audit 族，因此"切换默认前必须由
产品/安全裁定新增只读令牌（推荐），或放宽 scope=keys（不推荐）"。

R1-A 已按**推荐方案**落地，没有抬升成 admin、也没有放宽 scope=keys：

- Manager 侧：`scope=read` 显式白名单（只 GET、排除凭据面/用户面）+
  新增 `security.require_admin_or_read_scope` 供 `/api/audit-logs` 这类"只读但敏感"端点；
- QTG 侧：新增 `manager_read_token_file`，所有 `read_*` 以 `purpose="read"` 送只读令牌，
  keys 令牌只用于 `/api/keys` 的写与管理读；
- 真 uvicorn Manager + 真令牌的权限矩阵：`evidence/r1-k26/token-split-matrix.json`（22 个 case）。
