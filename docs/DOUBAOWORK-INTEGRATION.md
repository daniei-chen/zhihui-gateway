# 豆包工作（doubaowork）作为第四平台接入

> 状态：**已接入管理面与数据面，适配器对真实上游实测 16/16 通过**。
> 本文只写代码里能核对的结论，不写「应该可以」。

## 1. 定位

豆包工作在 qtg 里的角色与 `workbuddy` **同构**：上游自己有一个反代引擎
（[doubaowork2api](https://github.com/zhaosiqi9/doubaowork2api)，Python，默认
`127.0.0.1:18789`），负责账号池、冷却、并发与 OpenAI 兼容数据面；qtg 只做
**聚合展示 + 受控写入 + 统一入口**，不重造那套状态机。

```
下游客户端 ──sk-qtg──▶ qtg :8795 ──sk-上游──▶ doubaowork2api :18789 ──▶ 豆包工作
                          │
                          └── 管理面：/admin/{login,accounts,config,keys,logs}
```

## 2. 对接面（读自上游 0.3.0 源码，逐条实测过）

| 用途 | 上游入口 | 适配器方法 |
|---|---|---|
| 健康 | `GET /health`（无需会话） | `healthz()` |
| 管理登录 | `POST /admin/login {"password"}` → Set-Cookie | `authenticate()` |
| 配置与模型 | `GET /admin/config` | `settings()` / `models()` |
| 账号池 | `GET /admin/accounts` | `overview()` / `diagnostic_pool()` |
| 密钥 | `GET /admin/keys` | `keys()` |
| 请求日志 | `GET /admin/logs?limit=N` | `logs()` |
| 启停账号 | `PATCH /admin/accounts/{id}` | `set_account()` |
| 清冷却 | `POST /admin/accounts/{id}/clear-cooldown` | `refresh_account()` |
| 导入账号 | `POST /admin/accounts/import` | `add_account()` |
| 数据面 | `POST /v1/chat/completions`、`/v1/responses` | `_THREEWAY_SPEC` 直连 |

**CSRF 说明**：上游 `require_admin` 只在请求**带 `Origin` 头**时校验同源，并拒绝
`Sec-Fetch-Site: cross-site`。服务端到服务端调用不带这两个头，因此正常通过；
qtg **不伪造** `Origin`。

## 3. 权限：新平台不自动授权

这是本次接入最容易出错的地方，单独说明：

- `platformpolicy.PLATFORMS` 扩为四元组（**已知平台全集**，用于识别与校验）。
- `platformpolicy.DEFAULT_PLATFORMS` 保持三元组（**未显式声明时的授予集**）。
- `keysvc` 在密钥**缺 `platforms` 键**时退回 `DEFAULT_PLATFORMS`。注意这是
  **防御性分支**：正常路径下 `keysvc._parse()` 总会写出这个键，所以它平时不会生效。
  真正决定「新建密钥默认拿哪些平台」的是下面 `normalize(None)` 的语义。
- **系统代建路径必须显式传保守集**：`redpacket.py`（红包）内部调 `create_key`
  时原本不传 `platforms`，会走 `normalize(None)` = 全部平台 → 每一张红包密钥
  都会静默获得豆包工作。现已显式传 `DEFAULT_PLATFORMS`。
- 历史迁移 `migrations._key_platforms`（v5）**刻意不改**：它回填的三平台是当年的
  保守集合；且它受 `schemaver` 指纹校验，原地改实现会让数据库拒绝启动
  （实测报 `v5 key_platforms 已应用，但实现指纹变了；改实现必须升版本号，
  不能原地改历史步骤`）。要改历史回填语义必须新开版本。
- 数据面 `forced_platform` 缺省仍是 `workbuddy`；要用豆包工作必须显式：
  `/doubaowork/v1/...`、`X-QTG-Platform: doubaowork`、或模型前缀 `doubaowork/<model>`。

### 一个必须说清的语义差别

`platformpolicy.normalize(None)` 仍返回**全部平台**（含 doubaowork）。也就是：

- **旧密钥**：`platforms` 列在当年就固化成三平台，永远不会因为新增平台而变化；
- **新建密钥且 API 不传 `platforms`**：「不传 = 不限制 = 全部」，因此**会**包含豆包工作；
- **面板里新建**：默认勾的是**当前所在的平台 pill**（`keys/page.tsx` 的
  `openCreate` 用的是 `platforms: [platform]`）。所以在「豆包工作」下点新建密钥，
  不动任何勾选就会得到一把豆包工作密钥。这是既有行为（跟当前平台走），
  不是本次引入的放大；但想「必须先手动勾」的话这里是唯一的入口。

如果你希望「不传」也保守到三平台，把 `DEFAULT_PLATFORMS` 接到 `normalize` 的
默认分支即可——但这会改变既有 API 契约（`normalize(None)` 等于全集是被测试
锁住的行为），属于产品决策，本次没动。

## 4. 数据真实性：上游没有的东西不补 0

| 项目 | 上游实情 | qtg 的处理 |
|---|---|---|
| 额度 | `Account.public_dict()` **无任何余额字段** | `credits_*` 恒 0 且 `note` 写明「上游不提供额度」；概览的积分卡在**存在豆包工作账号**时追加「豆包 上游不提供额度」（引擎开着但 0 账号时不显示这句）；前端在 `unsupported` 时走 info 而不是绿色成功 |
| 用量 | 请求日志只有 `started_at/duration_ms/model/status`，**无 token 计数** | 适配器 `usage()` 如实返回空 + `unsupported:["usage","credits"]`；**stats 页改用 qtg 自己的 `proxy_requests` 按天聚合**（`Store.daily()`），并在图上标注「这是经过本网关的转发量，不含客户端直连」 |
| 签到 | **没有签到接口** | `checkin()` 返回 `ok=False, unsupported=True` 并说明原因，不伪造「已提交」 |
| 冷却时刻 | 只给剩余秒数 | `cooldown_until` 留 `None`，不反推一个看似精确的时间 |
| 版本(国内/国际) | 无此概念 | `realm=""`（空 = 未提供），不照抄别家默认值 |
| 多平台优选 | 余额未知 | `_platform_credits` **不含**豆包工作，避免用未知值参与排序 |

## 5. 启用

```bash
# 1) 上游先跑起来
docker compose up -d            # doubaowork2api，监听 127.0.0.1:18789

# 2) qtg 侧登记三个变量（已写入 qtgcore/config-schema.json，未登记会被
#    configcontract 直接拒绝启动）
export QTG_DOUBAOWORK_ENGINE=on
export QTG_DOUBAOWORK_BASE=http://127.0.0.1:18789
export QTG_DOUBAOWORK_ADMIN_PASSWORD_FILE=/opt/doubaowork2api/secrets/admin_password

# 3) 数据面密钥（上游自己的 sk-，不是 qtg 的 sk-qtg）
echo -n 'sk-xxxx' > /opt/workbuddy-manager/engine/secrets/doubaowork_upstream_key
```

`QTG_DOUBAOWORK_ENGINE` 默认 **off**：新增平台不得改变现网行为。

它走的是与 `QTG_WB_ENGINE` **同一套「开关文件」机制**
（`_doubaowork_engine_default()` → `_file_switch(...)`，优先级：
`QTG_DOUBAOWORK_ENGINE` > `/opt/workbuddy-manager/engine/var/doubaowork_engine` > `off`）。
这一点很关键：服务由 systemd 启动，改 unit 里的 `Environment` 要动 systemd 单元，
所以四个平台的引擎开关都以文件为准，写完重启即可生效。

可选：数据面地址可以用路由文件切换
（`/opt/workbuddy-manager/engine/var/doubaowork_gateway_base`；覆盖它的环境变量名
与 qoder/trae 同款，是 **`WB_DOUBAOWORK_UPSTREAM_FILE`**，见 `routers/gateway.py`
的 `_THREEWAY_ROUTE_FILES`/`_threeway_base`。只接受 `http://127.0.0.1:<port>`
形式，解析失败回退默认值）。

### 接入后的自检（在目标机器上照抄即可）

```bash
# 1) 上游活着（无需会话）
curl -fsS http://127.0.0.1:18789/health && echo

# 2) 管理密码文件存在且非空
sudo test -s /opt/doubaowork2api/secrets/admin_password && echo "password file ok"

# 3) qtg 把第四平台挂上了（enabled 应为 true；引擎 off 时是 false 且 note 说明原因）
curl -fsS 'http://127.0.0.1:8795/api/platforms' \
  -H "Cookie: <管理端会话>" | python -c "import json,sys; [print(p['id'], p['enabled'], p['note']) for p in json.load(sys.stdin)['platforms']]"

# 4) 适配器契约（需要 2 与 1 都成立）
DOUBAOWORK_ADMIN_PASSWORD_FILE=/opt/doubaowork2api/secrets/admin_password \
  python tools/verify_doubaowork_adapter.py

# 5) 聚合层（不依赖网络）
python tools/verify_doubaowork_aggregate.py

# 6) 签到链路诊断（**默认只读**，不领额度）
python tools/probe_doubaowork_checkin.py --accounts-dir /opt/doubaowork2api/accounts

# 7) 确认无误后再真签（会真的领额度）
python tools/probe_doubaowork_checkin.py --accounts-dir /opt/doubaowork2api/accounts --claim
```

第 6 条是**逐段输出原始响应**的诊断工具（launch / detail / 空 query 对照 / 领取 /
回查各一段），出问题时能立刻定位是哪个端点、哪个字段不对，而不是只得到
「签到失败」。它默认只读——诊断不该有副作用把当天额度领掉。

第 3 步是最直接的「是否接进去了」的判据：它同时验证了 `PLATFORMS`、
`_CAP_MATRIX`、`enabled` 与 `note` 四处是否一致。

## 6. 兼容层

外部面板按 `upstream_id` 分组，豆包工作是 **4**：

| upstream_id | 平台 | 名称 |
|---|---|---|
| 1 | qoder | Qoder |
| 2 | trae | Trae |
| 3 | workbuddy | WorkBuddy |
| 4 | doubaowork | 豆包工作 |

`/api/platforms`、`/api/models`、`/api/upstreams`、批量动作分派均已包含第 4 组。

## 7. 验证记录（可复现）

> 两个验证脚本已收进仓库的 `tools/`：
> · `tools/verify_doubaowork_aggregate.py` —— **可直接跑**（21/21，不依赖网络/上游）；
> · `tools/verify_doubaowork_adapter.py` —— 需要真实 doubaowork2api 在
>   `127.0.0.1:18789` 且 `DOUBAOWORK_ADMIN_PASSWORD_FILE` 指向其管理密码；
>   **CI 不跑**，是手工验收脚本。

| 项目 | 结果 | 证据 |
|---|---|---|
| 新增后端单测 | **47/47** | `server/tests/test_doubaowork_adapter.py`（21）、`test_doubaowork_platform.py`（26） |
| 适配器 vs 真实上游 | **16/16** | `tools/verify_doubaowork_adapter.py`（真跑 doubaowork2api 0.3.0：8 模型 / health / 账号池 / 日志 / 不支持项 / 路径注入被拒 / 非法 JSON 被拒 / 404 明确报错；上游 access log 可对照） |
| 聚合层输出契约 | **21/21** | `tools/verify_doubaowork_aggregate.py`（四平台 summary、credits 恒 0 不为 None、busy→ready、cooldown 不反推） |
| 前端类型检查 | 通过 | `web> npx tsc --noEmit` → exit 0 |
| 前端单元测试 | **243/243**（含新增 10 条第四平台用例） | `web> npm test` |
| 前端构建 | 通过 | `web> npm run build`，27 条路由全部 prerender |
| **后端全量回归** | **零代码回归** | 分段跑（4 片）：`1364+899+1627+910 = 4800 tests`，合计 `failures=26 / errors=42 / skipped=42`。失败用例集合与基线用 `Compare-Object` 逐条比对，**双向差集为空**（65 个唯一方法名完全相同）。基线：`4753 tests / 26F / 42E / 42S` |
| **结构性守卫** | **8/8** | `server/tests/test_platform_integration_guards.py`：能力矩阵/upstream_id/AccountsResponse 字段/适配器导出/`main.py` 路由白名单/DEFAULT_PLATFORMS 真子集/引擎开关走 `_file_switch`/密钥路径不漂移 |

> 那 65 个既有失败**在基线树与改动树上逐条同名复现**，属于既有的 Windows/node 环境问题
> （符号链接与 umask、`node` 直接跑 `.ts` 的 GBK 解码、缺 `OPENAI_BASE_URL` 的 live 用例等），
> 不是本次引入。
>
> **怎么判断自己踩了测试态污染**（本仓库的既有问题，与本改动无关，但会制造假失败）：
>
> ```bash
> python -c "from server import operational as o; r=o.readiness(); print(r['ok'], r['ledger_backlog'])"
> ```
>
> 第二项非 0 就是被污染了 —— 此时 `test_drain` 的 readiness 用例、`test_instance_runtime`
> 的 lifespan 用例会假失败。清掉 `data/` 重跑即可（`data/` 在 `.gitignore` 里，可安全删除）。
> 我在这轮里踩过两次：一次是并发跑两个全量回归，一次是跑完没清态就接着跑。

### 过程中修掉的真实缺陷

1. **`authz.platform_of` 权限放大**（BLOCKER 级）：它写死 `("qoder","trae","workbuddy")`
   三处判断，豆包工作请求全部落空并返回哨兵 `"三平台"` —— 下游把哨兵当「不限制」，
   等于新平台被当全平台放行。已改为取 `platformpolicy.PLATFORMS`。
2. **路径穿越**：账号 id 正则 `^[A-Za-z0-9._:-]{1,128}$` 允许 `.`，`..` 能通过校验，
   拼出 `/admin/accounts/..` 被 HTTP 客户端规范化成 `/admin`。已加「首字符不得为 `.`」。
3. **`services/platformauth._adapter` 误报**：用 `getattr(rt, platform)` 取适配器，
   但适配器挂在 `rt.dbw`，导致豆包工作被报成「授权服务未配置」（把「我们没接上」
   说成「部署没配」）。已加属性名映射。
4. **`configcontract` 白名单**：新环境变量未登记会让**整个进程拒绝启动**
   （`ConfigurationError: unknown_variable`）。三个 `QTG_DOUBAOWORK_*` 已写入
   `qtgcore/config-schema.json`。
5. **迁移指纹**：曾试图把 `migrations._key_platforms` 改成引用常量，触发
   `v5 key_platforms 应用后指纹变了` 导致数据库拒绝启动 —— 已回滚，并加注释说明
   历史迁移不可原地改。
6. **密钥平台徽章**：`keys/page.tsx` 用三元表达式渲染平台名，加第四个平台会把
   豆包工作显示成「Trae」。已改为显式标签表。
7. **`main.py` 平台路由白名单**（独立子代理审计发现，最严重的一处）：
   `_qtg_platform_route` 的判定是 `platform in ("qoder","trae")`，不含 doubaowork。
   前端切到豆包工作后，`/api/accounts`、`/api/status`、`/api/models`、`/api/stats/*`
   **根本不会路由到 qtgcore**，而是落到 manager 原生 handler —— 读的是 WorkBuddy
   的账号池，页面上传/停用等写操作也会写进 WorkBuddy 的目录。已补白名单，
   并把 `test_audit_20261029_security` 的平台遍历从两元组扩到三元组作为回归保护。
8. **`platform-context.tsx` 的 `label` 三元未穷尽**：`platform === 'trae' ? 'Trae' :
   ... : 'Qoder'`，豆包工作会显示成 **Qoder**，导致账号页/首页副标题
   「{platform} 账号池」、添加账号弹窗标题全部写错。已改为 `Record<Platform,string>`，
   新增平台时 tsc 会强制补齐。
9. **添加账号弹窗描述**：`unsupported` 对豆包工作为真，描述会渲染成「当前 **Trae**
   引擎尚未接入国际版授权」，与同一弹窗下方的状态文字自相矛盾。已加豆包工作优先判断。
10. **密钥徽章兜底集合**：列表在 `platforms` 缺失时用「可选平台」数组兜底，
    会把豆包工作徽章画在一把没有该权限的密钥上（显示层放大权限）。已拆出
    `LEGACY_DEFAULT_PLATFORMS`（三平台），与后端 `DEFAULT_PLATFORMS` 对齐。
11. **`/api/accounts/refresh-tokens` 与 `/refresh-plan/run`** 用
    `rt.trae if ... else rt.qoder` 选适配器，豆包账号会被拿去问 Qoder 刷令牌。
    已统一走 `_adapter_of()`（含 `doubaowork→dbw` 映射）。
12. **签到记录串台**：`compat.py` 的 `else` 分支读 `agg.trae_raw["checkin"]`，
    豆包工作会显示 Trae 的签到流水。已加显式分支返回空。
13. **`/api/logs` 平台降级成「不过滤」**：`if plat not in ("qoder","trae"): plat = ""`，
    豆包工作的日志页会显示**全网关**（三家混在一起）的流水。已把 doubaowork
    加入白名单（`main.py` 补白名单后这条才变可达，独立审计发现）。
14. **`/api/models`、`/api/model-catalog`、`/api/playground/models` 必 500**：
    `_load_models()` 的字典与循环只有三平台，使用处 `data[plat]` 直接 `KeyError`。
    已补第四项，并把三处改成 `data.get(plat) or []`（两道防线）。
15. **`runtime.health_snapshot` 直接访问 `self.dbw`**：单测夹具用
    `SimpleNamespace` 时 `AttributeError` —— 这是本次**唯一**的测试回归
    （`test_steward`）。已改 `getattr(self,'dbw',None)`。
16. **`/api/platforms` 第四组是空壳**：无 `_NAME`/`_UPID`/`_CAP_MATRIX` 条目，
    外部面板拿到 `name:"doubaowork"`、`upstream_id:0`、`capabilities:{}`。
    已按适配器**真实能力**补齐（`checkin:false`、`save_keys:false`、`oauth:false`、
    `refresh_account/set_account/add_account:true`），并在引擎 off 时标注。
17. **`checkin-all` 把「不支持」渲染成「签到完成 0/0」**：批量入口没有 doubaowork
    分支，落进通用汇总。已提前返回明确文案，不让「没有这个功能」变成「跑过一遍都失败了」。
18. **`refresh-credits` 把未知额度报成「刷新成功、余额 0」**：豆包工作
    `credits_remain` 恒 0.0（不是 None），通用逻辑会算成功。已改为 `unsupported: true`。
19. **登录失败把上游响应体拼进异常**：上游是 FastAPI，422 会把请求体（含**明文
    管理密码**）回显在 `detail.input` 里，而该异常会进 `last_error`、health、
    前端提示与审计详情。已改为只报状态码。
20. **红包密钥静默获得第四平台**：`redpacket.py` 内部调 `create_key` 时**从不**传
    `platforms`，走的是 `normalize(None)` = 全部平台。已显式传 `DEFAULT_PLATFORMS`。
21. **`/v1/models` 的 realm 裁剪**把 workbuddy 的 cn/global 维度套到豆包工作上
    （它 realm 恒空）→ 密钥 realm=global 时模型列表为空。已把 doubaowork 加入
    「不做 realm 裁剪」的名单。
22. **`platformauth.start()` 对豆包工作报「上游授权链接格式异常」**：适配器明确
    返回 `ok:false`，但流程继续去校验一个不存在的 URL。已改为把适配器给的原因
    以 409 透出。
23. **回滚开关是空操作**（验证审计抓出的最严重一项）：`doubaowork_engine` 第一版
    只写了 `_env_str("doubaowork_engine","off")`，**没有** `_file_switch`，
    于是「写 `var/doubaowork_engine` + 重启」完全不生效——运维以为关掉了，平台还开着。
    已补 `_doubaowork_engine_default()`，与 `_wb_engine_default()` 同构。
24. **`set_account` 没有回读也报成功**：上游 PATCH 返回 204 或 body 缺 `enabled`
    时，原实现直接 `ok:true/completed`。已改为 `state:"unknown"` + 明确原因
    （「没校验过」不能当成「校验通过」）。
25. **前端不消费 `unsupported`**：额度刷新在豆包工作下会弹绿色「积分已刷新 0/0」，
    签到批量把「没有这个接口」降级成「没有可签到的账号」。已在 `accounts` 页
    按 `unsupported` 走 info + 后端原因，并补五语言 `accounts.creditsUnsupported`。
26. **`refresh-credits` 在引擎 off（默认）时返回 502**：判断在
    `build_pool_snapshot()` 之后，`UpstreamError` 先抛出来了。已在
    `fixed_accounts`/`fixed_status` 把「平台未启用/上游不可达」转成 409 + 明确文案。
27. **`RealmToggle` 的横幅仍是三元**：豆包工作 + 国际版会显示「当前为 **Qoder**
    国际版视图」。已改 `Record<Platform,string>` 穷尽映射。
28. **`PUT /api/accounts/{file}/disabled`**：非法账号 id 的 `ValueError` 会冒成
    500；且「平台未启用」文案对所有平台都写 `QTG_WB_ENGINE=off`。已转 400
    并按平台给出正确的开关名。
29. **用量/额度的「未知」没有贯通到契约**：`points["doubaowork"]` 只是空列表，
    前端分不清「这段时间真的没调用」和「上游根本不给数据」，`dailySeries` 会把
    空数组补齐成 n 个 0 点 → 画出一条**看起来真实的 0 用量曲线**。
    已给 `UsageResponse` 加 `unsupported`/`notes`，`compat_stats._merge_daily`
    对豆包工作返回 `[]`，前端在 `daily.length === 0` 时走空状态而不是补零。
30. **`/api/upstreams` 在引擎 off 时 502**：与 `/api/accounts` 语义不一致。
    已统一为 409 + 明确文案。
31. **`_rt()` 在控制面未初始化时冒 `AttributeError`**：`load_settings()` 任何
    校验失败（QTG_* 未登记、路径非绝对）都会整块跳过注入，只剩一行
    `logger.exception`，运维看到的是 Python 堆栈。已改成可读的 UpstreamError，
    指出「去查 'qoder/trae control plane failed' 那条堆栈」。
32. **`/api/upstreams` 的 `has_key`/`api_key_masked` 是硬编码**：豆包密钥文件不存在
    也显示「已配置」。已改为按**密钥文件真实存在**计算（`_key_state()`），
    路径与数据面 `_THREEWAY_SPEC` 一致，并由守卫测试校验两处不漂移。
33. **数据面给豆包工作的请求加了 WorkBuddy 专属 header**：`gateway.py` 的判据是
    `if platform not in ('qoder','trae')` —— 新平台落进「非直连」分支，被塞了
    `X-WB-Attempt-Budget`/`X-WB-Remaining-Ms` 并打开了 provider 预算逻辑。
    判据已与 `_THREEWAY_SPEC` 对齐（三种直连平台都不加）。
34. **`/api/stats/by-key` 对豆包工作返回含糊的 503**：已明确成 409 +「上游不提供
    用量接口，无法按密钥统计」。
35. **Steward 快照漏掉第四平台**：`collect()` 只探三路，管家对豆包工作一无所知
    却像全知。已补第四路（引擎 off 时如实给 `adapter_unavailable`）。

### 顺手修掉的测试卫生与死代码

36. **`test_drain` 的 readiness 用例依赖全局账本状态**：它断言
    `readiness()['ok']` 为真，但该值同时取决于 `ledger_backlog`/
    `ledger_channel_ok`——同一进程里前面的用例留下 spool 积压就假失败
    （我踩了三次、审计员踩了一次）。已把这两个判据在用例内固定住，它现在
    只测 draining 这一件事。
37. **`_STATE_MAP` 是死代码**：适配器里定义了一份状态映射但从未引用，
    而 `service.map_doubaowork_account()` 里另写了 if/elif —— 两套口径迟早漂移。
    已删除，只留 service 一处。
38. **401 重登的注释与实现不符**：注释说「401 会由 BaseAdapter 自动重登一次」，
    但重登条件含 `retry`，而写路径全部 `retry=False`。已改成事实：
    **只读路径会重登，写路径直接抛**（重试与否取决于幂等性，不能靠框架默认）。

### 未验证（诚实列出）

- **完整 qtg 进程未在 Windows 上跑通**：`qtgcore/config.py` 的 `_abs()` 把
  `/opt/...` 判为非绝对路径，runtime 初始化失败（`qoder/trae control plane failed`，
  进程本身仍启动）。这是部署平台限制，不是本次改动引入；适配器与聚合层已用独立
  脚本直连真实上游/直接调用验证。
- **真实豆包工作账号未接入**：没有可用账号，账号列表实测为 0。账号写入路径的成功
  分支（导入真实 cookie 文档、启停真实账号）只做了契约级验证。
- **数据面端到端**：`_THREEWAY_SPEC` 直连需要上游 `sk-` 与真实账号才能产生一次
  真实对话，本次未执行。
- **`capabilities.py` 是死模块 —— 不需要往里加**。2026-11-16 核实：全仓没有任何
  模块 `import` 它，`capability_index()` / `capabilities_payload()` 没有调用点，
  它自己文档里写的 `GET /api/v1/capabilities` 端点也不存在（`main.py` / `routeaudit.py`
  只是把 "capabilities" 当成一个允许的 blueprint 名字）。**真正的、对外生效的能力矩阵
  是 `compat_fixes._CAP_MATRIX`**（`/api/platforms` 用的那个），已经纳入第四平台。
  往死代码里加数据只会制造「看起来做了」的错觉。
- **测试台（playground）支持豆包工作**：`routers/playground.py` 的平台分支是
  `_plat in gateway._THREEWAY_SPEC`，而 doubaowork 已在其中。密钥文件存在就正常转发；
  不存在则 `_threeway_upstream` 返回 `None` → **503「doubaowork 上游不可用」**，
  不会静默回落到 WorkBuddy 的 key。守卫测试锁住了这两条。
- **Steward 已贯通到第四平台**：后端 `collect()` 覆盖四路（引擎未启用时
  `platform_fact` 给 `state='unknown', reason='adapter_unavailable'`——是「没接上」，
  不是「健康」）；前端 `StewardPanel` 也改成按 `PLATFORMS` 遍历 + `PLATFORM_LABEL`
  映射，不再写死三平台与三元显示名。`tests/steward.test.cjs` 的 mock 已补上这两个模块。
- **第四个平台 pill 无条件显示**：`QTG_DOUBAOWORK_ENGINE` 默认 `off` 时它仍然可点。
  已加 `PlatformDisabledNotice`：切到未启用的平台时，布局上方会出现一条说明
  （数据来自 `/api/platforms` 的 `enabled` 布尔值），讲清「是引擎没挂，不是账号丢了」。
  没有做成「隐藏 pill」是因为平台选择是纯客户端状态，隐藏会让 URL/持久化里的
  旧值失去对应的 UI，反而更容易让人以为平台被删了。
- **窄屏 pill**：四个 pill 带文字合计约 280–300px，320px 视口会横向滚动。
  已改为 `<420px` 只显示图标（`sr-only` 保留可访问名）。375/390 这些常见机型
  仍显示文字——没必要提前收起来。
- **账号页**：`/api/accounts` 本来就是 WorkBuddy 账号池页（qoder/trae 同样不走这里），
  本次未改；豆包工作的账号维护在上游面板，添加账号对话框已改为明确引导
  「导出 JSON 再上传」而不是让用户点进 OAuth 才报错。


## 8. 回滚

```bash
echo off > /opt/workbuddy-manager/engine/var/doubaowork_engine
systemctl restart qtg
```

或 `QTG_DOUBAOWORK_ENGINE=off`。关闭后 `rt.dbw is None`，聚合跳过该平台，
其余三平台行为与改动前完全一致。

> 这个开关**真的生效**：`config.py` 的 `_doubaowork_engine_default()` 用
> `_file_switch` 读 `var/doubaowork_engine`，与 `wb_engine` 同构。第一版实现
> 只写了 `_env_str(...,"off")`，导致「写文件 + 重启」是**空操作**（运维以为关掉了，
> 平台其实还开着），这是独立审计抓出来的，已修。

## 9. 发版时需要补的

本次改动**没有动 `VERSION` 与 `CHANGELOG.md`**：发版在本仓库是独立流程
（见 `docs/release-process.md`），擅自改版本号会干扰签名发布链。正式发版时请：

1. 在 `CHANGELOG.md` 顶部补一节，按既有格式记录第四平台接入；
2. 按 release-process 走签名与门禁；
3. 若 `capabilities.py` 的能力矩阵要纳入 doubaowork，需先逐条验证再声明
   （该模块是「已验证能力」的自证清单，不是愿望清单）。

## 10. 再接入第五个平台：必改清单

这一节是给**下一个做同样事情的人/AI** 的。本次接入一共碰了 **62 个文件**
（51 个已跟踪文件被修改 + 11 个新增；`git status` 会多报一个
`docs/DEVELOPMENT.md`，那是 Windows 大小写不敏感克隆的伪影，不是真改动），
其中有一处漏掉会造成最严重后果（串台），特意标出来。

> **这份清单已经被测试固化**：`server/tests/test_platform_integration_guards.py`
> 会检查 `PLATFORMS` 的每个成员是否在能力矩阵、`upstream_id` 映射、
> `AccountsResponse` 字段、适配器导出里都有对应条目，并且会**从 `main.py` 源码里
> 读出平台路由白名单**、与 qtgcore 实际托管数据面的平台集合比对。
> 少改任何一处，这些测试直接变红——不必指望人记得住 15 个注册点。

### 后端注册点（漏一个就有功能缺口）

| # | 文件 | 要改什么 | 漏掉的后果 |
|---|---|---|---|
| 1 | `server/platformpolicy.py` | `PLATFORMS` 加名字 | 策略层不认识它 |
| 2 | `server/qtgcore/adapters/__init__.py` | 导出新适配器 | 导入失败 |
| 3 | `server/qtgcore/config.py` | 字段 + `load_settings` 构造 + **`_xxx_engine_default()`（走 `_file_switch`）** | 回滚开关变空操作 |
| 4 | `server/qtgcore/config-schema.json` | 登记每个 `QTG_*` | **整个进程拒绝启动** |
| 5 | `server/qtgcore/runtime.py` | 实例 + startup/shutdown/health_snapshot | 健康检查漏一路 |
| 6 | `server/qtgcore/models.py` | `Platform` Literal + `AccountsResponse` 字段 | API 校验拒收 |
| 7 | `server/qtgcore/service.py` | mapper + `Aggregate` 字段 + `build_aggregate` 的 `gather`/解包 + `build_pool_snapshot` + `all_accounts` + `accounts_payload` | 首页/账号页看不到 |
| 8 | `routers/{compat,compat_fixes,compat_stats}.py` | `PLATFORMS` 元组、`UPSTREAM_IDS/NAMES`、`_rows_of`、能力矩阵 `_CAP_MATRIX/_NAME/_UPID`、`_load_models` 的字典 | `/api/models` KeyError 500 |
| 9 | `server/routers/gateway.py` | `_THREEWAY_SPEC` + `_THREEWAY_ROUTE_FILES` | 数据面走不通 |
| 10 | `server/services/platformauth.py` | `platform_for()` 白名单 + `_adapter` 的属性名映射 | 报「授权服务未配置」（假原因） |
| 11 | `server/qtgcore/authz.py` | `platform_of()` 必须取 `PLATFORMS` | **权限放大**（落回哨兵当"不限制"） |
| 12 | **`server/main.py` `_qtg_platform_route` 白名单** | 加名字 | **串台**：请求落到 manager 原生 handler，读写都是别的平台的数据 |
| 13 | `server/routers/keys.py` | `platforms` 的 Literal（两处） | 无法给密钥授权新平台 |
| 14 | `server/metering.py` | `route_attribution` 的两处元组 | 用量记到别的平台 |
| 15 | `server/qtgcore/routers/_shared.py` | 无需改，但要知道 `_rt()` 会给出「控制面未初始化」 | — |

### 前端注册点

`web/lib/platform-store.ts`（`Platform` + `PLATFORMS`）、`web/lib/types.ts`、
`components/common/layout/PlatformToggle.tsx`、`keys/page.tsx`、
`accounts/AddAccountDialog.tsx`、`layout/RealmToggle.tsx`、`lib/i18n/locales/` 五语言。

**凡是渲染平台名的地方，一律用 `Record<Platform, string>`，不要用三元表达式。**
三元不穷尽新取值时 TypeScript 不报错，新平台会被静默显示成最后一个分支
（本项目已经在 `label`、`RealmNote` 上各踩过一次）。

### 三条铁律（都由本轮的审计事故换来）

1. **枚举 ≠ 授权**。`PLATFORMS` 是全集，`DEFAULT_PLATFORMS` 是授予集，两者必须分开；
   系统代建路径（红包、门户密钥）必须**显式**传保守集。
2. **迁移不回改**。历史迁移的 `platforms` 默认值保持当年的三平台：`schemaver`
   用源码指纹校验，原地改实现会让已有数据库**拒绝启动**。
3. **上游没有的能力如实报不支持**。返回 `ok:false, unsupported:true` + 原因，
   不要返回看起来成功的空结果，也不要补 0（0 的意思是「余额为零」，不是「取不到」）。

## 11. 每日签到：已实现（走豆包官方网页接口）

上游 `doubaowork2api` 没有签到端点，但**豆包官方的签到本来就是网页接口**，
桌面客户端也只是调它。因此 qtg 直接调这两个接口，不需要任何桌面环境：

```
POST https://www.doubao.com/alice/ug/campaign/v3/detail/?<稳定query>
     {"campaign_group_code": "pc_incentive_task_mvp_v3"}      ← 查状态（只读）
POST https://www.doubao.com/alice/ug/campaign/landing/checkin/?<稳定query>
     {"scene": "pc_incentive_task_mvp_v3"}                    ← 签到（发奖）
```

实现在 `server/qtgcore/adapters/doubao_checkin.py`，适配器的 `checkin()` 调它。

### 凭证从哪来

上游 `/admin/accounts` 的 `public_dict()` **不返回 cookie**（脱敏），但账号
**落盘文件**含完整凭证（`to_document()` 里是 `cookies` 字典，含 `sessionid`）：

```
/opt/doubaowork2api/accounts/*.json  →  {"id": ..., "cookies": {"sessionid": ..., "ttwid": ...}}
```

qtg 与上游同机部署，直接读该目录（`QTG_DOUBAOWORK_ACCOUNTS_DIR`，默认上述路径）。
**账号目录权限必须收紧**，日志里绝不打印 cookie。

### 接口出处在哪（可复核）

抓包固化结果来自 [`smart-open/TraeWorkAssistant`](https://github.com/smart-open/TraeWorkAssistant)
（MIT，480 stars）的 `src-tauri/src/tasks/doubao_checkin.rs`。原作者明确注明：

> msToken/a_bogus 为一次性反爬 token（CLI 无法生成），**实测仅稳定 query 参数 + Cookie 即可通过**。

本项目按此实现，**不伪造** `msToken` / `a_bogus`。

### 行为约定

| 项 | 做法 |
|---|---|
| **发现活动组** | 先 `POST /alice/user/launch` 取 `data.ug_campaign_info.campaign_group_code`。**不写死** `pc_incentive_task_mvp_v3`——官方一换组，写死的实现会静默失败 |
| **详情双路径** | 组等于 `pc_incentive_task_mvp_v3` 走 `/alice/ug/campaign/v3/detail/`，否则走 `/alice/ug/campaign/list/` |
| **任务识别** | 递归遍历 `tasks`/`campaigns`/`milestones`，只认 `daily_checkin.checked_in_today` 是**布尔值**的行；**必须唯一**，出现多个就报「无法确定该领哪个」不猜 |
| **回执判据** | 同时看顶层 `code` 和 **`data.biz_error.code`**。只看顶层会把「回执里 biz_error 非 0」当成成功——那是最危险的一种假成功 |
| **成功判据** | 提交后按 0/2/4 秒**回查**详情，只有 `checked_in_today=true` 才算 `claimed`；回执 `checked_in=true` 但回查没勾选 → `confirming`（**不重提**） |
| **幂等** | `already` 时连 claim 都不发；`confirming` 的账号下一轮只回读、不重复提交 |
| **风控如实上报** | `710012001` 登录态失效 / `712010702` cookie 或指纹缺失 / `710022004` 需人工验证 / `710010702` 需要 SDK 签名 |
| 频率 | 账号之间 800ms 间隔，避免同 IP 连续触发风控 |
| 失败 | 报失败，不编造成功 |

### 关于 `710010702`：我改过三次，第三次才对

| 版本 | 我的解释 | 对不对 |
|---|---|---|
| 第一版 | 「设备指纹与账号不匹配」 | ❌ 错 |
| 第二版 | 「需要 SDK 生成 `biz_params`/`AgwCommonParam`」 | ⚠️ 不准确 |
| **第三版** | **「每日签到尚未开放」（服务端按天开窗，开窗时刻逐日漂移）** | ✅ **有真机证据** |

第三版的证据来自 [`thintsing/agent-checkin`](https://github.com/thintsing/agent-checkin)
的 `recipes/doubaowork.yaml`（2026-10-21 真机取证 + 2026-11-02 掐点实测）：

> claim 回业务码 **710010702 = 「每日签到尚未开放」**
> 掐点实测（11-02）：**10:45 仍拒、10:55 即放行**（reward 400 到账）——每日开放
> 时刻被服务端后移且逐日漂移（10-31 到 11:34 仍未开放）。detail 里只有活动整体
> 起止（2025-07~2030），没有每日开放字段，**无法预报，只能重试覆盖**。

**所以正确反应是重试，不是让人去点按钮。** 本实现把 `not_open_yet` 单独成一个
状态（`retryable: true`），不计入失败。

**已知限制**：`checkin_scheduler` 的重试是 `MAX_ATTEMPTS=6 × RETRY_AFTER=300s`，
即触发后 **30 分钟窗口**。真机记录里最晚的开窗到 11:34，所以**如果本次触发太早，
30 分钟后会放弃**。补救：① 21:00 有 `FALLBACK_HOUR` 兜底一轮；② 次日再跑；
③ 需要更稳就把触发时刻设在下午（`WINDOWS` 里给的 09:05–22:00 随机，可以调窄）。

> 改 `RESULT_KINDS` 让「尚未开放」不消耗重试次数是更彻底的做法，但那会改变
> `validate_summary` 校验的持久化摘要格式，旧 state 文件会校验失败——收益不抵风险，
> 所以没做。

### 任务识别的两种判据（都要认）

两份真机取证用了不同字段：

| 判据 | 出处 | 含义 |
|---|---|---|
| `task_status` | `agent-checkin` 真机实录（`before signStatus:1` → `after signStatus:3`） | **3 = 已签，1 = 未签** |
| `daily_checkin.checked_in_today` | `DoubaoWork-Daily` | 布尔 |

本实现两个都认。**只认后者的话**，某些账号返回里没有 `daily_checkin` 就会被判成
「没有签到任务」——把「能签」误报成「没得签」，比报错更糟。

### 真机验证记录（2026-11-16，真实账号，一次成功）

用浏览器在已登录的 `doubao.com` 里直接 `fetch` 这些接口（浏览器自动带登录态，
全程没有导出 cookie），实测结果：

**① `POST /alice/user/launch` → 失败**

```json
{"code": 710010702, "msg": "系统错误"}
```

**② `POST /alice/ug/campaign/v3/detail/` 无 query → 失败**

```json
{"data": null, "code": 1, "msg": "", "message": "invalid param"}
```

**③ `POST /alice/ug/campaign/v3/detail/` 带稳定 query → 200**

```json
{"data": {"campaign_code": "pc_incentive_task_mvp_v3",
          "tasks": [...8 条...], "milestones": [...3 条...],
          "progress": {"total_amount": 700}}}
```

`tasks[]` 的元素长这样（注意 **`campaign_code` / `task_kind` / `task_status` /
`daily_checkin` 四个字段同时存在**，三份实现的说法都对）：

```json
{"campaign_code": "daily_signin",
 "campaign_group_code": "pc_incentive_task_mvp_v3",
 "task_kind": "daily_signin",
 "task_status": 1,
 "reward_mode": 2,
 "daily_checkin": {"checked_in_today": false,
                   "continuous_days": 0,
                   "last_checkin_date": "2026-11-13",
                   "reward": {"reward_amount": 400, "reward_type": "quota_grant"},
                   "total_days": 3}}
```

**④ `POST /alice/ug/campaign/landing/checkin/` body `{"scene":"pc_incentive_task_mvp_v3"}` → 成功**

```json
{"data": {"checked_in": true, "checkin_date": "2026-11-16",
          "continuous_days": 1, "idempotent": false,
          "reward_amount": 400, "reward_code": "quota_grant",
          "reward_status": 3, "reward_type": "quota_grant"},
 "code": 0}
```

**⑤ 回查确认（这才是成功判据）**

| 字段 | 签到前 | 签到后 |
|---|---|---|
| `checked_in_today` | `false` | **`true`** |
| `task_status` | `1` | **`3`** |
| `last_checkin_date` | `2026-11-13` | **`2026-11-16`** |
| `progress.total_amount` | `700` | **`1100`（+400）** |

**⑥ `task_status` 的三值语义（顺带实测出来）**

`milestones[]` 里 `points_milestone_500` 是 `task_status: 2`，而 2000/5000 是 `1`：

| 值 | 含义 | 依据 |
|---|---|---|
| `1` | 未达成 / 未签 | `daily_signin` 签到前、未达标的 milestone |
| `2` | **已领取** | `points_milestone_500`（当时 `total_amount=1100`，早已过 500） |
| `3` | **已签到** | `daily_signin` 签到后 |

所以**不能**看到 `task_status: 1` 就以为「可以领」——要结合 `milestone_threshold`
和 `progress.total_amount`。`milestones[]` 每项带 `milestone_threshold`
（如 500/2000/5000）和 `reward_summaries[].reward_amount`（200/500/1300），
够不够是算得出来的。

> **未做**：milestone 奖励的自动领取。它的领取端点/请求体与 `landing/checkin`
> 不同（`_walk_tasks` 里看到 `reward_mode=2` 走
> `{task_id, campaign_group_code, campaign_item_code}` 的直领分支），而当前账号
> 500 档已领、2000 档未达成，**没有可实测的样本**。在没有验证过的情况下接进去，
> 就是把「猜的端点」放到会真发奖的路径上——不做。

**结论（三条都是实测，不是推断）**：

1. **纯网页环境就能签**。不需要桌面客户端、不需要 `SamanthaDoubao` UA、
   不需要 `biz_params`/`AgwCommonParam`。活动页上那句「该活动仅支持在豆包电脑版
   参与」只是 **UI 层**的拦截，接口本身不校验。
2. **`launch` 不可依赖**。它会返回 `710010702`，但 `detail` 同时可用 ——
   原实现把 launch 当唯一发现途径，真机上会直接判 `inactive`，**永远签不到**。
   已改为「launch 失败就回退到已知活动组」。
3. **`710010702` 是通用风控拒绝码，不是签到专用**。它在 `launch` 上就出现，
   `msg` 是「系统错误」。所以代码里**不能**用它推断具体原因，要看 `msg`
   和实际调用的是哪个端点。

### 已知限制（都是真话）

1. **需要同机可读账号目录**。跨机部署读不到 → 明确报「没有可签到的账号」。
2. **设备级 cookie 必须随账号一起导入**（`ttwid` / `odin_tt` / `tt_scid` / `s_v_web_id`）。
   只带 `sessionid` 会被 `712010702` 拒——这是发奖端点的强校验，不是我方能绕过的。
3. **活动由官方控制**。`pc_incentive_task_mvp_v3` 下线时，签到会返回
   「该账号没有每日签到任务」，那是事实，不是 bug。
4. **签到窗口是 `11:10–22:00`，不是从 9 点开始**。真机取证显示签到按天开窗、
   时刻逐日漂移（10:45 仍 `710010702`、10:55 才放行、最晚拖到 11:34），
   9 点触发几乎必然白跑一轮。起点定 11:10，让后续重试（5 分钟 × 最多 6 次）
   覆盖到最晚的情况。
5. **记录页已开放，账号页也能看到「今天签没签」**：`tasks`（任务记录）原来只对
   WorkBuddy 显示，现在也对豆包工作显示（`TASK_PLATFORMS = ['workbuddy','doubaowork']`）；
   `_map_account` 的 `checkin_today` 对豆包工作改从 **qtg 自己的历史文件**读
   （`var/doubaowork_checkin.jsonl`，只认 `claimed`/`already` 两种状态）。
   `PageSectionTabs` 的过滤也同步改了——两处漏一处就会出现「导航有入口但页面把用户踢走」。
5. 我没有真实豆包账号，**这条链路只做过契约级验证**（18 条单测覆盖
   已签/成功/风控/无任务/HTTP 错误/detail 失败不提交），没有对真实上游跑过一次。
   第一次真跑请在目标机器上确认。

> 定位变了但红线没变：以前是「上游没有，如实报不支持」；现在是「官方网页接口
> 存在，我们如实去签」。两次都不允许把「没成功」写成「成功」。

---

## 附录 A：v1 上游已接入（`niyouqianma/doubaowork2api`，2026-11-17）

适配器现在**同时支持两个上游版本**，靠 `/health` 的 `version` 字段自动分流。

### 为什么要做双版本而不是直接换

我们现在跑的 v0.3.0（`zhaosiqi9/doubaowork2api`）**在 2026-11-16 真机签到成功过**
（+400 额度），是经过实战的；而 v1.0.0（`niyouqianma/doubaowork2api`，★22）
2026-11-14 才发布。**把一条验证过的链路换成没被时间验证过的，不该是默认动作**，
所以做成分流：谁在跑就用谁的能力。

### 分流规则（只有一处判据）

```python
def _supports_v1(self) -> bool:
    raw = self._upstream_version.strip()      # 来自 /health 的 version 字段
    ...
    return int(raw.split(".", 1)[0]) >= 1
```

**不靠探测端点猜**：`404 Not Found` 和 `405 Method Not Allowed` 在 httpx 里
长得一样，「端点不存在」和「端点存在但参数错」区分不开。`/health` 的 version
是唯一可靠信号；没有该字段（v0.3.0）一律当不支持。

### 分流后的行为差异

| 能力 | v0.3.0 | v1.0.0 |
|---|---|---|
| `usage()` 用量 | `unsupported: ["usage","credits"]`（如实报不支持） | **`GET /admin/analytics`**，`by_day` 北京时间分桶，返回 requests/tokens/failed |
| `checkin()` 签到 | 本地 `doubao_checkin.py`（读明文账号文件拿 cookie） | **`POST /admin/checkin`**（上游封装官方接口，不需要读账号文件） |
| 其余（账号/密钥/日志/模型/配置） | 同一套代码 | 同一套代码（格式兼容） |

**对外契约不变**：两条路的 `checkin()` 都返回
`{ok, results[], message, total, succeeded, already, failed, confirming}`，
调用方（`compat.py`、前端、记录页）不需要知道走的是哪条。

### 上游签到状态 → qtg 契约的映射

v1 的 `/admin/checkin` 返回 `{"results":[{"id","status","message","code",...}]}`，
它的 status 比我们的多，映射如下（**未知状态一律当 fail，不当成功**）：

| 上游 status | qtg | 理由 |
|---|---|---|
| `done` / `claimed` | `claimed` | 已回查确认的签到 |
| `skipped` | `already` | 账号未启用/冷却/今日已签 |
| `busy` | `confirming` | 该账号正在签，**别重复提交** |
| `manual_required` | `confirming` | `710010702` 那类，要人工看一眼 |
| `pending` | `confirming` | 提交了但没确认 |
| `verification` / `error` | `fail` | 真失败 |
| 其它 | `fail` | 契约变了也不假装成功 |

`confirming` 一律 `ok=False` —— 它既不是成功也不是可以无脑重试的失败。

### 实测记录（2026-11-17）

```
== 目标 http://127.0.0.1:18790（v1.0.0）==
healthz   = {'ok': True}
version   = '1.0.0'   supports_v1 = True
usage     : rows=0  unsupported=['credits']        ← 用上了 /admin/analytics
checkin   : ok=False source=upstream  msg=本次没有账号需要签到
```

`unsupported` 从 `['usage','credits']` 收窄到 `['credits']` —— 用量是真接上了，
额度折算上游不给，仍如实标注。

### 怎么切

```bash
# 起 v1 上游（端口默认也是 18789）
git clone https://github.com/niyouqianma/doubaowork2api.git
cd doubaowork2api && python -m pip install -e .
export DOUBAO_WORK_ADMIN_PASSWORD='...' DOUBAO_WORK_API_KEY='sk-...'
python -m doubao_work2api

# qtg 只改环境变量
QTG_DOUBAOWORK_BASE=http://127.0.0.1:18789
QTG_DOUBAOWORK_ADMIN_PASSWORD_FILE=/path/to/admin_password
```

**切之前建议先做一件事**：把 `doubao_checkin.py` 留着，让记录页观察几天
两边的签到结果差异。v1 的签到我**没在真账号上验证过**（今天那次是浏览器直接
打官方接口），所以它的成功率目前没有证据。

| 端点 | v0.3.0（现在用） | v1.0.0（niyouqianma） |
|---|---|---|
| `GET /health` | ✓ | ✓ **格式相同** `{"ok":true,...}` |
| `/v1/models`、`/v1/chat/completions`、`/v1/responses` | ✓ | ✓ + `/{id}` + `/{id}/input_items` + `/compact` |
| `GET /admin/accounts` | ✓ | ✓ **格式相同** `{"data":[...]}` |
| `/admin/config`、`/admin/logs` | ✓ | ✓ |
| `PATCH /admin/accounts/{id}`、`clear-cooldown`、`batch`、`import`、`export`、`scan-offline` | ✓ | ✓ **全都有** |
| **`POST /admin/checkin`**（批量，body 要 `ids`） | ❌ | **✓** |
| **`POST /admin/accounts/{id}/checkin`** | ❌ | **✓** |
| **`GET /admin/benefits`**（签到配置 + 每账号状态） | ❌ | **✓** |
| **`POST /admin/accounts/{id}/quota`**（额度） | ❌ | **✓** |
| **`POST /admin/accounts/{id}/test`**、`/admin/models/test` | ❌ | **✓** |
| **`/admin/accounts/qr`**（扫码登录）、`/admin/accounts/browser` | ❌ | **✓** |
| **`/admin/catalog/refresh`**、`/admin/analytics`、`/admin/routing` | ❌ | **✓** |
| 账号存储 | 明文 JSON | **加密**（`vault.py`） |

**它是超集**：v0.3.0 有的端点它全有，而且**关键响应格式一致**
（`/health` 和 `/admin/accounts` 实测过，我们的适配器直接就能用）。

### 实测过的事（不是看 README 推断的）

```bash
# 1. clone 后零额外依赖直接起
python -m doubao_work2api          # → Application startup complete，监听 18790

# 2. 我们的适配器指向它，读路径全部通过
healthz  = {'ok': True}
health   = {'up': True, 'latency_ms': 0, 'last_error': ''}
accounts = 0                        # {"data":[]} 格式兼容
overview = {'accounts': [], 'checkin': []}

# 3. 自带签到是真的
GET  /admin/benefits → {"config":{"enabled":false,"hour":9,"minute":0},
                        "today":"2026-11-17","timezone":"Asia/Shanghai"}
POST /admin/checkin  → 422 {"loc":["body","ids"],"msg":"Field required"}   # 要账号 id

# 4. 模型目录比 v0.3.0 新
GET /admin/config → person-deepseek-v4-1-flash-261017, 300000 context,
                     reasoning_efforts low/medium/high/xhigh/max, image_input=true
```

### 升级的收益与代价

**收益**：

1. **不用自己维护签到** —— 它的 `/admin/checkin` + `/admin/benefits` 就是官方接口封装，
   `adapters/doubao_checkin.py`（约 550 行）理论上可以退役
2. **额度查询**（`/quota`）—— 现在我们的 `usage()` 只能报「上游不提供」
3. **扫码/浏览器登录** —— 用户不用手抠 cookie
4. 模型能力（图片输入、五档 reasoning、Responses 完整实现）

**代价 / 风险**：

1. **v1.0.0 才发布 3 天**（2026-11-14 创建），生产环境未经验证；
   我们现在用的是经历过实战的 v0.3.0
2. **账号加密存储** —— `doubao_checkin` 靠读明文 `accounts/*.json` 拿 cookie，
   切过去后这条路断了（但这不重要：切过去就不需要自己签了）
3. 管理密码/token 环境变量名不同：`DOUBAO_WORK_ADMIN_PASSWORD` / `DOUBAO_WORK_API_KEY`
4. `POST /admin/checkin` 要 `{"ids":[...]}`；`/admin/benefits` 的 `config.enabled`
   默认 **false** —— 不显式打开不会自动签

### 如果决定切，步骤是

```bash
# 1. 起新上游（端口默认也是 18789，和你现在跑 0.3.0 一致）
git clone https://github.com/niyouqianma/doubaowork2api.git
cd doubaowork2api && python -m pip install -e .
export DOUBAO_WORK_ADMIN_PASSWORD='...' DOUBAO_WORK_API_KEY='sk-...'
python -m doubao_work2api

# 2. qtg 这边只改环境变量（base 地址不变的话几乎什么都不用改）
QTG_DOUBAOWORK_BASE=http://127.0.0.1:18789
QTG_DOUBAOWORK_ADMIN_PASSWORD_FILE=/path/to/admin_password

# 3. 体检
python tools/verify_doubaowork_adapter.py
python tools/probe_doubaowork_checkin.py --accounts-dir <新上游 accounts 目录>
```

**我没有替你切。** 理由：我们的 v0.3.0 链路已经**真机签到成功过**
（2026-11-16，+400 额度），而新版本才 3 天。
**换一个没被时间验证过的上游，不该由一个 AI 单方面决定。**

但如果你想要额度查询、扫码登录、或者不想再维护自己那份签到，上面这条路实测是通的。
