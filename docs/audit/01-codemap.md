# B 节：代码地图与依赖（zhihui-gateway @ ec87de9）

- **范围**：仅 `daniei-chen/zhihui-gateway`，`main` @ `ec87de9`（VERSION 1.6.28），只读。
- **方法**：`os.walk` 统计（跳过 `.git/node_modules/__pycache__/.next/out`，**二进制不计行**）；用 Python `ast` 建全仓导入图（203 个非测试源码模块、521 个测试文件）反推"谁引用谁"；`difflib` + `ast` 方法集算重复度；PyPI / npm registry / GHSA 查版本与 CVE。**没有一条结论来自 README**。
- **时间**：2026-11-27 16:42Z–17:20Z（设备 PC-20260816OWHB，Windows + Git Bash，Python 3.12 venv，node 24.19.0）。
- **复现总入口**（工作目录 = 仓库根）：
  ```bash
  # 目录/行数（等价脚本见各节）
  /d/AI/capy/work/venvs/zhihui-gateway/Scripts/python.exe - <<'PY'
  import os;T={'.py','.ts','.tsx','.js','.mjs','.cjs','.cs','.sh','.ps1','.md','.json','.yml','.css','.html'}
  for d in ['server','web','client','windows','deploy','ops','tools','site','dev']:
      n=l=0
      for dp,dn,fn in os.walk(d):
          dn[:]=[x for x in dn if x not in {'.git','node_modules','__pycache__','.next','out'}]
          for f in fn:
              n+=1
              if os.path.splitext(f)[1].lower() in T:
                  l+=open(os.path.join(dp,f),'rb').read().count(b'\n')
      print(d,n,l)
  PY
  ```

---

## 0. 数字速览（全部实测）

| 指标 | 值 | 命令/证据 |
|---|---|---|
| 仓库 tracked 文件 | **2,060**（`git ls-files \| wc -l`），pack 47.28 MiB | `git count-objects -vH` |
| server/ Python | **194,484 行 / 724 文件** | 上表 |
| 其中 `server/tests` | **112,448 行 / 521 文件**（占 server Python 57.8%） | 同上 |
| 其中非测试代码 | **82,036 行 / 203 模块** | 194,484−112,448 |
| 测试:代码 行比 | **1.37 : 1** | 112,448 / 82,036 |
| server 测试用例 | `def test_*` **6,206** 个，`class Test` 4 个，`subTest` 437 处 | `grep -rho "^\s*def test_" server/tests \| wc -l` |
| web 文本行 | 70,637（tsx 28,941 / json 23,515 / ts 7,849） | 上表 |
| web 测试 | 47 文件（`lib/*.test.mjs` 29 + `tests/*.test.cjs` 18），`test()` 调用 **226** 处 | §3 |
| Python 直接依赖 | lock 27 个 pin，代码真正 import 的第三方顶层模块 **10 个** | §5 |
| 前端直接依赖 | `dependencies` 43 + `devDependencies` 12 = 55 | §5 |

---

## 1. 顶层目录 → 职责 → 文件数/行数

职责从代码/脚本内容反推，行数为**文本行**（不含 png/jpg/glb 等二进制；二进制文件数列在"bin"列）。

| 目录 | 职责（证据） | 文件 | 文本行 | 主要构成 | bin |
|---|---|---|---|---|---|
| `server/` | FastAPI 网关本体。生产入口 `Dockerfile:166` → `uvicorn server.main:app`；可选 systemd 入口 `server/serve.py:3`（`python -m server.serve`） | 740 | 208,159 | .py 194,484 / .md 11,124 / .json 2,490 | 1 |
| `web/` | Next.js 16.4.5 控制台 + 静态导出（`web/package.json` scripts：`build:export` 走 `NEXT_OUTPUT_EXPORT=1`，产物 `web/out/` 供 gateway 静态托管） | 276 | 70,637 | .tsx 28,941 / .json 23,515（其中 i18n 13,701）/ .ts 7,849 | 17 |
| `site/` | **内容寻址**的首页/登录页资产树，不是构建产物（无 `_next/`）：`site/home/r-<hash>/` 四个不可变版本，`site/home-import.json` 绑定来源+哈希，`site/home-source/` 是可编辑源码 | 399 | 72,734 | .js 55,961 / .css 4,741 / .json 3,493 | 166 |
| `deploy/` | Linux 生产发布/更新/回滚/签名/nginx/systemd。58 个顶层条目；`deploy/release.py`(35 KB)、`deploy/update.py`(94 KB)、`deploy/healthcheck.sh`(34 KB) | 65 | 12,858 | .py 7,312 / .sh 3,664 / .md 1,204 | 2 |
| `dev/` | 一次性诊断脚本 + **提交进库的截图证据**：96 个顶层条目，`.shots-*` 目录里 86 个二进制（最大 776 KB） | 197 | 17,862 | .py 10,096 / .mjs 5,849 / .ps1 1,325 | 86 |
| `ops/` | 运行期运维层（`ops/README.md:3` "不属于上游项目、但网关运行必需"）：`wb-gateway-alert.py` 巡检告警、`container-maintenance/`、`writer-guard/`、`healthcheck.py` | 40 | 6,826 | .py 6,430 | 16 |
| `windows/` | **第二条安装路径**：Windows 原生部署（非 Docker）。`windows/start.cmd`/`stop`/`update`/`service-tools.ps1` + `windows/launcher/src` C# 启动器 | 26 | 4,530 | .cs 2,653 / .ps1 1,741 | 0 |
| `tools/` | 仓库级门禁与发布工具：`ci_regression.py`（枚举全部测试模块）、`validate.py`、`check_reviewed_hashes.py`、`release_policy.py` | 12 | 2,033 | .py 2,001 | 0 |
| `client/` | 单个可选 helper：`client/zhihui_upload.py`（29 行，JSON gzip 上传策略），**产品代码零引用**（§4 S10） | 1 | 29 | .py | 0 |
| `docs/` | 275 个文件/31,547 行设计文档与历史审计（md 30,010 行） | 275 | 31,547 | .md | 26 |
| `fork-tools/` | 派生版打包脚本：`build_release.sh`（rsync server/、web/out/、deploy/ → 签名），`apply_*` 补丁 | 8 | 731 | .sh 471 / .py 260 | 0 |
| `.github/` | CI：`release.yml`(辅助行数最大)、门禁 | 6 | 1,114 | .yml | 0 |
| 根文件 | 15 个 / 12,288 行；`CHANGELOG.md` 523,470 B（与 `server/CHANGELOG.md` **字节相同**，§4 S3）、`docker-compose.yml` 6,975 B、`fork-manifest.json` | 15 | 12,288 | — | 0 |

