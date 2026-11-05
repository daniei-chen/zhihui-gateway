# QTG 生产源码补充审阅（只读）

日期：2026-11-05。基线：4c6b4cb8cb790698e0753909745dfe8a7c78dd9f；审阅对象为共享 audit/strict-review-20261105 工作树，含团队当前已冻结修复。

## 结论与覆盖

已人工全文阅读 `manager/server/qtgcore/` 全部 38 个 Python 文件，共 12,965 物理行；路径、范围、SHA-256 在 `frontend-extra-reviewed.tsv`。这包括 root 已读的 base/cache/runtime 再核读。另定向读取 main.py 145–190、400–570 及 qtg_panel.py 65–205，核对真实可达性与输出投影；不宣称这两个文件由本代理全文审完。

**没有确认新的 P0/P1，也没有确认已冻结前端/accounts/Tencent 修复引入的 P0/P1 回归。** 本补审没有修改业务代码，没有启动广泛测试，没有联系真实 provider。以下是具体残余或条件风险，不等于已证实可利用的高危漏洞，也不等于全部应当在本轮改动。

## 真实入口与被排除的假阳性

- main.py 的 `_qtg_platform_route` 只在 Qoder/Trae 平台改写指定 `/api/*` 路径；直接外部访问 `/__qtg` 被 404 拒绝。改写前解析 Manager 身份，所有非 GET/HEAD/OPTIONS 方法要求 admin 且非服务令牌。因此 compat 写处理器没有自己的 Depends 不构成已证明的匿名/只读用户越权。
- `qtgcore/routers/proxy.py` 目前只保留 helper，没有数据面端点注册。其 `_client_ip` 信任转发头、Store 的进程内限流等旧逻辑不能直接算成当前 Manager 数据面漏洞。
- Trae2 `_api_keys()` 确实读取原始 key，但当前 `/api/qtg/keys` 经 `service.keys_payload` 显式投影为 masked，未发现该路径下发原始 key；`qtg_panel.settings` 已对 account_admin 使用投影。不能只凭内部存在 key 就报泄露。
- QTG 旧 `CredentialStore.secret/revoke_sessions` 有下述缺陷，但 Manager 入口采用自身会话鉴权，旧 QTG login/logout 路由已移除。不能把旧路径风险当成已证明的当前会话伪造。

## 可达 P2 与需保留的边界

### QTG-EX-01：整合读取仍有串行放大和非当前平台故障耦合

证据：`service.py:354` 的 `build_aggregate` 并发 Qoder/Trae 调用后，395–398 的四个 WorkBuddy 调用继续逐项 await；405 又在 Qoder 与 Trae 都不健康时拒绝整份聚合，即使 WB 自身健康。`routers/compat_fixes.py:559` 的 playground model list 使用 `_load_models(rt)`，按顺序读取三平台，再只返回所选平台。

影响：冷缓存时总延迟包含多组上游时延；一个无关平台慢/失败会拖慢聚合页面。已引入的 `build_pool_snapshot` 缩小账号页面范围，但没有覆盖所有 aggregate/model 使用方。属于可测量优化候选；本轮没有给未经测量的生产 TTFT、吞吐量改善承诺。

### QTG-EX-02：同步磁盘/SQLite/全文件扫描仍在 async 读路径

证据：`service.systemd_timers` 使用最长 8 秒的 subprocess.run；qtg_panel.py overview/tasks/ops 直接调用。`audit.py:101` tail 为取末 N 条先读入全部历史；verify/count 也扫全文件。Trae2 `logs`（391）读取全部最近两份日志再截取末尾。账号列表 `_map_account` 每行 `_note_of` 重读整份 notes；`compat_fixes._ttl_seconds`（571）每账号再打开 JSON。WorkBuddy/Trae2 多个 async adapter 直接调用同步 sqlite 或目录扫描。

影响：大账号池、大日志或慢磁盘会阻塞同一事件循环，影响其他管理请求以及同进程流式响应。需要按实际热点将阻塞读移入线程、notes 一次快照、末尾读取或 SQL 分页；不能在无压测条件下断言具体收益。

### QTG-EX-03：兼容刷新接口把明确业务失败计为成功

证据：`routers/compat.py:935` refresh-tokens、1057 refresh-plan/run 只检查 adapter 是否抛异常，忽略返回 `{ok:false}`。Trae2/旧 adapter 可能以该结构报告刷新拒绝或不需要刷新；单账号 refresh 已用 `_res_ok`，批量分支没有对齐。

