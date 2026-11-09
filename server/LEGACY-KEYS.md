# K01-S1：Manager 兼容 QTG 旧 key（`legacy_qtg_keys`）

状态：本地实现（未发布）。基线 `b53f36e`。对应审计任务 K01（方案 a：
**Manager 为公开推理密钥权威；QTG 旧 key 经迁移继续可用；新 key 统一 `wbk_` 前缀**）。

本文件说明三件事：**字段怎么映射**、**怎么回退**、**哪些还没验证/待决**。
代码入口：

| 位置 | 作用 |
|---|---|
| `manager/server/legacymig.py` | 迁移台账 DDL（由 `db.py` 初始化路径执行）、映射与迁移逻辑、CLI 实现（`legacymig.cli`） |
| `manager/server/keysvc.py` | `resolve()` 的旧 key 分支、`validate(..., platform=...)` 平台边界 |
| `manager/server/db.py` | `SCHEMA += legacymig.SCHEMA`；`api_keys.source` 列（先扩后收） |
| `tools/migrate_qtg_keys.py` | CLI 薄壳：`--qtg-db --manager-db [--dry-run]`（逻辑在 `legacymig.cli`） |
| `manager/server/tests/test_legacy_keys.py` | 迁移/resolve/边界/幂等/CLI 回归 |

---

## 1. 为什么需要"权威行 + 迁移台账"两张表

`resolve()` 的返回值不是"只看一眼"，它会被这些地方按 `key['id']` 消费：

* `budget.prepare()`：`SELECT quota,... FROM api_keys WHERE id=?`，**查不到直接拒绝**
  （"密钥已被删除"），并写 `budget_reservations.key_id`；
* `keysvc.touch()` / `db.bump_usage()`：按 id 回写 `used_tokens` / `used_credit` /
  `api_key_ips` / `usage_daily`；
* `request_logs.key_id`、`observability` 并发槽、管理端列表/停用/删除。

所以旧 key **不能**返回一个"迁移表自己的 id"：那会和 `api_keys` 的自增 id 撞车
（最坏情况是给别的 key 记账），或者让每个旧 key 请求都被预算层拒绝。

因此迁移同时写两处，角色分开：

| 表 | 角色 | 说明 |
|---|---|---|
| `legacy_qtg_keys` | **迁移台账/来源记录** | 只从 QTG 迁移来；`key_hash` 主键；保留 QTG 原始语义（日/月限额、平台、并发、备注）；`manager_key_id` 指向权威行。`resolve()` **只查这张表**来定位旧 key，未登记的一律 None |
| `api_keys` | **权威行** | 每把旧 key 一行，`source='qtg-legacy'`；与 `wbk_` key 同一套校验/预算/计量/管理能力；管理端停用/删除/改配额立即生效 |

`resolve()` 的判定链（`keysvc._resolve_legacy_qtg`）：

1. 前缀必须是 `sk-qtg-` —— **不放宽成任意前缀**；
2. 用 `prefix = token[:12]` 走 `idx_legacy_qtg_prefix` 索引定位，再
   `secrets.compare_digest` 恒时比较 `key_hash`（与 `wbk_` 分支同口径）；
3. 台账 `enabled=1` 且未过期；
4. 权威行 `api_keys` 存在、`enabled=1`、未过期（双保险：管理端状态优先）；
5. 返回 `_parse(权威行)` + `legacy_platforms`（QTG 平台范围，供 `validate` 守边界）。

随机 `sk-xxx`、未迁移的 `sk-qtg-`、已删除/停用的旧 key：**一律 None**。
`wbk_` 分支一行未改（`resolve()` 里旧 key 判断在它之前，原有代码保持原样）。

---

## 2. 字段映射表

### 2.1 逐字段对照（QTG `qtg_keys` → Manager）

