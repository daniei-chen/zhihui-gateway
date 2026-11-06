# K15 完整性清单（设计 + 工具）

**问题**：Manager 的 494 文件验签是有效的，但只覆盖发布包；`local-overrides` 仍是额外执行层，
QTG 清单有三处漂移，"有签名标志"不等于验签通过。线上更新缺摘要/签名时不得静默降级。

## 目标

1. 运行目录的**每个可执行文件**都能追溯到签名清单；
2. 运行覆盖层（`local-overrides/`、部署后追加补丁）被单独列出并审计，而不是混进"已验签"；
3. 依赖与部署配置（requirements、systemd unit、nginx 片段）进入同一份清单；
4. 在线更新缺摘要/签名时**拒绝**，不静默降级；
5. 信任锚（公钥）不可由服务账号改写。

## 工具

`ops/integrity/verify-runtime.py`：

```bash
# 生成运行目录清单（只读）
python3 ops/integrity/verify-runtime.py \
  --release /opt/workbuddy-manager/releases/current \
  --out /tmp/runtime-manifest.json \
  --baseline /opt/workbuddy-manager/releases/current/deploy/managed-release.json \
  --fail-on-overlay
```

退出码：`0` 报告生成完成；`2` 目录/参数错误；`3` 在 `--fail-on-overlay` 下发现运行覆盖层。
`0` 不代表来源可信或基线一致：本工具不验签，missing/extra/changed 仍须由调用方检查，
不能仅以退出码批准发布。

输出包含：逐文件 sha256/size、`untrusted_overlay` 列表、与基线清单的 missing/extra/changed 差集。

## 与发布流程的衔接

- R1 批次（发布护栏）要求"门禁可拒错误制品"：本工具可作为 G1 门禁的一项；
- 发布前对**候选释放目录**跑一次，确认差集里只有本批次预期的文件；
- 发布后对线上 current 再跑一次，两次清单一致才算"发布的字节 = 构建的字节"。

## 未验证 / 边界

- 未在真实 release 目录跑过（本地无 `/opt` 路径），脚本已用合成目录自测；
- 兼容仓库 managed-release 的 `candidate_sha256`、运行清单的 `sha256`，以及 dict/list 两种结构；缺失、冲突或无效摘要拒绝作为基线；
- 信任锚的权限模型（公钥文件属主、是否 root-only）需在服务器上核对。
