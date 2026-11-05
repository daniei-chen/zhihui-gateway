# 部署辅助代码收尾审读

审读人：`/root/frontend_audit`。日期：2026-11-05。基于当前候选工作树，文件字节摘要及全文范围见 `deployment-helpers-reviewed.tsv`。

## 范围与方法

指定的 15 个文件全部逐文件阅读全文，修复前共 1,311 行；没有把搜索命中、自动检查或引用的其他模块算作这些文件的人工全文覆盖。补充核对了企业支持目录的两份 README 全文，以及本次新增拒绝合同测试全文。仅定点查看了 `config-schema.json` 的代理敏感性条目及当前发布文档引用，不声称全文审读这两个额外依赖文件。

初始阶段只读取源文件，没有执行任何被审部署、真实请求、凭据读取、计费对账、签名或测试 runner 脚本。发现历史企业补丁入口的条件 P1 后，主审明确授权最小退役修复：只修改该脚本、两份 README，并新增隔离的 root 工具回归。其余 14 个被审脚本未修改。前后端业务、构建导出与正式 managed 发布入口继续冻结。

结论：**1 项条件 P1 已通过入口退役处理；未确认其他 P0/P1。** 下列 P2 与限制如实保留，未借本次收尾扩大重构或把离线工具的推测写成在线漏洞。

## DHELP-01 — 条件 P1：历史企业脚本破坏性重建且不保留完整配置（已退役）

原 `manager/docs/enterprise-support/apply.sh` 的 `patch_manager` 在原 122 行读取 `old_env`，后续并未把环境变量传给 `docker run`；原 144 行先 `docker rm -f`，原 150 行才读取已删除容器的镜像，通常只能回退到未固定的 `latest`。原 130–132 行重建挂载只保留 Source/Destination，丢失 readonly 等属性；原 146–150 行也未保留原端口、命令等完整启动合同。失败后没有恢复原容器。原上游路径还将测试失败降为警告，并以 `docker ps` 输出而非 readiness 成功结束。两份 README 修复前仍直接推荐一键执行。

**可达性必须限定：** 当前公开树只包含 `verified/upstream-enterprise_usage.go`，没有历史的 `verified/manager-tencent.py` 和 `verified/manager-wb2api.py`。因此，现有 manager 路径通常会在原 115–116 行的 `cp` 缺文件处失败，不能宣称当前树默认必然走到删除容器。上述破坏性分支在运维补齐历史文件、使用完整历史补丁包等条件下可达；其危险顺序由源码直接证明。本次未补齐文件，也未执行该分支。

按主审授权，脚本在路径解析、环境设置、文件操作和所有历史函数之前输出固定 `RETIRED` 提示：直接执行退出 2，被 source 时返回 2。历史主体保留用于阅读，不再提供可调用安装入口。中英文 README 顶部明确停用，删除一键执行教程，说明缺失成品文件，提示所有历史手工重建/回滚命令也不适用于当前实例，并链接当前产品发布合同与签名要求。没有修改正式发布实现。

**验证：** `tests/test_retired_enterprise_apply.py` 通过 2 个测试方法、11 个子场景、0 跳过。覆盖默认参数、`all`、`upstream`、`manager`、无效参数、`--help`，以及 source 的默认/有效/无效目标。使用最小环境、私有临时目录和失败 shim，实际运行当前脚本的拒绝入口；所有场景返回 2、stderr 包含退役说明、stdout 为空、无 shim 调用、临时目录的文件/目录及内容摘要保持不变。未启动真实 Docker、Go、curl、Python 凭据读取或任何部署命令。

验证命令：

```text
/workspace/scratch/913dc6e1a7c0/audit-venv/bin/python -B -X utf8 -m unittest discover -s tests -p test_retired_enterprise_apply.py -v
```

## 保留的明确 P2 / 操作边界

### DHELP-02 — 环境示例生成会原样输出未标记敏感的运行值

