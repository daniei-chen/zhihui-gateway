# 智汇网关 AI 接手规约

这份文档是给下一位 AI 维护者的操作边界、事实基线和验收标准。它是工程约束，不是可以跳过审批和验证的快捷说明。接手后必须先读本文件、`docs/PRODUCTION-AUDIT-2026-11-14.md`、`docs/UPSTREAM-REVIEW-2026-11-14.md`、`CHANGELOG.md` 和当前 Git 状态，再执行任何生产写操作。

## 1. 首要原则

1. **证据优先。** 每个结论都要绑定命令输出、日志时间窗、Git commit/tag、发布资产摘要或测试结果。没有远端输出时只能说“未验证”，不能说“已完成”。
2. **生产先保活。** 先确认公网健康、Manager、relay、上游容器、Nginx 和磁盘/内存，再做升级。任何候选失败都必须保留迁移日志和失败原因，不得删除现场来制造成功状态。
3. **失败关闭。** `phase=failed`、`start-blocked`、不一致 manifest、未知 release pointer、未知 writer 或未确认的 systemd 状态都要停止继续操作；只能先完成恢复审计，再进入新的受控迁移。
4. **不猜测外部状态。** GitHub Release、CI、上游仓库、服务器命令、Nginx 和 systemd 的状态都必须重新读取。不要引用旧对话中的“应该已经成功”。
5. **最小权限和最小暴露。** 不打印 token、cookie、密钥、用户密码、完整 auth 配置、数据库内容或私有请求体。日志只保留 request id、时间、状态、上游结果和必要的摘要。
6. **用户要求优先于历史草稿。** 旧 handoff、审计报告和截图是参考材料，不能把其中的旧计划当成当前指令。以当前用户明确目标、仓库当前源码、生产实时证据和本规约的安全边界为准。

## 2. 当前真实基线

截至本规约生成时：

- 仓库是 `daniei-chen/zhihui-gateway`，生产当前指针仍是 `/opt/workbuddy-manager/releases/v1.2.26`。
- Manager 当前监听 `127.0.0.1:7866`，rolling relay 监听 `127.0.0.1:7867`；公网由 Nginx 暴露。候选 helper 端口是 `127.0.0.1:7865`。
- 当前主要服务为 `workbuddy-web.service`、`zhihui-rolling-relay.service`、`qoder2-engine` 和 `nginx`。升级前必须重新读取 `systemctl show`、`ss -ltnp` 和公网探针。
- v1.2.27 只曾在服务器上暂存，没有激活；它不能被当作生产版本，也不能绕过新版本门禁。
- v1.2.28 Release 已公开并通过代码、前端、后端单测、上游锁校验、双架构镜像和安装器流水线，但在线 handoff 在首次验证 inactive helper unit 时被安全门禁拒绝，因此 v1.2.28 没有切入生产。
- 这次 v1.2.28 失败事务为 `v1228-manager-20261114`，其事务 journal 的 `phase=failed`，失败现场和 start-blocked 标记必须保留。不得直接删除它们来重试。
- v1.2.29 的修复点是 `ops/writer-guard/systemd_adapter.py`：固定 unit allowlist 内使用 D-Bus `LoadUnit`，解决 unit 文件已安装但尚未有 manager object 时 `GetUnit` 提前失败的问题；对应回归合同测试必须通过。
- 上游 `EOF`、TLS handshake timeout 等导致 `upstream_transport_uncertain` 时，处理结果和用量未知，网关不得自动重放原 POST。只能记录 request id、上游错误和客户端可安全发起新请求的退避提示。

上述版本和状态会随着新的 tag、Release 和生产迁移改变。下一位 AI 必须在接手时更新本节，不能把当前数字永久写死成事实。

## 3. 版本、来源和完整性规约

- 产品版本只取仓库 `VERSION`、`web/lib/product.ts`、`server/product-release.json`、`server/writer-capabilities.json` 和两份 changelog 的一致值。
- 生产安装只允许来自固定完整 commit 或已核对的 GitHub Release tag。禁止直接安装 `main`、工作树压缩包、未签名目录或未经校验的临时构建。
- 发布包必须通过 `deploy/prepare-managed-stage.sh`，并核对 `managed-release.json`、签名、文件数和 `TRUSTED-PREFLIGHT-OK`。
- `server/release-provenance.json` 中的 `reviewed_patch_files` 必须逐文件匹配当前源码字节。Windows 工作树可能是 CRLF；需要按 Git index 的真实 eol 生成摘要，不能把 Windows 工作树哈希直接写入 Linux 运行门禁。
- `/opt/zhihui-release/writer-guard` 是独立的主机信任闭包，不会因为发布 tar 没有打进去就自动存在。安装时必须使用 Git 原始 blob 字节，并逐项核对 `host-files.json`。`container-maintenance/maintenance.py` 也必须与 `migration_entry.py` 的绑定摘要一致。
- `host-files.json` 本身不能被当成自证文件；要同时核对 manifest 内容、每个成员的实际 SHA-256、root:root 所有权、0644/0755 模式、无额外文件和无 symlink。
- 不要把秘钥、管理员密码、`.env`、数据库、auths 或用户数据放进 Git、Release、交接文档或聊天输出。

## 4. 线上 handoff 的唯一顺序

