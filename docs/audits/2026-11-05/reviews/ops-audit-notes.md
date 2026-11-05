# 运维、供应链、恢复与后台生命周期审计

审计基线：`daniei-chen/zhihui-gateway`，完整提交 `4c6b4cb8cb790698e0753909745dfe8a7c78dd9f`。审计日期：2026-11-05。工作目录：`/workspace/scratch/913dc6e1a7c0/zhihui-gateway`。

## 结论与边界

本分工确认并修复了 **14 组问题**，覆盖实例与任务租约、账本准入与落盘证据、两条签名工具链、备份清理与凭据轮换、恢复演练证据、后台子进程生命周期、每日调度所有权、本机凭据导入、通知配置和发布文档闭包。新增 **100 个 `test_` 回归方法**，其中 19 个是根目录工具测试。选用正确视图的聚焦隔离测试全部通过；最终全量结果由主审计报告合并，不用这些相互重叠的聚焦集合冒充完整全量通过。

最值得优先处理的是：数据库锁等待阻塞服务事件循环；已过期任务仍可续租、与发布互斥冲突；无法读取账本积压被当作零；恢复演练已发现归档摘要不符却仍输出 `verified`；清理工具跟随目录链接删除备份根以外的文件；凭据清理写出的“备份”不是原始数据且默认可能是 0644。

公开源码不包含完整私有 engine、已安装的独立 root-owned 发布控制器、生产信任锚权限或真实恢复密钥。本轮没有读取生产密钥，没有调用真实模型供应商，没有修改生产服务或做部署、提交、推送。仓库内实验工具和历史运维脚本的缺陷，不直接等同于当前公网入口可利用漏洞。

优先级使用 P1/P2 表示修复顺序，不是 CVSS 分数。针对离线管理员工具的问题，触发条件包括操作者选择的候选目录、被替换的备份文件或错误的运行参数；没有把需要本机高权限的场景写成匿名远程代码执行。

## 实际覆盖

逐文件证据见 `ops-reviewed-files.txt` 和 `ops-coverage.json`。覆盖记录包含路径、审阅层级、行数、内容 SHA-256、执行过的检查与局部阅读范围。

| 范围 | 结果 | 准确含义 |
| --- | ---: | --- |
| 本分工登记文件 | 368 | 包含源码、脚本、测试、文档与大体积 fixtures，不是 368 个生产模块 |
| 全文人工阅读 `full` | 77 | 实际读过整个文件；不保证文件没有未发现的问题 |
| 局部人工阅读 `partial` | 13 | 清单注明具体函数或调用路径；没有假称整文件逐行审完 |
| 工具扫描 `automated` | 278 | 文本风险模式与适用的语法检查，不是人工语义证明 |
| Python 编译检查 | 207 | `compile`，不导入、不执行脚本 |
| Shell 语法检查 | 12 | `bash -n`，没有执行 systemctl/安装/回滚 |
| JavaScript 工具语法检查 | 36 | `node --check`，没有运行浏览器或生产操作 |
| Windows 脚本 | 23 | 文本/行尾扫描；本环境没有原生 PowerShell/Windows 验证 |

登记文件共约 90,000 行，其中包含历史 fixtures 和文档。这不能改写成“9 万行均已人工逐行审计”。上述语法检查没有解析失败；`git diff --check` 通过。风险模式命中只是审阅线索，不按命中数统计漏洞。

全文人工阅读的关键后端模块包括 `archival.py`、`archiveworker.py`、`backup_application.py`、`backupstatus.py`、`offsitebackup.py`、`portalrecovery.py`、`retention.py`、`instancelease.py`、`instance_runtime.py`、`tasklease.py`、`ledgerreplay.py`、`operational.py`、`packagecheck.py`、`release_identity.py`、`services/updater.py`、`services/taskrun.py`。`taskregistry.py` 不存在；实际任务执行实现位于 `services/taskrun.py`，已按主代理追加授权审阅修复。

主代理后来追加的 **26 个生产模块已全部全文人工阅读**，单列在 `ops-extra-reviewed.tsv`：基线 8,346 行，修后 8,551 行；另 4 个支持文件只登记实际局部阅读范围。补充发现、PG 恢复 P1 残余与边界见 `ops-extra-findings.md`，不把独立清单中的重叠文件相加冒充不同文件数。原范围登记文件现为 91,003 行。