---

## 2. `server/` 模块分组与体量

### 2.1 分组总计（非测试代码）

| 分组 | 文件 | 行数 | 说明 |
|---|---|---|---|
| 顶层单文件（`server/*.py`） | 101 | **33,034** | 按领域切一个文件：`db.py` 2,207、`main.py` 1,951、`observability.py` 1,702、`operational.py` 1,439、`security.py` 1,250、`budget.py` 862、`ledger*.py` 1,572（ledger+ledgerreplay+ledgerbackfill） |
| `server/routers/` | 27 | **16,495** | HTTP 面：`gateway.py` 3,374、`accounts.py` 2,884、`responses.py` 2,222、`anthropic.py` 1,715、`stats.py` 1,019 |
| `server/qtgcore/` | 40 | **16,205** | **第二个服务**（原独立 qtg 控制面）被折进本仓库：`routers/compat.py` 1,777、`adapters/workbuddy.py` 1,383、`service.py` 1,215；条件挂载见 `server/main.py:199-207` |
| `server/services/` | 33 | **14,277** | 业务服务：`wb2api.py` 2,323、`tencent.py` 1,788、`keyimport.py` 1,284、`updater.py` 1,089、`pgsync.py` 929 |
| `server/deploy/` | 1 | **1,941** | `update.py`：CI 从 `deploy/update.py` 复制进来的**字节副本**（§4 S1） |
| `server/tools/` | 1 | 84 | `platform_selfcheck.py`（无 importer） |
| `server/tests/` | 521 | 112,448 | — |

### 2.2 最大的 20 个模块（行数 / 直接引用它的 prod 模块数 / 直接 import 它的测试文件数）

测试覆盖列用 AST 导入图统计，只算"该测试文件 import 了此模块"，是**引用面**而非行覆盖率。

| # | 模块 | 行 | prod 引用 | 测试文件 |
|---|---|---|---|---|
| 1 | `server/routers/gateway.py` | 3,374 | 8 | 72 |
| 2 | `server/routers/accounts.py` | 2,884 | 3 | 33 |
| 3 | `server/services/wb2api.py` | 2,323 | 14 | 34 |
| 4 | `server/routers/responses.py` | 2,222 | 2 | 21 |
| 5 | `server/db.py` | 2,207 | 58 | 183 |
| 6 | `server/main.py` | 1,951 | 1（`serve.py`） | 110 |
| 7 | `server/deploy/update.py` | 1,941 | **0** | **0**（CLI 脚本，见 S1） |
| 8 | `server/services/tencent.py` | 1,788 | 10 | 28 |
| 9 | `server/qtgcore/routers/compat.py` | 1,777 | 3 | 10 |
| 10 | `server/routers/anthropic.py` | 1,715 | 2 | 20 |
| 11 | `server/observability.py` | 1,702 | 16 | 60 |
| 12 | `server/credit_watch.py` | 1,451 | 4 | 3 |
| 13 | `server/operational.py` | 1,439 | 6 | 40 |
| 14 | `server/qtgcore/adapters/workbuddy.py` | 1,383 | **0** | 4 |
| 15 | `server/services/keyimport.py` | 1,284 | 1 | 2 |
| 16 | `server/security.py` | 1,250 | 29 | 89 |
| 17 | `server/qtgcore/service.py` | 1,215 | 6 | 7 |
| 18 | `server/services/updater.py` | 1,089 | 6 | 20 |
| 19 | `server/routers/stats.py` | 1,019 | 1 | 10 |
| 20 | `server/services/pgsync.py` | 929 | 2 | 2 |

复现：`wc -l` 见 §2.1；引用数用 `ast` 建图（脚本见文末附录 A）。

