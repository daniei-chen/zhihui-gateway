# 接入一个新平台（第三方）的清单

> 2026-11-22 起，「有哪些平台、各自什么属性」只有**一个真相**：
> `server/platformpolicy.PLATFORM_SPEC`。原先各处写死的平台名元组，全部改为从这张表派生。
>
> 这份清单按代码里每条**真实依赖**列出，每条后面都写着「漏了会怎样」——
> 那些都来自线上故障或实测，不是假设。

## 0. 动手前先决定两件事

| 问题 | 影响 |
| --- | --- |
| **这个平台计费吗？** | 决定要不要为它做价格表，见第 4 节 |
| **它怎么授权？** | 浏览器回跳 / 轮询上游会话 / 用户自己贴 token，见第 5 节 |

## 1. 加一行（必做）

`server/platformpolicy.py` 的 `PLATFORM_SPEC` 加一条：

```
'yourplatform': PlatformSpec(
    name='yourplatform', enabled=True, default_grant=False,
    adapter='', passthrough=True, realm_scoped=False,
    auth_mode='callback', display='Your Platform'),
```

字段含义见 `PlatformSpec` 的 docstring。**加完这一行，第 2 节之外的硬编码判定
会自动认出新平台。**

`default_grant` 想清楚再开：它决定「系统代建的密钥（红包等）」会不会自动获得
该平台。新平台默认必须是 `False`，否则等于静默放开。

## 2. 配上游（必做）

`server/routers/gateway.py` 的 `_THREEWAY_SPEC` 加一项：上游地址 + 密钥文件。

```
'yourplatform': ('http://127.0.0.1:PORT',
                 '/opt/workbuddy-manager/engine/secrets/yourplatform_upstream_key'),
```

上游地址若需运行时切换（像 Trae 那样多引擎），再加进 `_THREEWAY_ROUTE_FILES`。

> **注意**：`passthrough=True` 的平台名集合必须与 `_THREEWAY_SPEC` 的键集合一致。
> `platformpolicy.passthrough_names()` 就是给这个用的；两者不一致时，记账会把
> 该平台的请求记成 `workbuddy`（`metering.py` 的注释写了为什么这很严重）。

## 3. 加路由入口（必做）

`/etc/nginx/sites-enabled/zhihui.kaogong.art` 加 location，**必须覆写** `X-QTG-Platform`：

```
location ^~ /yourplatform/v1/ {
    proxy_pass http://zhihui_mgr/yourplatform/v1/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-QTG-Platform yourplatform;   # 这一行不能少
}
```

> **漏了会怎样**：不覆写时，客户端自己发的 `X-QTG-Platform` 会直接生效，
> 「拿 A 平台密钥调 B 平台」就成了可行路径。现有各平台入口都覆写了。

`PlatformRoutingMiddleware` 会自动把 `/yourplatform/v1/*` 改写成 `/v1/*`
（前缀列表是 `tuple(f'/{p}/v1' for p in _ACCEPTED)`，跟随枚举），中间件不用改。

## 4. 计费（看第 0 节的决定）

当前实现：`credit` **只来自上游 usage 自带的 `credit` 字段**（`metering.parse_credit`）。

- 上游返回 `credit` → 能扣额度，`billing='upstream_credit'`
- 上游不返回 → `billing='none'`，**只记 token、不扣额度、不限量**

> **最容易踩的静默坑**：第三方 API 几乎都只返回
> `usage: {prompt_tokens, completion_tokens, total_tokens}`，没有 `credit`。
> 直接接进来又不做价格表，结果就是「密钥的每日额度对这个平台完全无效」，
> 而且**没有任何告警会告诉你**。要么明确接受，要么先做「token × 单价 → credit」
> 的本地价格表。

## 5. 授权形态（看第 0 节的决定）

| 字段 | 含义 | 现用者 |
| --- | --- | --- |
| `auth_mode='callback'` | 浏览器回跳完成授权 | Trae、豆包工作 |
| `auth_mode='poll'` | 轮询上游会话 | Qoder |
| `oauth_session=True` | 有「授权会话台账」，要防重复发起 | Qoder、Trae |
| `qr_self_driven=True` | 扫码由独立扫码页驱动，网关不重复轮询 | Trae、豆包工作 |

若新平台是「用户自己贴 API key」，四个都不勾，走 manager 的普通账号新增路径。

## 6. 前端（必做，但在另一处构建）

`web/lib/platform-store.ts` 的 `PLATFORMS` 从后端 `/api/platforms` 拿，但类型枚举
`Platform` 是静态的。加平台要同时改：

1. `Platform` 类型（否则 `platform === 'yourplatform'` 的分支编译不过）
2. i18n 展示名（五种语言）
3. 重新构建 `web/out`（整树签名，用 `deploy/create-web-manifest.py`）

> **漏了会怎样**：后端认了、UI 切换器里没有这个平台，用户看不见但 API 能用——
> 「能用但没人知道」，比报错更难发现。

## 7. 验证（必做，别跳）

```
# 1) 属性自洽 + 判定等价（本仓库自带）
python3 server/tools/platform_selfcheck.py

# 2) 数据面连通
curl -sS -o /dev/null -w '%{http_code}\n' https://<host>/yourplatform/v1/models \
     -H "Authorization: Bearer $KEY"

# 3) 记账归属正确（最容易错也最难发现）
sqlite3 /opt/workbuddy-manager/data/manager.db \
  "SELECT platform, usage_source, usage_complete, count(*) FROM request_logs
   WHERE ts > strftime('%s','now')-3600 GROUP BY 1,2,3;"

# 4) 平台隔离：拿只授权 workbuddy 的密钥打新平台，必须 403 platform_not_allowed
```

第 3 步是真正的验收线：`platform` 必须是新平台名，`usage_source` 必须是
`upstream` 且 `usage_complete=1`。若显示 `workbuddy`，说明第 2 节的
`passthrough_names()` 与 `_THREEWAY_SPEC` 不一致。

## 8. 已经踩过的三个坑（别再踩）

1. **只改 `PLATFORMS`，忘了 `qtgcore/service.py` 的白名单**
   → 账号页直接 `ValueError: known pool platform required`，页面全白。
   现在已收敛到 `_ACCEPTED`（从 `PLATFORM_SPEC` 派生）；但若新增了「独立引擎
   适配器」，记得同步 `PlatformSpec.adapter`。

2. **`_THREEWAY_SPEC` 里把上游地址钉死**
   → 切换引擎后请求仍打旧地址，报错还指向完全无关的原因
   （2026-11-21 的 Trae 切换事故，见该处注释）。

3. **记账归属与实际上游不一致**
   → 账本把 A 平台用量算到 B 平台头上，配额、统计、告警全错。
   只改网关不改 `metering`，或只改 `metering` 不改 `_THREEWAY_SPEC`，都会这样。
