# 运维、安全与生命周期补充审计

基线：`4c6b4cb8cb790698e0753909745dfe8a7c78dd9f`。本轮按主代理追加清单，对 26 个生产模块的 8,346 行基线源代码逐文件全文人工阅读；修复后的相同 26 文件共 8,551 行。覆盖表为 `ops-extra-reviewed.tsv`，每行有阅读范围、SHA-256 与阅读要点；另外 4 个支持文件只登记实际局部范围，没有提升成全文覆盖。这里的人工阅读不等于形式化证明或“零缺陷”保证。

## 新增确认的 P1：每日调度租约未覆盖实际执行生命周期，已修复

位置：`manager/server/services/dailyplan.py` 的 `_lease_heartbeat`、`run_task`、`_run_claimed`；`manager/server/services/taskrun.py` 的后台启动接口。

原行为存在两个独立缺口。续租返回 False 后只退出心跳协程，正在执行的签到或补偿任务继续运行；而 claim/full 的 run_task 只等待 start_async 排队，后台脚本仍在运行时，调度器就写 last_run 并释放租约。这使跨进程唯一执行权和发布互斥所观察的租约不能代表真实的副作用窗口。实例租约本身不是全局 writer election，不能代替该任务租约。

只使用异步假任务的修前反例得到事件序列：

| 场景 | 实际结果 |
| --- | --- |
| 续租返回 False | `lease_renew_returned_false → upstream_side_effect_after_lease_loss → lease_released` |
| claim/full 后台执行 | `background_work_started → lease_released → background_work_completed` |

主代理确认 P1 后，仅解冻相邻的任务生命周期代码。修复内容：

- taskrun 新增 `run_and_wait`，在成功启动返回后不经过其他 await，捕获本次 Task；等待、取消和 join 均使用这个句柄，不调用可能指向新任务的全局 stop。
- dailyplan 的调度路径使用 `wait_for_completion=True`；手动路由默认仍及时返回“已开始”，保持原界面交互。
- claim、renew、mark、release 与计划/发布探测的阻塞工作离开事件循环；取消期间即使 claim 在线程中迟到成功，仍会释放实际捕获的 fence。
- 续租拒绝或异常停止本次任务；另外设置保守的单调到期计时器，在 renewal 线程仍被 SQLite 锁阻塞时也会取消工作。预算在写入前开始，扣除数据库秒级时间戳的 1 秒边际；没有安全剩余预算就不派发任务。
- finally 在重复取消下也会等待捕获的 job 和 heartbeat 的 owned work 全部结束，再执行带 fence 的 last_run 与 release。过期/被接管时，已有 mark_ran_fenced/release 的 fence 检查保护新 owner。
- 新增 `close_scheduler`，未启动时 no-op，停止时等待捕获的 scheduler 与租约清理。主代理负责把 main lifespan 的调用改为 await 这个方法。

补充回归 `test_audit_dailyplan_lifecycle.py` 有 **12 个测试方法**：实际后台生命周期覆盖、续租拒绝/异常、阻塞续租时到期取消、取消期间迟到 claim、重复取消与清理顺序、不能取消后续新任务、已有任务拒绝启动、手动入口仍及时返回、claim 预算耗尽、正常与重复取消的 scheduler close。全部使用假任务/临时存储，没有运行真实供应商脚本。

最终定向隔离回执：`20261105-203925-e29dafbe`，target `manager-assets`，**132/132 通过**，26.04 秒，`view_changed_during_tests=false`。视图 SHA-256 为 `5bd2c31f3e4490dfefdf26aaa5a7e93173a3c2f463406f561b4a19e381c11e8a`；假 provider 调用 0，非回环尝试 0，保护路径拦截 0，5 条记录型越界写均为 `/dev/null`。这组运行未指定 require-built-web，完整带构建门禁由主代理最终全量执行。

第一次错误地选用不含 web 资产的 manager 视图时，86 个测试中 84 通过，2 个 compensation 前端文件读取失败；没有改断言或 skip，改用正确的 manager-assets 视图后完整 132 个测试均通过。

冻结的修改文件：

| 文件 | SHA-256 |
| --- | --- |
| services/dailyplan.py | `66e610f5218716fba0085c2763053d8466a780cd671cdf1ae574a29b33ed6f78` |
| services/taskrun.py | `70c92cb4634518755b2211dd4fdf5ae77351d24eff5adad2b75f7a7c7001b18b` |
| tests/test_audit_dailyplan_lifecycle.py | `9729f32722fc5929ccc84a28f3933e28a97af02abae3ae1b848873cf15ae95c4` |
| tests/test_task_leases.py | `689ee88af8082cbdf3851e49890461ff482de6c7eed6d79fb17a158355921b6d` |