### 2.3 完全无测试引用的模块（20 / 203 = 10%）

按行数降序，`prod 引用 = 0` 的另标 **死模块**：

| 模块 | 行 | prod 引用 | 备注 |
|---|---|---|---|
| `server/deploy/update.py` | 1,941 | 0 | CLI，被 `deploy/README.md:385`、`docker compose exec … /app/deploy/update.py` 调用 |
| `server/qtgcore/store.py` | 513 | 2 | |
| `server/qtgcore/identity.py` | 506 | 8 | 只在 `main.py:782` 迟到导入 |
| `server/qtgcore/capabilities.py` | 416 | **0** | 死模块（S7） |
| `server/qtgcore/routeaudit.py` | 321 | **0** | 死模块，仅自述 `python -m qtg.routeaudit`（本仓库无 `qtg` 包） |
| `server/qtgcore/adapters/qoder.py` | 151 | **0** | 死模块（`qoder2.py` 才是活的） |
| `server/qtgcore/metrics.py` | 132 | 4 | |
| `server/qtgcore/adapters/trae.py` | 128 | **0** | 死模块（`trae2.py` 才是活的） |
| `server/schema_compat.py` | 111 | 1 | |
| `server/routers/models.py` | 111 | 1 | |
| `server/tools/platform_selfcheck.py` | 84 | **0** | 死模块 |
| `server/qtgcore/model_map.py` | 79 | 1 | |
| `server/routers/pgsync.py` | 72 | 1 | |
| `server/services/cooling.py` | 64 | 1 | |
| `server/qtgcore/manager_session.py` | 59 | 1 | |
| `server/userstore.py` | 57 | 2 | |
| `server/routers/engine.py` | 56 | 1 | |
| `server/relay_app.py` | 44 | **0** | 配置加载入口（`rolling-relay.service:19`），非死代码 |
| `server/services/filelock.py` | 36 | 5 | |
| `server/services/native_modality.py` | 24 | 1 | |

另有一类"**只有测试引用、产品代码不 import**"的 11 个模块（多为 `qtgcore` 里被 `main.py` 用 `from .qtgcore.routers import (…)` 之外的方式接线，或纯测试夹具）：
`qtgcore/adapters/workbuddy.py`(1,383)、`trae2.py`(743)、`doubaowork.py`(712)、`qtgcore/authz.py`(675)、`backup_application.py`(531)、`qtgcore/routers/proxy.py`(460)、`qoder2.py`(421)、`diagnostics.py`(176)、`serve.py`(108)、`budget_reconcile.py`(33)、`pooledhttp.py`(16)。
其中 `serve.py` 是**生产入口**（`deploy/systemd/INSTALL.md:15`），`workbuddy/trae2/qoder2/doubaowork` 经 `qtgcore/adapters/__init__.py` 注册表动态取用——说明**"零 importer"不等于死代码**，S7 的判定都额外人工核对了字符串引用；`relay_app.py` 是例外，它由 systemd 配置加载（`deploy/rolling-relay.service:19`），不在死模块清单里。

---

## 3. 测试规模

### 3.1 后端 `server/tests`

- 文件：**521** 个 `.py`；其中 `test_*.py` **514** 个，其余 7 个是 `__init__.py`/`conftest.py`/夹具（`_db_fixture.py`、`audit_fixtures.py`、`authorization_test_support.py` 等）。
- 代码行：**112,448**；用例：`def test_` **6,206**、`class Test` **4**（unittest 风格主导，与 Captain 记录的"502 unittest 模块 + 12 pytest 模块"吻合）。
- 结构：单体 + 共享 `conftest.py`；大量测试通过 `fastapi.testclient` 直打 `server.main:app`（例如 `test_account_groups.py`），因此**引用面覆盖率虚高**：几乎所有 router 都被 20–72 个测试文件 import 或间接覆盖，但 `credit_watch.py`(1,451 行) 只有 3 个测试文件、`keyimport.py`(1,284) 只有 2 个。

本机抽跑（证明环境可用，且 `WB_DATA_DIR` 落到 /tmp）：
```bash
export WB_DATA_DIR=$(mktemp -d /tmp/wbdata-XXXX)
/d/AI/capy/work/venvs/zhihui-gateway/Scripts/python.exe -m pytest server/tests/test_audit_protocol_limits.py -q
# → 10 passed, 5 subtests passed in 2.42s
```

### 3.2 前端 `web/`

- `web/lib/*.test.mjs` **29** 个（`node --test`，直接引 `.ts` 源码）；`web/tests/*.test.cjs` **18** 个（含 browser-scenario 脚本不在此列）。
- 静态 `test(`/`it(` 调用：`lib` 42 处、`tests` 184 处，合计 **226**。`web/package.json` 的 `test` 脚本 = `node --test lib/*.test.mjs tests/*.test.cjs`。
- **本机跑不全**：`web/node_modules` 不存在，`node --test` 可见 34 个文件结果，19 pass / 15 fail，失败全是 `Cannot find module 'typescript'`（如 `web/tests/language.test.cjs:8`）。即**该测试集预设先 `npm ci`**；本机只读、未安装，故前端用例真实通过数**未验证**（留给 C 节 Linux 云机）。