补充的外置授权/回调工具 6 文件 **1,360 行全部全文阅读**，另 5 个上下文文档/配置也全文读取，逐文件哈希在 `ops-tools-extra-reviewed.tsv`。这 11 文件已从原工具扫描集合升级为实际 full，不重复增加不同文件总数。没有运行浏览器、SSH、真实授权或远端部署；实验入口和运行时残余见 `ops-tools-extra-notes.md`。

根 CI 追加审查单列在 `ops-ci-reviewed.tsv`：新 workflow 与 QA hash 锁，以及三个支持锁文件，共 5 个文件均全文阅读。它们的实际 CI 行为、官方来源与静态验证见 `docs/audits/2026-11-05/CI.md`；不将重复支持文件和原范围简单相加。

## 已修复发现

### OPS-01 / P1：心跳数据库锁等待冻结事件循环；离开事件循环后还必须及时关闭准入

原 `instance_runtime` 在 async 心跳循环内直接调用同步 `instancelease.heartbeat`。其数据库调用会争用进程内锁及 SQLite 锁；仅将函数写在协程中不会使这些等待异步化。这个路径一旦等待，会拖住同一事件循环上的网关和管理请求。

修复使用既有的 `joined_thread` 执行同步心跳。取消时先等待自己拥有的线程工作结束，再释放实例租约，避免已释放后出现迟到的续租写入。增加独立的单调时钟 deadline watchdog：即使数据库线程还在等待，实例过期也按时进入内存中的关闭准入状态。

`drain.begin` 新增可选 `persist=True`，默认保持原行为；租约失效路径用 `persist=False`，先关闭内存准入而不再等待同一个数据库锁。它不允许失效实例通过再次写租约来恢复自己。此小段接口变更已与网关审计代理协调。

新增回归覆盖：慢心跳期间其它协程持续运行；心跳线程仍阻塞时按 deadline 关准入；取消及重复取消关闭流程时续租完成先于 release，且 release 后没有迟到续租。相关租约/实例组 49/49 通过。

涉及：`manager/server/instance_runtime.py`、`manager/server/drain.py`、`test_instance_runtime.py`。

### OPS-02 / P1：过期任务可以复活；锁等待前采样时间可误判租约有效

原 `tasklease.renew` 仅匹配 owner/fence，没有要求旧租约仍未过期。旧任务的过期租约可能在发布器已接管互斥区后被续活。部分时间戳在拿到数据库锁之前采样，因此锁等待也可能消耗实际剩余租期却未反映在判断中。

修复在事务锁取得后采样 claim/renew/release 时间；renew 强制 `lease_until > now`，并检查发布状态。`still_holding` 在查询完成后取时间。读取任务租约时只对确实不存在的遗留表保持兼容，其它数据库错误不再静默返回“没有租约”。现有但损坏的发布状态不再被解释为“没有发布”。

新增六条回归包括过期 owner 在有/无新 publisher 时均不能复活、模拟等待锁期间时钟推进、损坏 publish 状态拒绝执行、缺表兼容与其它 SQL 错误区分。

涉及：`manager/server/tasklease.py`、`test_task_leases.py`。

### OPS-03 / P1：账本健康的未知状态被放行；每次准入扫描历史处置日志；fsync 失败仍可报告落盘成功

原 backlog/dead-letter 统计会将部分目录读取错误转成 0，或返回 -1 后准入只检查 `> 0` 而继续放行。无法核实待重放证据是否积压，不能作为新的计费推理请求继续准入的依据。

修复用显式 `os.scandir`/不跟随链接的文件检查统计回执；读取失败、非目录/链接等返回未知值。准入看到负计数会以既有稳定错误码拒绝，而不是当成零积压。

原 `admission_block → channel_state → dead_dispositions` 每次读取不断增长的 `dispositions.jsonl`，即使准入并不使用该历史统计。修复在准入调用中关闭历史统计，诊断页的默认完整统计保持可用。这是把与请求准入无关的 O(历史长度) 文件扫描移出热路径，不是声称某个生产 p99 数字已经提升。

POSIX 目录 fsync 失败现在向调用方传播。spill 不能以成功返回声称已持久化；保留不确定状态下已出现的回执供重试/核对。死信处置也同步被写入的 processed 目录及其父目录。

