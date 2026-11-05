# 最终测试回执独立复核

复核日期：2026-11-05。复核人：`ops_security_audit`。本次仅核对现有回执、日志、锁文件及相关测试/守卫源码；没有重跑测试或修改业务、测试夹具、harness。

## 1. 运行完整，结果仍有失败

最终运行 `20261105-212828-13b0299c` 的 [stats](../evidence/final-manager/stats-manager-assets.json) 与 [JUnit](../evidence/final-manager/junit-manager-assets.xml) 一致：**4,600 项测试，4,588 通过、3 失败、0 error、9 跳过**，总耗时 **379.54 秒**；pytest 和验证退出码均为 **1**。独立解析 JUnit 得到相同计数。日志另报 **3,219 个 subtests 通过**，它们不应重复计入 4,600 项。属于完整执行、有未通过项的验收结果，不能称为全绿。

[运行参数](../evidence/final-run-command.json) 为完整 `server/tests` 目录、`manager-assets --require-built-web --keep --verbose`，没有 `-k` 或用例选测；`PYTEST_ADDOPTS='-o faulthandler_timeout=60'` 只启用诊断栈输出，不改变断言、跳过条件或超时终止策略。[锁环境回执](../evidence/final-locked-environment.json) 的 37 项版本，经当前环境与三个实际 hash 锁独立比对，无差异：27 项生产依赖、7 项原 QA 依赖、3 项审计 QA 依赖；包括 pytest **9.1.1**、pytest-asyncio **1.4.0**。最终日志确认 asyncio 为 STRICT 模式。历史 pytest 9.2.0 基线不能改写为此环境。

重新按 `manifest_digest` 算法计算 [view-before](../evidence/final-manager/view-before.json) 和 [view-after](../evidence/final-manager/view-after.json)，两者均为 `f0e74e97e90e9e4387ccbb9ab6603abb13718a039d4cb8d3a4682c22ad960a6b`，没有测试期视图变化。此四份归档回执与原始 `evidence/runs/20261105-212828-13b0299c/` 文件逐字节相同。

三项失败均须保留，不能因与进程环境相关就视为通过；原生 Linux 进程语义仍需门禁验证：

| JUnit 测试 ID（省略共同前缀 `server.tests.`） | 实际失败 |
| --- | --- |
| `test_instance_leases.ClaimAndStateMachineTest::test_pid_alive_helpers` | 当前进程的 `pid_alive` 返回 `False`，预期 `True` 或 `None` |
| `test_smtp_transport.SMTPWorkerTests::test_linux_parent_death_stops_actual_private_smtp_worker` | `child_record()` 返回 `None`，取身份时抛 `TypeError` |
| `test_update_lifecycle.PidAlive::test_zombie_pid_reports_false_posix` | 僵尸进程被报告为存活 |

9 项既有跳过的具体 ID/原因已在 stats 中保留：6 项因 manager 视图缺少相应 QTG、three-platform、nginx 源路径，2 项 Windows 注册表/可执行文件扫描用例，1 项未配置 `WB_TEST_PG_DSN` 的真实 PostgreSQL 用例。不能由该轮结果推定这些平台及集成场景已经通过。

## 2. 隔离记录逐项归属

[隔离回执](../evidence/final-manager/isolation-report.json) 与原隔离目录回执逐字节相同，报告 `net_policy=block`、守卫已安装、环境问题列表为空。网络观察总数为 **525**，普通样本只保留 **300** 条；**10 条阻断样本全部保留，未截断**。525 是套接字观察次数，不是成功连接数。假上游完成 32 次请求，没有假上游错误。

### 10 次非回环网络尝试

下表测试 ID 均以 `server/tests/` 开头，原记录均处于 `(call)` 阶段。8 次为 `socket.getaddrinfo`，另 2 次见表中说明。这些记录证明该批操作被守卫拒绝；**不能将它们写成“10 个专门验证隔离拒绝的测试”**。