---

## 4. 结构问题（按严重度排序）

> 严重度：高 = 会导致数据/供应链错误；中 = 明显维护/正确性风险；低 = 卫生/可读性。
> 本节**先报"假设被证伪"**，避免最终文档写错结论。

### S0（证伪记录）"重复翻译层"只在脚手架层成立，整类重复不成立

`difflib` 去空行/注释后的行相似度 + `ast` 函数名 Jaccard（复现脚本见附录 B）：

| 对比 | 行相似度 | 函数名 Jaccard |
|---|---|---|
| `services/wb2api.py` vs `services/tencent.py` | **0.031** | 0.008 |
| `routers/gateway.py` vs `routers/anthropic.py` | **0.046** | 0.014 |
| `routers/gateway.py` vs `routers/responses.py` | **0.049** | 0.006 |
| `routers/anthropic.py` vs `routers/responses.py` | **0.203** | 0.089 |

结论：`wb2api`/`tencent` 是两条**不同上游**的客户端，不是复制；三协议 router 的**主体是各自协议的独立状态机**（`_StreamTranslator` 两份仅共 4 个方法：`__init__/feed/finish/_check_item_budget`，`anthropic.py:894-1244` 351 行 vs `responses.py:1345-1843` 499 行，相似度 0.153）。真正重复的是**流式脚手架**（S6）。

### S1（中）发布器双份入库，靠 CI 复制维持字节一致，无任何自动校验

- 证据：`md5sum deploy/update.py server/deploy/update.py` → 两行同为 `f4b5e9d168f1c38337fa82439597274a`，1941 行 / 94,563 B。
- 复制的源头：`.github/workflows/release.yml:155-156` `mkdir -p "$STAGE/server/deploy" && cp deploy/update.py "$STAGE/server/deploy/update.py"`。
- 为什么是问题：`server/deploy/update.py` 的**唯一存在理由**是发行包里 `python /app/deploy/update.py` 与 `python server/deploy/update.py` 都能跑；两份一旦漂移，同一版本会带两个不同签名校验行为的更新器（该文件 253 行是 `ssh-keygen -Y verify` 验签入口）。仓库里没有任何测试或 CI 步骤断言两份相等（`grep -rn "server/deploy/update.py" server/tests .github` 无断言）。
- 怎么验：`md5sum deploy/update.py server/deploy/update.py`；再 `diff -q` 两文件；想验证漂移风险，改其中一份的注释后跑 `tools/ci_regression.py`，不会有任何测试失败。

### S2（中）陈旧的可执行更新器备份随每个发行树出厂

- 文件：`deploy/update.py.pre-srcpatch-20260916-153909`，46,833 B，含 `if __name__ == '__main__':` 入口（文件末 3 行）。
- 与现行差 **1,972 行 diff**（现行 94,563 B / 1941 行），即少了约一半代码；同时自带 `verify_release_signature`（`:305`）。
- 出厂路径：`fork-tools/build_release.sh:27-30` `rsync -a "$REPO/deploy/" "$ROOT/deploy/"` 整目录复制 → `:54` 把它 `chmod 755` → `:68` 整树签名。
- 为什么是问题：一个 9 月的旧更新器被赋予可执行权限并进入签名发行包，等于把"旧版供应链逻辑"作为可用工具留下；运维若按旧笔记/旧 cron 调用它，会绕过今年新增的校验。
- 怎么验：`diff deploy/update.py.pre-srcpatch-20260916-153909 deploy/update.py | wc -l` → 1972；`tail -3 deploy/update.py.pre-srcpatch-20260916-153909`；`sed -n '27,30p;54p' fork-tools/build_release.sh`。

### S3（中）523 KB 的 CHANGELOG 双份入库，且构建脚本本来就会同步

- 证据：`md5sum CHANGELOG.md server/CHANGELOG.md` → 同为 `1e668b0e5c5ae874d1c56450dcc87e86`，各 523,470 B。
- `fork-tools/build_release.sh:44` 明确每次打包 `cp -f "$REPO/CHANGELOG.md" "$ROOT/server/CHANGELOG.md"`，其上方注释自述 2026-10-22 因两份不一致"面板显示旧内容"已经踩过。
- 为什么是问题：仓库里常驻两份 523 KB 的同一内容，git 多一份 blob，且任何人只改一份就会复现那次事故；同步靠构建脚本而非结构保证。
- 怎么验：`md5sum CHANGELOG.md server/CHANGELOG.md`；`git log --oneline -3 -- server/CHANGELOG.md` 看是否与根文件同源提交。

### S4（中）服务层反向依赖路由层，形成真实循环依赖

- `server/routers/accounts.py:21` 顶层 `from ..services import (… dailyplan …)`；
- `server/services/dailyplan.py:298` 与 `:306` 在函数体内 `from ..routers import accounts as accounts_router`，注释自认："`routers.accounts` 顶层 import 了 taskrun，这里再顶层 import accounts 会成环"。
- 为什么是问题：服务层本应可脱离 HTTP 层复用；现在 `dailyplan.run_task('checkin')` 直接调用路由函数 `accounts_router.checkin_all(..., user={'role':'admin'})`——**用伪造的 user 字典绕过鉴权依赖**（`:299-300`）。任何后台调度路径都因此依赖 FastAPI 请求签名形状，改签名即断。
- 怎么验：`grep -n "from ..routers" server/services/*.py` 命中 `dailyplan.py:298,306`、`platformauth.py:88`；`sed -n '294,308p' server/services/dailyplan.py`。