影响：管理员界面可能展示“已刷新”或增加 renewed，而上游并未确认完成。Qoder/Trae 兼容入口可达；非 Trae 一律选 qoder 的额外分支在当前改写只接受 Qoder/Trae 的条件下不能据此声称 WB 会被误刷。应记录业务状态与副作用证据，不用 HTTP/异常缺失替代成功。

### QTG-EX-04：Trae/旧 BFF 用量仍存在时间窗口、realm 和未知值伪零

证据：`compat_stats._payload` 始终基于 build_aggregate 的 14 天快照；fallback summary 的 total_*（164附近）合计这 14 天，而不是全表；请求 30/90 天时 `_merge_daily` 把缺失旧日期补 0。`_realm_available` 只识别 cn/intl，公开前端 realm 为 cn/global，global 在旧分支直接放行，不能证明该版本真实用量。部分 SQLite 不可读转换为 []/{}，下游仍标 stats_health.ok=true。Qoder2 的 scoped evidence 已显式校验，不能把该残余误归于新 scoped 分支。

另：WorkBuddy `_credits_flow_from_sql`（660）对支出按 realm 过滤，但 task_logs 的收入 SQL（682）未按 realm 限定；`_build_usage`（922）按 day 合并多个 realm 却保留首行 realm 标签。当前 workbuddy `/api/*` 通常走 Manager 原生路由；QTG `/api/qtg/*` 聚合仍会使用 adapter，必须在使用该输出时注明口径。

### QTG-EX-05：Qoder 清冷却直接覆盖整个凭据 JSON

证据：`compat_accounts_ext.py:107` 通过 `_account_path` 的字符白名单与 resolve 包含性保护路径；随后 read JSON→固定 `<name>.tmp`→replace，未与引擎 token 更新共用锁，也未保留旧 mode。main.py 管理员写闸门之后此兼容路径可达。

条件影响：如果该目录可写且真实引擎也同时更新同一凭据文件，read-modify-write 会丢失并发 token 更新；固定临时文件还存在多进程互相替换/冲突，替换后的权限取决于 umask。实际部署目录是否只读、引擎是否使用该文件以及共享锁契约未现场验证，因此没有升级为已确认 P1。不要在不明 cross-UID 读取需求时盲改 0600。

### QTG-EX-06：兼容 playground 与独立 Manager client 的资源/代理边界

证据：`compat.py:658` chat 在创建 AsyncClient 后，`client.send` 失败路径无 finally；HTTP 错误 `resp.aread()` 无显式上限；成功流仅有 httpx 单操作 timeout，不含总流时限/字节上限。`manager_api.py:213` 每次新建 AsyncClient，默认 trust_env=true；Trae2 models 及 proxy catalog 也单独建 client。主 BaseAdapter 已显式 trust_env=false，不应混为一谈。

影响：管理员并发探测/失败路径可能增加连接与内存占用；环境代理可改变带服务凭据请求的传输目标，属于受信部署配置边界。没有把受管理员授权的 provider 流当成匿名 DoS，亦未测试真实 provider。

## 旧/条件逻辑，不算当前已证实 P1

- `security.py:170` 旧 session secret 首次创建用 write_text 后 chmod，非独占/非原子；失败被吞，多个进程启动可能临时使用不同 secret；285 revoke_sessions 写失败仍返回 epoch。当前 Manager 认证不依赖这些旧签发路由。若恢复独立 QTG 身份服务，须先做安全文件创建与可观察失败。
- `audit.py` 哈希链使用线程锁和永久 `_LAST_HASH[path]` 缓存，未使用跨进程锁。多个 worker 追加或外部轮换时会产生错误 prev；tail 的无界读取影响已列出。这不影响 Manager 独立数据库审计链的实现。
- `model_map.save`、compat `_write_plans`、account_notes 使用固定临时路径且无进程间 read-modify-write 事务；并发配置写可能丢更新。只读值不含自动评估任意代码，未发现命令注入。
- `capabilities.py` 与 `routeaudit.REGISTRATION_ORDER` 仍引用已移除的独立路由与旧单管理员能力，不能作为当前 Manager 的真实可用性或授权证据。真实路由应以 main 的实际注册/改写和运行时展开清单为准。

## 冻结修复的最终独立复核

此前已按 root 要求独立复查 Tencent probe 的 identity 编码拒绝、HTTP 错误有界读取、外层总超时、CancelledError 传播与流关闭；以及三条 accounts final save 的 owner-generation→DB→凭据 mutation 锁顺序与 resolved group binding。未发现具体新引入的 P0/P1。此结论不表示全仓不存在其他缺陷；手工源码覆盖与测试通过分别记录，不混成绝对安全证明。
