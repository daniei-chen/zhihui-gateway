# 前端业务源码补充全文阅读与授权 P2 修复（最终冻结）

仓库：`/workspace/scratch/913dc6e1a7c0/zhihui-gateway`，分支 `audit/strict-review-20261105`，基线 `4c6b4cb8cb790698e0753909745dfe8a7c78dd9f`。初始补读阶段遵从业务冻结约束，仅阅读全文、检查具体调用链和写审计材料；其后 root 根据用户“全部优化”授权局部修复下列 10 项 P2。本文件现已合并两个阶段。最终没有提交、推送、部署、真实供应商请求或浏览器性能测试。

## 覆盖与结论

按原 `frontend-coverage.tsv` 中非 `manual-full` 的生产业务 TS/TSX 源码建立队列；排除 tests、开发检查脚本、生成文件，以及由 gateway_audit 独立负责的 `components/ui/**` 和 `components/animate-ui/**`。本阶段新增 **106 个文件、21,492 行完整人工阅读**：app 33 个 / 9,929 行，components 48 个 / 8,040 行，hooks 2 个 / 146 行，lib 23 个 / 3,377 行。包含 `lib/types.ts` 的全部 1,522 行类型和契约注释。

实际逐文件范围、当前 SHA-256、完整行号区间在 `frontend-business-extra-reviewed.tsv`；`frontend-business-extra-queue.tsv` 仅是最初队列，不能单独作为阅读证据。完成后核对两者集合一致且所有 106 个摘要与工作树相符。加上此前的 `manual-full` 范围，指定 app/components（不含独立 UI primitive 责任区）/hooks/lib 的非测试生产 TS/TSX 已没有尚未全文阅读的文件。剩余 `next-env.d.ts` 是生成声明；本阶段未将 CSS、JSON 语言词条、设计文档、生成站点或第三方依赖算作逐行审阅。

**没有发现新的、可证明的 P0/P1。** 以下 P2 的原始证据来自静态代码及必要后端调用链；10 项均已完成授权范围内的局部修复，并通过实际组件逻辑回归及统一门禁。各节保留修复前行号，当前修复位置和约束列于下一节；并非浏览器实测结论。先前已修复的流式解析、认证/请求取消、账号积分作用域、扫码凭据最终保存和 Tencent probe 仍在各自正式记录中。

## 最终处置状态与验证

2026-11-05 的最终业务代码已冻结，`frontend-p2-fixed-files.tsv` 记录 10 个生产文件、3 个前端回归/合同测试文件以及后追加的 2 个 Python 结构合同测试文件的当前 SHA-256。原 106 个补读文件的行数与摘要同步到 `frontend-business-extra-reviewed.tsv`；本阶段原始阅读规模 21,492 行，修复后同一 106 文件共 21,993 行。所有 121 条摘要（106 阅读 + 15 修复文件，存在交集）核对当前工作树成功；这些条目数不能相加当成不同文件数。

| 编号 | 状态 | 当前实现与证据 |
|---|---|---|
| F-BIZ-01 | 已修复 | `keys/page.tsx` 的 upstreamsReady 同时约束 New Key、绑定 Select 和提交函数；目录失败保留常驻错误与重试，不能用缺失目录创建默认绑定。 |
| F-BIZ-02 | 已修复 | issued 弹窗、目标切换和身份/卸载使旧结果失效；导出与本机状态读取分别有代次；导入/检测共用同步写锁，换目标也不会假装取消已发出的本机操作。关闭清空导出、导入结果和一次性明文；迟到创建结果不恢复旧身份凭据。 |
| F-BIZ-03 | 已修复 | `settings/layout.tsx` 要求 modelMap 成功读取才可编辑；unknown 不显示成空表。整份替换串行；成功保存值绑定当前作用域，后续权威读取可替代乐观值，失败保留草稿。 |
| F-BIZ-04 | 已修复 | duration 回填统一采用 `String(raw ?? '').trim()`，与校验相同；undefined/null 不再变成字面文本。 |
| F-BIZ-05 | 已修复 | `security/page.tsx` 的开关和模式共用同步写锁与保存代次，禁用在途整份替换；失败只回退当前写操作，身份变化后旧响应无效。ProxyPool 的整份写入也使用同步锁。 |
| F-BIZ-06 | 已修复 | 红包分享链接带 BASE_PATH；领取页先显示本人已领密钥，再处理领完/过期。draw 成功立即更新已确认的本人数据；关闭后仍可回看，不猜测未返回的全局剩余数。 |
| F-BIZ-07 | 已修复 | 两类计划均有显式失败/重试，unknown 或失败期间禁止写入和运行；保留已确认旧值并标失败。保存/执行互斥，卸载/旧读取不提交结果；full 立即执行必须确认且返回完整执行 Promise。 |
| F-BIZ-08 | 已修复 | Proxy 首次任一配置读取失败均不显示可保存的空白表单；成功读取后才可写，后台刷新保留未保存草稿。整份写入串行并校验所属代次；读取在途 ref 阻止旧按钮快照废弃读取导致 loading 悬挂，删除确认同步禁用。 |
| F-BIZ-09 | 已修复 | ConfirmDialog 的程序成功关闭走同一 close callback，真实 ResetPasswordDialog 密码被清空；同步确认锁阻止连点，在确认过程中不能关闭后重开另一个输入上下文。 |
| F-BIZ-10 | 已修复 | PgSync 完成后定时、最多一条状态读取；隐藏/离开取消、恢复读取、迟到结果受代次约束。config/status API 传递 AbortSignal；先前轮询不能覆盖新运行。初始状态因隐藏失效时保持 unknown，UI 与 run 函数都不将其当 idle。 |