### S5（中）三个协议 router 互相 import，共享阈值定义在 HTTP 模块里

- `server/routers/anthropic.py:48` `from . import gateway, responses`；`server/routers/responses.py:50` `from . import gateway`；`server/routers/gateway.py:29` 反向 import `routers.security`。
- `MAX_OUTPUT_ITEMS = 256` 定义在 `server/routers/responses.py:59`，被 `server/routers/anthropic.py:1082` 以 `responses.MAX_OUTPUT_ITEMS` 取用（测试同样 `patch.object(responses, 'MAX_OUTPUT_ITEMS', 2)`，`test_audit_protocol_limits.py:61`）。
- 为什么是问题：一个**协议无关的上限**住在某个协议的 HTTP 模块里，其它协议要 import 整个 router 才能拿到；router 之间的 import 图是环状（gateway↔anthropic），改任意一个都会牵动另外两个的导入期行为。仓库已经为此造了 `server/outputlimits.py`（docstring 自称"shared by the three inference protocols"），却没有把 `MAX_OUTPUT_ITEMS` 也放进去——共享层被用了一半。
- 怎么验：`grep -n "from \. import" server/routers/{gateway,anthropic,responses}.py`；`grep -rn "responses.MAX_OUTPUT_ITEMS" server --include=*.py`。

### S6（低）流式脚手架复制 5 处

- `_check_item_budget` 两份，逻辑逐字等价（仅空格差异）：`server/routers/anthropic.py:1081-1085` 与 `server/routers/responses.py:1628-1632`（都是 `if next_index >= MAX_OUTPUT_ITEMS: raise OutputLimitExceeded(...)`）。
- `_mark_first_byte` 闭包三份：`server/routers/anthropic.py:1511`、`server/routers/gateway.py:3215`、`server/routers/responses.py:2087`。
- 另有 `feed()` 头部的"已收尾就丢弃后续帧"守卫三份（`anthropic.py:1096-1099`、`responses.py:1643-1646`、`gateway.py` 同段落）。
- 为什么是问题：这类守卫是**协议污染防线**（注释写得很清楚），三份拷贝意味着修一处漏两处；它已经在 `outputlimits.py` 有一半抽象，剩下三处没收敛。
- 怎么验：`grep -rn "_check_item_budget\|_mark_first_byte" server/routers/*.py`。

### S7（低）5 个模块零引用（含测试），累计 1,100 行

`server/qtgcore/capabilities.py`(416)、`server/qtgcore/routeaudit.py`(321)、`server/qtgcore/adapters/qoder.py`(151)、`server/qtgcore/adapters/trae.py`(128)、`server/tools/platform_selfcheck.py`(84) —— 5 个合计 1,100 行。`server/relay_app.py`(44) 虽然 0 importer，但由 `deploy/rolling-relay.service:19` 以 `uvicorn server.relay_app:app` 启动，是配置加载的生产入口，不计入死模块。另有 `server/deploy/update.py` 同样 0 importer，但它是 CLI（见 §2.3 脚注）。
- 判定方法（比 grep 更硬）：AST 导入图里这 5 个模块的 importer 集合为空；再对 `routeaudit`/`capabilities`/`trae`/`qoder` 做字符串引用检索（排除注释），唯一的命中是 `main.py:678,710` 的**注释**和 `qtgcore/routers/__init__.py:7` 的说明文字。
- 为什么是问题：`adapters/trae.py`、`qoder.py` 与活的 `trae2.py`(743 行)、`qoder2.py`(421 行) 同名同形（函数名 Jaccard 0.44 / 0.67），后来者极易改错文件；`routeaudit.py` 指向一个本仓库不存在的 `qtg` 包（`:19` `python -m qtg.routeaudit`）。
- 怎么验：附录 A 脚本的"zero importers"段；`grep -rn "routeaudit" server --include=*.py | grep -v tests`。

### S8（低）router 直接开库写 SQL，`stats.py` 最重

- `grep -c "db\.query\|db\.query_one\|db\.execute\|db\.connect\|db\.executemany"`：`routers/stats.py` **23** 处 / 1,019 行；`routers/security.py` 9 / 176；`routers/gateway.py` 7 / 3,374；`routers/logs.py` 6；`routers/portal.py` 4；`routers/read_api.py` 4。
- `server/db.py` 提供 `query/query_one/execute/executemany`（`:1002-1045`），本身是"连接层"，但**没有服务层**承接统计聚合，`stats.py` 直接用。
- 为什么是问题：分层上 router 变成 SQL 宿主，SQL 无法被非 HTTP 调用方复用，也无法脱离 TestClient 单测（这正是 §3.1 里测试只能走 app 的原因）。
- 怎么验：上面那条 `grep -c` 循环（`for f in server/routers/*.py; do …`）。

### S9（低）`qtgcore` 是整编进来的第二个服务，条件挂载