| QTG 字段 | Manager 落点 | 规则 | 是否等价 |
|---|---|---|---|
| `id`（TEXT） | `legacy_qtg_keys.qtg_key_id` | 原值保留，仅作来源标识；**不复用为 id** | — |
| `key_hash` | `legacy_qtg_keys.key_hash`（PK）+ `api_keys.key_hash` | 原值（两边都是 `sha256(明文).hexdigest()`，迁移不需要明文） | ✅ 完全等价 |
| `key_prefix`（明文前 12 位） | `legacy_qtg_keys.prefix` + `api_keys.prefix` | 原值；`resolve` 的索引定位列 | ✅ 等价 |
| `name` | `legacy_qtg_keys.name` + `api_keys.name` | 原值（截 64 字符） | ✅ |
| `enabled` | 两表 `enabled` | 原值（迁移时快照）；两边都为 1 才放行 | ✅ |
| `expires_at`（0=永不过期） | `legacy_qtg_keys.expires_at` + `api_keys.expires_at`（0→NULL） | 原值 | ✅ |
| `max_ips` | 同上 | 原值 | ⚠ 见 2.3 |
| `ip_allowlist`（逗号串） | `api_keys.ip_allowlist`（JSON 数组） | 逗号拆分 → JSON 数组；`validate` 用 `ip_matches` 判定 | ✅ 语义一致 |
| `models`（逗号串） | `api_keys.models`（JSON 数组） | 逗号拆分 → JSON 数组（原样，不去平台前缀） | ⚠ 见 2.4 |
| `platforms`（逗号串） | `legacy_qtg_keys.platforms`；`resolve` 输出 `legacy_platforms` | 原值；`validate` 按"请求生效平台"判 | ✅ 语义保留（见 2.2） |
| `daily_limit`（按日 token） | `legacy_qtg_keys.daily_limit` | 原值留档；**不映射** `quota` | ❌ 不等价（见 2.5） |
| `monthly_limit`（按月 token） | `legacy_qtg_keys.monthly_limit` | 同上 | ❌ 不等价（见 2.5） |
| `max_concurrency` | `legacy_qtg_keys.max_concurrency` | 原值留档；Manager 无 per-key 并发字段 | ❌ 未迁移（见 2.6） |
| `note` | `legacy_qtg_keys.note` | 原值（Manager `api_keys` 无备注列） | ✅ 留档 |
| `created_at`（本地时间文本） | `api_keys.created_at`（unix 秒） | 解析失败用迁移时刻兜底（只影响列表排序） | ✅ |
| `last_used_at` | — | 不迁移：Manager 从迁移后开始记自己的 `last_used_at` | — |

`api_keys` 上其余字段的取值：`realm=''`（不限制）、`quota=0`、`used_tokens=0`、
`quota_credit=0`、`used_credit=0`、`upstream_id=NULL`、`source='qtg-legacy'`。

### 2.2 平台范围（platforms）：保留 + 强制，不默认全平台

QTG 的 `platforms` 是**数据面真实约束**（`qtg/app/qtg/routers/proxy.py` 会拒绝不在
列表里的平台：`platform_not_allowed` / `platform_mismatch`；`anthropic.py` 还要求
`qoder`）。Manager 目前没有 per-key 平台字段，等价概念是**入口决定的生效平台**：

* `/qoder/v1/*` → `qoder`（`main.py` 三平台中间件写 `gateway.forced_platform`）；
* `/trae/v1/*` → `trae`；
* 其余（含 `/v1/*`、`/v1/messages`）→ `workbuddy`（与 `db.add_request_log` 的平台归属同口径）。

迁移把 `platforms` 原值放进台账，`resolve()` 产出 `legacy_platforms`，
`keysvc.validate(..., platform=<生效平台>)` 命中则放行、否则 400
`platform_not_allowed`。两条协议入口（`gateway._authorize`、`anthropic._authorize`）
都已显式传入平台；`wbk_` key 没有该字段，行为不变。

> 若不这样做，一把"只绑 qoder"的旧 key 迁进 Manager 后会静默变成三平台通用——
> 这正是计划 §6.2.4 禁止的"不默认全平台"。

### 2.3 `max_ips`：比 QTG 更严格（已知差异）

QTG 的 `max_ips` 只统计**最近 24h** 出现过的 IP（换网络后旧 IP 会退出占位）；
Manager 的 `api_key_ips` 记**历史全量** IP，只在"新 IP 且已满额"时拒绝。
迁移沿用原值 → Manager 侧可能比 QTG **更严格**（换过网络后可能因历史 IP 占位被拒）。
处置：管理端把该 key 的 `max_ips` 调大/清零，或在受控窗口清理 `api_key_ips`；
未处理前属于"更严不放宽"，可接受。

### 2.4 `models`：原样迁移，平台前缀写法登记待决