统一门禁：`npm test` **226/226，0 fail / 0 skip**；`npm run typecheck` 通过；`npm run lint` **0 errors / 79 warnings**；`npm run build:export` 通过，Next.js 16.4.3 报告静态生成 **30/30**（路由表列 28 项）；`git diff --check` 通过。最新日志为 `frontend-final-p2-{tests,typecheck,lint,build}.log`，结构化 lint 为 `frontend-final-p2-lint.json`。

新增测试共 **33 项**：`tests/audit-business-state.test.cjs` 19 项 + `tests/audit-plan-claim.test.cjs` 14 项。它们执行真实 TSX 源码与真实事件处理函数，通过可控 Promise/时钟模拟失败、连续点击、迟到返回、切换上下文、隐藏与卸载；没有复制业务实现作为被测函数，也没有用源码正则替代这些竞态断言。已有 panel-reader 的 fieldset 合同仅更新为当前更严格的 writable 谓词。测试不是 DOM/Playwright 或真实供应商验证。

独立交叉复核发现并在冻结前修复了三个窄边界：Proxy 读取刚启动时旧写回调造成永久 loading；PgSync 初始 status 因隐藏失效时 unknown 被当 idle；Proxy portal 删除确认未同步 writable。两个时序回归及删除确认属性验证已加入前述 19 项中。最终未发现新引入 P0/P1。

### Lint 与实际限制

相比先前 75 条警告，新增加的 4 条均是仓库现有配置设为 warn 的 `react-hooks/set-state-in-effect`：keys 的身份变化清除明文/结果；security 的旧身份保存状态清理；settings 的身份或独立 tab 切换清理 map 状态；PgSync 新身份读取前清除旧连接/运行状态并标记 loading。没有新增 suppression，也没有用微任务/定时器掩盖。若彻底去掉需改为 keyed 内层组件或统一作用域 state，这是后续结构调整；本轮保持局部安全修复，明确保留 **79**。

同步锁只序列化当前组件的写入，不能替代服务端 revision/CAS：另一个浏览器或管理员仍可能合法并发修改整份配置。除 PgSync config/status 外，旧 API 没有新增 HTTP 取消合同；失效结果被丢弃不代表已经发出的本机导入、配置保存或领取服务端操作被撤销。未知状态遵从 fail-closed UI，但服务端仍是最终授权、任务互斥和幂等边界。

最终导出 258 文件 / 5,595,234 bytes；94 个 JS / 3,379,458 原始 bytes，逐文件 gzip 和 1,068,261 bytes；4 个 CSS / 143,290 bytes。生产 JS 未发现公开演示身份标记。记录在 `frontend-final-p2-export-stats.json`。没有据此声称首屏、TTFT、token/s 或 WAN p95/p99 提升。

## 原始发现证据（保留修复前位置）

## F-BIZ-01（P2）：密钥表单依赖缺失仍允许创建/保存

`app/(main)/keys/page.tsx:127` 的 `useAsyncAll` 逐项独立失败；只要 keys 成功、upstreams 失败，页面仍显示 New Key。1046 附近上游 Select 未按读取失败禁用；submit 286 未要求 upstreams 可用，默认 `form.upstream_id=null`。页面顶部虽然展示 `upstreamsFailed` 提示，但表单仍可提交默认绑定。

