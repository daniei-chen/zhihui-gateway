# 隔离测试环境与质量门禁（T02 + K48）

本目录是**测试基础设施**，不属于业务代码。目标：在无生产凭据、无外网、无 Docker 的
Windows 本机上，可重复地跑出 Manager / QTG 的真实测试结果，并且**绝不**让测试写到
真实数据目录、`/opt` 或生产资产上。

对应审计要求：`02-工程化迭代计划.md` §4 T02、`01-问题台账.md` K48、`04-全量核验范围
与验收矩阵.md` §7、`06-执行模型交接与防误操作手册.md` §7/§10。

## 快速开始

2026-10-24 接管更新：每轮证据写入 `evidence/runs/<run_id>/`，`latest-*.json`
只是指针，可能指向专项。汇报全量结果须检查 `scope=full` 与实际 `pytest_args`。
`--pytest-arg` 追加参数，不替换默认目录；选定文件使用新的 `--test-path`：

```powershell
python -X utf8 tests/zhihui_harness/run_tests.py --target manager-assets --require-built-web --keep
python -X utf8 tests/zhihui_harness/run_tests.py --target manager-assets --require-built-web --keep --test-path=server/tests/test_metrics_auth.py
```

JUnit 先写隔离根，pytest 结束并恢复进程级守卫后再复制到证据目录。
`view-before.json/view-after.json` 记录目录、链接与文件哈希；若视图变化，即使用例通过，
运行器也返回验证失败 4，并列出变更路径。不要排除有问题的文件来消除该门禁。
独立 VM 的安装、全量回归和真实 Nginx 检查见 `linux_lab/`；此目录的脚本都限制本地实验 VM，
不能复制到生产执行。当前完整验收状态以 `../tasks/status.json` 与检查点为准。

```powershell
# Manager 全量（部署副本原样：server/ 源码 + 测试，无前端资产）
.\tests\run-isolated.ps1

# Manager + 前端/dev/docs 资产（诊断视图，用于区分“缺资产”与真实失败）
.\tests\run-isolated.ps1 -Target manager-assets

# QTG 控制面
.\tests\run-isolated.ps1 -Target qtg

# 只验证可收集（不跑用例，1~2 秒）
.\tests\run-isolated.ps1 -CollectOnly

# 全部目标的归因汇总
python tests\zhihui_harness\analyze_results.py `
  --stats evidence\stats-manager.json `
  --assets-stats evidence\stats-manager-assets.json
```

PowerShell 入口只负责日志留档与退出码透传；真正干活的是
`tests/zhihui_harness/run_tests.py`（跨平台 Python 入口）。

## 目录结构

| 路径 | 职责 |
|---|---|
| `run-isolated.ps1` | 一键入口：前置安全校验 → 调 Python 运行器 → 日志 tee 到 `evidence/` |
| `gate.ps1` | 质量门禁：语法编译 / 敏感值扫描 / 假 provider 冒烟 / 测试可收集 |
| `zhihui_harness/isolation.py` | 临时根、`WB_*` 环境变量、受保护路径判定、只读测试视图 |
| `zhihui_harness/pytest_plugin.py` | pytest 插件：无隔离变量拒绝运行、挂 `dev` 包、拦外网、拦生产路径写 |
| `zhihui_harness/run_tests.py` | 运行器：建视图 → 起假 provider → 跑 pytest → 写 junit/stats |
| `zhihui_harness/analyze_results.py` | 归因：环境问题 / 平台差异 / 真实缺陷候选，输出 CSV+MD |
| `zhihui_harness/gate_checks.py` | 门禁的 Python 检查项（`ast.parse` 语法、敏感值正则） |
| `fake_provider/` | 零依赖 OpenAI 兼容假上游（见下） |
| `RESULTS.md` | 一次真实运行的完整结果与归因 |
| `PROPOSALS.md` | 必须改业务源码才能验证时，记录为建议而不是直接改 |

## 隔离保证（可核对）

1. **全新临时根**：每次运行在 `%TEMP%\zhihui-iso-<时间戳>-<随机>` 下创建
   `db/ data/ auths/ config/ logs/ locks/ tmp/ home/ cache/ view/ artifacts/`；
   `-Keep` 可保留诊断，否则结束整树删除。