QTG 允许 `qoder/模型名` 这种"平台/模型"写法（`proxy.py` 的 `_route`）。Manager 的
模型白名单没有平台维度，原样搬过去这类条目**匹配不上**（表现为 400
`model_not_allowed`，且平台范围已由 `legacy_platforms` 单独守住）。迁移脚本会累计
`pending_model_map` 计数并在输出里报告，不假装映射成功。裸模型名（如 `glm-5.2`）
按 Manager 白名单口径生效（`_bare_model` 兼容 `cn:` 前缀）。

### 2.5 额度映射（**重点**）：`daily_limit` / `monthly_limit` 不硬等价

两边语义不同：

| 维度 | QTG | Manager |
|---|---|---|
| `daily_limit` | **按日** token 上限（当日用量累计） | **无此字段** |
| `monthly_limit` | **按月** token 上限（当月用量累计） | **无此字段** |
| `quota` | — | **总量** token 上限（无时间窗口，用尽即永久拒绝） |
| `quota_credit` | — | 积分额度（与 token 额度各自独立） |

把日限额塞进 `quota` 会造成"当天就用完整月/整把钥匙永久失效"（放大为永久锁死）；
把月限额塞进 `quota` 同理。**因此不硬等价**，本轮取值：

> **QTG `daily_limit` / `monthly_limit` 原值保留在 `legacy_qtg_keys`，不写入
> `api_keys.quota`；`quota = 0`（不限总量）。旧 key 的日/月限额由 QTG 侧语义迁移，
> Manager 当前无对应字段，保守为 0。**（该句同时写在 `legacymig._build_api_keys_args`
> 的代码注释里，不是静默行为。）

`quota_credit = 0`：QTG 没有积分概念，不臆造。
影响：**迁移后旧 key 在 Manager 侧不受日/月限额限制**（唯一权威执行点已从 QTG
切到 Manager，而 Manager 没有窗口字段）。这是本阶段已知缺口，见 §4 未决项 1。

### 2.6 并发上限（`max_concurrency`）：未迁移

QTG 按 per-key `max_concurrency` 拒绝并发（`proxy.py` 的并发计数）；Manager 只有
按额度推导的并发槽（有额度 1、无额度 4，见 `observability.admit_key`）。
迁移保留原值到台账但**不执行** → 旧 key 在 Manager 侧按 Manager 默认并发策略运行，
可能比原上限宽松。见 §4 未决项 2。

### 2.7 `enabled` / 过期 / 吊销的权威归属

* 迁移时 `enabled=0` 的 key 也写入台账（`enabled=0` 快照），但 `resolve` 拒绝；
* 迁移后**以 Manager 权威行为准**：管理端停用/删除/改过期时间立即生效
  （`resolve` 每次都回查 `api_keys`）；
* 迁移是 `INSERT OR IGNORE` 幂等：**重跑不会覆盖**已登记行的 `enabled`/额度，
  也不会"复活"已删除的权威行；QTG 侧迁移后的启停不会自动同步（见 §4 未决项 3）。

---

## 3. 迁移与回退

### 3.1 执行（本地/预发）

```bash
# 1) 预演：只读两边库，输出计数与哈希前 8 位指纹，一个字节都不写
python tools/migrate_qtg_keys.py --qtg-db <qtg.db> --manager-db <manager.db> --dry-run

# 2) 真实执行（幂等，可重复跑；建议对 Manager 停机副本执行）
python tools/migrate_qtg_keys.py --qtg-db <qtg.db> --manager-db <manager.db>
```

输出只有：`fp=<哈希前8位> action=insert|skip_existing|...` 与计数
（`qtg_rows/inserted/skipped_existing/skipped_invalid_hash/skipped_missing_prefix/
skipped_bad_prefix/skipped_missing_platforms/reused_canonical/pending_model_map/
pending_unknown_platform/failed`）。**不输出明文 key、不输出完整哈希、不输出备注**。

核对点：

1. `inserted + skipped_existing == qtg_rows - 被跳过行数`（被跳过行由计数项体现）；
2. `legacy_total` 应等于 QTG 侧可用 key 数（生产该表当前仅 2 行，以只读统计为准）；
3. 用旧 key 实测：模型列表 + 流式调用 + 吊销（停用后立即 401）；
4. 随机 `sk-xxx`、未迁移 `sk-qtg-` 仍 401。

### 3.2 回退（不碰 QTG、不恢复已吊销 key）

