# K26 读写令牌分离与只读契约（R1-A 实现说明）

- 状态：**读通道已实施并验证**（2026-10-25，R1-A）；`wb_read_mode` 默认值仍为 `sqlite`，
  切换默认与撤 DB 挂载属**发布步骤**，前置条件见 §6。
- 相关代码：`qtg/app/qtg/manager_api.py`、`qtg/app/qtg/adapters/workbuddy.py`、
  `qtg/app/qtg/config.py`、`manager/server/security.py`、`manager/server/routers/read_api.py`、
  `manager/server/routers/settings.py`。
- 证据：`evidence/r1-k26/`（`probe_loopback_real_manager.py` + 4 个 JSON）。
- 历史评估稿：`design/qtg/K26-read-api-migration-assessment.md`（含 2026-10-24/25 的评估过程；
  其"待实施"结论已被本文取代）。

---

## 1. 两种服务凭据：真的分开，且不回退

| 用途 | 配置项 | 默认文件 | Manager scope | 送哪些端点 |
|---|---|---|---|---|
| 只读读取（全部 `read_*`） | `manager_read_token_file` | `/opt/qtg/secrets/manager_read_token` | `read` | GET：`/api/read-api/*`、`/api/stats/*`、`/api/logs`、`/api/task-logs`、`/api/checkin-logs`、`/api/audit-logs`、`/api/accounts`、`/api/models`、`/api/settings/panel`、`/api/keys`（列表） |
| 密钥管理（K01-S2） | `manager_service_token_file` | `/opt/qtg/secrets/manager_service_token` | `keys` | `/api/keys` 的 POST / PATCH / DELETE / `reset-usage` |

实现要点：

- `_request(..., purpose="read"|"keys")` 与 `_headers(..., purpose=...)` 的 `purpose`
  是**必填参数**：新增调用点必须显式声明走哪把令牌，没有默认值可以"顺手"选错。
- 缺令牌 → `ConfigMissing`，消息分别写明 `manager_read_token_file` / `manager_service_token_file`、
  `scope=read` / `scope=keys` 与各自的部署说明；**没有任何"换另一把兜底"的代码路径**。
- 401/403 → `UpstreamUnauthorized`，带 `token_purpose` 属性（read / keys），消息里写明
  是"令牌无效或已过期"（401）还是"该令牌的权限不足/scope 不含此端点"（403）。
- `read_service_token()` 保留原签名（`purpose` 只影响提示语），`read_purpose_token(settings, purpose)`
  是新的按用途入口。

## 2. 端点权限矩阵（Manager 侧，`scope` 是唯一判据）

`security.current_user` 对 `scope=read` 的白名单（`manager/server/security.py`）与端点自身依赖
必须**同时**允许，才真正可达；表里的"实际"列是按真 uvicorn Manager 打出来的
（`evidence/r1-k26/token-split-matrix.json`，22 个 case 全过）。

| 端点 | 方法 | read 令牌 | keys 令牌 | readonly 令牌 | 结论 |
|---|---|---|---|---|---|
| `/api/read-api/version`、`/health` | GET | 200 | 403 | 200* | read 可用 |
| `/api/stats/{summary,daily,hourly,credits,by-model,by-key,upstream}` | GET | 200 | 403 | 200* | read 可用 |
| `/api/logs`、`/api/task-logs`、`/api/checkin-logs` | GET | 200 | 403 | 200* | read 可用 |
| `/api/audit-logs` | GET | **200** | 403 | **403** | R1-A 打开（见下） |
| `/api/accounts`、`/api/models`、`/api/settings/panel` | GET | 200 | 403 | 200* | read 可用 |
| `/api/keys` | GET | 200 | 200 | 200* | read 也可读列表（只含 prefix） |
| `/api/read-api/usage-matrix`、`/usage-totals` | GET | 200 | 403 | 200* | v2 新增 |
| `/api/tokens`、`/api/users` | GET | **403** | 403 | 403 | 凭据面/用户面，永不对服务令牌开放 |
| `/api/keys`、`/api/logs/clear`、`/api/stats/repair-usage` 等写端点 | POST/PATCH/DELETE | **403** | 200（仅 keys 族） | 403* | read 令牌写不进去 |

\* readonly 是"人用的只读令牌"，角色是 viewer；它不在 K26 通道里，列出仅为对照。
**readonly/viewer 对审计日志仍然 403** —— R1-A 只给 `scope=read` 内部服务令牌开了审计日志，
没有把人类用户的权限一起放大。

