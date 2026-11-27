# C. 运行实况（实测，2026-11-28，Windows 设备）

- 环境：Windows 11（10.0.22631），i5-14400F / 16 核 / 32GB；Python 3.12.10；Node 24.19.0 / npm 11.17.0。
- 仓库：`daniei-chen/zhihui-gateway` main @ `ec87de9`，VERSION 1.6.28。
- 说明：本机是设备（Windows），不是 Linux CI。Linux 门禁只能引用 PR #58 的 CI 结果（下详）；所有本机命令与结果如下。

## C1. 依赖安装

| 步骤 | 命令 | 结果 |
|---|---|---|
| 生产锁文件（原样） | `pip install --dry-run --no-deps -r server/requirements.lock.txt` | **失败**：`Getting requirements to build wheel did not run successfully`（Linux 锁里的 `uvloop` 无 Windows wheel，转源码构建失败）。结论：`server/requirements.lock.txt` 不是跨平台锁。 |
| 测试环境 | `grep -v '^uvloop' server/requirements.lock.txt` → pip install + `pytest==9.2.0` + `ruff==0.17.6` | 成功；34 个锁定包中除 uvloop 外全部装上。CI 用的 `--require-hashes`（Linux x86_64 wheel）在 Windows 上不可用。 |
| 前端 | `cd web && npm ci` | 成功，**93s**，`node_modules` 727MB。 |

## C2. 前端检查