这与该页 123 附近“上游列表是表单全部选项，宁可同生共死”的注释不一致。条件影响为管理员在上游目录不可用时把新 key 绑定默认池，或编辑现有 key 却无法看到完整候选池。服务器权限仍有效，未证明未授权访问。应要求可验证的上游列表后才启用绑定字段，或让未加载状态阻止会改变绑定的提交。

## F-BIZ-02（P2）：导出/导入结果缺少 issued/target 代次

`keys/page.tsx:404–480` 的 doExport/doImport/doDetect 异步回调直接 setExported/setImported/setImportStatus。目标选择器和一次性密钥弹窗在等待期间可以关闭/换目标；同步切换时清空结果（1143、1187），旧响应仍可稍后重新写入。可能出现界面已选 Codex、展示旧 Claude 片段，或新密钥弹窗收到旧密钥导出内容。当前只有初始 importLocalStatus effect 有 alive fence。

应把结果绑定到触发时的 issued+target，或冻结对应上下文并忽略过期响应。这不等于服务端 key 泄露：客户端本已被授权持有该一次性密钥；问题在错误归属与误复制。

## F-BIZ-03（P2）：模型映射首次读取失败后仍可全量覆盖

`app/(main)/settings/layout.tsx:990` 使用 `optimisticMap ?? values.modelMap ?? {}`；当 cfg 成功但 modelMap 首次失败，模型别名仍显示空表。1995 的添加按钮只检查管理员、别名和目标；1269 的 saveModelMap 把整份对象提交。`lib/api.ts:792–794` 使用 POST，后端 `manager/server/routers/settings.py:95–117` 明确 `db.set_setting('model_map', clean)` 全量替换。

因此初始映射读取失败后添加一个别名可清除原有其余映射；连续添加/删除也无写入互斥或 revision 条件，乱序整表写入可丢更新。建议读失败时禁用编辑，保存前要求已知 revision，或提供服务端按单别名增删的接口。读取成功后的乐观状态不应掩盖另一请求失败。

## F-BIZ-04（P2）：可空时长字段被回填成 undefined/null 文本

`settings/layout.tsx:197–202` 的 durationOk 先 `String(raw ?? '')`，offWhenZero/allowEmpty 字段把缺失/null 视为空串合法；`pickValues` 803 却再做 `String(raw).trim()`。缺失值于是回填为字面文本 `undefined` 或 `null`，后续字段校验会失败，正常缺省配置显示成无效值。

影响范围是允许空值的 duration 字段，而不是所有配置字段。现有保存按字段变化构建补丁，不能据此声称未修改字段一定被写坏。应把同一归一化值同时用于校验与回填，并明确缺省值语义。

## F-BIZ-05（P2）：安全策略全量保存可以相互覆盖

`app/(main)/security/page.tsx:207–219` 每次保存都 setOptimistic、发送 enabled+mode 整份配置、reload 后无条件撤销乐观状态；303/311 的两个控件仅按 isAdmin 禁用，没有在途锁或请求代次。快速改开关及模式时，旧网络请求可以在新请求之后落库；旧失败也会撤销新请求的乐观显示。

这是合法管理员操作中的状态竞争，不是绕过后端管理员权限。需要串行写入或带 revision 的条件更新，并让显示结果绑定本次保存代次。ProxyPanel.savePool 与模型映射有同类整份对象更新模式。

## F-BIZ-06（P2）：红包链接忽略部署前缀，已领取密钥回看被状态顺序挡住

`app/(main)/red-packets/page.tsx:162–164` 的 claimUrl 固定 `origin + /claim/`，没有 BASE_PATH；非根路径部署时复制链接可能 404 或进入别的站点路径。公开领取页本身的调用 endpoint 已使用 BASE_PATH，两个路径规则不一致。

`app/claim/page.tsx:210–246` 先判断 expired、left<=0，再判断 claimed&&my_key。拿到最后一份的用户重新打开时会优先看到“已领完”，无法使用本页已有的 my_key 回看/复制能力；过期后也一样。不能把该 UI 逻辑说成 key 丢失，后端是否继续保留凭据是另一层问题。成功 draw 后也没有同步 info，关闭弹窗仍显示旧的领取状态。

## F-BIZ-07（P2）：计划读取失败被显示成默认状态/永久加载

