# 06 · E 上游关系 + F 同行对照

**范围**：`daniei-chen/zhihui-gateway` @ `main` ec87de9（VERSION 1.6.28），只读审计，未改仓库任何文件、未 commit、未 push。
**方法**：先读仓库内 `fork-manifest.json` / `UPSTREAM-NOTICES.md` / `docs/UPSTREAM-REVIEW-*.md` / `docs/history/` 反推上游；再 `git clone --depth 1` 取上游 v2.4.0 / v2.6.0 / v2.8.3 / v2.10.0 / v2.11.14 / v2.11.23 逐 tag 做 `git diff --numstat` 比对；同行对照用 GitHub API + 各产品官方文档（2026-11-28 采集）。
**时间**：2026-11-27 16:42Z → 17:35Z（约 53 分钟）。
**环境**：卡皮巴拉 Windows 设备（PC-20260816OWHB），Git Bash；上游 clone 落在系统临时目录，未写入仓库。
**证据口径**：每条带 `file:line`（仓库相对路径）或命令；拿不到的直接写「未取得」。star 数、release 日期均为 2026-11-28 当日 API 读数，不凭记忆。

---

## 结论摘要（先看这 3 条）

1. **E-03（P1）我们落后上游 28 个 release，不是文档写的「1 个版本」。** 代码实际基线是上游 **v2.8.3**（`fork-manifest.json:7-8`），上游最新是 **v2.11.23**（2026-11-22），之后还有 31 个 commit 到 2026-11-27。`docs/UPSTREAM-REVIEW-2026-11-14.md:11` 记录的「固定身份」是 v2.11.14，那是**评审口径**，不是**代码口径**，两者被混读成「只落后 1 个版本」。
2. **E-06（P2）上游有 96 个文件我们没有**，其中 `/v1/credits`、`/v1/tools/*` 透传、请求级 trace（tap）、watchdog、account risk、gateway CORS、pricing 都是整模块缺失，不是「实现不同」。
3. **E-04（P2）`UPSTREAM-NOTICES.md:14` 指的 `licenses/` 目录不存在**，许可证实物在 `docs/upstream-licenses/`；做许可证合规审计时按通知里的路径会直接找不到文件。

---

# E. 上游关系

## E-01 上游身份（已核实）

| 项 | 值 | 证据 |
|---|---|---|
| 上游仓库 | `https://github.com/ithtelab/workbuddy-manager` | GitHub API `repos/ithtelab/workbuddy-manager`，2026-11-28 |
| 许可证 | MIT（`LICENSE` 原文；API `license.spdx_id` 返回 `NOASSERTION`，需按文件判） | 上游 `LICENSE` 1–3 行 |
| 建仓时间 | 2026-09-11 | API `created_at` |
| commit 总数 | 957 | API `repos/.../commits?per_page=1` 的 Link 尾页 |
| release 总数 | 143 | API 分页累计（`per_page=100`，2 页） |
| stars / forks | 3066 / 597 | API，2026-11-28 |
| 产品名 | WorkBuddy Manager（本仓库 README 自称「智汇」） | `README.md`、`UPSTREAM-NOTICES.md:1-3` |

> 注意：`UPSTREAM-NOTICES.md` 同时列了 WorkBuddy Manager、Qoder Hub、TraeWorkAssistant 三个「上游」。**本仓库的代码祖先只有 WorkBuddy Manager 一个**；Qoder/Trae 是运行时对接的引擎，不是代码上游（见 `docs/UPSTREAM-REVIEW-2026-11-14.md:11-17`）。

## E-02 版本谱系（反推结果）

