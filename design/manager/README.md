# Manager 数据层候选实现索引（K21/K23/K24/K25/K27）

- 执行树：`D:\AI\codex\codex1\zhihui-execution`，基线 `b53f36e`（本轮改动只在本工作树）
- 授权范围：**本地候选实现**，未连接生产、未 SSH、未读写生产 DB/账号/密钥
- 写入边界：`manager/server/`（db.py、budget.py、ledger.py、services/、routers/gateway.py 计量部分）、
  `manager/server/tests/`、`tools/`、`design/manager/`
- 测试方式：`python -X utf8 tests\zhihui_harness\run_tests.py --target manager --keep`（隔离运行器）

## 1. 交付物一览

| 任务 | 新增 | 改动 | 文档 |
|---|---|---|---|
| K23 迁移版本化 | `server/schemaver.py`、`server/migrations.py`、`tests/test_schema_versioning.py` | `server/db.py`（connect 内 2 处锚点） | [K23](K23-迁移版本化.md) |
| K25 日志索引 | `tools/k25_log_index_bench.py`、`tests/test_log_query_plan.py` | `migrations.py` v2（索引 DDL） | [K25](K25-日志查询与索引.md) |
| K24 保留归档 | `server/retention.py`、`server/archival.py`、`tools/archive_ledgers.py`、`tests/test_retention_archive.py` | `migrations.py` v3（archive_runs） | [K24](K24-保留与归档.md) |
| K27 计量归因 | `server/metering.py`、`tools/metering_reconcile.py`、`tests/test_metering_attribution.py` | `server/db.py`（request_logs 两列）、`routers/gateway.py`、`migrations.py` v4 | [K27](K27-计量归因.md) |
| K21 超时重试取消 | `server/deadline.py`、`tests/test_request_budgets.py` | `routers/gateway.py` | [K21](K21-超时重试取消.md) |

## 2. 未改动（明确保留）

- `manager/server/legacymig.py`、`test_legacy_keys.py`、`test_keys_scope.py`（K01 同事的成果，一行未动）
- `ledger.py` 的五张不可变事件表与 `no_update`/`no_delete` 触发器：**未删、未改**
- `budget.py` 的 `unknown` 语义：**未自动释放、未自动归档**
- `qtg/`、`src/`、`tests/`（harness）、`contracts/`：未改

## 3. 回退方式（逐项，均为「回退代码」而不是还原数据库）

| 项 | 回退动作 | 影响 |
|---|---|---|
| K23 | 删 `schemaver.py`/`migrations.py`，撤掉 `db.connect()` 里那 6 行（含 try/except） | `schema_migrations`/`schema_backfill_progress` 两张表成为无害残留，可 DROP |
| K25 | 回退 v2 步骤；或直接 `DROP INDEX idx_logs_key_id_id; DROP INDEX idx_logs_unfilled_ts` | 只影响查询速度，不影响正确性 |
| K24 | 删除 `retention.py`/`archival.py` 与 CLI | `archive_runs` 表可保留（只增不删） |
| K27 | 回退 v4 步骤与 `_record`/`_stamp_route`、`add_request_log` 两列 | 新列保留为空即可（`actual_platform`/`upstream_ref` 可空） |
| K21 | 回退 `routers/gateway.py` 的三处（预算/重试/迭代）；`deadline.py` 可留（未被调用则零影响） | 默认 `max_attempts=1`、预算默认等于现有读超时 → 行为与改动前一致 |

## 4. 测试结果

见 [TESTS.md](TESTS.md)：新增 5 个测试文件共 81 个用例全部通过；全量 Manager 套件结果与基线对比见该文件。

## 5. 未验证项（不要当成通过）

1. 生产库上的迁移窗口与耗时：本轮只在 12 万行合成库上测了建索引耗时（见 K25），
   真实库（约 158MB）上的窗口、WAL 增长、锁等待**未测**。
2. K21 的预算/重试只在单元与策略层验证；**没有对真实上游**验证首包/空闲/总时长
   在真实链路上的表现（本地假 provider 未被 Manager 测试真正打到）。
3. K27 的 `actual_platform` 对 Anthropic/Responses 入口是**粗归因**（平台 + 密钥绑定
   上游 id，无 host）：那两个路由文件不在本任务写入边界内。
4. K24 归档脚本只在本机临时库上验证；真实 158MB 库的归档耗时与磁盘占用未测。
5. QTG 库的 `user_version=0` 只做了只读核对（`qtg/app/qtg/store.py` 不在写入边界内）：
   候选接入方式见 K23 文档 §6，**未应用**。