`manager/deploy/gen_env_example.py:110–131` 仅以 schema 的 `sensitive` 判断是否隐藏值，否则把进程环境原值直接拼入示例。当前 `manager/server/config-schema.json` 的 `WB_HTTP_PROXY` 标为 `sensitive: false`。如果代理 URL 包含用户名/密码，这些凭据会进入默认 `.env.example`；值中的换行也没有转义。这是需要本机运维执行、且环境包含相关值的泄露/格式风险，未读取任何实际 `/proc/*/environ`，未声称当前部署正在泄露。建议对可能包含用户信息的 URL 做结构化脱敏，并给输出明确的示例语法转义。

### DHELP-03 — 旧测试 runner 不能独立证明完整生产隔离

`manager/deploy/isolated_runner.py:134–141` 复制整个父环境，仅覆盖部分 `WB_*`；例如 `WB_DB`、`WB_USERS_FILE` 等既有值未被清除。其临时目录和 loopback fake provider 是测试便利设施，不是文件系统/出网强制隔离。是否有具体测试触碰这些路径取决于测试内容，不能仅凭该文件断言已经发生生产写入。

`manager/tools/validate.py:12–23` 覆盖的路径更多，但默认也没有 OS 级网络/文件访问约束；`--isolated` 只检查与 PID 1 的网络 namespace 是否不同并启用 loopback，不能单靠该比较验证 namespace 内没有其他联网接口。其 54 行 `production_model_calls: 0` 是常量，不是实测计数。故这些工具的输出不应作为“真实供应商请求必为零、生产文件不可能触及”的证据。本轮正式 Manager 验证使用主审现有的独立 harness；本次没有运行这两个旧 runner。

### DHELP-04 — 提示词探测的凭据选择与失败判定

`manager/deploy/probe_prompt_trigger.py` **不是离线工具**：只有 `--dry-run` 在 187–192 行提前返回；其他模式会解析本机 profile/凭据库并实际请求供应商。

其 `key_from_store` 在 96–98 行可回退到任意首个 `llm-pi-ai/` 记录；`resolve_credentials` 在 115–128 行允许单独覆盖目标 `base_url`，仍沿用 profile/凭据库取到的 key，没有将凭据绑定到端点或严格匹配 provider。需要明确运维调用条件；未读取任何实际凭据，不把它描述成无认证远程窃取路径。

此外，150–156 行对非标记 HTTP 错误及网络异常返回 `rejected: false`，204–224 行据此可能输出 `ok` / “所有片段都通过”并退出 0。例如连接失败的 `status: 0` 会被当作未触发拒绝。这不能证明一次有效的上游成功探测。应分别表示成功、目标拒绝、未知/请求失败，并避免未知结果进入“通过”。

### DHELP-05 — 冻结生成器在发现失败前已覆写目标文件

`manager/deploy/freeze_from_junit.py:47–52` 先写目标 JSON，再因报告含 failure/error 返回 3。返回码和告警并非假成功，但若用户将目标直接指向现用冻结清单，失败执行也已替换文件。建议验证输入可用于冻结之后再原子写入；本次没有执行生成器或修改冻结清单。

### DHELP-06 — 资源池命令有零工作量成功分支

`manager/deploy/web_pool_manifest.py:164–172` 对显式指定但不存在的 build 只打印“跳过”，最终返回 0；`--verify` 在池存在但没有任何 build 时也返回 0（144–158 行）。调用者不能仅凭退出码认定“指定 build 已签名”或“至少有一个 build 完成验证”。`--list` 的返回 0 则是列举行为，不能误作安全门禁。

该文件确实先验 Ed25519 签名再验内容，但清单路径没有明确拒绝绝对路径、父目录跳转和重复条目；磁盘完整性只比较数量，没有比较精确路径集合（99–114 行）。这些是对可信签名输入的格式约束不足，不足以证明攻击者在没有签名能力时能伪造新清单或绕过正式启动门禁。签名/清单写入非原子及手工 `--build` 路径输入也要求可信运维边界。未执行 OpenSSL、未访问真实 pool 或密钥。