## P1 残余：PG 镜像恢复与当前不可变账本冲突

位置：`services/pgsync.py:732` 的 import_from_pg，尤其第 807 行的逐表 DELETE；`server/ledger.py` 的 `*_no_delete` 触发器。入口 `routers/pgsync.py` 是当前会话管理员可达的 `/api/settings/pg-sync/import`。

恢复先 VACUUM 当前 SQLite 得到 staging，再按 PG 与 staging 的已有列交集搬数据。staging 保留当前不可变账本触发器；随后对镜像中同名表先 DELETE，遇到非空 admin_events 等不可变表就失败。

实验使用生产 import_from_pg 和生产 ledger.SCHEMA，PG transport 替换成临时 SQLite 镜像。只需本地已有一条合成 admin_events：

```text
IntegrityError: admin events are immutable
replace_called = false
original_unchanged = true
```

这证明的是**当前带账本数据的恢复不可用**，不是静默清库或公网未授权破坏。真实导入入口还有 security.audit，不能用没有任何账本行的空库测试代替使用过的实例恢复证明。失败发生在 staging，原库未被换掉；不应把异常包装成“恢复成功”。

主代理决定此轮保留为 P1 残余，不临时发明恢复协议，也没有 DROP 不可变触发器。最小安全产品处理是预检后明确拒绝不支持的在线恢复并提示经过验证的受控恢复流程；真正修复需同时定义全库/账本的一致性、缺表兼容、审计链校验、控制面保留、在途请求与恢复期间写入、失败回滚等合同，再做真实 PostgreSQL 往返及离线恢复演练。

两项容易误判的边界：

- 该入口复制当前 schema 后只搬行，因此“换库后预算 schema 缓存还在、恢复进缺少预算列的旧 schema”没有在这里得到证明，不作为缺陷。
- 恢复是经管理员显式触发的破坏性动作；服务日志已明确写明恢复期间的新写入会被镜像覆盖，不能把所有历史数据回滚都报成未经授权的数据丢失。

## P2 已修复：本机客户端导入扩大凭据权限与临时文件边界

修前位置：`services/keyimport.py:866` 的 `_atomic_write_text`，由 import_zcode 写 provider_config.json。原文件 0600，在 umask 022 下经固定 `.wbimport.tmp` 和 os.replace 后变成 0644。功能默认关闭，路由另有限制；实验仅含 synthetic_secret，没有真实 key，不能描述为匿名远程利用。

主代理后续授权并已完成局部修复：同目录随机独占临时文件、写权限限制在原 owner 模式与 0600 交集、保留必要 uid/gid、拒绝文件/父目录链接与 reparse point、硬链接及特殊文件、通过验证句柄读取、原子替换前复核目标身份和并发修改、文件/目录 fsync、失败临时文件清理。POSIX 替换绑定父目录 fd；Windows 分支避开 dir_fd/fchown 等不支持的接口，不宣称已经验证原生 ACL。

备份也改为随机独占的私有副本。import_zcode 删除失败 copy-back，因为替换前失败已保留原文，copy-back 可能跟随后来换上的链接或覆盖并发修改。rename 后目录 fsync 错误表示耐久性不确定，不能声称此时原文必未改变。

`test_audit_local_import_files.py` 新增 11 个回归，覆盖 0600 保持、0644 收紧、owner、旧固定名称/target/parent/hardlink拒绝、fsync/replace失败保留原文、并发修改、随机私有备份、失败导入不通过链接回写、portable分支无 POSIX-only 参数。该模块已冻结 SHA `fe566c840bea609f48bdce8626b8c93ad1771db4bf2be1383c18e2845826ff1b`。

该模块的进程终止还使用 Windows 镜像名与 POSIX pkill -f，未绑定此次识别出的 executable/PID 身份；这是既有本机选配功能的残余边界，未在没有原生平台验证时改终止语义。

## P2 已修复：通知配置类型与有限超时

notify 的非对象 JSON 现安全返回 disabled，损坏编码/JSON 也不会外抛；只有布尔 true 启用。dispatch 使用单次配置快照。timeout 统一规范为有限 0.1–3 秒 socket 超时，非法/非有限/非正值默认 3 秒，超大值限制为 3 秒。注释明确它不是 DNS/重定向全部阶段的合计硬期限，调用方仍须避免事件循环同步等待。

`test_audit_notify_config.py` 有 6 个新回归；与 keyimport 新旧测试合并的 `20261105-205711-c96ae1c7` 共 106 项，104 passed、0 failed/error、2 原有 Windows 注册表/扫描条件 skip，2.31 秒。view 前后相同 `1df6d5d2839389d460b523566919ad0452f3cf67783cc90bd75eaae338015c5d`，provider/非回环/受保护写均为 0，唯一 outside write 为 `/dev/null`。没有新增 skip，没有执行真实客户端。

