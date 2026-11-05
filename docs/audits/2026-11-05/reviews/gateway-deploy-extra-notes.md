# 部署脚本补读与调度生命周期交叉复核

日期：2026-11-05。仓库：`/workspace/scratch/913dc6e1a7c0/zhihui-gateway`。本轮为业务代码冻结后的独立只读审查；未执行安装、更新、清理、邮件、Windows/PowerShell/C# 构建、私有引擎或任何新增测试。7 份部署源文件共 4,629 行全部人工全文阅读，文件摘要见 `gateway-deploy-extra-reviewed.tsv`。父代理另行执行最终全量验收。

## 结论与适用范围

没有发现能够据本轮证据确认为当前默认 Linux 受控发布中新引入 P0/P1 的问题。此结论不等于历史安装器、Windows 分发或所有运维清理入口都可安全使用。`manager/README.md` 明确本产品仅通过可信发布器安装，历史安装指南只是组件参考；该文档约束不能替代旧脚本自身的执行门禁。

### 1. 旧 Windows 更新入口仍可调用，发行源与当前产品不一致

`manager/windows/update.ps1:157,181,223,364,395` 固定访问 `ithtelab/workbuddy-manager`，不是本仓库发行源。`start.ps1:10` 调用 `prepare.ps1`；后者在缺运行时且无 uv（159）或缺前端（180）时调用 `update.ps1`，`launcher/build.ps1:530` 又把这些脚本打进安装包。更新前还有交互确认，因此不能描述为无需确认的静默远程覆盖，但来源选择发生在这个旧调用链中。

此脚本没有 Python 更新器同等的 `managed-release.json` 拒绝门，也不验证智汇产品/构建身份。摘要和包均可来自同一用户配置镜像；摘要匹配证明该来源自洽，不证明获得当前可信发布器授权。若仍支持该 Windows 分发，应阻断旧来源更新入口，完成产品身份迁移与原生验证后再启用。没有 Windows 实机和当前安装回执，不能声称它正在影响当前 Linux 实例，也不能把它当作已经停用的死代码。

已有 ops 残余结论经全文复核仍成立：`Replace-Dir` 对单目录失败可恢复旧目录，但 server 成功替换后 web 失败没有跨目录回滚；`Fail-Update` 的“现有文件未做替换”文案在该分支不实。停服发生在解压和 Python 运行时检查前，失败不自动恢复服务。依赖安装在替换后进行，外部 pip/uv 的退出码没有验收，仍可能显示成功。旧 `prepare.ps1` 同样只导入 FastAPI/uvicorn 作最低检查，安装依赖失败并不确保终止启动。以上保留为 legacy Windows 可达残余，未改脚本。

### 2. Python 旧更新器：受控实例有门禁，未受控流程仍有明确边界

`update.py` 的 `main`、`update_manager`、`update_upstream`、`_release_root` 对受控标记拒绝更新；标记路径存在但无法读取时也拒绝。当前 Python 3.12 解压先拒链接和越界，再使用 `filter='data'`。不能把静态扫描的 `extractall` 命中直接当成当前受控实例任意写漏洞。

未受控流程仍有以下边界：API 获取失败后的 redirect 路径以及无摘要的离线包可以仅记录本地摘要后安装；下载只限制单次 socket 操作，没有总字节、总耗时、归档成员和解压量预算；旧 Python 的 `TypeError` fallback 未显式拒绝设备/FIFO。安装依赖会改现有 Python 环境，server/web/deploy/upstream 也不是一个跨目录事务，不能从单目录 `swap_dir` 推导整次更新可原子回滚。上游文件同步逐项捕获写失败、缺 Docker 时跳过重建及就绪失败只警告，都可能形成“代码或版本标记已变，运行服务未完全匹配”的旧流程状态。

`run()` 使用 argv 调用子进程，无 shell 拼接执行；但 capture_output、Reporter 日志没有总量边界。代理凭据有专用脱敏逻辑，原始下载/命令错误仍不应被解释为整个更新链已经通过秘密日志证明。上述不用于扩大当前冻结修改范围。

### 3. 资产清理：保护规则有边界，不能代替发布锁和资产身份

`asset_lifecycle.py` 默认 audit；apply 要匹配候选路径列表摘要。当前 release、最近 3 个语义版本、最近备份/快照、数据库本体和预算 owner 目录有保护。快照按逻辑身份保留全部 db/WAL/SHM 成员，避免旧版按单文件计数的错误。

仍有三项具体残余：

- `denied()` 对 `opened` 只作精确路径匹配；删除某 release 目录时，打开的子文件不匹配目录本身。文档“任何正在被进程打开的路径”不能推导成目录树级保护。
- `apply_prune()` 使用 audit 时记录的 active_release，不重新读取 current，也没有发布锁；若并发发布/回滚更换 current，可能依据旧快照清理。候选 token 只覆盖路径，不覆盖 inode/摘要/当前运行身份。这是需显式执行 apply 才发生的运维风险，未运行任何删除复现。
- 排序以版本数字优先；如果 v10.7.x 历史目录和 v0.0.x 产品目录共存，最高 3 个语义版本不等于本产品最近 3 个可回退版本。当前 active 仍单独保留，但旧智汇回退点可能被列为 prune。未取得真实 release 目录清单，不声称已经发生数据或回退点丢失。