新增回归覆盖目录不可读、负值健康状态、准入不调用历史日志扫描、fsync EIO 不得产生成功证据。账本/docs/drain 组合 76/76 通过。

涉及：`manager/server/ledgerreplay.py`、`test_ledger_channel_health.py`。

### OPS-04 / P1（实验工具使用场景）：bundle 自带公钥被当作可信发布者，且验证闭包/权限/路径不严格

`docs/release-signing.md` 已明确 `tools/release_sign.py` 是另一套独立离线签名实验工具，不能自动成为生产信任锚。审计没有把该工具漏洞误写成已安装生产控制器的漏洞。

但实验工具原本允许由同一个 bundle 自带的公钥决定信任。攻击者替换文件、公钥、BOM 和签名后仍能输出 `VERIFIED`。另外，实际 0777 模式被归一为已签署的 0755、`__pycache__` 内未登记文件被忽略、已签名的 `../outside.py` 可以逃出运行树。

修复要求明确提供独立配置、位于发布目录之外的 `--trusted-key`，核对 Ed25519 类型、key id、签名元数据与 BOM 字节。拒绝非规范路径、父目录、目录或文件链接、硬链接、重复 JSON 键、重复/冲突路径、错误计数、无效模式和超界元数据。校验完整运行树，不再跳过隐藏字节码。验证比较真实权限，不在验证时静默归一化。

打包阶段只在明确的安全常见模式集合中调整 staged 副本，并先确保源/目标不重叠、私钥不在被打包范围。keygen 私钥从创建起即 0600，独占创建并落盘。输出增加 `production_approval: false`，避免实验验签回执被误用为部署批准。

全仓查找没有外部调用该 verify CLI 的现有脚本；使用说明已更新为必需的外部 trust 参数。本工具 15 个对抗性测试通过，包含合法 round-trip。

涉及：`tools/release_sign.py`、`tests/test_release_sign_strict.py`。

### OPS-05 / P1：管理员 Ed25519 签名器的 dry-run 会写数据，失败会破坏旧签名

原 `--add ... --dry-run` 在判断 dry-run 之前已经改写 manifest；`--sign-only --dry-run` 根本不走 dry-run 分支，会真正签名。原签名函数先 unlink 旧签名，再调用 OpenSSL；失败丢失旧证据。即使 OpenSSL 签名成功，私钥与受信公钥不匹配时也应先拒绝，再考虑替换旧签名。

修复统一在内存里校验、准备清单，dry-run 不落盘、不要求私钥、不签名，且执行与真实 rebuild 相同的路径/权限检查。无效 `--add` 不能留下部分新增条目。拒绝非规范、越界及中间目录链接，清单不允许把自身或签名包含进去。真实运行先确认显式私钥存在。

签名先写独占创建的同目录临时文件；main 将受信公钥传入 `sign_file`，在原子替换前校验临时签名。签名失败、超时或 wrong-key 校验失败均保留原签名。单独函数的可选 trust 参数保留历史调用兼容；正常 main 路径始终启用校验。

新增十个测试，包含 real OpenSSL 合成密钥 round-trip、两种 dry-run 字节不变、错误 key 后旧签名仍可验。这里保护的是独立签名文件；manifest 与 signature 两个文件并不是跨文件原子事务，生产仍须由可信发布器在隔离候选中准备再发布。

涉及：`manager/deploy/sign-release-ed25519.py`、`test_audit_release_signer.py`。

### OPS-06 / P1：恢复清理可跟随快照目录链接删除根外明文，分片名称也未限制

原明文清理只检查字符串父目录关系，`snapshot_dirs()` 会跟随目录链接。把 `snapshots/<时间戳>` 指向备份根外一个含合法密文元数据的目录，清理会删除该外部目录里的 `snapshot.tar.gz`。合成安全实验已重现原版删除 sentinel，修复版保留。

修复拒绝快照目录及祖先链接，用 `O_DIRECTORY|O_NOFOLLOW` 打开目录。读取 metadata、校验密文分片和 unlink 明文均绑定同一个目录 fd，防止校验后替换父目录使删除跳到别处。分片必须是本目录唯一的 `.age` 文件，拒绝绝对/父目录路径、符号链接、硬链接、非普通文件；如 metadata 记录分片/总大小和各分片 digest，逐项核对。非对象 metadata 保守保留，不发生 AttributeError 后的误操作。

