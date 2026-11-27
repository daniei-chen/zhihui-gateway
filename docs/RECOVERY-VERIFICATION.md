# 受限恢复验收与异地回读证据

恢复检查默认使用私有临时目录；不启动生产服务、不注册 systemd、不执行归档里的代码，也不连接供应商。`deploy/recovery-drill.sh` 是 `restore_drill.py` 的薄入口，必须显式提供输入，已移除旧脚本的固定生产备份、安装目录、凭据目录和端口。

## 证据含义

| 字段 | 能证明的事实 |
|---|---|
| `offsite.readback.verified` | 发布后从接收端重新 fetch 到一个空对象库，收据、对象大小和密文 SHA256 全部一致 |
| `verified_remote` / 公共状态中的 `offsite` | 上述回读通过，且目标为配置允许的独立远端；本机 Git fixture 为 false |
| `data_restore_verified` | 归档收据、全部文件清单/哈希验证通过，声明的 SQLite 文件在临时副本中通过完整性和外键检查 |
| `application_start_verified` | 精确提交、干净工作区且源字节匹配的受信 checkout，以临时恢复数据完成 Manager ASGI lifespan；`/api/healthz` 与 `/readyz` 均返回 200。checkout 包含内嵌平台时，还必须确认真实 `Runtime.startup()` 完成后的 `app.state.rt`；任何越界操作或生命周期关闭失败都使验收失败 |
| `embedded_platform_start_verified` | 受信 checkout 内的真实平台 `Runtime` 已完成启动并挂载；不证明供应商连接或真实凭据可用。最小 ASGI 测试 fixture 会标明 `embedded_platform_required: false`，不能当作完整网关启动证据 |
| `external_services_verified` | 离线探针不连接真实外部依赖，因此保持 false |
| `complete_restore_verified` | 本工具不把局部 Manager/数据验收扩大为整台服务器、边缘入口或全部供应商恢复证明，因此保持 false |
| `offsite_restore_verified` | 本机模拟和远端密文回读都不能证明实际异地系统已经恢复，因此保持 false |

健康状态文件升级为 schema 2。旧的 `offsite: true` 或只有 `verified_remote: true` 的 push/引用检查不能成为新回读证明。新证明绑定同一归档 SHA、密文 SHA、提交、大小及验证时间，超过 26 小时或明显来自未来即失效；仅刷新文件 mtime 不会延长证据有效期。未配置异地渠道仍按原策略不触发异地告警。

## 离线数据验收

已有解密归档时，使用可信收据中的哈希：

```bash
python3 deploy/restore_drill.py \
  --archive /absolute/private/application.tar.gz \
  --archive-sha256 <可信收据中的64位SHA256> \
  --work-dir /absolute/private/new-report-directory
```

加密分片目录包含 `snapshot.json`、`snapshot.part-000.age` 等文件时：

```bash
python3 deploy/restore_drill.py /absolute/private/snapshot \
  --key /absolute/offline/recovery.agekey \
  --work-dir /absolute/private/new-report-directory
```

默认执行完整文件清单和 SQLite 检查。`--skip-sqlite` 只产生 `sampled`，永不产生数据或完整恢复成功。`--restore-sidecar` 是一个显式、可选的源目录写操作；默认不写源目录。

新 `backup_application.py` 基线包在流式写入时生成 `recovery-manifest.json`，包含全部普通文件的字节数、SHA256 和 SQLite 清单，并在建包后回读校验。该格式可直接送入此工具；旧包或外部工具的不同格式必须先核对清单，不猜测、不跳过未知成员。

## 受信应用验收

只有指定受信源码才会启动应用生命周期，归档内的 Python/脚本始终不执行：

```bash
python3 deploy/restore_drill.py \
  --archive /absolute/private/application.tar.gz \
  --archive-sha256 <可信归档SHA256> \
  --trusted-checkout /absolute/reviewed/zhihui-gateway \
  --source-commit <精确40位Git提交> \
  --work-dir /absolute/private/new-report-directory
```