代码回退：恢复 `keysvc.resolve()` / `validate()` / `db.py` 的改动即可；旧数据不影响
`wbk_` key（`legacy_qtg_keys` 表与 `api_keys.source` 列都是追加式，旧代码不读它们）。

数据回退（如确认要撤销迁移，按 source 精确清理，不误删新 key）：

```sql
-- 仅删除"从 QTG 迁移来的权威行"及其台账；wbk_ 新 key 不受影响
DELETE FROM api_key_ips WHERE key_id IN (SELECT id FROM api_keys WHERE source='qtg-legacy');
DELETE FROM api_keys     WHERE source='qtg-legacy';
DELETE FROM legacy_qtg_keys;
```

注意：**不恢复已吊销/已删除的 key**；不重置用量账本；不覆盖切换后新建的 key。
回退后旧 key 在 Manager 上立即 401，但 QTG 本地仍保有原 key（本轮不改 QTG），
业务可按"QTG 兼容边界"继续运行。

---

## 4. 未决项 / 未验证项（不得当成已通过）

1. **日/月限额在 Manager 侧不执行**（§2.5）：需要 K02/K27 决定 Manager 是否新增
   "日/月 token 窗口"字段；在此之前迁移后旧 key 不受原日/月限额约束。
2. **per-key 并发上限未迁移**（§2.6），Manager 按默认并发策略（有额度 1 / 无额度 4）。
3. **QTG 侧迁移后的启停不同步**：`INSERT OR IGNORE` 幂等意味着重跑不更新
   `enabled`。当前以 Manager 为权威；若需反向同步，需新增 `--sync` 模式（本轮未做）。
4. **平台前缀模型写法**（`qoder/xxx`）未映射，只计数上报（§2.4）；需要 K02 定
   "平台×模型"口径后再补转换。
5. **`realm` 未迁移**（QTG 无版本概念）：取不限制（`''`），与迁移前 QTG 行为一致；
   若后续要求旧 key 只允许某版本模型，需人工在管理端补填。
6. **生产迁移未执行、未授权**：本轮只在本地实现与隔离测试；生产 `qtg_keys` 行数、
   真实 key 的平台范围、`pending_*` 计数都需要在生产只读环境中核对后才能发布。
7. **未做端到端 HTTP/浏览器验证**：本轮验证到 `resolve/validate` 单元与迁移逻辑；
   `/qoder/v1`、`/trae/v1` 的真实流式调用/计量/吊销闭环属于 K01 验收第 3–8 项，
   需在隔离三平台环境（假 provider）里另行执行。

## 5. 本轮验证（隔离运行器，2026-10-23）

```bash
cd D:\AI\codex\codex1\zhihui-execution
# 只跑本卡相关用例（隔离视图不含 tools/，CLI 逻辑经 legacymig.cli 单测）
python -X utf8 tests\zhihui_harness\run_tests.py --target manager --keep \
    --pytest-arg=-k --pytest-arg="Legacy or GatewayWiring"

# 全量（用于回归对比）
python -X utf8 tests\zhihui_harness\run_tests.py --target manager --keep
```

实测结果：

* **聚焦运行**：`61 passed / 0 failed / 0 errors / 0 skipped`（exit 0）。
  `test_legacy_keys.py` 37 个用例（含全部子测试）全过：迁移后 resolve、
  未迁移/随机 `sk-` 拒绝、enabled/过期拒绝、Manager 侧停用/删除即时失效、
  幂等、dry-run 不写库、平台/IP/模型边界、`wbk_` 行为不变、schema 先扩后收、
  CLI 参数/退出码/输出只含计数与指纹。
* **CLI e2e（独立临时库，非隔离视图）**：`--dry-run` → `inserted=1` 且不写库；
  真实执行 → `inserted=1`；重复执行 → `inserted=0 / skipped_existing=1`；
  迁移后 `keysvc.resolve(旧 key)` 成功；stdout 不含明文与完整哈希。
* **全量运行**（含当时并行改动）：`tests=2702 passed=2358 failed=257 errors=70
  skipped=17`。相对基线（`2662 / 2321 / 255 / 70 / 16`）：本卡新增用例 0 失败；
  基线用例**无一翻红**；新增的 2 个失败属于并行进行的 K01-S2（`keys_service`
  服务令牌）改动（`test_keys_scope`、`test_api_tokens` 的写端点矩阵），与本卡
  无关，发布前由对应任务收尾。