已有但无效/无法读取的 `latest.json` 阻止整份快照删除；没有 marker 的旧离线备份集合保留原兼容行为。手动 `--apply` 不再自动填写确认 token，明确的 `--auto` 模式契约保留。这是修复工具本身参数语义，并不是向用户增加审计确认步骤。

九条新增回归覆盖目录替换竞态、链接/越界/重复分片、元数据类型和内容、确认 token、损坏 latest。新实现的 no-follow/dir-fd 删除防护针对原工具的 Linux 运维用途；没有声称经过 Windows 原生验证。

涉及：`manager/deploy/recovery_retention.py`、`test_recovery_retention.py`。

### OPS-07 / P1：凭据轮换“备份”保存新配置，且可能公开可读

原 `purge_credentials.py` 修改 cfg 后才把同一 cfg 写到 `.bak`，所以备份不是旧内容，回滚承诺失真。在 umask 022 下，新 `.bak` 是 0644；若父目录可遍历，其他本机用户可读其中 secret/密码哈希。原凭据也原地截断写入，失败可能损坏状态。

修复先读取并保存原始字节；随机独占创建 0600 备份，避免跟随/覆盖可预测 `.bak`；以 0600 临时文件写新凭据、fsync，再原子替换原路径，保留原 uid/gid。拒绝输入 symlink 与畸形 users 数组。写入失败保留原文件和私有备份。

四条回归通过。合成实验结果：原版 backup 不等于 original 且 0644；修复版逐字节等于原始数据，backup 和新凭据均 0600。路径搜索中发现的 `src/three-platform/deploy/purge_credentials.py` 仅存在于历史 fixture manifest，当前树没有该镜像文件，没有编造同步操作。

涉及：`manager/deploy/purge_credentials.py`、`test_audit_credential_purge.py`。

### OPS-08 / P1：恢复演练摘要不符仍输出 verified；抽查被表述成完整恢复

原 `restore_drill.py` 算出 `archive_sha256` 不符后继续执行，最终固定 `result='verified'`、返回 0。它最多只抽查三个 SQLite 文件，`--skip-sqlite` 时也宣称“完整重建并逐项校验”。这会把已知失败或未经核实的灾备能力变成成功证据。

修复归档摘要不符返回 3，并在写回 sidecar 之前停止。缺失摘要显式记为未检查（`ok: null`），不伪装成匹配。正常抽查输出 `result='sampled'`、`complete_restore_verified=false`、采样上限与 SQLite 跳过状态。cat/age 任一失败都拒绝。sidecar 通过 0600 临时文件原子替换，不能经现有文件 symlink 覆盖外部内容；实际写回 source 时 `read_only_source=false`。

四条回归用合成归档和模拟 age 数据流完成，没有读取真实恢复私钥、运行真实 age 解密或恢复生产数据。基线反例同样重现 `archive_check=false` 但 `exit=0,result=verified`；修复为 `exit=3,result=archive_mismatch`。

涉及：`manager/deploy/restore_drill.py`、`test_audit_restore_evidence.py`。

### OPS-09 / P2：候选运行完整性工具忽略真实 managed manifest 的摘要字段

`ops/integrity/verify-runtime.py` 只读取 `sha256`，而仓库 managed-release 条目使用 `candidate_sha256`。读成 None 后差异计算又跳过空值，因此已修改的运行字节可以报告 `changed=[]`。

修复支持两种明确的 digest 字段，校验十六进制长度、拒绝冲突摘要与重复/不安全路径。缺少有效摘要不能作为比较基线。四条工具测试通过。

该工具是候选报告生成器，未执行签名验证。README 已改为准确描述：退出 0 只代表报告生成，不能仅据此批准发布；必须检查 missing/extra/changed，并由独立的可信验签/发布合同决定来源。没有把修复 diff 计算夸大成实现了完整发布认证。

涉及：`ops/integrity/verify-runtime.py`、`ops/integrity/README.md`、`tests/test_runtime_integrity.py`。

### OPS-10 / P2：发布包遗漏四份 v0.0.7 文档，嵌套路径会丢失

基线全量中的 docs-packaging 失败是真实组装缺陷：`UPSTREAM-ADOPTION-v007.md`、`V007-OPERATIONS.md`、`releases/v0.0.7.md`、`releases/v0.0.7-operations.md` 已存在但未进入 whitelist。旧复制逻辑也没有保证 `releases/` 相对目录。