- `server/main.py:199-207`：`if not (config.SHARED_ADMISSION and not config.BACKGROUND_JOBS): … Runtime(_qtg_load_settings()); await _qtg_rt.startup()`，失败只影响 qoder/trae 分支；`main.py:685` 起 `from .qtgcore.routers import (compat as _qc, …)`。
- 规模：40 文件 / 16,205 行，自带 `store.py`(513)、`identity.py`(506)、`authz.py`(675)、`security.py`(587)——**第二套身份与存储**。`main.py:731-746` 注释还写明"哪些路径走 qtgcore 不可静态推断"（前缀匹配）。
- 为什么是问题：两个服务的路由表在同一 ASGI app 里按字符串前缀分流，鉴权由各自的 handler 负责（`main.py:785` 注释："qtgcore 的 handler 签名里没有任何 Depends 授权依赖"）。这是 B 节能给出的最大结构性风险面，具体鉴权正确性归 D 节。
- 怎么验：`grep -rn "qtgcore" server --include=*.py | grep -v "^server/qtgcore/" | grep -v tests`；`sed -n '199,207p;680,750p' server/main.py`。

### S10（低）`client/zhihui_upload.py` 产品零引用

29 行；引用的只有 3 篇文档 + 一个测试把它当 fixture 动态加载（`server/tests/test_requestbody_gzip.py:139-140` `spec_from_file_location('zhihui_upload_fixture', …/client/zhihui_upload.py)`）。它描述的是**客户端侧** gzip 策略，服务端不 import 属正常，但仓库里没有任何客户端会用它——实际是"规格文档的可执行副本"。
- 怎么验：`grep -rn "zhihui_upload" --include=*.py .`。

---

## 5. 依赖清单与风险

### 5.1 Python（`server/requirements.lock.txt`，27 pin；`requirements.hashes.txt` 28 行 sha256）

- 依赖闭包**完整**：AST 扫 203 个源码模块的第三方顶层 import = 10 个（`fastapi/starlette/uvicorn/httpx/pydantic/psycopg/yaml/dotenv/socksio` 等），全部能在 lock 里找到；无"代码 import 了但没钉版本"的包。（我把 `offsitebackup`、`backupstatus` 两条误报排除了——它们是 `server/` 内本地模块。）
- 版本对比（lock 值 vs PyPI 当前值，2026-11-27 查）：

| 包 | lock | PyPI 最新 | 状态 |
|---|---|---|---|
| `fastapi` | 0.141.1 | **0.165.0** | 落后 24 个 minor；`docs/DEPENDENCY-AUDIT-PYTHON-2026-11-15.md:47` 记录为"故意不升"（行为变更风险） |
| `starlette` | 1.6.0 | 1.10.0 | 落后 4 个 minor |
| `h11` | 0.16.0 | **0.16.0** | 与最新一致 |
| `httpx` | 0.28.1 | 0.29.0 | 落后 1 minor |
| `uvicorn` | 0.53.0 | 0.54.0 | 落后 1 minor |
| `pydantic` | 2.13.5 | 2.14.4 | 落后 1 minor |
| `pydantic_core` | 2.46.5 | 2.50.5 | 与 pydantic 配套 |
| `anyio` | 4.15.1 | 4.16.0 | 落后 1 minor |
| `psycopg`(+binary) | 3.3.6 | 3.3.9 | 落后 1 patch |
| `python-multipart` | 0.0.32 | 0.0.34 | 落后 2 patch |
| `websockets` | 17.1 | 17.4 | 落后 |
| `certifi` | 2026.10.19 | 2026.11.20 | 落后 1 个月（CA 包，建议跟） |
| `click` / `PyYAML` / `uvloop` / `watchfiles` / `idna` / `tzdata` / `python-dotenv` / `httpcore` / `typing_extensions` | — | — | 各落后 1 patch；`httpcore`、`typing_extensions`、`socksio` 与最新一致 |

- **CVE 线索**（可查部分已查，其余标注未验证）：
  - `h11` 0.16.0 正是 **CVE-2025-43859**（HTTP request smuggling，修复线 0.16.0）的修复版 → 不中招（`docs/DEPENDENCY-AUDIT-PYTHON-2026-11-15.md:26` 结论与本机查询一致）。
  - `starlette` **CVE-2026-54283**（urlencoded 表单不吃 `max_fields/max_part_size`，CVSS 7.5）受影响区间 `>=0.4.1, <1.3.1` → pin 的 1.6.0 不在区间。
  - `starlette` 旧 DoS `GHSA-2c2j-9gv5-cj73` 受影响 `<0.47.1` → 不适用。
  - **未验证**：全套 Python CVE 扫描没做——本机 venv 无 `pip-audit`（`python -c 'import pip_audit'` → ModuleNotFoundError），且审计期只读、不装包。上面 3 条是手工按版本核对，不构成"无洞"结论。
  - `certifi` 落后一个月属 CA 包，通常无 CVE 但会影响新根证书；`fastapi 0.141→0.165` 这条**只能靠升级验证**，不是安全结论。

### 5.2 前端（`web/package.json`：deps 43 + devDeps 12）