| 节点 | 版本 | 日期 | 证据 |
|---|---|---|---|
| fork 起点 | **v2.4.0** | 2026-10-18 | `fork-manifest.json:6`（`"baseline": {"manager": "v2.4.0"}`）；本仓库首提交 `ee2323a "upstream v1.0.79"` 2026-10-18，`b24e... "upstream v2.4.0"` |
| 最近一次合并的上游 | **v2.8.3** | 2026-10-21 | `fork-manifest.json:7-8`（`"current": "v2.8.3+local.20261022.3"`，`"upstream_go": "v2.8.3"`） |
| 上游当前最新 | **v2.11.23** | 2026-11-22 | API `releases` 第 1 页首条 |
| 上游未发版 HEAD | 31 个 commit | 至 2026-11-27 | API `commits?since=2026-11-22` |
| 本仓库产品版本 | **1.6.28** | 2026-11-27 | `VERSION`；`CHANGELOG.md` 首条 |
| 本仓库 HEAD | ec87de9，586 commits，2060 tracked 文件 | — | `git log --oneline -1`、`git rev-list --count HEAD`、`git ls-files | wc -l` |

**「基线 = v2.8.3」的独立验证**（不靠 manifest 自述）：取「上游 v2.8.3→v2.11.23 之间发生变化的 210 个 server/web 文件」，逐个看本仓库对应文件是否等于 v2.11.23 版本——**0 个相等**；等于 v2.8.3 版本的有 21 个，其余 95 个是本地改造、94 个根本不存在。若基线是 v2.11.x，不可能出现「210 个变化文件里 0 个跟到新版本」。

```bash
# 复现：在 clone 出来的上游仓库里
git clone --depth 1 --branch v2.11.23 https://github.com/ithtelab/workbuddy-manager.git up
cd up && git remote add ours D:/AI/capy/worktrees/jam_01M8EY6HG7AH3VEFZ9A2MW35MT/zhihui-gateway
git fetch --depth 1 ours main:refs/heads/ours
git diff --numstat v2.8.3 v2.11.23 -- server web | awk '{print $3}' | sort > /tmp/changed.txt
# 再对每个 changed 文件比较 ours / v2.8.3 / v2.11.23 三份内容
```

## E-03 [P1] 落后 28 个 release + 31 个 commit，文档口径让人以为是 1 个

**现象**：`docs/UPSTREAM-REVIEW-2026-11-14.md:11` 的固定身份表写「WorkBuddy Manager … v2.11.14」，`docs/UPSTREAM-REVIEW-2026-11-15.md` 标题写「v2.11.14 / v2.11.15」，`UPSTREAM-NOTICES.md:9` 也写「The current review records WorkBuddy Manager v2.11.14」；而代码实际停在 v2.8.3（E-02）。

**为什么是问题**：任何按这三份文档做决策的人会得到「我们只落后 1 个 release」的结论，实际是 **28 个 release（v2.9.0、v2.10.0–2、v2.11.0–23）+ 31 个未发版 commit**。这直接影响三件事：是否要排期跟进、能否引用上游的安全修复、以及「上游已吸收我们的补丁」这一判断是否还成立（`fork-manifest.json:111-123` 说 0001–0011 补丁在 v2.8.3 已吸收，之后的版本没有再核对过）。

**怎么验**：
```bash
grep -n "v2.11.14" UPSTREAM-NOTICES.md docs/UPSTREAM-REVIEW-2026-11-14.md docs/UPSTREAM-REVIEW-2026-11-15.md
grep -n '"current"\|"upstream_go"' fork-manifest.json     # → v2.8.3
curl -s https://api.github.com/repos/ithtelab/workbuddy-manager/releases/latest | grep tag_name   # → v2.11.23
```

**建议（不在本次执行）**：把「评审口径」与「代码口径」拆成两栏写进 `UPSTREAM-NOTICES.md`，例如「评审到 v2.11.14 / 代码停在 v2.8.3 / 落后 28 个 release」。

## E-04 [P2] `UPSTREAM-NOTICES.md` 指向的 `licenses/` 目录不存在

**现象**：`UPSTREAM-NOTICES.md:14` 写「Corresponding upstream MIT license notices are retained in `licenses/`.」；仓库根目录没有 `licenses/`，实物在 `docs/upstream-licenses/`（`TraeWorkAssistant-LICENSE.txt`、`qoder2api-hub-LICENSE.txt`、`workbuddy-manager-LICENSE.txt`）。

