# 复现说明

本目录记录对 `4c6b4cb8cb790698e0753909745dfe8a7c78dd9f`（v0.0.7）的审计。
所有性能数字来自临时合成数据和本机执行，不是生产容量承诺。
完整问题和剩余风险见 [AUDIT.zh-CN.md](AUDIT.zh-CN.md)。

## 环境与隔离

审计运行环境为 Python 3.12.14、Node 24.22.1、npm 11.9.0、SQLite 3.53.1。
Manager 生产依赖按 `manager/server/requirements.lock.txt` 安装。
早期基线、定向测试和检查点使用 pytest 9.2.0，测试锁声明为 9.1.1；这项历史差异
保留在回执中。最终完整验收前，已按官方 wheel 摘要重装全部 37 个生产/QA 锁定
依赖，包含 pytest 9.1.1 和 pytest-asyncio 1.4.0，并通过 pip check。
根工具安全实验使用 cryptography 50.0.2，列入独立 QA hash 锁，不增加应用生产依赖。

Ubuntu x86_64 / CPython 3.12 的确切安装命令：

```bash
python -m pip --isolated install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r manager/server/requirements.hashes.txt -r tests/linux_lab/requirements-dev.hashes.txt -r tests/requirements-audit.hashes.txt
python -m pip check
```

准备两个独立 checkout：一个固定在上述基线，另一个使用本审计分支。
不要在现有业务安装目录、真实账号目录或生产数据库旁运行测试。
Manager 测试使用仓库自己的隔离入口；它复制测试视图并限制网络和文件写入。

```bash
python -X utf8 tests/zhihui_harness/run_tests.py --target manager-assets --require-built-web
python -B -m unittest discover -s tests -p 'test_*.py' -v
```

最终完整运行另外启用逐例输出和诊断用栈转储，未过滤用例：

```bash
PYTEST_ADDOPTS='-o faulthandler_timeout=60' python -X utf8 tests/zhihui_harness/run_tests.py --target manager-assets --require-built-web --keep --verbose
```

该诊断设置在单项超过 60 秒时打印线程栈，本身不改变断言、不终止测试。
完整命令、环境和状态保存在 `evidence/final-run-command.json` 与验证汇总。

第二条只执行根目录的离线工具测试，不能代替第一条 Manager 测试。
执行第一条前，在 `src/three-platform/web` 安装锁定依赖并构建静态导出。
前端门禁在该目录运行：

```bash
npm test
npx --no-install next typegen
npm run typecheck
npm run lint
npm run build:export
npm audit --omit=dev --json
npm audit --json
```

`site/home-source` 是另一套历史主页源工程，其 `check/build` 因公开树缺少
`public/scripts/upgrade.js` 及 `public/` 输入而失败。不能用当前管理台构建成功
代替该工程的结果，也不能用临时假资源制造成功。

## 本机性能实验

在审计分支根目录运行，参数为独立基线 checkout 的绝对路径：

```bash
python tests/benchmarks/audit_hotpaths.py --baseline /absolute/path/to/baseline
python tests/benchmarks/gateway_resources.py --baseline /absolute/path/to/baseline
node tests/benchmarks/frontend_sse.mjs /absolute/path/to/baseline
node tests/benchmarks/frontend_sse.mjs /absolute/path/to/audit-checkout
```

前两个脚本交替执行 before/after，使用临时 SQLite 或纯函数输入。
第三、四条复现前端解析场景；原始数据各取 5 次中位数，未运行真实浏览器。
短于数毫秒的测量存在调度噪声，多代理共用主机也会影响数字。
ASGI 单字节分片实验中 CPU 变慢而内存下降，这是明确的代价。

## 安全反例与逐文件清单

`reproduce_ops_security.py` 用合成文件、临时目录和新测试 Ed25519 key
对照基线与当前文件，重现十类拒绝/接受差异。需要本地基线 git 对象。
输出到标准输出；只有显式给 `--output` 才写报告文件。

```bash
python docs/audits/2026-11-05/reproduce_ops_security.py --output /tmp/ops-audit.json
python docs/audits/2026-11-05/reproduce_extra_security.py --output /tmp/extra-audit.json
python tools/audit_control_plane.py --out /tmp/control-plane.json
python docs/audits/2026-11-05/source_checks.py --output /tmp/source-checks.json
```

第二个安全脚本明确从基线 Git 对象重现 dailyplan、keyimport、notify 的修前行为；
PG 恢复反例使用当前源代码与临时 SQLite transport。其输出不能当作修后回归已经执行。
修后行为以命名的回归文件及完整 harness 结果为准。

`coverage.tsv` 逐项区分全文人工审读、重点路径审读、仅自动检查、仅清点，
并记录基线/候选 SHA256。扫描命中、编译成功、测试通过都不等于人工逐行阅读。
控制面清单描述源码可见的鉴权机制；没有 `Depends` 不能直接推断为公开接口。

## 必须在匹配环境补验的门禁

当前执行环境中 `/proc/self/stat` 与数字 PID 路径不一致；已回收的自有子进程
在该视图中仍可能显示运行。随后已在 GitHub 原生 Linux runner 完成独立复验：
4,600 项中 4,591 通过、9 个既有跳过、0 失败，PID、zombie 和 PDEATHSIG 三项
均在真实 JUnit 中 PASS。回执见 `evidence/native-ci/`；本地失败历史仍保留。
未修改这些断言、未为本次审计新增 skip。
Windows 注册表、原生进程关闭和 PowerShell 安装/更新需要 Windows runner。
真实 PostgreSQL 恢复、真实 age 解密、离机全量恢复、私有完整引擎以及供应商
首 token 延迟、token/s、生产 P95/P99 不在本地合成测试的证明范围内。

根 CI 候选及供应链验证见 [CI.md](CI.md)。本地 YAML/actionlint/锁依赖验证不能代替
GitHub Actions 对实际提交的原生执行；不要将静态验收写成云 CI 已通过。