修复补齐四项、逐文件创建目标父目录并按相对路径复制；反向闭包检查用递归 `find -print0` 核对，而不是只看顶层目录。新增回归把真实 workflow 中的 docs-pack shell 块提取到临时目录执行，并比对完整 Markdown 路径集合及文件字节。

基线实际 workflow 位于 **`manager/.github/workflows/`**，当时仓库根没有 `.github/workflows/`；继承脚本不会自动成为根仓库门禁。主审后续授权新增根 `.github/workflows/audit-checks.yml` 审查候选，按锁运行前后端完整检查并上传原始回执，已通过静态/依赖核验，尚未运行云 CI。详见仓库审计目录 CI.md；没有发布任何新包。

涉及：`manager/.github/workflows/release.yml`、`test_docs_packaging.py`。

### OPS-11 / P1：持续输出绕过后台任务总期限；取消释放状态早于子进程退出

原 `_run` 只在 stdout `readline` 超时分支检查总时长，持续输出就永远不进入该检查。stdout 关闭后无期限 `proc.wait` 也能绕过总上限。异常或取消的 finally 只 kill，不 wait/reap，就释放 running；任务准备发生在 try 之外，失败可留下 running 状态。

修复用 `asyncio.timeout(MAX_TOTAL_SECONDS)` 包住输出泵和等待退出；空闲限制使用 monotonic 时钟独立检查。准备、可用性探测、创建子进程都有明确 owned 生命周期：线程/创建任务取消后会 join，取得晚到的进程句柄再清理。失败和取消路径先 kill 直系 owned child，再 wait/reap、写历史，最后释放 running。历史和 schedule 数据库操作从事件循环移到会 join 的线程。

复核又发现单纯排空到 EOF 可能受继承 stdout 的后代阻塞。因此最终实现给排空 2 秒上限；未 EOF 则关闭本任务自己的 stdout transport，仍等待直系 child reap。没有按端口或任意 PID 结束别的进程。StreamReader 无公开 close，内部 transport 访问集中到一个兼容 selector/proactor 的辅助函数；这是需随 Python 版本回归的实现接点。

stop 等待自己捕获的 task，重复取消也不放弃 owned 清理；旧 stop 返回时不覆盖后来新启动任务的状态。新增十条无 host/proc 依赖的假子进程/线程回归，涵盖持续输出、EOF 后不退出、超长行异常、创建期间取消、重复取消、未 EOF pipe、准备失败与阻塞 schedule 读。

最终 taskrun/concurrency 组 62/62 通过（增加已退出 child 仍有继承 pipe 的回归）。该测试证明直系 subprocess 的生命周期合同；没有声称已实现 POSIX 进程组和 Windows Job Object 的完整后代树隔离。

涉及：`manager/server/services/taskrun.py`、`test_audit_taskrun_lifecycle.py`。

### OPS-12 / P1：每日调度提前释放后台执行租约，失去租约后仍可继续任务

补充逐文件审计确认：dailyplan 的 claim/full 只等待 start_async 排队就写 last_run 并 release，实际子进程尚未完成；heartbeat renew=False 也只停心跳，不停正在执行的签到/补偿任务。主代理授权仅修相邻生命周期，现由 taskrun.run_and_wait 捕获本次 Task，调度路径保留租约直到本次任务清理完毕。

claim/renew/mark/release 与计划读取离开事件循环；续租拒绝/异常或保守单调预算到期都会取消本次任务；重复取消仍 join owned job 和 heartbeat，再带 fence 写戳/释放。取消期间迟到的成功 claim 也会被释放。新增 close_scheduler 供 main lifespan await，手动 run_task 默认仍及时返回“已开始”。没有使用全局 stop 误停后续任务。

新增 12 个生命周期回归、最终 132/132 定向通过；修前两个异步反例绑定固定 baseline 原文，详见 `ops-extra-experiments.json` 与 `ops-extra-findings.md`。

### OPS-13 / P2：本机客户端导入扩大凭据权限，固定临时文件与失败 copy-back 有链接边界缺口

原 import_zcode 的 `_atomic_write_text` 使用固定 `.wbimport.tmp`，在 umask 022 下把原 0600 配置替换成 0644。该文件可能包含供应商密钥；本机功能默认关闭、另有路由权限，实际跨用户可读还取决于父目录权限，不把它写成匿名远程漏洞。