**为什么是问题**：许可证合规审计（尤其对外分发、企业采购）按通知里的路径取证会直接扑空；三个上游的许可证是「保留声明」义务的物证，路径错了等于举证链断了。

**怎么验**：
```bash
ls licenses/            # No such file or directory
ls docs/upstream-licenses/
grep -n "licenses/" UPSTREAM-NOTICES.md
```

## E-05 [P2] `fork-manifest.json` 里的 Go 文件不在本仓库

**现象**：`fork-manifest.json:51-108`（`go_files_added`）与 `:169-215`（`go_files_modified`）列了 `internal/pool/slowdegrade.go`、`internal/server/session_namespace.go`、`internal/redisstore/filebinds.go` 等 20 余个 Go 文件；本仓库 `git ls-files '*.go'` 只有 1 个文件（`docs/enterprise-support/verified/upstream-enterprise_usage.go`），`git log --all -- internal/` 无任何提交。

**为什么是问题**：这份 manifest 被 `fork-tools/fork-verify.sh` / `fork-upgrade.sh` 读取（`fork-manifest.json:2`），而它把**两个代码库**（本仓库的 Python/TS 应用 + 另一个 Go 网关仓库）混在一份清单里。按它做「升级前核对」的人会以为 Go 文件丢失，或者反过来以为本仓库包含 Go 引擎。上游关系因此失真：**本仓库对上游的关系是「Python 应用 + 前端」的 fork，Go 引擎的 fork 关系在另一处**。

**怎么验**：
```bash
git ls-files '*.go'                       # 只有 1 个 docs 下的样例
git log --oneline --all -- internal/      # 空
grep -n "slowdegrade.go\|internal/pool" fork-manifest.json
```

## E-06 [P2] 96 个上游文件整模块缺失（不是「实现不同」）

**现象**：上游 v2.11.23 的 `server/` 与 `web/` 里，有 96 个文件在本仓库不存在。按模块归类：

| 模块 | 上游文件 | 本仓库 |
|---|---|---|
| 数据面新能力 | `server/routers/balance.py`（`/v1/credits`）、`server/services/balance_realm.py` | 无 |
| 请求级 trace | `server/services/tap.py`、`server/routers/tap.py`、`web/.../traces/page.tsx` | 无 |
| 上游页面改版监控 | `server/services/watchdog.py`、`server/routers/watchdog.py`（抓上游网页 → LLM 判读 → 邮件上报） | 无 |
| 账号恢复/排序 | `server/services/modelrecovery.py`、`server/services/accountorder.py` | 无 |
| 账号级退避与告警 | `server/services/accountrisk.py`、`server/routers/accountrisk.py` | 无 |
| 网关配置面 | `server/services/gatewaycors.py`（`/v1`、`/v2` 的 CORS 白名单，面板可改）、`server/services/pricing.py` + `pricing.json`（按官方价估算成本） | 无 |
| 运行时/通知 | `server/inflight.py`（客户端在途计数）、`server/logsetup.py`、`server/services/taskpacing.py`、`server/services/autoupdate.py`、`server/services/qq.py`（AstrBot QQ 通知） | 无 |

**为什么是问题**：`/v1/credits` 是上游给第三方客户端（RikkaHub 等）的额度自查端点，`tap` 是请求级可回溯（我们的 `ledger` + `observability` 是**计费/指标**视角，不是**请求内容**视角），`watchdog` 是上游网页改版监控（不是「上游僵死检测」）——这些都不是「我们实现得更土」，而是**没有这个面**。对外承诺「兼容 OpenAI 客户端」时，`/v1/credits` 缺失是可见的功能缺口。

**怎么验**：
```bash
cd up && git ls-tree -r --name-only v2.11.23 | grep -E '^server/(services|routers)/|^server/[a-z_]+\.py$' | sort > /tmp/up.txt
comm -23 /tmp/up.txt <(cd D:/AI/.../zhihui-gateway && git ls-tree -r --name-only HEAD | grep -E '^server/(services|routers)/|^server/[a-z_]+\.py$' | sort)
```