### 为什么 `/api/audit-logs` 需要改依赖

白名单里早就有 `/api/audit-logs`，但端点自身用 `security.require_admin` 把关，而
`scope_to_role('read')` 是 `viewer` → read 令牌实际拿到 403（"白名单允许、端点不允许"，两边自相矛盾）。
现在新增 `security.require_admin_or_read_scope`：**管理员或 scope=read**，viewer/readonly 不变。
`test_read_scope.py` 里同时钉住：read 令牌 200、readonly 403、keys 令牌 403、写操作 403。

## 3. 读取契约 v2（`manager/server/routers/read_api.py`）

- `READ_API_VERSION = 2`；`CAPABILITIES` 新增 `usage` 族；`ENDPOINTS['usage'] =
  ['/api/read-api/usage-matrix', '/api/read-api/usage-totals']`。
- QTG 侧 `manager_api.KNOWN_READ_API_VERSIONS = {1, 2}`，`READ_FAMILY_MIN_VERSION = {"usage": 2}`；
  **未知版本、版本过低、能力缺失都会显式失败**（`UpstreamError(code="read_api_unsupported")`），
  不是"读个大概"。探测结果在适配器内缓存 300 秒（失败不缓存）。
- 每个读取族在真正读之前校验一次能力：`stats / keys / logs / tasks / checkin / audit /
  accounts / models / settings / usage`。这是 RV05 第 2 条"读取契约探测要应用在实际运行链路"的落点。

### usage 族的口径（与 QTG 原 sqlite 读法逐位一致）

| 端点 | 返回 | 为什么这么设计 |
|---|---|---|
| `/api/read-api/usage-matrix?since=YYYY-MM-DD` | `{version,since,rows:[{day,model,requests,prompt_tokens,completion_tokens,credit,realm}],failures:{day:n}}` | 返回**原始行**而不是二次聚合：QTG 两种读模式共用同一段映射代码，切换时逐位对账。`since` 形态错误一律 400（不回落"今天"） |
| `/api/read-api/usage-totals` | `{version,requests,prompt_tokens,completion_tokens,credits}` | 全表拆分；`/api/stats/summary` 只有合并后的 `total_tokens`，无法无损表达 `usage_analytics()` |

**刻意镜像的边界语义**（不是笔误，改了就对不上账）：

- rows 只过滤 `day >= since`，**无上界**（QTG 原读法也没有）；
- failures 用 `ts >= strftime('%s', since || ' 00:00:00')` —— SQLite 把这个字符串按
  **UTC** 解释，即实际边界是当地 08:00；分组用 `+8 hours` 的日期。QTG 原 sqlite 实现就是这么写的。
  统一到本地零点（`db.day_start_ts`）要**同时改 sqlite 分支并重跑对账**，归 R2。

## 4. http 模式零 DB 依赖（验收口径）

`evidence/r1-k26/http-no-db-read.json`：`wb_read_mode=http`、`WB_DB` 指向**不存在**的路径，
16 条读方法（14 条既有 + `usage` / `usage_analytics`）全部返回真实数据，`_db()` 调用**合计 0 次**。

- `_db()` 在 http 模式下直接抛 `RuntimeError`（硬兜底），任何漏分流的分支都会立刻暴露；
- 未迁移读方法的 501 `ReadChannelNotMigrated` 已随 R1-A 删除（`errors.py` 有注释说明），
  目前**没有**读方法仍走"跳过 HTTP 直读 DB"。

## 5. sqlite / http 口径对账

- `evidence/r1-k26/parity-sqlite-vs-http.json`：同一份真 Manager 数据，
  11 个投影逐值相等（summary / hourly / by-model / by-key / usage / analytics / keys /
  logs / task_logs / audit_logs / checkin 聚合）。
- QTG 测试里还有一份同 fixture 的快速对账：`qtg/tests/test_read_token_split.py::ParityTest`。
- **形状差异仍在**（值相同，JSON 形状不同，属 R2）：
  - `logs()` / `task_logs()` / `audit_logs()`：http 返回 Manager 的 `{total,items}` / `{logs,...}`，
    sqlite 返回 QTG 的 `{data:[...]}`；
  - `keys()`：http 透传 Manager 行（字段更多）；
  - `settings()`：http 返回 `{tasks,schedule,panel,source}`，sqlite 返回 `{scheduler,pool,raw}`；
  - `overview()` 的 accounts 段两种模式都读本地 auths/state 文件（不是 Manager DB），
    容器化部署要迁到 Manager `/api/accounts`（QTG 侧 `read_accounts()` 已就绪但未被调用）。