## 各文件审读结论

| 文件 | 实际用途和主要边界 |
|---|---|
| `manager/client/zhihui_upload.py` | 纯序列化/压缩 helper。拒绝非有限 JSON 数字，检查 128 MiB 原始字节限制，压缩收益至少 10% 才启用 gzip；无网络、凭据和文件访问。限制发生在序列化后，不等于序列化阶段恒定内存保证。未确认 P0/P1。 |
| `manager/deploy/check-upstream.sh` | 明示不再查询外部上游，只输出维护说明并返回 0。不能把它当成补丁、构建或 Go 测试门禁；不是隐蔽假检查。 |
| `manager/deploy/freeze_from_junit.py` | 人工复核的 JUnit 冻结生成器；失败报告返回 3，但先写目标，见 DHELP-05。 |
| `manager/deploy/gen_env_example.py` | 本机生产进程环境到示例的转换，不是纯离线生成；schema 标记决定脱敏，见 DHELP-02。 |
| `manager/deploy/isolated_runner.py` | 私有临时状态和 loopback provider 的测试工具，不能代替强隔离，见 DHELP-03。 |
| `manager/deploy/probe_prompt_trigger.py` | 默认带凭据真实请求，dry-run 才离线；见 DHELP-04。 |
| `manager/deploy/protocol_contract_diff.py` | 导入本地可信 `server/protocolcontracts.py` 比较两个本地 JSON；无下载/部署；解析失败非零，breaking 返回 2。该依赖不在本组全文覆盖声明内。 |
| `manager/deploy/reconcile_credits.py` | `--apply` 在打开连接前固定拒绝；正常路径使用 SQLite `mode=ro`。日志明细受保留期限制，源码已明确不能当修复权威。尾部“加 --apply 执行”的提示陈旧，实际不会执行覆盖。 |
| `manager/deploy/release_state.py` | 只切换发布 symlink、fsync 父目录并在 readiness 失败时尝试回滚；不碰业务库/凭据。签名校验与服务生命周期由调用者负责；stop/start/rollback 自身异常不具备无条件自动恢复保证。未据此推断真实发布调用链存在 P1。 |
| `manager/deploy/remove_manifest_key.py` | 显式本地清单删条目，无文件递归删除、无网络。直接非原子覆写会使既有签名不再适配，需按签名流程重签；操作需要可信目标文件和人工复核。 |
| `manager/deploy/upstream_review.py` | 仅比较本地 JSON；验证仓库标识和 40 位提交摘要，未知 head 返回 2。成功是离线观测比较成功，不证明远端最新版。 |
| `manager/deploy/web_pool_manifest.py` | 本地资源清单签名/验证工具，真实磁盘和密钥操作；见 DHELP-06。 |
| `manager/docs/enterprise-support/apply.sh` | 历史容器补丁器，条件 P1 已由首部拒绝门禁退役；正文仅保留历史阅读。 |
| `manager/local-patches/apply_patches.sh` | 固定 exec `/opt/workbuddy-manager/deploy/source_preflight.py`，自身不是补丁实现；返回被委托程序状态。没有运行该固定生产路径，也未把它的内容算进本次覆盖。 |
| `manager/tools/validate.py` | 每模块子进程、合成路径和超时的旧测试调度器；结果汇总中的供应商零请求是声明而非测量，见 DHELP-03。 |

## 验证与限制

本次只运行经授权的退役入口回归，且 PATH 中的所有可能部署命令均为失败 shim；没有运行这 14 个未修改 helper，没有启动广泛测试，没有触碰真实服务、生产凭据、账号库、签名密钥、资源池或任何发布目标。新测试验证拒绝入口的行为，不声称已验证历史 Docker 迁移或其他 helper 的运行表现。文件清单保存当前摘要，便于主审与其他审读清单按路径合并，避免重复计数。