## E-07 与上游的实际 diff（数字）

**本仓库 vs 上游 v2.8.3（基线）**：

| 范围 | 文件数 | 增 | 删 |
|---|---|---|---|
| 全仓库 | 1646 | +12999 | −293917 |
| server/ + web/ | 857 | +8208 | −162686 |

**本仓库 vs 上游 v2.11.23（最新）**：

| 范围 | 文件数 | 增 | 删 |
|---|---|---|---|
| 全仓库 | 1867 | +67499 | −301749 |
| server/ + web/ | 972 | +49649 | −170217 |

**上游 v2.8.3 → v2.11.23（我们没取的那部分）**：331 文件，+57497 / −10829；其中 server+web 210 文件，+44073 / −10163。

**改动最多的模块 top 20**（本仓库相对 v2.8.3 的偏离行数，`git diff --numstat ours v2.8.3`）：

| # | 模块 | 偏离行数 |
|---|---|---|
| 1 | server/tests | 69157 |
| 2 | site/home | 58550 |
| 3 | server/qtgcore | 16921 |
| 4 | web/lib | 13380 |
| 5 | CHANGELOG.md | 13289 |
| 6 | site/home-source | 12728 |
| 7 | server/CHANGELOG.md | 10884 |
| 8 | server/routers | 8990 |
| 9 | docs/history | 7497 |
| 10 | web/tests | 5431 |
| 11 | web/app | 4534 |
| 12 | ops/writer-guard | 4381 |
| 13 | web/components | 4223 |
| 14 | server/services | 3736 |
| 15 | server/deploy | 1941 |
| 16 | server/observability.py | 1702 |
| 17 | server/main.py | 1603 |
| 18 | server/credit_watch.py | 1451 |
| 19 | server/operational.py | 1429 |
| 20 | ops/wb-gateway-alert.py | 1335 |

> 观察（不算问题，但值得记）：`server/CHANGELOG.md` 与根 `CHANGELOG.md` 各 10884 / 13289 行偏离，是两份分叉的更新日志；`site/home` 与 `site/home-source` 共 7 万行是站点资产，不是网关代码，把这两块算进「网关仓库」会稀释 diff 的意义。

## E-08 上游最近 6 个月 release / commit 主题

上游 2026-09-11 建仓，**实际只有约 2.5 个月历史**，「6 个月」窗口内即全部历史。release 节奏：**2026-09 77 个、2026-10 49 个、2026-11 17 个**（至 11-22）。

我们**没跟上**的 28 个 release 里，按主题挑出对网关有实质影响的：

| 主题 | 上游版本 | 我们是否已有 |
|---|---|---|
| `/v1/credits` 额度自查端点（第三方客户端） | v2.11.x（`server/routers/balance.py`） | 无（E-06） |
| 请求级 trace（`tap`，请求内容可回溯） | v2.11.x（`server/services/tap.py`） | 无（E-06） |
| 上游网页改版监控 watchdog + 模型恢复 | v2.11.x（`watchdog.py`、`modelrecovery.py`） | 无（E-06） |
| 账号风控分级（`accountrisk`） | v2.11.x | 无（E-06） |
| 网关 CORS 配置面（`gatewaycors`） | v2.11.x | 无（E-06） |
| 按 provider 计价（`pricing.py` + `pricing.json`） | v2.11.x | 无（E-06） |
| `inflight` 请求在途计数 / `logsetup` 日志装配 | v2.11.x | 无（E-06） |
| 账号调度顺序（`accountorder`）与任务限速（`taskpacing`） | v2.11.x | 无（E-06） |
| 客户端「按 consumer 配额」类能力（`balance_realm`） | v2.11.x | 无（E-06） |

> 上面这张表是按**文件是否存在**列的，不是按 release note 逐条核的；release note 的逐条对照需要另一次专门工作（本次时间不够，明确标「未逐条核」）。