### 4. 宿主 watchdog：只读业务控制面与有界通知，非发布许可

`steward_watchdog.py` 只读 SQLite 的 policy/heartbeat，禁止查询写入；状态目录、锁和文件检查绝对路径、uid、0600/0700、单链接及大小；原子写以随机 O_EXCL 临时文件、fsync 与目录 fsync 完成。最小就绪探针禁代理/跳转、最多读取 257 字节，发布锁元数据只用于最多 15 分钟静默，不能授权发布。通知发送前先持久化 sending，重启时将它变成 unknown 且不重放；每轮最多 3 封，错误码限枚举字符，发送前重新校验策略/事件代次。

残余可用性边界：unknown 不在终态清理集合，积累到 200 条会按 outbox_full 拒绝继续，需受控人工核对而不能盲重放。业务数据库暂不可读时可沿用 24 小时内公开策略缓存，所以“撤销收件人立即生效”以能够重新读取控制面为前提。没有在本轮发送邮件或运行 watchdog。

### 5. Linux 旧安装与 Windows 构建

`install.sh` 是 root 的历史一次安装脚本，没有当前受控发布门禁；依赖安装使用 hash 清单，但服务/源码更新并非事务，重跑会混合覆盖部分代码并可能保留旧 web/out 和版本标记。无 Docker 或 skip-upstream 且未探测 Docker 时，末尾 heredoc 引用未定义 COMPOSE，与 `set -u` 组合可在实际安装后报错。Python 最低版本文案仍为 3.9，与现代码使用较新的 asyncio 等接口不一致。这些属于旧工具一致性问题，非当前受控部署成功证明。

`build.ps1` 的前端、上游与运行时缓存记录源码/配方/脚本哈希；上游产物还验证 exe 哈希，并排除 auths/data/config/用户密钥类文件。与此同时，首次工具链、uv、Python 和企业上游下载仍依赖 HTTPS 及可变 URL，没有独立固定摘要/发布者验签；本地运行时缓存主要检查 Python 可执行文件与两项导入，不证明全部依赖字节身份。脚本不应用本轮公开引擎补丁，因此不能认为用旧 upstream-src 编译出的 Windows 引擎与当前 v0.0.7 私有引擎一致。固定 TEMP 文件名和缺总下载 deadline 是构建可靠性残余。未编译、下载或执行这些资源。

## dailyplan/taskrun/main 最终只读交叉复核

完整阅读 `dailyplan.py`、`asyncwork.py`、`taskrun.py` 本轮生命周期改动及其调用部分，核对 `tasklease.py:260–369` 和 `main.py:203–260`，并阅读新增两个 lifecycle 测试源；本轮没有新增测试执行。未发现可确认的新 P0/P1。

1. 迟到的 claim 在工作线程闭包中保存 claimed/fence；joined_thread 在取消后等归属结果，finally 仍能释放该 fence，不会漏掉已经成功但调用协程被取消的取得结果。
2. monotonic 本地到期回调不等 SQLite 续租线程，renew 返回 False 或异常都会取消 owned；续约的 DB 条件包含 owner/fence/未过期，锁等待后再取时钟，不复活已经过期的租约。
3. finish_claim 先取消并 gather 工作及续租，再 fenced mark_ran/release；晚完成的续租不会越过释放。外层重复取消通过独立 joined_task finalizer 等到底，不提前释放。
4. run_and_wait 在 start_async 成功返回后没有 await 间隙就捕获具体 `_task`；stop/取消后清状态均核对同一 handle，避免停止后续新任务。预准备取消后不再 launch。
5. `_run` 保留迟到 spawn 的进程句柄；持续输出和 stdout 提前 EOF 均受总 lifetime deadline；取消/读失败后 kill，最多 2 秒排空继承 pipe 后关闭本端，再 wait/reap，再记历史与释放 running。重复取消不跳过该归属链。此为直系子进程合同，不证明已实现完整后代进程树隔离。
6. main 在关闭共享 HTTP clients 前 await dailyplan.close_scheduler。默认关闭的旧 `_claim_loop` 和手工独立启动的后台 task 不由 dailyplan handle 托管；这是既有独立入口的边界，不是本轮新增的 dailyplan 提前释放问题。

## 阅读证明与未覆盖

清单 FULL 表示全部当前文件文本已显示并人工阅读，含注释、错误分支、内嵌 Python 和 PowerShell；输出被工具截断的片段已单独补读。清单不把自动扫描当人工审阅。没有当前生产磁盘、签名控制器私有实现、真实 Windows 运行时、供应商网络或完整私有引擎，因此不作这些环境的运行成功/可利用性保证。