修复拒绝配置及父目录的符号链接/Windows reparse point、硬链接、目录和其他特殊文件；通过验证后的只读句柄读取。备份改为同目录随机独占创建、0600、保留必要 uid/gid，写完 flush/fsync。配置替换使用随机独占临时文件，模式限制为原 owner 权限与 0600 的交集，不因 umask 扩大权限；保留必要 uid/gid，写完 flush/fsync，检查文件/父目录身份及并发修改，再原子替换。

POSIX 创建、替换与清理绑定已打开的父目录 fd，最后 fsync 目录；Windows 分支不使用不支持的 dir_fd/fchown。失败清理本次临时文件。删除 import_zcode 的旧 copy-back，因为替换前失败已经保留原文件，copy-back 反而可能跟随被换成链接的目标或覆盖后来编辑。目录 fsync 失败若发生在 rename 之后，只能报告耐久性不确定，不能声称原路径一定仍是旧内容。

新增 11 个临时文件回归，证明 0600 不变 0644、旧 0644 收紧、owner 保留、旧固定临时/备份链接不被写入、目标/祖先链接和硬链接拒绝、写入/fsync/replace 失败保留原文件、并发变更拒绝、失败导入不 copy-back 通过链接。portable OS 代理证明不调用 POSIX 专属参数；没有将这个代理测试当作 Windows ACL 的原生验证，也没有修改进程终止语义。

涉及：`manager/server/services/keyimport.py`、`test_audit_local_import_files.py`。

### OPS-14 / P2：通知配置类型错误可外抛，超时参数没有有限上限

原 `_settings` 把任意 JSON 返回给 configured_mode，`[]` 会导致 AttributeError；timeout_sec 可为任意大、非数或非有限值，违背短超时注释。operational.transition 外层有异常隔离，所以不声称配置错误必然使整个主服务崩溃。

修复只接受对象配置，损坏编码/JSON 返回安全默认；只有 JSON true 可启用投递，其他 enabled 形状不启用。dispatch 读取一个一致配置快照。HTTP socket 超时统一规范到 0.1–3 秒，非法/非有限/非正值使用 3 秒，超大值截到 3 秒。注释明确这不是 DNS、重定向等所有阶段总和的硬 deadline，不能据此宣称已消除整个同步网络路径的事件循环等待。

新增 6 个回归覆盖非对象 JSON、损坏编码、非布尔 enabled、非法/无穷/巨大超时、有效短超时、单次读取配置。与 keyimport 原有测试合并的隔离组为 106 项：104 通过、0 失败、2 原有 Windows 专属 skip；没有新增 skip。

涉及：`manager/server/notify.py`、`test_audit_notify_config.py`。

## 可复现安全实验

脚本：`/workspace/scratch/913dc6e1a7c0/ops-security-experiments.py`。原始 JSON：`ops-security-experiments.json`。脚本从固定 baseline commit 读取原版，再与工作区补丁比较；每个场景只用新建临时目录、合成数据和新生成的测试 Ed25519 key。JSON 绑定被测补丁输入 SHA-256。

| 场景 | 原版结果 | 补丁结果 |
| --- | --- | --- |
| bundle 自带攻击者公钥、自签 | accepted，exit 0 | 独立 publisher 不匹配，拒绝 |
| 真实文件 0777 / 签署 0755 | accepted，exit 0 | mode mismatch，exit 1 |
| 未登记 `__pycache__/extra.pyc` | accepted，exit 0 | 闭包不符，exit 1 |
| BOM 文件路径 `../outside.py` | accepted，exit 0 | 非安全路径，拒绝 |
| signer `--add --dry-run` | manifest 字节改变 | manifest/signature 均不变 |
| signer `--sign-only --dry-run` | signature 字节改变 | 两者均不变 |
| 快照目录 symlink 指向外部目录 | 删除外部 sentinel，exit 0 | 外部文件保留，0 候选 |
| 凭据原始备份与权限 | backup 非原始，0644 | backup 为原始，0600 |
| `candidate_sha256` 基线漂移 | `changed=[]` | `changed=['app.py']` |
| 归档摘要故意不符 | exit 0，`verified` | exit 3，`archive_mismatch` |

