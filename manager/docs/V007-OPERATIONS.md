# v0.0.7 操作合同

## 当前授权

仅允许本地开发、合并、测试和准备候选。**没有生产切换、GitHub 发布或新一轮内部密钥轮换授权。** 候选不携带真实内部密钥、用户资料、生产数据库、`.env` 或签名私钥。

不得直接使用继承的旧一键部署脚本；现网协调发布/签名信任根不在本地改写。最后一次观察不能代替实际上线前检查。

## 配置默认值

| 配置 | 默认 | 语义 |
|---|---:|---|
| `WB_ARCHIVE_ENABLED` | `true` | 主实例启用安全归档；关闭后也不直接删过期日志 |
| `WB_ARCHIVE_INTERVAL_SEC` | `300` | 60–86400 秒；不因请求频繁通知而突破间隔 |
| `WB_ARCHIVE_BATCH_ROWS` | `200` | 每轮每个数据集最多 1–1000 行 |
| `WB_BACKGROUND_JOBS` | 保持现有值 | 过渡实例不启动归档等主实例作业 |
| `pool.credit_floor` | `0` | 引擎已知余额软偏好，0 关闭 |
| `pool.large_request_limit` | `0` | 大请求窗口次数软偏好，0 关闭 |
| `pool.large_request_threshold_bytes` | 有效默认 `65536` | 仅 limit >0 时使用 |
| `pool.large_request_window_seconds` | 有效默认 `300` | 仅 limit >0 时使用，上限 24 小时 |

软偏好只在有其他健康可用候选时分散流量；不是额度扣减或费用上限。计数仅表示本进程逻辑派发，重启重置，不冒充供应商收费次数。启用需单独验证真实账号/模型成本分布；本候选不热改现网配置。

## 归档与诊断

默认归档位置为 `DATA_DIR/archives`，私有目录。仅对 `request_logs` / `request_outcomes` 自动执行“归档后删除”，不自动核销预算，不删归档文件，也不对主库执行 VACUUM。

空间低于 256 MiB、锁竞争、校验失败会停止本轮；原数据保留。归档释放的是 SQLite 可重用页，不保证数据库文件即时缩小；档案仍占本地磁盘，必须监测空间。

手动工具：

```text
python -m server.archival plan --db <显式数据库路径>
python -m server.archival archive --db <路径> --dataset request_logs --out <私有目录>
python -m server.diagnostics --db <路径> --minutes 60 --max-rows 1000
```

未加 `--apply` 不写归档；删除还需实际已校验 SHA。不得用猜测 SHA、水位、旧备份或一份 integrity_check 代替匹配的归档证据。

`GET /api/logs/retention/status` 是最近采样状态，不发网络探测；没采样或过期为未知，不是绿色。归档异常属于可观测运维项，不因为一个归档周期失败就中断推理。

## 未决用量

`GET /api/budgets/review`：管理员只读，默认 25、最大 100 项；带桶筛选和游标，每次最多扫描 500 行。`GET /api/budgets/review/evidence`：有界的用量、结束证明和 provider 证据，不提供原始提示词或密钥。

网页只读。现有人工核销接口仍要求近期交互管理员会话、足够长度的原因/凭证、传输结束或 owner 确认死亡、无回执冲突。不能根据取消、超时、`11128` 或缺事件直接核销为 0。

## 上线前必须完成

1. 用户明确同意上线本候选；重新核对实际版本、PID、引擎、请求在途与依赖状态。
2. 固定本地 source tree / frontend / ELF 哈希；隔离 Linux Manager 与引擎全量门禁通过，平台已知差异不得伪装成 Linux 绿灯。
3. 签名完整运行闭包并绑定测试证明；验证部署者、写者能力、文件 UID/mode 和容器绑定目录。
4. 生成新鲜、加密、本地配对备份，验证账本配对和恢复隔离。旧备份仅作历史资料，不能用来回滚现有余额。
5. 将本地失败演练映射到现有已审核协调发布控制器，确认 fenced/stopped 状态恢复路径；不另开绕过锁的发布通道。
6. 有界 fence / drain。超时保留旧服务，不强杀在途请求。确认 writer quiescence 后才切换。
7. 验证版本 API、前端源常量、静态导出、公开首页入口和 README 状态一致，不能只看某一个版本标签。
8. 少量真实 Codex / Claude / DSH 的三种协议、工具调用、取消与流式终态验收；上游真实拒绝不算网关伪造成功。
9. 服务 ready 且业务链路通过后再解除入口限制；持续观察后才发布 GitHub 正式版本。

## 智汇发布状态与旧记录

发布页的版本来自智汇发布物，不从旧升级器状态恢复 `v1.x` 等历史标记。旧状态及原始日志默认隔离，不读作当前结果，也不自动删除。兼容 URL `DELETE /api/system/update-status` 现在表示管理员显式归档旧展示记录，不再删除当前发布回执。

新发布器状态文件为 `DATA_DIR/zhihui-release-status.json`。当前候选没有执行发布器，也不会生成伪回执。后续经批准集成控制器时，记录 schema `1`、repository `daniei-chen/zhihui-gateway` 和实际 `release_id`；可使用阶段 `candidate_verified`、`backup_verified`、`drained`、`activated`、`ready_verified`、`rolled_back`、`failed`，事件只带时间和阶段，不能写原始命令或密钥。本页只展示匹配回执，不以文件声称代替签名和就绪验证。

旧历史归档只处理 `update-status.json` 与 `update.log`，位置为 `DATA_DIR/release-history/legacy-updater-*`，私有目录和文件并带哈希 manifest。归档需旧状态明确结束，无旧锁且无存活/未知进程；只有旧日志而没有结束状态也拒绝。归档不自动执行，不碰用户数据、配置、授权、密钥、调用/审计日志、钱包和主库。

既有 `/opt/workbuddy-manager`、`workbuddy-web`、`WB_*` 等是兼容标识，不是产品展示名称。保留它们以免改变发布信任链、挂载、权限、启动器或用户偏好。不能为“改名”绕过现有协调发布体系。

## 失败处理

- **停止旧写者前**失败：确认旧服务实际健康后解除围栏；不能把 unknown 当健康。
- **停止旧写者后**失败：先恢复旧发布点/引擎并启动、验证旧服务，最后才解除围栏，避免“没有服务但入口已放开”。
- 新服务健康不确定、旧服务无法恢复或 writer 活性不明：保留围栏和证据，进入人工处理；不伪报回滚成功。
- 只回滚代码、镜像和有明确兼容合同的配置。不恢复业务库，不撤销用户新产生的消费，不复用已泄露内部密钥。

本地模拟器不调用服务器、不具备生产发布能力；最终以现有签名控制器及真实验收为准。