- `npm audit --package-lock-only --json`（本机实际执行，npm registry 可达）：`vulnerabilities: {}`，`prod 193 / dev 372 / optional 93 / total 601` 个包，**high/critical 均 0**。
- `next`：manifest 与 lock **都是 16.4.5**；这正是 Next.js **2026-11-03 安全版**（官方公告：`16.4.5` 修复 7 个 GHSA/CVE，含 `use cache` 跨租户缓存泄漏、Edge HttpOnly cookie 泄漏、自托管响应注入）。升级提交为 `72358a0`「升级 Next.js 到 16.4.5，清掉 7 条依赖告警」。
  - 适用性：`grep -rl "use cache" web/app web/lib` = **0 文件**，所以 4 条 Cache Components 类通告不适用；`web/next.config.ts:33-36` 是 `images: { unoptimized: true, remotePatterns: [] }`——图像优化器**整体关闭**，SVG 优化器通告（GHSA-7qx7-33qx-rqx2）无攻击面。
  - 仍落后：npm `latest` 已是 **16.4.6**（2026-11-09 发布，非安全版），即生产落后 1 个 patch / 3 周。
- 版本偏斜：`eslint-config-next` 固定 **16.4.3**（`web/package.json`），与 next 16.4.5 不同步；`react` lock 19.3.0、`axios` lock 1.20.0（manifest 写 `^1.9.0`）、`lucide-react` 1.45.0（manifest `^1.8.0`）——manifest 范围与 lock 实际值差得很远，升级时容易误判。
- **文档漂移**：`docs/DEPENDENCY-VERSION-AUDIT-2026-11-15.md:28` 仍写 `next 16.4.3`，而 lock 已是 16.4.5（11-03 安全升级之后未回填）。该文件自己第 78 行要求"升级任何一个依赖后回来更新第二张表"。
- 生产不带依赖树（同文档第 20 行），所以前端风险面是**打包进静态产物里的代码**，不是服务器上装了多少包。

### 5.3 依赖风险表（结论版）

| 风险 | 对象 | 证据 | 严重度 | 建议动作 |
|---|---|---|---|---|
| 更新器双份漂移 | `deploy/update.py` vs `server/deploy/update.py` | md5 相同但无校验（S1） | 中 | 加一条 CI 断言 `diff -q`，或只保留一份由 CI 生成 |
| 旧更新器随机出厂 | `deploy/update.py.pre-srcpatch-…` | `fork-tools/build_release.sh:27-30,54` | 中 | 删除或移出 `deploy/`（不随 rsync 出厂） |
| 前端落后 1 patch | `next` 16.4.5 → 16.4.6 | npm dist-tags `latest=16.4.6`（11-09） | 低 | 下一窗口顺带升；非安全版 |
| 框架大面积落后 | `fastapi` 0.141→0.165、`starlette` 1.6→1.10 | PyPI | 中（维护性，非 CVE） | 单独 PR + 全套门禁，别混进安全修复 |
| CA/时间数据过期 | `certifi` 2026.10.19、`tzdata` 2026.5 | PyPI | 低 | 跟随月度更新 |
| 已知 CVE | h11 / starlette / next | 见 5.1、5.2 | **无命中** | 保持 pin，勿回退 |
| 未验证 | Python 全量 CVE | 无 `pip-audit` | — | 由 C 节在 Linux 云机跑 `pip-audit -r server/requirements.lock.txt` |

---

## 6. 总表：目录 → 职责 → 行数 → 测试覆盖 → 风险

| 目录 | 职责 | 文本行 | 测试覆盖（实测） | 主要风险 |
|---|---|---|---|---|
| `server/`（非测试） | 网关 + 折进来的 qtg 控制面 | 82,036 | 203 模块中 183 个被 ≥1 测试文件直接 import（90%）；但 20 个 0 引用（含 5 个死模块 + `relay_app.py` 配置加载） | **S4 服务↔路由成环**、**S5 三 router 互联**、S9 双服务同进程、S8 router 直写 SQL、S1 更新器双份 |
| `server/tests/` | 回归门禁主体 | 112,448 | 自身即测试；6,206 用例 | 引用面广≠覆盖深（`credit_watch` 1,451 行 / 3 个测试） |
| `web/` | Next 控制台 + 静态导出 | 70,637 | 47 文件 / 226 `test()`；本机因缺 `node_modules` 只 19/34 可见通过 | 版本偏斜（eslint-config-next 16.4.3）、文档版本漂移 |
| `site/` | 内容寻址首页/登录资产 | 72,734 | `site/tests/` 5 文件（`home-import.test.mjs`） | 166 个二进制 + 4 份 10.7 MB `thinker.glb`（同 blob）占 pack 主体 |
| `deploy/` | 发布/更新/回滚/签名 | 12,858 | 无独立测试目录；`server/tests/test_docker_deploy.py`(1,246 行) 间接覆盖部分 | **S2 旧更新器出厂**、S1 副本、`release.py` 35 KB 单文件 |
| `dev/` | 诊断脚本 + 截图证据 | 17,862 | 无 | 86 个二进制（累计数 MB）随库增长 |
| `ops/` | 运行期巡检/告警/writer-guard | 6,826 | 无（由 `ops/install.sh` 部署验证） | 与 `server/` 无共享代码，规则易与网关实现漂移 |
| `windows/` | Windows 原生安装路径 | 4,530 | 无自动化；靠 `windows/README.md` 手测 | 与 Linux/Docker 路径**两套更新语义**（`update.cmd` 校验 sha256） |
| `tools/` | CI 门禁 | 2,033 | 被 `.github/workflows` 调用 | 门禁本身无测试 |
| `client/` | 可选 gzip 上传 helper | 29 | 1 个测试当 fixture 加载 | 产品零引用（S10） |
| `docs/` | 设计与历史审计 | 31,547 | — | 已有版本漂移（5.2） |