`components/common/accounts/RefreshPlanCard.tsx:88–113` 令牌计划先设置 enabled=false、days=5、hour=3。首次请求失败后只 toast 并结束 loading，plan 仍为 null，却渲染默认关闭、从未执行/已禁用。保存的 dirty 判据会阻止直接保存未知计划，但“立即执行”仍可点，且页面没有常驻错误和读取重试。

daily 部分 237–246 读取失败后 draft 仍为 null；295 的 `loading || !draft` 永远渲染 loading，无法表明失败。应显式区分 unknown/failed/loaded，提供重试且不把未知当默认配置。计划卡内 full 的“立即执行”也没有 TaskRunnerPanel 对 full 使用的二次确认，交互约束不一致（仍是授权管理员主动动作）。

## F-BIZ-08（P2）：代理配置读取失败后空表单可清除已保存的出口设置

`components/common/settings/ProxyPanel.tsx:78–103` 的初始 globalForm 为 http/update 空串、enabled=true；load 使用 Promise.all，失败仅 toast，不设置阻止操作的错误态。saveGlobal 202 附近提交全部三个字段，按钮只检查 saving。初次 routes 或 settings 任一路失败后，管理员仍能从默认表单保存。

已核对 `manager/server/routers/proxies.py:247–307`：空串明确清除对应已存 http_proxy/update_proxy，并可能回落到环境代理。因此这不仅是“空列表不好看”，可导致既有出口配置被意外清掉。应在设置成功加载前禁用全局保存，并将新读取与用户未保存的草稿分开；当前 probe/load 刷新也会覆盖在编辑中的全局表单。

## F-BIZ-09（P2）：成功确认关闭未触发调用方清除密码回调

`components/common/layout/ConfirmDialog.tsx:65–68` 只在 Radix 的 onOpenChange 回调里通知父组件；确认成功 92 直接 setOpen(false)，不会调用外部 onOpenChange(false)。`components/common/settings/ResetPasswordDialog.tsx:63–65` 依赖该回调 setPwd('')，成功重置他人密码时没有独立清理。

结果是该 dialog 实例的密码 state 在成功后仍留存，再打开同一重置弹窗可回填刚才的密码；这违背本组件“关闭即清空”的注释。未证明跨用户或跨会话泄露，也没有外传路径，不提升为 P1。应统一 close helper，确保所有关闭路径通知外部并清空敏感表单。

## F-BIZ-10（P2）：数据库备份状态轮询无互斥/可见性门控

`components/common/settings/PgSyncPanel.tsx:86–107` 使用 setInterval，每 1.5 秒（运行中）或 15 秒发起 status 请求，无 in-flight 锁、代次检查、AbortController 或 document.hidden 判断。响应慢于周期时会重叠；旧响应晚到可能把 newer running 状态覆盖，后台标签页也会持续请求。切走只清 interval，既有请求继续。

延迟优化应改完成后定时或在途互斥轮询，离开/隐藏时取消或暂停；对请求返回绑定代次。不能仅缩短轮询间隔来声称“提速”，那会增加排队负载。此阶段未生成新压测数据。

## 性能与维护观察（条件性，未作为 P0/P1）

- `dashboard/page.tsx:123–197` 对 N 个分组并发发起 2N 个账号/状态请求和 N 个缓存积分请求，各自还取分组目录；无前端并发上限。供应商查询没有在这条首屏积分路径触发，注释的 provider_called=false 与代码相符。大量分组时可引入有上限批量接口并保留每组失败语义；不能直接以共享一次失败把所有独立数据都清空。
- `ModelWhitelistInput.tsx:82–95` 首次打开会并发取两种 realm 的目录；没有 pending promise 复用，关闭/重开于请求期间可重复两次请求；一次瞬时错误把 failed 置真后本实例不再重试。适合加在途 promise 去重和明确重试。
- 公开日志、任务和主要统计列表已有数量上限或独立懒加载 chart chunk；账号/成员/密钥等仍以全列表映射渲染。数量很大时的 CPU/DOM 成本需要实际大数据集测量，不能仅凭阅读宣布所有页面已达最佳延迟。
- 一些传统编辑弹窗（AccountNote/Weight/Cooling、UpstreamForm）允许保存期间关闭/重新打开而未对完成回调绑定目标代次；旧保存完成可关闭新打开的弹窗。后端操作的目标已在调用时捕获，主要是界面结果归属与草稿丢失风险。
- `UpstreamReloadNotice.tsx` 的 dismissed 是整次组件生命周期布尔值，不随不同失败 last_at 重置；关闭一次失败提示后可能压住后续失败。建议按事件标识记录 dismiss。
- 静态类型不能校验运行时 JSON。类型注释存在历史矛盾（例如红包“无需领取页”与已经实现领取页，full 定时注释与 DailyPlan full 开关），应以当前路由和实际业务校验为准；本轮没有据注释推断权限。