## E-09 回灌（backport）策略可行性与成本

**冲突面**：上游 v2.8.3→v2.11.23 改过的 331 个文件里，**110 个**本仓库也改过（`comm -12` 交集）。按目录：

| 目录 | 冲突文件数 |
|---|---|
| server/tests | 29 |
| web/lib | 16 |
| web/app | 13 |
| server/services | 12 |
| server/routers | 9 |
| web/components | 8 |
| server/main.py / serve.py / security.py | 各 1 |
| web/package.json + lock | 2 |
| windows/launcher | 1 |

**可行性判断**：可以做，但**不适合整包 merge**，理由有三条，都有证据：

1. **冲突集中在测试**（29/110）。上游测试文件和我们自己的测试同路径不同内容，整包 merge 会得到一堆「两边都想要」的测试，回归基线会失效。
2. **我们的本地模块与上游同名不同源**。`server/budget.py`、`server/ledger.py`、`server/observability.py`、`server/pooledhttp.py` 在 `fork-manifest.json:37-50` 被列为「本地新增」，而上游在 v2.11.x 也新增了 `pricing.py`、`gatewaycors.py`、`watchdog.py` 等同类职责文件——**职责重叠、命名不重叠**，merge 后会出现两套预算/两套限流。
3. **`fork-manifest.json:133-152` 记录的四类文本冲突 + 一处语义断层**（`server/upstreamschema.py` 需要 `top_level` 支持 `reasoning_as_content`）在 v2.8.3 那次升级里已经踩过；v2.9→v2.11 的 `server/main.py`、`server/routers/gateway.py` 改动只会更多（`git diff --numstat v2.8.3 v2.11.23` 显示上游侧 `server/main.py` +440/−25、`server/routers/gateway.py` +559/−48）。

**建议路线（供拍板，不在本次执行）**：
- **先做「能力摘取」而不是「版本合并」**：把 E-06 的 96 个文件按「能否独立成模块」分三档——`/v1/credits` + `pricing`（可独立，低风险）、`tap` + `watchdog`（依赖上游 `inflight`/`logsetup`，中风险）、`accountrisk` + `accountorder` + `taskpacing`（与我们 `budget`/`pool` 职责重叠，高风险，建议先设计再动手）。
- **安全类修复单独摘**（见 E-10），不跟着整包走。
- **补一份「上游吸收核对表」**：`fork-manifest.json:111-123` 的 0001–0011 补丁只在 v2.8.3 核对过，v2.9–v2.11 是否仍被吸收，需要逐条 grep 重核（这是回灌前的前置条件，成本约 1 人日）。

## E-10 安全类修复核对（结论：我们已有等价实现，不构成 P0）

上游 v2.10.2 的六条安全修复，逐条在本仓库找等价实现：

| 上游修复 | 本仓库等价实现 | 证据 |
|---|---|---|
| CORS `*` + credentials 拒绝启动 | `config.CORS_ORIGINS` 检查 | `server/main.py:467-478`（`allow_origins`/`allow_credentials` 前的校验） |
| 哑哈希防用户名枚举 | `dummy_verify` 路径 | `server/routers/auth.py:82`（`security.dummy_verify(password)`） |
| `X-Forwarded-Proto` 只信反代 | 显式校验 + 提示 | `server/security.py:494`、`:517`、`:532` |
| body 硬顶（含 chunked） | `bodyguard` 中间件 | `server/bodyguard.py`（`WB_GATEWAY_MAX_BODY_CEILING_MB`） |
| 管理端 Cookie 认证的 CSRF | 审计记录 | `docs/CSRF-AUDIT-2026-11-15.md` |
| 上游限流 11-128 告警 | 告警规则 R10 | `fork-manifest.json` `ops_layer.alert_rules` |

> 结论：**这六条不构成「我们缺安全修复」**。但这不等于「上游 v2.9–v2.11 的所有安全修复都覆盖了」——本次只核了 v2.10.2 这一组，其余版本的安全类改动**未逐条核**，明确标「未取得」。