- 另外：`/api/models` 是 Manager **代理上游** `/v1/models` 的实现，上游不可达时 Manager 回 502、
  QTG 按契约显式失败（探针因此把 Manager 的上游指向假 provider）。这不是 K26 的缺陷，
  但说明"models 读到的可用性"取决于 Manager→上游链路。

## 6. 切换默认值 `wb_read_mode=http` 的前置条件与回退（发布步骤）

**本轮不改代码默认值**：没有 read 令牌的部署一旦默认 http 就会启动即坏（每个读都
`ConfigMissing`）。切换是发布步骤，按顺序做：

前置条件（全部满足才切）：

1. Manager ≥ 本批次版本（读取契约 `version=2`，`/api/read-api/version` 自检通过）；
2. `/opt/qtg/secrets/manager_read_token` 已按 `manager_read_token_file` 部署（600、root 可读），
   对应 Manager 里 `scope=read` 的 token；`/opt/qtg/secrets/manager_service_token`（`scope=keys`）仍在；
3. QTG 指向的 Manager 地址（`wb_panel_base`）可达；`/api/read-api/health` 200；
4. 用 `WB_DB` 指向不存在路径做过一次演练：16 条读全部有数据、`_db()` 0 次
   （本仓库 `evidence/r1-k26/` 即该演练结果）；
5. 面板侧抽查：统计/日志/任务/审计/设置/密钥列表六类与 sqlite 模式数字一致。

切换与回退（用环境变量 `QTG_WB_READ_MODE`，启动期校验只接受 `sqlite|http`）：

```
# 切换（单台）：改 unit 的 env 或 drop-in，然后重启
QTG_WB_READ_MODE=http
systemctl restart qtg
curl -s localhost:8795/healthz                      # 200
# 抽查读端点（统计/日志/任务/审计/设置/密钥列表）

# 回退（30 秒级）：改回
QTG_WB_READ_MODE=sqlite
systemctl restart qtg
```

> 注：`wb_read_mode` 目前没有"开关文件"（`/opt/qtg/var/*` 那套只用于 `wb_engine` /
> `qoder_engine` / `trae_engine`）；如需开关文件形式，属发布脚本改动（R2）。
> 撤 DB 挂载（K26 的最终目标）必须等默认切到 http 且观察一个发布周期之后再单独做，
> 本轮不做。

## 6.5 令牌轮换（R3-L5 起支持 `.next` 双读）

- 读取顺序：**`<path>.next` 优先、`<path>` 兜底**；只有 401/403（令牌不被接受）才回退，
  超时/连接失败/5xx 不换令牌。实现：`manager_api.token_candidates()` / `_request()`。
- 轮换用 `tests/linux_lab/rotate_qtg_token.py`：`stage`（原子写 `.next` + 校验）→
  QTG 下个请求即用新值（**不重启**）→ `promote`（旧值备份 `.prev`）→ Manager 撤销旧令牌 →
  删除 `.prev`。工具含临时文件+fsync+rename、0600、读回校验、失败自动回滚、不输出明文。
- 实测（真实 `manager_api` + 真实 loopback HTTP）：轮换窗口 40 次读取 **0 失败**，
  旧令牌撤销后 **401**，两个槽位都缺时 **ConfigMissing**（列出两个路径）。
  详见 `evidence/r3-l5/README.md` 与 `design/qtg/ADR-credential-rotation.md`。
- 两把令牌（read / keys）各自独立轮换，**互不回退**的约束不变。

## 7. 未完成（明确记录，不算完成）

| 项 | 类型 | 归属 |
|---|---|---|
| `logs/task_logs/audit_logs/keys/settings` 的 http 响应形状与 sqlite 不一致 | 代码缺口 | R2 |
| `overview()` 的 accounts 段仍读本地 auths/state 文件（未用 `/api/accounts`） | 代码缺口 | R2 |
| failures 日边界按 UTC 解释（镜像既有 sqlite 语义，两处一起改） | 代码缺口 + 需重跑对账 | R2 |
| 默认值切换、撤 DB 挂载、生产观察 | 发布步骤 | R2/R4 |
| QTG 的 `manager_read_token_file` 轮换周期与纳管（K17） | 运维/发布 | R2 |