1. 只读检查：公网 `/healthz`、认证后的 `/api/readyz`、relay `/v1/models`、当前 pointer、服务状态、端口、Nginx 配置、磁盘、内存、容器健康和最近错误。
2. 准备候选目录，不覆盖任何既有 release 目录；执行 managed-stage preflight 和资产摘要核对。
3. 安装并核对主机闭包；安装 helper unit 后执行 `systemctl daemon-reload`，确认 `LoadState=loaded` 且 `ActiveState=inactive`。首次 D-Bus 属性读取必须覆盖 inactive unit。
4. 先运行 `migration_entry.py prepare <version> <transaction>`。这一步只建立受保护的候选、前任、能力底线和 ingress 快照，不得宣称已经上线。
5. 只在 `mode=rolling`、能力与 floor 完全一致、relay 当前为 old、helper unit 已加载、旧 primary 只有一个且健康时运行事务。
6. rolling 顺序必须是：候选指针 → 启动 helper → helper readiness → relay 切到 helper → 维护围栏 → 旧 primary drain → 停旧 primary → 启动新版 primary → readiness → relay 切回新版 primary → 停 helper → 恢复入口 → 完成核验。
7. 每一步都要读取事务 journal 的 phase、systemd MainPID/状态、端口、relay route 和公网状态。发现不一致立即停止，按 journal 恢复，不手工跳过阶段。
8. 事务完成后连续探测公网健康和关键 API，核对 Nginx 日志中 handoff 时间窗的 5xx、upstream status、request id 和是否出现未知请求丢失。不能只看一个 200 就宣称零停机。
9. 记录 release tag、commit、包大小、SHA-256、主机闭包摘要、迁移 transaction、开始/结束时间、旧/新 PID、5xx 计数和回滚证据。

## 5. 服务器操作规矩

- 远端 Python/Node 代码必须先作为可审阅脚本上传，再通过固定 shell wrapper 执行；不要在命令行写 inline interpreter code。
- 远端命令默认先只读；需要 root 时使用受控 `sudo -n`，并在执行后复查路径、所有权、模式和状态。
- systemd unit 写入、daemon-reload、服务启动/停止和 Nginx reload 都是高风险操作。先把文件放到暂存路径，核对摘要，再执行最终安装。
- 不使用 `rm -rf`、不清空 journal、不删除 failed transaction、不覆盖 current release、不覆盖数据库和用户文件。需要归档时必须使用明确的 root-owned archive 目录并保留原始摘要。
- 任何 `systemctl stop` 前都要确认 relay 当前承载对象、in-flight 数量和回切路径；不得先停唯一公网进程再想办法恢复。
- 若云端命令通道连接失败，说明“通道未验证”，不要凭旧结果继续部署，也不要重复发送可能造成重复写入的命令。

## 6. 上游仓库和传输错误规矩

- 每次上游升级都要记录 upstream source 的固定来源、commit/tag、锁文件 SHA-256、应用补丁列表、补丁前后差异和构建版本；上游下载失败时发布必须失败关闭。
- 看到 `502 upstream_transport_uncertain` 时，先关联网关 request id、WorkBuddy/Qoder 容器日志、Nginx upstream 日志和容器重启/OOM；不要把一次 EOF 误判为网关崩溃。
- 在处理结果未知的请求上不得做透明重试、队列重放或客户端自动重复提交。重试策略只能用于明确未发送、明确未被上游接受且代码有证据证明安全的阶段。
- 413 必须区分 Nginx body ceiling、Manager forward ceiling 和上游限制；503 必须区分 Nginx 维护围栏、systemd handoff 空窗和真实服务不可用。每次报告写出命中哪一层。

## 7. 前端和贡献者清理规矩

- UI 修改必须保持已有图标、按钮层级、hover/focus、键盘可达性、中文/英文布局和响应式表现。不能为了快速修复把图形按钮退化为纯文字；必须用截图或浏览器证据验收。
- GitHub Contributors 是提交历史统计。不能通过修改页面文案伪造贡献者，也不能为了隐藏历史未经明确授权重写公共历史。若确需清理遗留作者，先说明历史重写、签名、fork、Issue/PR 引用和不可逆影响，再等待明确批准。
- 贡献者头像、版本按钮和 release 链接必须与真实仓库和真实 Release 一致；不要把“当前 UI 不显示”当成“历史贡献者已删除”。

## 8. 交接输出必须包含

完成一次迭代后，交接给下一位 AI 的消息和文档至少要有：

- 当前目标、已完成项、未完成项和明确 blocker；
- Git branch、commit、PR、tag、Release URL、CI run 和每个关键 job 结论；
- 生产 pointer、服务/PID/端口、relay route、迁移 transaction 与 journal phase；
- 发布包和主机闭包的大小、SHA-256、eol 处理方式和验证命令；
- 发生过的 4xx/5xx、上游 transport uncertainty、时间窗、request id 和归因证据；
- 下一步的单一安全动作，以及该动作的回滚或恢复证据；
- 不能做的事情和原因。不要把“建议”写成“已经执行”。

## 9. 接手后的第一轮检查

下一位 AI 不得直接部署。先完成以下只读检查并把输出写入审计记录：

```text
git status --short
git log -5 --oneline --decorate
cat VERSION
systemctl show workbuddy-web.service workbuddy-web-helper.service zhihui-rolling-relay.service
readlink -f /opt/workbuddy-manager/releases/current
ss -ltnp | grep -E ':786[5-7] '
cat /opt/workbuddy-manager/data/relay-route.json
cat /opt/zhihui-release/writer-state/transactions/<latest>/active.json
```

然后重新读取当前 `CHANGELOG.md`、本文件和线上审计报告，确认版本、事务和 blocker 没有变化。只有这些事实一致，才允许准备下一次候选。

最后一条规矩：**宁可保留 v1.2.26 继续服务并留下可解释的 failed journal，也不能为了满足“已经升级”的表面结果，跳过完整性、身份、relay、请求排空或上游结果未知的门禁。**