2. **业务路径全部重定向**：`WB_DATA_DIR`、`WB_DB`、`WB_USERS_FILE`、`WB_AUTH_DIR`、
   `WB_UPSTREAM_CONFIG`、`WB_UPSTREAM_DIR`、`WB_STATIC_DIR`、`TMP/TEMP`、`HOME/
   USERPROFILE`、`XDG_*` 均指向临时根；`config.py` 里默认的
   `/opt/workbuddy2api/auths` 与 `/opt/.../config.json` 因此不会被触碰。
3. **源码只读**：需要补资产时把源码**复制**进临时视图再跑；`manager/`、`qtg/` 里
   不落任何字节（运行前后可用 `git status` 核对）。
4. **强制隔离**：pytest 插件在缺少 `ZH_ISO_ROOT` 时直接 `exit 3`，禁止绕过运行器
   直接 `pytest`。
5. **网络**：默认 `block`，只放行 `127.0.0.1/::1/localhost`；非回环的
   `connect/getaddrinfo/create_connection` 一律抛 `NetworkBlocked` 并计数。
   假 provider 因此可用，真实上游不可达。
6. **路径守卫**：隔离根之外的系统目录（`/opt`、`/var`、`/etc`、`/root`、`/tmp`、
   `/home`）与生产资产标记（`workbuddy-manager`、`workbuddy2api`、`zhihui-prod`）
   写入即抛 `PermissionError`；隔离根内的写入一律放行（测试要在临时目录里模拟
   部署包）。隔离根外的其它写入只记录不阻断，报告里可核对。
7. **不写 `.pyc`**：`PYTHONDONTWRITEBYTECODE=1`，视图里也不留 `__pycache__`。

## 假 provider（`fake_provider/`）

标准库实现，无第三方依赖。监听 `127.0.0.1`，端口自动分配。

| 模式 | 语义 |
|---|---|
| `normal` | 正常成功：非流式 JSON / 流式多块 SSE + 末帧 usage |
| `delay_first` | 流式延迟首包（`--delay-first`，默认 2s） |
| `error_400/429/500/503` | 对应状态码 + OpenAI 风格 error JSON（429 带 `Retry-After`） |
| `broken_stream` | 发 2 帧后不发终止 chunk 直接关连接（半截关闭） |
| `multiline_sse` | 每个事件拆成多行 `data:` |
| `crlf_sse` | CRLF 行尾 + UTF-8 中文（跨块解码） |
| `cancel` | 客户端取消：发 N 帧（默认 3，`X-Fake-Cancel-After` 可覆盖）后记录 `cancelled` 并断开 |
| `missing_usage` | 成功但 usage 缺失 |
| `duplicate_chunk` | 同一 delta 帧重复发两次 |
| `huge_response` | 单块 8MB 响应 |
| `slow_headers` | 延迟返回响应头 |

- 模式选择：`model` 后缀 `:mode` > 请求头 `X-Fake-Mode` > 查询串 `?mode=` > 启动参数。
- 计数：`GET /__stats`（total/completed/cancelled/by_path/by_mode/bytes），`POST /__reset`。
- 冒烟：`python tests/fake_provider/smoke.py`（`--quick` 跳过 8MB/20MB 两项）。
- 说明：`cancel` 模式是**确定性模拟**。Windows 上客户端 `close()` 后 loopback 缓冲
  仍可写入很久，`select` 看到 FIN / 写超时都不可靠（已实测），所以用“发 N 帧后判定
  取消并断开”的固定语义；同时保留写失败/写超时作为真实取消信号。**不要**把它当成
  “TCP FIN 检测”来引用。

## 已知限制（不要当成通过）

- **视图补资产是诊断手段**：`manager-assets` 把 `src/three-platform` 的 `web/dev/docs/
  windows` 复制进临时视图，它们与 `manager/server` **不是同一个 commit**（前者是
  b53f36e 完整源码树，后者是线上部署副本）。补资产后通过的用例只能证明“失败源于
  执行树缺资产”，不能证明前端与后端版本匹配。
- 隔离网络策略是**进程内 socket 拦截**，不是内核防火墙；它覆盖 `socket.connect/
  getaddrinfo/create_connection`，不覆盖 `subprocess` 里另起的进程（例如真跑
  `git`/`pip`）。本仓库测试没有这种真出网路径，但换仓库要重新评估。
- Windows 没有 Docker / systemd / POSIX 权限位 / `bash`，相关用例要么跳过要么按
  「平台差异」归因，见 `RESULTS.md`。