---

# F. 同行对照（5 个产品，每个 3 条）

> 事实采集：2026-11-28，GitHub API + 官方文档；星数、release 均为当日读数。每条带 URL。三个问题：**它做了我们没做的** / **它做得比我们好的（具体机制）** / **适不适合我们**。

## F-1 LiteLLM（`BerriAI/litellm`，64025 stars，MIT + `enterprise/` 例外，最新 v1.111.0 / 2026-11-25）

- **它做了我们没做的**：**同一逻辑模型名挂多个 deployment 的路由层**——`model_list` 里一个 `model_name` 对应多个 deployment，内置 cooldowns、fallbacks、timeouts，并可用 Redis 跨实例跟踪 cooldown 与 tpm/rpm。我们是「单上游 + 账号池」，没有「同一模型多 provider」这一层。URL：https://docs.litellm.ai/docs/routing
- **它做得比我们好的**：**预算按「个人/团队/团队成员/Agent」四层，在 DB 里强制**；文档明确写了 DB-less 模式下预算不生效——这条 caveat 比机制本身更值得记。我们只有 `server/budget.py` 的密钥级额度 + `server/ledger.py` 的账本，没有团队/成员维度。URL：https://docs.litellm.ai/docs/proxy/users
- **适不适合我们**：**借层级维度，不借 DB-less 语义**。加「owner/team」维度是数据模型改动（`server/budget.py` 现在按 key 查 `api_keys` 表），不碰协议；但「DB 不接就静默不生效」这种降级方式不能学——我们的预算失效必须是显式错误，不能悄悄放行。

## F-2 one-api（37487 stars，MIT，最新 v0.6.10 / 2025-02-02）/ new-api（52654 stars，AGPL-3.0，最新 v1.0.0-rc.51 / 2026-11-13）

- **它做了我们没做的**：**「渠道 → 模型 → 令牌」三级抽象**。one-api 把「负载均衡访问多渠道、失败自动重试、令牌过期/额度/模型限制、分组倍率」做成对象属性；new-api 把「会话保持三档（不保持 / 优先原渠道失败再换 / 必须原渠道）+ `model_regex` / `path_regex` / `key_sources` 规则」做成配置。我们会话保持是 `server/routers/gateway.py` 里的代码分支，不是配置。URL：https://github.com/songquanpeng/one-api（README「功能」节）、https://docs.newapi.ai/zh/docs/guide/feature-guide/system-settings/settings-request-policies
- **它做得比我们好的**：**重试链路可观测**——new-api 的请求日志记录「经过哪些渠道、重试了几次、请求策略怎么决策的」；我们的 `ledger.py` 记的是结果（token/成本/状态），不记「为什么换渠道」。URL：https://docs.newapi.ai/zh/docs/guide/feature-guide/user/log/
- **适不适合我们**：**借对象模型，不借代码**。new-api 是 AGPL-3.0，我们 MIT，直接抄会传染许可证；one-api 是 MIT 但最新 release 停在 2025-02，只能当历史参考。真正可借的是「把会话保持/重试策略从代码分支变成配置」这个方向。

## F-3 Portkey（`Portkey-AI/gateway`，13458 stars，MIT，最新 v1.15.2 / 2026-01-12）

- **它做了我们没做的**：**配置驱动负载均衡 + fallback + circuit breaker**——`strategy.mode = loadbalance` 配 targets 权重（文档示例 0.7/0.3），主 LLM 失败自动切备用，circuit breaker 在目标恢复前停止路由。我们的 fallback 是 `attempt` 循环里的代码分支。URL：https://docs.portkey.ai/docs/product/ai-gateway/load-balancing、https://docs.portkey.ai/docs/product/ai-gateway/fallbacks
- **它做得比我们好的**：**guardrail 是插件目录**——`plugins/Contributing.md` 说明怎么加一个 guardrail，请求前/后各一串 hook。我们的 `sanitize` 是硬编码规则（`fork-manifest.json` 的 `dsn_fingerprint_rules`），加一条规则要改代码。URL：https://github.com/Portkey-AI/gateway/blob/main/plugins/Contributing.md
- **适不适合我们**：**借「配置即路由 + guardrail 目录」，不借运行时**。Portkey 是 Node/TS，我们是 Python + Go；但它的预算/限流页标注 Enterprise/部分 Pro，开源版能力边界要先核实，不能按文档全量假设。

