# 根目录持续集成候选

本轮新增 `.github/workflows/audit-checks.yml`。此前继承的 workflow 位于
`manager/.github/workflows/`，不会自动作为此仓库根目录的 GitHub Actions 入口。
新文件是可审查的候选；本地静态验证不证明 GitHub 云端成功，实际提交的运行状态
以对应 GitHub Actions 回执为准。工作流没有发布、部署、写仓库权限、生产密钥、
SSH 或真实供应商调用。

## 执行合同

触发条件为面向 `main` 的 pull request、`main` push 和手动 workflow_dispatch。
权限只有 `contents: read`，checkout 不持久保存凭据，未使用 pull_request_target。
执行器为 GitHub 托管的 `ubuntu-24.04`，Python 3.12、Node 24；工作流不设置
continue-on-error、不改原生 PID/proc/PDEATHSIG 断言、不替换已有 skip 规则。

1. 使用现有生产 `manager/server/requirements.hashes.txt`、现有 QA
   `tests/linux_lab/requirements-dev.hashes.txt` 和新增 QA 专用
   `tests/requirements-audit.hashes.txt`，统一从官方 PyPI 安装，要求 wheel、
   精确版本与 SHA-256，并执行 pip check。没有修改生产依赖。
2. 在 `src/three-platform/web` 运行 npm ci，以 package-lock 为安装依据。
   运行 npm test、npm run typecheck、npm run lint、npm run build:export。
   typecheck 前先调用本地已锁版本的 next typegen，生成 fresh checkout 的路由类型；
   不依赖审计机已有的 `.next` 文件。[Next 官方 CLI 合同](https://nextjs.org/docs/app/api-reference/cli/next#next-typegen-options)
3. 运行完整 `tests/zhihui_harness/run_tests.py --target manager-assets
   --require-built-web --keep`，不选择单文件、不过滤失败、不放宽隔离。
4. 运行根目录 `python -B -m unittest discover -s tests -p 'test_*.py' -v`。

可独立执行的检查在其依赖已安装后仍会运行，方便同一次 CI 收集多个问题；任何失败
仍使整个 job 失败。Bash 管道使用 `set -euo pipefail`，tee 不吞掉原始命令退出码。

## 回执

收集与上传步骤均使用 always()。日志、完整 stats/JUnit、view-before/view-after、
isolation-report 和 run-manifest 汇入 `evidence/ci/`，保留 14 天。stats 与 JUnit
按字节复制，保留 harness 的全部计数和 test ID，不重新生成“通过”摘要。
收集器只接受 `iso_root` 位于本 job 专用临时根的回执，避免把仓库内历史结果冒充
本轮结果。即使依赖安装阶段失败、尚未产生测试回执，也保留 collection.json 与已有
日志；collection.json 明确“收集完成不等于测试成功”。平台中断或 artifact 服务失败
仍须看 GitHub job 状态，不能凭 workflow 文件存在声称证据已经上传。

## 固定的官方 Action 提交

2026-11-05 从各 Action 官方 release 页面进入对应 commit 页面确认完整 SHA，
并下载同一提交的 action.yml 核对输入与 Node 24 runtime。记录保存在
`evidence/ci-action-pins.json`；没有仅凭可移动的 major tag 选择代码。

| Action | 发布 | 完整 commit |
| --- | --- | --- |
| actions/checkout | [v6.1.0](https://github.com/actions/checkout/releases/tag/v6.1.0) | [d23441a48e516b6c34aea4fa41551a30e30af803](https://github.com/actions/checkout/commit/d23441a48e516b6c34aea4fa41551a30e30af803) |
| actions/setup-python | [v6.3.0](https://github.com/actions/setup-python/releases/tag/v6.3.0) | [ece7cb06caefa5fff74198d8649806c4678c61a1](https://github.com/actions/setup-python/commit/ece7cb06caefa5fff74198d8649806c4678c61a1) |
| actions/setup-node | [v6.5.0](https://github.com/actions/setup-node/releases/tag/v6.5.0) | [249970729cb0ef3589644e2896645e5dc5ba9c38](https://github.com/actions/setup-node/commit/249970729cb0ef3589644e2896645e5dc5ba9c38) |
| actions/upload-artifact | [v7.0.2](https://github.com/actions/upload-artifact/releases/tag/v7.0.2) | [cf430e030ddbb5b0abf93d22962f4752f3646cd9](https://github.com/actions/upload-artifact/commit/cf430e030ddbb5b0abf93d22962f4752f3646cd9) |

## Python 锁的官方核验

现有 27 个生产版本、7 个 QA 版本和新增 3 个 QA 版本，共 37 个版本均从官方 PyPI
version JSON 核对到 CPython 3.12 / Linux x86_64 兼容、未撤销的 wheel。
原 QA 的 pytest 9.1.1 及其摘要可取得，无需改成审计早期临时环境中的 9.2.0。
新增文件只包含 cryptography 50.0.2、cffi 2.1.1、pycparser 3.12：

- [cryptography 官方版本元数据](https://pypi.org/pypi/cryptography/50.0.2/json)
- [cffi 官方版本元数据](https://pypi.org/pypi/cffi/2.1.1/json)
- [pycparser 官方版本元数据](https://pypi.org/pypi/pycparser/3.12/json)
- [pytest 官方版本元数据](https://pypi.org/pypi/pytest/9.1.1/json)

随后实际执行三个锁合并的 `pip download --require-hashes --only-binary=:all:`，
从官方 index 完整解析并下载 37 个 wheel，退出 0。没有安装到系统或修改旧虚拟环境。
逐版本 URL/文件名/摘要见 `evidence/ci-pypi-lock-verification.json`，下载日志及文件
核验见 `evidence/ci-lock-download.log` 和 `evidence/ci-wheel-download-receipt.json`。

主审随后使用这份 wheelhouse，以三个 hash 锁重新准备最终验证环境，pip check 通过，
根工具初次 73/73 通过，末轮历史脚本拒绝修复后 75/75 通过；最终完整 Manager 结果以主报告的新回执为准。早期基线、微基准和
定向回归仍准确记录其 pytest 9.2.0 环境，不事后改写历史。

## 本地验证与尚待完成的门槛

本次没有为检查 YAML 再跑整套业务测试。完成了：

- PyYAML 解析、actionlint 1.7.12 检查通过；后者从官方发布下载并比对其发布摘要，
  工具来源记录在 `evidence/ci-actionlint-provenance.json`。
- 全部 10 个 Bash run 块 `bash -n` 通过；确认所引用的目录、锁和 npm scripts 存在，
  Action 输入和 GitHub context 使用位置匹配官方合同。
- 直接执行收集器的两个临时合成场景：测试前失败仍有收集说明；本轮回执与历史回执
  并存时只复制本轮完整证据。未使用真实用户数据或伪造云 CI 成功。

结果及 workflow SHA 在 `evidence/ci-static-validation.json`。本地 YAML 和下载通过
不会自动改变仓库设置。Windows 的 registry、ACL、PowerShell、原生进程生命周期
尚需独立 Windows runner 门槛，本 workflow 不声称已覆盖它们。

## 实际 GitHub 执行结果

[Run 42030666659](https://github.com/daniei-chen/zhihui-gateway/actions/runs/42030666659)
的 attempt 1 已完成，结论为 **success**；全部 19 个 job 步骤成功，包括回执上传。
候选 head 是 `a6664aeeb5567f9cbcb0ca10d45a381662622962`，GitHub 临时合并提交
`32ad580c1d455243d64672f424c9dbd8e8275c35` 的 tree 与候选相同，均为
`1076a402dda2e7322a9954d50d5313e44b94bf63`。

- Python 3.12.16 / Node 24.22.2 / npm 11.19.1；三个 hash 锁 37 个版本逐项匹配，
  pip check 成功。
- 前端 226/226，typecheck、lint 和静态导出成功；lint 0 errors / 79 warnings。
- 完整 Manager 4,600 项：**4,591 passed、0 failed、0 errors、9 skipped**，
  另 3,219 subtests passed。三个本地 proc/PDEATHSIG 失败用例在实际 JUnit 中
  均 PASS，未改为 skip；9 个既有 skip 的 ID 和原因与本地逐项一致。
- 根工具 75/75 通过；测试视图前后摘要一致。

Artifact 13416357861 已下载并比对 GitHub digest；原始 ZIP、JUnit、stats、视图、
隔离回执、依赖清单与 job 日志已归档至 `evidence/native-ci/`，不依赖 14 天后失效
的下载地址。该成功不消除 QTG 隔离夹具、PG 恢复、Windows 和私有引擎等剩余门槛。
此云端结果只通过 docs/audits 下的补充提交记录，没有改业务、测试或 workflow。
仓库分支保护设置未被修改。