此模式要求 POSIX、精确 Git 提交、无工作区变化；还逐文件比对 Git 对象，防止 `assume-unchanged` 隐藏改动。只容许 UTF-8 的 LF/CRLF checkout 转换。原始状态全部留在源归档，应用得到的是临时副本。

子进程不继承生产 WB/QTG 设置、代理或凭据环境，关闭后台作业。对受信 Python 代码安装文件/SQLite、网络和进程审计约束：只许在恢复临时目录内写，只读受信源码及 Python 运行环境，拒绝网络访问和子进程。另有 1 GiB 地址空间、60 秒 CPU、90 秒墙上时间和文件大小上限。这不是运行敌对原生代码的沙箱；只接受用户已审查的源码，不能把不可信归档作为 checkout。

平台仍经过真实 Manager lifespan 初始化；没有改成跳过平台的 helper 模式。探针显式使用现有安全默认引擎组合 `qoder=off / trae=bff / workbuddy=on / doubaowork=off`，全部 QTG 数据、审计、配置、凭据及运维路径指向演练私有状态目录，后台签到关闭。初始化适配器和 HTTP 客户端不等于已连接上游；网络审计依然拒绝所有连接。缺失生产密钥的状态也不会被替换为已验证凭据。

即使应用捕获了越界读取的错误，验收也会失败。Python 对受信源码对应的未跟踪 `.pyc` 的探测单独计数：缓存仍被拒绝打开，允许解释器回退读取精确提交中的 `.py`；不能用未审查的缓存执行代码，也不能把其它越界数据读取当作正常缓存探测。

应用的最小运行元数据单独处理并计数：允许只读探针自身 `/proc/<本进程PID>/stat`，用于实例启动身份，不允许读取其它进程。时区查询清空系统搜索路径，只从已锁定哈希的 Python `tzdata` 包加载，不开放宿主机时区目录。源码 checkout 没有生成的 `.version` 部署标记时，保留 `FileNotFoundError` 并记录缺失次数，不伪造版本；实际存在但未跟踪的标记、指向受信 `VERSION` 的标记软链接和悬空软链接仍会拒绝并使验收失败。报告的 `runtime_reads`、`missing_release_marker` 与真正的 `blocked_operations` 分别表示这些情况。

早期应用烟测使用基线提交 `181ef4f7d6846a0ccaa8071bccc99ed5caeea2fa` 的独立干净 checkout 与合成数据库，完成真实 ASGI 启停及两个健康端点 200。交付候选须在自己的精确提交上按上述更严格边界重新执行，并以报告中的 `source_commit` 核对；基线结果不能替代候选证明。未提交工作树的诊断烟测明确标记 `diagnostic_only: true`、`exact_commit_verified: false`，也不能替代干净候选验收。这些演练仍不代表生产凭据、Nginx、上游 Go 引擎、真实异地存储或业务恢复点已验收。真实恢复验收应在独立主机/容器及受控网络中继续验证这些外部依赖。

## 资源边界与失败行为

- 分片最多 512 个，密文总量最多 2 GiB；逐块拼接、哈希，不把整份快照保留在内存。
- 解密过程最长 900 秒，POSIX 子进程文件大小受限；磁盘保留至少 256 MiB。
- 解密归档最多 2 GiB、单成员最多 2 GiB、展开总量最多 4 GiB、成员最多 100,000 个；路径及扩展头另受元数据预算限制。
- 路径跳转、重复成员、软/硬链接、设备、稀疏文件和巨型 PAX/GNU 扩展头均拒绝。
- SQLite 在只读不可变临时副本中检查，设置值长度、缓存及执行时间上限。
- 验收失败返回非零，失败报告只含错误类型和非敏感计数；临时解密/恢复文件自动清理。
- 备份生产端不在内存中累积整份发布源码；归档写入前检查成员、总量和剩余磁盘。未验收候选包留在可自动清理的私有工作区，不提前发布为正式备份文件。

远端 Git 发布仍沿用现有密文大小上限与既定目标。启用/更换真正的异地存储、提供离线密钥、取得实际远端恢复证明属于部署和运维动作，不由本次仓库更新自动执行。