## F-4 Higress（`higress-group/higress`，9870 stars，Apache-2.0，最新 v2.2.6 / 2026-11-10）

- **它做了我们没做的**：**按 consumer 的配额与两级限流**——`ai-quota` 按 consumer 分固定配额，`ai-token-ratelimit` 支持「规则级全局」与「按 consumer」两种模式（Redis 实现）。我们的限流是 `server/bodyguard.py` + `budget.py` 的密钥级，没有 consumer 维度。URL：https://higress.ai/docs/latest/user/plugins/ai/api-consumer/ai-quota/、https://higress.ai/docs/latest/user/plugins/ai/api-consumer/ai-token-ratelimit/
- **它做得比我们好的**：**四维观测口径**——`ai-statistics` 默认给输入/输出 token、首 token RT、请求总 RT，并在「网关/路由/服务/模型」四个维度切分。我们的 `server/observability.py` 有指标，但没有「按模型」这个维度。URL：https://higress.ai/docs/latest/user/plugins/ai/api-o11y/ai-statistics/
- **适不适合我们**：**不照搬 Wasm/Envoy，借两个口径**。Higress 是 Istio/Envoy 数据面 + Wasm 插件，我们单进程 FastAPI，换框架是另一个量级的工程；但「consumer 维度限流」和「四维观测」是纯口径，可以直接落到现有表结构上。注意它的 token 限流依赖 Redis，我们本地是单机 SQLite，要换成本地计数。

## F-5 Kong AI Gateway（`Kong/kong`，44425 stars，Apache-2.0，最新 3.9.5 / 2026-11-19）

- **它做了我们没做的**：**AI Model 实体上的 balancer + 多算法**——`config.balancer` 配 targets，支持均衡/提升缓存命中/降延迟/按成本/failover 五种算法，内置「重试同一 target 或改投其他 target」。我们的选号是 `internal/pool/pick.go` 的固定权重 + `slowdegrade` 的 TTFB 降权。URL：https://developer.konghq.com/ai-gateway/load-balancing/
- **它做得比我们好的**：**按成本计费 + 按 provider/model 的指标**——`AI Rate Limiting Advanced` 用 provider 返回的 token 数据算每次请求成本；`config.ai_metrics=true` 暴露 AI requests/cost/tokens/latency/cache fetch/embeddings 指标。我们的 `budget.py` 按本地价目表算，`observability.py` 没有 provider/model 维度。URL：https://developer.konghq.com/plugins/ai-rate-limiting-advanced/、https://developer.konghq.com/ai-gateway/monitor-ai-llm-metrics/
- **适不适合我们**：**不借形态，借两个口径**。Kong 是 Nginx/OpenResty + Lua，我们不是；但「按成本选 target」可以加到 `pick.go` 的权重里（现在是固定权重 + TTFB），「按 provider/model 的指标」可以加到 `observability.py` 的 label 集。

---

# G. 5 条「可借鉴但不要照搬」的结论（对应到我们仓库的具体模块）