---

## 附录 A：导入图 / 死模块复现脚本

```python
# 在仓库根运行：ast 解析 server/**.py，输出每个模块的 prod/test importer 数
import os, ast, collections
SKIP={'__pycache__','.pytest_cache'}
def mod(f):
    p=f[:-3].replace('/', '.')                      # f 用 '/' 分隔，必须按 '/' 换点
    return p[:-9] if p.endswith('.__init__') else p
files=[]
for dp,dn,fn in os.walk('server'):
    dn[:]=[d for d in dn if d not in SKIP]
    files+=[os.path.join(dp,f).replace(os.sep,'/') for f in fn if f.endswith('.py')]
graph={}
for f in files:
    t=ast.parse(open(f,encoding='utf-8',errors='replace').read()); out=set()
    parts=mod(f).rsplit('.',1)[0].split('.')
    for n in ast.walk(t):
        if isinstance(n,ast.Import): out|={a.name for a in n.names}
        elif isinstance(n,ast.ImportFrom):
            if n.level:
                tgt='.'.join(parts[:len(parts)-(n.level-1)])+(('.'+n.module) if n.module else '')
            else:
                tgt=n.module or ''
            if tgt: out.add(tgt); out|={tgt+'.'+a.name for a in n.names}
    graph[f]=out
code=[f for f in files if not f.startswith('server/tests/')]
tests=[f for f in files if f.startswith('server/tests/')]
def impr(m,pool): return {f for f in pool if any(i==m or i.startswith(m+'.') for i in graph[f])}
print('code_modules=%d test_files=%d'%(len(code),len(tests)))
for f in sorted(code,key=lambda x:-os.path.getsize(x)):
    m=mod(f); prod=impr(m,code)-{f}
    if not prod: print('NO-PROD-IMPORTER %-44s tests=%d'%(f,len(impr(m,tests))))
# 本机实测输出：code_modules=203 test_files=521，18 行 NO-PROD-IMPORTER
# （7 个测试也不引用 = §S7 的 5 个 + deploy/update.py + relay_app.py（配置加载）；11 个只有测试引用 = §2.3 脚注）
```

## 附录 B：重复度复现

```python
import difflib, ast
def norm(p): return [l.strip() for l in open(p,encoding='utf-8',errors='replace') if l.strip() and not l.strip().startswith('#')]
def funcs(p):
    return {n.name for n in ast.walk(ast.parse(open(p,encoding='utf-8').read())) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))}
for a,b in [('server/services/wb2api.py','server/services/tencent.py'),
            ('server/routers/gateway.py','server/routers/anthropic.py'),
            ('server/routers/anthropic.py','server/routers/responses.py'),
            ('deploy/update.py','server/deploy/update.py')]:
    fa,fb=funcs(a),funcs(b)
    print(a,b,round(difflib.SequenceMatcher(None,norm(a),norm(b),autojunk=False).ratio(),3),
          round(len(fa&fb)/len(fa|fb),3))
```

## 附录 C：本线程跑过的命令（全部只读）

```
git log --oneline -1 ; git count-objects -vH ; git ls-files | wc -l
python stats.py / stats2.py                     # 目录×行数（排除二进制）
python server_stats.py                          # server 子目录与 top-25
python import_graph.py ; modtable.py            # AST 导入图 / 零引用 / 测试引用面
python dup.py ; translator.py                   # difflib + 函数名 Jaccard
grep -c "db\..*" server/routers/*.py            # 分层违规计数
md5sum deploy/update.py server/deploy/update.py CHANGELOG.md server/CHANGELOG.md
diff deploy/update.py.pre-srcpatch-20260916-153909 deploy/update.py | wc -l
npm audit --package-lock-only --json            # 0 vulnerabilities / 601 packages
curl registry.npmjs.org/next{,/16.4.6,-/package/next/dist-tags}
pypi.org/project/<pkg>/ via web_fetch           # 21 个包的最新版本
WB_DATA_DIR=$(mktemp -d /tmp/…) pytest server/tests/test_audit_protocol_limits.py -q
node --test lib/*.test.mjs tests/*.test.cjs     # 34 文件可见，19 pass / 15 fail(缺 node_modules)
```

**未做/未验证**：Python 全量 CVE 扫描（无 `pip-audit`，只读了 h11/starlette/next 三条通告）；前端用例真实通过数（缺 `node_modules`，只读期未 `npm ci`）；行覆盖率（未装 `pytest-cov`）；`deploy/`、`ops/`、`windows/`、`tools/` 的行为正确性归 05-ops-deploy.md，本文件只给结构与引用面。
