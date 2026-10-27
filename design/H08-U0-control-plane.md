# H08 U0：统一控制面盘点（路由 / 权限 / 数据所有者）

日期：2026-10-26T14:30+08:00。状态：**盘点与计划**，本轮未改动任何应用代码。
依据：`evidence/lossless-20261026T013213Z/h01-20261026T0335Z/control-plane-manager.json`、
`control-plane-qtg.json`、`control-plane-overlap.json`，以及 `design/cross/ADR-001-unified-manager.md`。

对应 ADR 的 U0 退出条件：「同一候选能重建；旧/新路由的语义差异逐项登记」。本文件是那一步的输入，不是合并完成的证明。

## 一、实测规模（同一候选：archive `f17a5f52…`）

| 面 | 路由数 | 说明 |
|---|---|---|
| Manager（`manager/server/main.py::app`） | **164** | 153 条 `/api/*`、7 条 `/v1/*`、`/healthz`、`/metrics`、`/responses`、`/v2` |
| Manager 权限分布 | 17 public / 90 需 admin / 147 已鉴权 | 权限由 FastAPI 依赖树解析，不是文档抄写 |
| QTG（`qtg.routeaudit` AST 枚举） | **214** | 214 声明 = 214 唯一 method/path，重复组 0，effective digest `d2db5351…` |
| 两侧同名 method+path | **119** | 即"同一个 URL 有两个实现"，是收敛的主要工作量 |
| QTG 独有 | 95 | 引擎侧能力、平台专属操作 |
| Manager 独有 | 45 | 新版控制面已实现但 QTG 还没有的端点 |

历史口径「213 条 QTG 路由」指的是 QTG 侧声明数；**Manager 侧 164 条是本次新算的**。两个数字不能相加当总能力，也不能用一个覆盖另一个。

## 二、119 条重叠的性质（已抽样核对，非全部）

QTG 侧 handler 命名可区分两类：`compat_` 132 个、`fixed_` 17 个。

| 类型 | 抽样证据 | 判定 |
|---|---|---|
| `fixed_keys` / `fixed_keys_create`（`/api/keys`） | `routers/compat_fixes.py:623-659` 明确「密钥列表统一来自 Manager（K01-S2）…这里只做转发」，调用 `manager_api.list_keys`；Manager 不可达时抛错而不是返回空列表 | **已收敛**，QTG 只做代理 |
| `fixed_accounts` / `fixed_status`（`/api/accounts`、`/api/status`） | 读 `build_aggregate(rt)` 与 `rt.settings.qoder_data_dir`，是 QTG 进程内的引擎运行态 | **未收敛**：Manager 的 `/api/accounts` 与 QTG 的 `/api/accounts` 数据来源不同 |
| `stats_*`（`/api/stats/*`） | QTG 侧同名端点存在；Manager 侧为账本权威 | 需在 U2 逐条确认读的是 Manager 只读 API 还是本地聚合 |
| `compat_*` 其余约 115 条 | 多数经 `manager_api`（`compat.py` 6 处、`compat_fixes.py` 4 处引用） | 属"旧 URL 兼容代理"，方向正确；但**转发不等于收敛**，需逐条登记语义差异 |

**结论**：不能因为"URL 同名且有 compat 前缀"就认定收敛完成。U3 的退出条件要求 214 条历史路由**每条都有去向**，目前只抽检了 4 类。

## 三、数据所有者现状（只写有代码证据的）

| 领域 | 当前权威 | 证据 | 风险 |
|---|---|---|---|
| 用户 / 会话 / RBAC | Manager | `routers/auth.py`、`security.py` 的依赖守卫（90 条 admin） | 低；QTG 侧为签名身份桥，保留 scope 上限 |
| 公开推理密钥 | Manager | `keysvc.py` + QTG `fixed_keys` 转发 | 低；已收敛 |
| 请求日志 / 用量 / 预算 | Manager | `budget.py`、`routers/logs.py`、`routers/stats.py`；17 项回退检查 | 低；不过 QTG `stats_*` 的读取路径待逐条确认 |
| WorkBuddy 账号/用量 | **双模式** | `adapters/workbuddy.py:18` 注明存在 `http`（全部走 Manager 只读 API）与 `sql`（直读 `manager.db`，17 万条请求日志）两种模式；第 186 行注释提到本地 SQL 与 HTTP 404 都可能返回 `None` | **高**：这是 ADR 点名的"存储耦合"，且有静默回退观感，U2 必须明确失败可见 |
| Qoder / Trae 账号池 | QTG 引擎运行态 | `routers/compat_fixes.py:354-411` 读 `build_aggregate(rt)`；`adapters/trae2.py:106-127` 直读引擎 SQLite（只读 URI） | 中：Manager 侧 `accounts` 是副本，两侧同 URL 不同源 |
| 任务 / 签到 / 清理 / 更新 | 待逐条确认 | K28 已有跨进程任务租约（`task_leases`），本轮未验证唯一执行 | 中：U3 要证明"不双执行" |

## 四、U1–U4 的下一步（可执行，按此顺序）

| 阶段 | 本轮之后要做的第一件事 | 退出条件 |
|---|---|---|
| U1 | 用 164 条路由的 `admin_required` 清单核对前端每个写操作是否都有服务端鉴权；把 17 条 public 端点逐条登记用途 | 三平台真实会话/令牌链路通过；不批量换钥匙；public 端点 0 例外 |
| U2 | 逐条确认 `stats_*` 与 `adapters/workbuddy.py` 的 `sql` 模式是否构成静默回退；把读取统一到版本化只读 API | 聚合不依赖 Manager 表结构；断连/不兼容可见 |
| U3 | 把 119 条重叠端点逐条登记去向（转发/迁走/删除/保留兼容），并验证任务类端点"不双执行" | 214 条历史路由全部有去向 |
| U4 | 仅在无活跃调用、任务、会话、恢复依赖，且并发/资源/故障/回退演练通过后评估移除 QTG 控制进程 | 拆除有回退演练证据 |

## 五、本轮明确没做的

- 没有改任何 `manager/`、`qtg/`、`src/` 代码。
- 没有把 119 条重叠全部核完（只抽检 4 类），所以不能宣称"控制面已收敛"。
- 没有做 U1 的真实三平台链路验证（缺真实账号）。
- 没有评估移除 QTG 进程（U4 前置条件远未满足）。

## 六、复跑方式

```powershell
# Manager 侧（隔离临时数据目录，不碰工作树状态）
python -B -X utf8 tools/audit_control_plane.py --out <证据路径>/control-plane-manager.json

# QTG 侧（AST 枚举，不导入应用）
cd qtg/app ; python -B -X utf8 -m qtg.routeaudit --json <证据路径>/control-plane-qtg.json
```

两份 JSON 与 `control-plane-overlap.json` 一起构成可复算的路由基线；任何一侧路由变化都会改变这份对比。