| 次数/目标 | 测试 ID（省略 `server/tests/`） | 源码核对后的解释 |
| --- | --- | --- |
| 1：`api.github.com` | `test_settings_contract.py::EndpointMatrixTest::test_viewer_is_allowed_on_read_scope_endpoints` | viewer 只读端点矩阵没有替换更新检查，触发 `socket.create_connection`；同文件 admin 矩阵才替换 `check_updates`。属于夹具遗漏。 |
| 3：`us1-xxx.upstash.io`、`upstash.io`、`my-redis.example.com` | `test_ssrf_guard.py::InternalHostRejectionTest::test_allowed_hosts` | 用例要求公网主机判定放行，但 DNS 未替换；解析异常被 `_reject_internal_host` 捕获后返回允许。通过不证明这些主机实际解析安全。 |
| 1：`us1-abc.upstash.io` | `test_ssrf_guard.py::TestUpstashEndpointTest::test_public_target_is_actually_probed` | HTTP 客户端已替换、前置 DNS 未替换；证明的是模拟 HTTP 调用契约。 |
| 1：`10.255.255.1` | `test_ssrf_guard.py::SelfHostedRedisProbeTest::test_私网地址不再被拦而是如实报连接结果` | 用例刻意探测不可达私网 Redis，断言“无法连接”；`socket.connect` 实际被隔离守卫拒绝。没有证明真实私网 Redis 可达性，也非专门检查 harness 的用例。 |
| 1：`evil.example` | `test_trust_and_upstash_hardening.py::UpstashSavedTokenTest::test_empty_token_with_foreign_url_is_refused_without_request` | 是故意验证“异址不能复用已存 Token”的安全拒绝测试，但断言仅检查模拟 HTTP 调用为空；此前仍发生 DNS 尝试。不能把“无 HTTP/无 Token 外带”扩大为“未尝试任何网络操作”。 |
| 1：`saved.upstash.io` | `test_trust_and_upstash_hardening.py::UpstashSavedTokenTest::test_empty_token_with_saved_url_uses_saved_token` | HTTP 已替换而前置 DNS 未替换，属于夹具遗漏。 |
| 1：`another.upstash.io` | `test_trust_and_upstash_hardening.py::UpstashSavedTokenTest::test_explicit_token_still_allows_any_public_url` | 同上；验证显式 Token 的模拟请求行为。 |
| 1：`a.example` | `test_upstreams.py::ApiKeyNeverReturnedTest::test_no_endpoint_returns_the_plaintext_key` | 响应不泄露密钥用例调用实际 `/probe`，未替换探测 DNS；不能据此宣称真实上游探测成功。 |

### 7 次受保护路径写入尝试

全部是 `os.open` 写入 `/opt/workbuddy-manager/engine/var/audit.jsonl`，因生产资产标记而被 `PermissionError` 拒绝。精确测试 ID 如下，原记录均带 `(call)`：

1. `server/tests/test_instance_runtime.py::TestInstanceRuntime::test_real_application_lifespan_claims_and_releases`
2. `server/tests/test_k36_security_headers.py::ManagerUnauthorizedHeaderTest::test_unauthorized_response_carries_headers`
3. `server/tests/test_max_tokens_e2e.py::SentToUpstreamTest::test_chat_completions_clamps_on_the_wire`
4. `server/tests/test_max_tokens_e2e.py::SentToUpstreamTest::test_chat_completions_normal_value_untouched`
5. `server/tests/test_max_tokens_e2e.py::SentToUpstreamTest::test_max_completion_tokens_clamps_on_the_wire`
6. `server/tests/test_max_tokens_e2e.py::SentToUpstreamTest::test_responses_clamps_on_the_wire`
7. `server/tests/test_metrics_auth.py::MetricsAuthTest::test_prefix_alias_uses_the_same_authority`

这些测试没有断言生产路径写入应被拒绝。harness 重定向 `WB_*`，没有设置 `QTG_DATA_DIR`/`QTG_AUDIT_FILE`；`qtgcore.config.Settings` 的默认数据目录仍是上述生产目录，`Runtime` 构造 `AuditLog` 时触发写守卫。`main` 捕获 QTG 初始化异常后继续启动，所以相关测试通过**不等于 QTG 集成启动成功**。应将其记录为测试夹具与集成覆盖债务，同时保留“该批写操作已被拦截”的事实。

### 6 条隔离根外写入观察

全部为 `/dev/null`：`builtins.open` 一次（t=0.016），`os.open` 五次（t=283.166、311.351、313.870、314.983、322.952）。这些是空设备写入观察，没有生产文件写入成功证据。**该字段的旧记录 schema 没有保存 `test`，无法准确还原 6 个测试 ID**；不能用时间邻近猜测归属。该守卫是 Python 进程内的拦截与记录，不能据此宣称已经证明所有原生子进程的系统级隔离。

本次没有发现上述 10 次外部尝试或 7 次受保护文件写操作被放行的证据。后续应补齐 DNS/更新探针替身、将 QTG 数据与审计路径指向隔离根，并改善 outside 记录归属；本复核遵守冻结要求，未修改这些代码或夹具。

## 3. 前一轮停顿仍无根因

`20261105-211331-5084861e` 曾在约 82% 停住数分钟，由主审中断其拥有的会话；没有完整 JUnit/汇总，也没有当时的调用栈和确切活动用例。该轮属于**未完成**，不能补记为通过。

之后的 [定向诊断](../evidence/locked-taskrun-diagnostic/diagnosis.json) `20261105-212330-161220fa` 在相同严格锁环境下完成 **67/67**，耗时 16.34 秒，30 秒 faulthandler 未触发。最终完整运行中 `test_taskrun.py` **45/45**、`test_audit_taskrun_lifecycle.py` **10/10**、`test_audit_dailyplan_lifecycle.py` **12/12**、相邻 `test_tasklog_collect.py` **17/17** 均通过，未出现 60 秒 faulthandler 栈输出。

因此只能结论为：**停顿未复现，根因尚未查明**。不能归因于 pytest-asyncio，也不能因后一次完整运行完成就声称已修复停顿或排除前序状态污染/间歇性生命周期问题。此残余不确定性与三项原生进程失败、上述隔离夹具债务应一并保留。