1. **借 LiteLLM 的「预算层级」，不借它的 DB-less 降级**（`server/budget.py`、`server/ledger.py`）。现在预算按 `api_keys` 行查，加 owner/team 维度是加列 + 改查询，不动协议；但预算失效必须显式报错，不能像 LiteLLM 那样「DB 不接就静默不生效」。
2. **借 new-api 的「请求策略配置化」，不借它的 AGPL 代码**（`server/routers/gateway.py`、`internal/pool/pick.go`）。会话保持/重试现在是代码分支，做成配置能少改代码；但 new-api 是 AGPL-3.0，只能借设计。
3. **借 Portkey 的「配置即路由 + guardrail 目录」，不借它的 Node 运行时**（`server/routers/gateway.py`、`server/upstream/sanitize.go`）。`sanitize` 现在是硬编码 DSH 规则，做成「规则目录 + 前后置 hook」比换运行时便宜；先核实它的开源版能力边界，别按企业版文档设计。
4. **借 Higress 的「consumer 维度限流 + 四维观测」，不借它的 Wasm/Envoy**（`server/bodyguard.py`、`server/observability.py`）。限流现在按密钥，加 consumer 维度是加表；观测现在没有「按模型」维度，加 label 即可。它的 token 限流依赖 Redis，我们本地是 SQLite，要换成本地计数。
5. **借 Kong 的「按成本选 target + 按 provider/model 的指标」，不借它的 Lua 插件**（`internal/pool/pick.go`、`server/observability.py`）。`pick.go` 现在是固定权重 + TTFB 降权，加「成本」维度是加一个权重项；指标加 provider/model label 是低成本改动。

---

## 附：复现命令清单（全部只读）

```bash
# 0) 仓库内证据
cd D:/AI/capy/worktrees/jam_01M8EY6HG7AH3VEFZ9A2MW35MT/zhihui-gateway
git log --oneline -1; git rev-list --count HEAD; git ls-files | wc -l
grep -n '"current"\|"upstream_go"\|"manager"' fork-manifest.json
grep -n "v2.11.14" UPSTREAM-NOTICES.md docs/UPSTREAM-REVIEW-2026-11-14.md docs/UPSTREAM-REVIEW-2026-11-15.md
ls licenses/ 2>&1; ls docs/upstream-licenses/
git ls-files '*.go'

# 1) 上游元数据
curl -s https://api.github.com/repos/ithtelab/workbuddy-manager | grep -E 'stargazers_count|created_at|pushed_at'
curl -s https://api.github.com/repos/ithtelab/workbuddy-manager/releases/latest | grep tag_name
curl -s "https://api.github.com/repos/ithtelab/workbuddy-manager/commits?since=2026-11-22&per_page=100" | grep -c '"sha"'

# 2) 逐 tag 比对（在系统临时目录，不写仓库）
git clone --depth 1 --branch v2.11.23 https://github.com/ithtelab/workbuddy-manager.git up
cd up
git remote add ours D:/AI/capy/worktrees/jam_01M8EY6HG7AH3VEFZ9A2MW35MT/zhihui-gateway
git fetch --depth 1 ours main:refs/heads/ours
git diff --numstat ours v2.8.3  -- server web | awk '{f++; a+=$1; d+=$2} END {print f, a, d}'
git diff --numstat ours v2.11.23 -- server web | awk '{f++; a+=$1; d+=$2} END {print f, a, d}'
git diff --numstat v2.8.3 v2.11.23 -- server web | awk '{f++; a+=$1; d+=$2} END {print f, a, d}'

# 3) 冲突面
git diff --name-only v2.8.3 v2.11.23 | sort > /tmp/up.txt
git diff --name-only ours v2.8.3 | sort > /tmp/ours.txt
comm -12 /tmp/up.txt /tmp/ours.txt | wc -l     # → 110
```

---

**未取得 / 未核实**（不猜）：
- 上游 v2.9–v2.11 的 release note **逐条**对照（本次只按「文件是否存在」对照，未逐条核功能语义）。
- 上游 v2.9–v2.11 的**安全类**改动全量核对（只核了 v2.10.2 一组）。
- Qoder Hub / TraeWorkAssistant 的版本与许可证（不在本线程范围，`docs/UPSTREAM-REVIEW-2026-11-14.md` 有记录但未复核）。
- 同行产品的**企业版**能力与价格（LiteLLM Enterprise、Portkey Enterprise、Kong Konnect）——本次只核了开源版文档。