| 命令 | 结果 | 耗时 |
|---|---|---|
| `npm run lint`（eslint .） | **0 errors / 87 warnings**，exit 0 | ~54s |
| `npm run typecheck`（tsc --noEmit） | exit 0 | ~7s |
| `npm test`（node --test lib/*.test.mjs tests/*.test.cjs） | **264 passed**（在门禁内执行） | 见 C4 |
| `npm run build:export` | **exit 0**，web/out **6.1MB**，28 个 Windows RSC 段文件名被规范化（内容未变） | **156s** |

## C3. 启动实测

```bash
WB_DATA_DIR=<tmp> WB_AUTH_DIR=<tmp> WB_ADMIN_PASSWORD='SmokeTest-Pass-2026xQ' \
python -m uvicorn server.main:app --host 127.0.0.1 --port 7899
```

- 首次用弱口令 `admin123` 启动：**被拒**，`RuntimeError: WB_ADMIN_PASSWORD 不符合管理端密码策略：密码至少 12 位`（策略生效）。
- 强口令启动：`Application startup complete`；`/healthz` → **200** `{"service":"zhihui-gateway","upstream_ok":false}`。
- 未构建静态导出时：`/`、`/login` → 404。
- `npm run build:export` 之后：`/` → **200**（9390B HTML），`/login` → **200**；`/_next/*` 由 StaticFiles 托管。

## C4. 全套门禁（`tools/ci_regression.py --require-static`）

```
Ran 6746 tests in 2144.790s          # 35m45s
FAILED (failures=7, errors=9, skipped=104)

历史 pytest 回归：272 passed / 11 skipped   （共 295：10 failed, 2 errors）
前端用例通过 264 例（npm test）
静态检查通过（F821 0 处；F811 0 处）
进程 exit=1
```

汇总文件：`ci-validation-summary.json`（`tests=6746, failures=7, errors=9, skips=104`；`historical_pytest.ok=false`；`frontend.ok=true`；`static.ok=true`）。

**结论：本机 Windows 全量门禁不绿（16 个 unittest failure/error + 12 个 pytest failure/error），但失败几乎全部是平台/环境问题，且 Linux CI（PR #58 head `e9bb293`）的 checks 是 3/3 success。**

### 失败分类（逐条核对过堆栈）

| 类别 | 数量 | 代表用例 | 根因 |
|---|---|---|---|
| 中文 Windows 默认 GBK 解码 | 3 | `test_security_audit_hardening::test_write_boundary_scrubs_stream_errors_metadata_and_index`、`test_release_quality_closure::test_release_requires_reusable_quality_and_same_immutable_checkout`、`test_docker_deploy::test_dockerfile_build_logic_branches` | 读文件/子进程输出未指定 UTF-8（`UnicodeDecodeError: 'gbk' codec`） |
| `fcntl` 在 Windows 不存在 | 3 | `test_portal_closeout`、`test_recovery_closure`、`test_portal_capped_billing`（经 `server/backup_application.py:9`） | 生产模块顶层 `import fcntl`，Windows 上无法 import；而仓库提供 `windows/` 原生启动器，属真实可移植性缺陷 |
| Windows 文件锁导致临时目录清理失败 | 1 | `test_protocol_error_closure.tearDownClass` | `PermissionError: [WinError 32]`（SQLite/owner 文件仍被占用） |
| 滚动发布证据依赖干净工作树 + Windows 时序 | 5 | `test_rolling_stream_evidence`（5 个） | `source.clean=False` 因工作树有未跟踪文件 `NUL`；另有 `ReadTimeout`/`asyncio` 超时。**不是生产缺陷**，是测试对干净树与 POSIX 时序的假设 |
| 文本模式哈希（CRLF） | 1 | `test_version_candidate_provenance::test_same_version_changed_source_invalidates_old_build_claims` | 期望 LF 归一化哈希，Windows 文本写入产出 CRLF |
| 亚秒级时序断言 | 1 | `test_runtime_ownership_memory::test_slow_writer_and_rejection_flood_have_bounded_request_owners` | 断言 `<1s`，实测 1.125s（Windows 调度） |
| `qtgcore` 路径判定 | 9（pytest） | `test_platform_integration_guards`（9 个参数化） | `/opt/qoder-hub/...` 在 Windows 上 `Path.is_absolute()==False`，`qtgcore/config.py:69` 抛 `ValueError` |
| 静态产物缺失 | 1（pytest） | `test_workbench_registration::test_full_application_registration_login_and_single_default_key` | 当时未建 `web/out`，`/portal/` 404；构建后**重跑通过**（1 passed） |

### 环境性说明

- 工作树里有一个未跟踪的 `NUL` 文件（Windows 设备名，`git status` 显示 `?? NUL`）。它会让 `test_rolling_stream_evidence` 的 `clean` 断言失败；这不是代码问题，但会在任何 Windows 检出上复现。
- 本机没有 Linux 环境，也未安装浏览器（Playwright Chromium 未下载），`npm run test:browser` 未执行；CI 的 frontend job 会装 lockfile 绑定的 Chromium 并执行。
- 云端没有可用额度，无法另开 Linux 机器复跑；Linux 侧结论只引用 PR #58 的 CI（3/3 success）。

## C5. CI 与本地对照

| 项 | CI（.github/workflows/quality.yml） | 本机能否复现 |
|---|---|---|
| Python 依赖 | `--require-hashes`（Linux x86_64 wheel） | 否（uvloop 无 Windows wheel） |
| 后端套件 | `tools/ci_regression.py --require-static`，网络命名空间隔离 `unshare` | 部分：能跑，但无网络隔离，且平台失败如上 |
| 前端 | `npm ci` → eslint → tsc → `npm test` → `build:export` → Playwright 浏览器验收 → `npm audit --audit-level=high` | 前四项可（264 passed）；浏览器验收与 npm audit 未跑 |
| 静态产物绑定 | `tools/ci_artifacts.py create/verify`，产物跨 job 复用 | 未跑（需要 CI 产物） |
| 运行时镜像 | Docker build + trivy HIGH/CRITICAL 门禁 | 未跑（本机未构建镜像） |
| 发布 | `release.yml` 复用同一提交的 quality 产物 | 未跑 |

**可复现的核心结论**：代码能构建、能启动、前端 264 例与后端 6746 例能跑；Linux CI 在 PR #58 是绿的；Windows 本机有 28 个平台相关 failure/error，其中 3 个暴露真实可移植性缺陷（`fcntl`、GBK 假设、`qtgcore` 绝对路径判定）。