notify 冻结 SHA `ab1d8efa4b3e1c01c0d4319be7795d42a01b9291974bc2587e538013148272b2`。新 local-import 测试 SHA `d8c1be8a3bf44757882ad06b977f744667b0eb995a1895e69261054a0554d73b`；notify 测试 SHA `fc64ad99ef6d51405bb55fb66de655512c5c438a656afa32a48b2d0064e593c4`。

## P2/P3 残余与低风险审读结果

| 模块 | 事实与准确影响 |
| --- | --- |
| schemaver.py | Step.row 仍写 v1 字节码 checksum；ensure 只是允许匹配 v1 或 v2，并未把当前记录迁为源码 v2。不能仅凭存在 checksum_v2 就声称 Python 解释器升级已自动兼容。本轮仅核对代码，不声称执行过另一 Python 版本的真实升级。迁移认证还依赖已存 v8 指纹，不能简单全量改 checksum 列。 |
| keyexport.py | TOML `_q` 只处理反斜杠和双引号；换行/控制字符没有编码完整性保证。属于配置输入输出有效性问题，未把它升级为已经证明的命令执行。 |
| tasklog.py | Bandit 的 SHA1 命中用于任务事件去重，未用于密码、认证或发布签名，不能作为高危密码学漏洞。时间戳实际截成秒，与注释“纳秒级唯一性”不符，完全相同的同秒日志会合并；应明确事件语义。 |
| accountlog.py / tasklog.py | 阻塞日志读取使用 asyncio.to_thread，但旧 stop_collector 只 cancel 而没有 owned-thread join，停止后仍可能有一段在途采集；本轮没有在真实容器/进程上验证其最坏清理时长。原生无日期日志的时间恢复仍是旧相关性推断，不能与明确 request identity 证据等同。 |
| steward_ai.py | 请求与工具结果有超时、体积和调用次数限制；只读工具没有暴露写操作。部分策略/密钥读取仍同步发生在 async 路径，数据库等待可能影响事件循环；没有生产 p99 数据，未编造性能幅度。 |
| steward_* / routers/steward.py | 逐项审阅了 secret 文件归属/模式、收件人 CAS、队列与冷却、unknown SMTP 不自动重放、只读模型工具、授权再检查、输入上限；未确认新的 P0/P1。原生父进程死亡仍受当前 proc 环境限制。 |
| migrations/schema_compat/legacymig | 审阅全部迁移步骤、版本/证书校验、默认 deferred 的 contract、旧 key 的 canonical 映射与事务/幂等。没有运行真实迁移或把文档内历史“已知缺口”直接当成当前网关漏洞；执行期旧 key 策略由网关审计并行核查。 |
| system/settings/pgsync routes | 检查完整路由依赖、可写配置、敏感字段投影、用户变更锁和会话撤销、升级拒绝门、固定上传路径及本机身份签发。公开路径库存须与继承 Depends/中间件/处理器鉴权一起解释，不能只凭路由装饰器标成 public。 |

## 实验证据和复跑边界

可携带规范脚本为 `docs/audits/2026-11-05/reproduce_extra_security.py`，可传 `--output <JSON>`；标准库即可执行，前提是本 checkout 存在固定 Git 对象 `4c6b4cb8cb790698e0753909745dfe8a7c78dd9f`。它不导入生产 app、不连接生产 DB、不调用真实 provider、不发网络请求，所有实验数据都在新建 TemporaryDirectory。

dailyplan/keyimport/notify 三个修前反例直接从该 Git 对象读取，SHA 分别是 `30ada917ca159e82941a627eac524e76eaf1960bc56f44f4dec5981b2085fedb`、`4cecc8411cec2509d62d273aba4627fb1638dbbfa7aef65de6ba05442882d98a`、`34374bf90a39708a00056c2ed790ea6db1449174c9edfac4d3fea866e364bb00`。PG/ledger 残余反例仍读取当前工作树。新 JSON `evidence/ops-extra-portable-experiments.json` 逐项标明 pre_fix_git_object/current_worktree，以及修后回归文件 SHA 和 executed_by_this_script=false；不把原版反例说成当前补丁行为。

本地旧 `ops-extra-experiments.py` 已成为规范脚本的兼容 launcher，旧 JSON 保留为历史证据。修后行为由 12 个 dailyplan 回归及 17 个本机导入/notify 回归和各自定向隔离运行证明。不要仅执行反例脚本就声称修后门禁通过。

所有业务源码已冻结，未提交、未推送、未部署。后续最终全量回执由主代理统一汇总。