## 本轮边界检查结果

本轮审阅的 JSX 自由文本均通过 React 文本/属性渲染；未发现新增可证明的 HTML 注入或任意脚本执行路径。管理 UI 的 isAdmin/fieldset 只是显示和交互约束，审计未把它们当作服务器权限边界。Portal 业务页使用用户身份 key 重新挂载、读取代次/abort 和处置 action_id 等边界，与此前后端审阅独立确认；没有根据前端隐藏按钮推导数据安全。

最初补读阶段只读，不产生新的测试结论；随后授权 P2 修复的新增门禁和真实组件逻辑回归见本文件“最终处置状态与验证”。先前 Python、依赖审计和微基准仍保持其原始范围，没有重命名为本轮浏览器或生产性能证明。没有提交、推送或部署。

## 后端全量门禁中的前端源码合同复核

前端源码冻结后，root 的最终 Manager 隔离全量出现一项 `SecurityPageHonestyTest.test_reload_resolves_after_the_request_lands` 失败。用同一 harness 的 manager-assets 单文件视图准确复现为 **16 passed / 1 failed**：旧源码正则只接受 `if (await reload())`，无法接受现实现更严格的 `if (await reload() && current()) setOptimistic(null)`。不是保存状态行为回归；KeysPageHonesty 两项全部通过。

按授权只改 `manager/server/tests/test_async_state.py` 的该结构断言：移除任意 900 字符取样上限，按下一处同层 handler 确定范围；正则明确要求刷新成功和当前代次两个条件，才允许清空乐观状态，没有削弱“不假保存/不显示旧状态”的要求。前端业务源码、out 和依赖均未改变。

单文件复验 **17/17 passed + 11 subtests，0 skip**。使用 `tests/zhihui_harness/run_tests.py --target manager-assets --test-path server/tests/test_async_state.py --require-built-web`；before run `20261105-211630-df74d613`，after run `20261105-211752-412f80c2`。当前视图摘要 `view_changed_paths=[]`，非回环调用 0，假 provider 调用 0。证据 `frontend-async-state-{before,after}.log` 和仓库 `evidence/runs/<run-id>/`。该 Python 测试仅登记目标路径阅读，不伪称全文件人工覆盖；当时补入修复 SHA 表后该表共 14 行（后续 PgSync 合同追加后为 15 行）。

## PgSync 旧源码形状合同追加复核

同一轮 Manager 全量的三项相邻失败用 manager-assets 单文件准确复现为 `test_pgsync_review.py` **11 passed / 3 failed**，均在 `PgSyncPanelPollingTest`：轮询仍按旧 setInterval 提取，load 回调仍限定空依赖数组，变异守卫仍替换已移除的 pollStatus。before run `20261105-212107-318b78c7`，证据 `frontend-pgsync-contract-before.log`。

仅修改该测试类，保留“进度轮询不得完整 load/写表单、回填必须保护脏输入、保存后清脏标记”的语义。新结构合同要求完成后 setTimeout 的 1.5 秒/15 秒间隔、发请求前 inFlight 门控、AbortController 与传到底层 API 的 signal、状态代次、隐藏/恢复、当前身份依赖、卸载清 timer/abort，以及任务完成后的配置刷新。四个实际源码变异子项证明恢复完整 load、移除互斥、移除取消门控或删除身份依赖均不能通过。业务源码及 out 保持冻结。

最终只组合运行 `test_async_state.py` 与 `test_pgsync_review.py`，使用既有隔离 harness 和真实已构建 web/out：**31/31 passed + 15 subtests，0 skip**。run `20261105-212347-8aaec8cf`；日志 `frontend-contracts-after.log`。非回环调用 0，假 provider 调用 0，隔离视图未变。新增 Python 文件按实际范围登记为 targeted，不伪称全文阅读；当前修复摘要表为 15 个文件（10 生产 + 3 前端测试 + 2 Python 合同测试）。