这些是具体反例，不是自动扫描推测。没有生成可用于真实目标的 payload、使用生产秘密或对外发送任何请求。

新增可携带补充脚本 `docs/audits/2026-11-05/reproduce_extra_security.py` 已执行成功。它直接从固定 Git 对象读取 dailyplan/keyimport/notify 的修前函数；PG/ledger 使用当前源码与临时 SQLite 适配器。输出 `evidence/ops-extra-portable-experiments.json` 记录每个输入版本/SHA 和修后回归文件 SHA，明确修后测试不由此反例脚本执行；不依赖 scratch 的历史副本。

## 验证记录

所有 manager 测试通过仓库隔离 harness，在临时副本上执行，假上游仅监听回环。使用 `/workspace/scratch/913dc6e1a7c0/audit-venv/bin/python`；生产依赖匹配 lock，QA pytest 为 9.2.0，锁声明为 9.1.1，这个差异由主代理记录，不把此次运行伪装成生产签署门禁。

| 隔离 run ID | 重点 | 测试结果 | 秒 |
| --- | --- | --- | ---: |
| `20261105-194417-7abff137` | instance/task lease/runtime | 49/49 | 20.40 |
| `20261105-194813-31505cdc` | docs、ledger、daily replay、drain ASGI | 76/76 | 8.43 |
| `20261105-195730-d0e548e7` | signer、recovery retention、release integrity | 83/83 | 2.84 |
| `20261105-195959-cc930cfc` | 凭据备份/轮换 | 4/4 | 0.33 |
| `20261105-200217-67ec0df4` | 恢复证据真实性 | 4/4 | 0.40 |
| `20261105-200917-5180d34f` | taskrun、concurrency、wrong-key signature | 81/81 | 15.63 |
| `20261105-201410-f03aa9f0` | 有界 stdout/reap 与既有 taskrun | 61/61 | 14.17 |
| `20261105-201950-f734b9df` | 最终已退出 child 的继承 pipe 边界 | 62/62 | 14.04 |
| `20261105-203925-e29dafbe` | 每日调度/后台任务租约全生命周期、既有 daily/taskrun/compensation | 132/132 | 26.04 |
| `20261105-205711-c96ae1c7` | 本机凭据导入、通知配置与既有 key import/notify | 104 passed / 2 原有 Windows skip / 0 failed | 2.31 |

集合有重叠，不将表格相加当作不同用例数。各 run JSON 均记录 `view_changed_during_tests=false`，没有非回环请求尝试。逐份检查 isolation-report.json 后确认，stdout 中的“越界写(仅记录)”样本全部是 `/dev/null`，不是生产路径写入；受保护路径拦截均为 0。每组在 `evidence/runs/<run-id>/` 留有 stats 与 JUnit。

根工具测试使用主 Python runtime，因为该环境已安装 cryptography 50.0.2，audit-venv 未安装：

```bash
python -B tests/test_release_sign_strict.py -v
python -B tests/test_runtime_integrity.py -v
```

结果 15/15、4/4。完整回归方法清单与原版的差集见 `ops-new-tests.json`。主代理将跑最终全量 harness；本报告不提前宣布全量通过。

补充调度回归的初次 run `20261105-203844-f2c9e4a0` 误用了不包含 web 源文件的 manager 视图，结果 84/86，两个失败均为 compensation 的前端文件不存在。未改变断言/skip，使用 manager-assets 后上表 132/132 全部通过；该次没有 require-built-web，最终带构建门禁的全量由主代理另行执行。

## 环境限制、残余风险与后续顺序

### 三个原生进程测试失败不能靠放宽断言解决

主代理基线全量中的 `test_instance_leases::test_pid_alive_helpers`、`test_update_lifecycle::test_zombie_pid_reports_false_posix`、`test_smtp_transport::test_linux_parent_death_stops_actual_private_smtp_worker` 依赖真实 `/proc/<pid>` 语义。

独立实验只检查自身和自己启动的 sleep 子进程，记录在 `ops-proc-evidence.json`：`/proc/self/stat` 可读，而 `/proc/<os.getpid()>/stat` 返回 ENOENT；自己 child terminate/wait 后，同数值 `/proc/<childpid>/stat` 仍报告 S，start_ticks 也未变。这表明当前执行环境的数字 PID 与 proc 可见视图不是通常的同一 namespace 语义。

所以这里无法证明 zombie/PDEATHSIG 等原生合同。未删除、放宽或新增 skip 来制造通过；应在匹配真实 PID/proc 的 Linux runner 重跑。也不能从当前环境反推出生产 Linux 必然存在同样故障。

### 历史 updater 与 Windows 脚本

`manager/deploy/update.py:_safe_extract` 的当前 Python 3.12 路径先拒绝链接/绝对/父目录逃逸，再使用 `filter='data'`。把 Bandit 的整段 extractall 命中直接写成“当前可任意文件写入”不准确。不过旧 Python `TypeError` fallback 没有拒绝设备/FIFO，整个函数也没有成员数、单成员和总解压字节预算。这是旧更新路径仍需硬化的残余，不是已证明当前公网漏洞；main 对 managed release 拒绝自助更新，当前服务升级走独立可信发布器。

Windows legacy `update.ps1` 没有同等 managed-release 拒绝门；摘要与包可从同一配置镜像取得，不等于独立发布者身份验证。替换 server 成功后 web 失败也没有跨目录事务回滚。`service-tools.ps1` 的 Stop-Manager 通过监听端口取 owning process 后结束，缺少明确二进制/命令行身份；native stop cmd 使用同名镜像 taskkill。以上是实际代码级残余风险，本轮未在没有 Windows runtime 的情况下改终止语义或声称通过原生证明。

### 公开仓库、发布控制器与恢复资产

基线缺根 GitHub workflow，本轮已新增只读权限、无 secrets/发布/部署的 root audit-checks 候选。官方 Action SHA、37个Python锁定wheel、actionlint/YAML/10个bash块与合成收据采集核验通过；实际云 CI 待推送后确认，不能声称已运行成功。主审已用同37个wheel纠正最终验证环境，pip check 与根工具73/73通过；最终Manager结果以新回执为准，历史pytest9.2证据不改写。Windows/native平台覆盖仍按真实runner结果判断，不能刷新失败基线、放宽skip或复用旧回执。

外部可信发布器、签名私钥保护、生产公钥权限、数据库/密文真实离机恢复、持久化磁盘与整机故障不在公开树和本地合成测试可证明范围内。恢复清理的 ciphertext hash 成功也不等于已证明全部资产可还原。现在 sampled 演练明确保留这个边界。

### 性能结论的精确范围

已消除的可证明工作是心跳/schedule/history 同步 DB 在事件循环上的等待、准入每请求读取完整处置历史、持续 stdout 绕过执行时间上限和未回收子进程释放状态。测试证明协程继续推进、deadline/取消/准入行为符合合同。

没有跑真实供应商请求，也没有生产流量、机器规格、连接池分布和长稳态样本，因此不提供“延迟降低百分比”“吞吐提升倍数”或“已达到最高速率”的数字。后续应在真实部署等价 runner 做固定输入、固定依赖的并发/流式取消/SQLite 争用/磁盘故障矩阵，分别记录 p50/p95/p99、事件循环延迟、队列深度、文件描述符和孤儿进程数；先保持计费与恢复证据正确，再扩大并发。

## 交接材料

本分工代码已在共享工作树，未提交、未推送、未部署。主代理可直接合并全量审查与测试。材料路径：

- `ops-reviewed-files.txt`：完整逐文件覆盖级别及输入 SHA。
- `ops-coverage.json`、`ops-coverage-summary.json`：自动检查结果与分类统计。
- `ops-new-tests.json`：相对固定基线新增的 100 个回归方法。
- `ops-extra-reviewed.tsv`、`ops-extra-findings.md`：追加 26 个生产模块的全文审计与实际残余。
- `ops-extra-experiments.py`、`ops-extra-experiments.json`、`ops-extra-dailyplan-before.py`：补充反例，dailyplan 明确固定修前代码，PG/keyimport/notify 固定输入哈希。
- `ops-security-experiments.py`、`ops-security-experiments.json`：10 组原版/补丁反例。
- `ops-proc-evidence.json`：当前执行环境 proc/PID 限制证据。
- `ops-*-tests.log` 及仓库 `evidence/runs/`：聚焦测试原始日志、JUnit、视图哈希。

不应将本分工报告作为“全部代码零漏洞、私有 engine 已审计、生产部署已验证”的证明。
