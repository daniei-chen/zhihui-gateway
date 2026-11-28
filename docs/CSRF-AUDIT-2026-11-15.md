# CSRF 防护审计 — 2026-11-15

上一版的前端审计里明确写了「CSRF 的完整链路没查」。这一版补上，**并做了实测**。

**结论：防护存在、实现完善、生产实测生效。**

---

## 一、为什么这件事值得单独查

这个产品的管理面会话是**签名 Cookie**。浏览器会**自动携带**它 ——
所以一个外部页面只要让受害者的浏览器发起请求，就能借到那份凭证。

`SameSite=Lax` 能挡掉大部分跨站 POST，但它是**纵深防御，不是替代品**：

- 老浏览器不认 `SameSite`
- Lax 仍然允许**顶层 GET 导航**带 cookie

**来源校验（Origin / Referer）才是那一层真正的拦截。**

## 二、代码里已有的实现

`security.py` 里有一段专门的注释，直接写着审计结论：

> 管理面的写端点此前**没有任何** Origin/Referer 校验。管理面的会话是签名 Cookie，
> 因此跨站页面可以让受害者的浏览器带着 Cookie 打管理面写接口。
> `SameSite` 只是纵深防御（且老浏览器不认），不能替代来源校验。

**换句话说：这个洞被找到过，也已经修了。**

实现要点：

| 项 | 内容 |
| --- | --- |
| 覆盖的方法 | `POST` / `PUT` / `PATCH` / `DELETE` |
| 「浏览器发起」的判据 | `Sec-Fetch-Site` / `Sec-Fetch-Mode` / `Sec-Fetch-Dest` 任一存在 |
| 来源归一 | 复用另一模块的解析器，处理 userinfo 混淆、`null`、缺省端口 |
| 期望来源 | 服务端配置的锚点（`WB_PUBLIC_ORIGIN` / `WB_CORS_ORIGINS`）；都没配才回退到可信反代的 `X-Forwarded-Host`，最后是不可能是攻击者的 Host（IP 字面量 / localhost / 单标签内网名）。**D-41 之前**这里取的是 `Host` + 本机反代时的原始协议，见第九节 |
| 拒绝原因 | 五种，各自命名（多重 Origin、缺失、Host 缺失、格式非法、不匹配） |

**它不是只做了「有 Origin 就比一下」**：`null`、多重 Origin、userinfo 混淆形态
（`https://host@evil`）都在处理范围内。

## 三、生产实测

对 `POST /api/login`（写端点）打不同的 Origin：

| `Origin` 头 | 状态码 | 判定 |
| --- | --- | --- |
| `https://evil.example` | **403** | **被 CSRF 拦截** |
| `null` | **403** | **被 CSRF 拦截** |
| 不带 | 401 | 通过 CSRF，进入鉴权（未认证） |
| 同源 `http://127.0.0.1:7866` | 401 | 通过 CSRF，进入鉴权（未认证） |

跨站那条的响应体是 `{"detail":"Forbidden"}`。

**三条结论：**

1. **跨站来源被拒**，而且是在鉴权之前
2. **同源来源放行** —— 说明它不是「一律拒绝」，而是真的在比对
3. **非浏览器客户端（不带 Origin）放行** —— 这是对的：
   `curl` 没有浏览器环境凭证，CSRF 这个攻击模型对它不成立；
   它走的是**另一条**鉴权路径（Bearer）

## 四、这个设计正确的地方

「浏览器请求」的判据是 `Sec-Fetch-*` 三个头**任一存在**，
而不是「Origin 存在才算浏览器」。

**这个方向是对的**：现代浏览器对**每个**请求都会带 `Sec-Fetch-*`，
而非浏览器客户端一个都不带。用「缺 `Sec-Fetch-*` 且缺 Origin」判成非浏览器，
不会给攻击者留下「去掉 Origin 就绕过」的空间 —— 因为浏览器**不会**让页面脚本
去掉 `Sec-Fetch-*`（那是 forbidden header）。

## 五、需要留意的一处

不带 `Origin` 且不带 `Sec-Fetch-*` 的请求**会通过 CSRF 校验**。

这是设计上必须如此（否则所有 CLI 与内部调用都会被打死），但它意味着：

- **攻击面取决于「攻击者能否构造一个不带这两个头的请求并让它带上 cookie」**
- 浏览器**做不到**（脚本无法移除 forbidden header，表单提交必然带 Origin）
- 所以这条路在浏览器模型下是堵住的

**风险只在「非浏览器客户端被诱导发送」时出现**，而那种场景需要攻击者已经能控制
受害者的 HTTP 客户端 —— 那已经不是 CSRF 了。

## 六、`SameSite=Lax` 唯一覆盖不到的地方：GET 端点的副作用

`Lax` 会放行**顶层 GET 导航**带 cookie。所以真正的问题是：
**有没有 GET 端点在做状态变更？** 如果有，它就在 CSRF 覆盖之外 ——
一个链接点击就够了。

**逐个查了。没有。**

按源码扫描「GET 端点函数体里是否出现写库调用」，命中 6 处：

| GET 端点 | 位置 | SQL |
| --- | --- | --- |
| `/accounts` | 账号列表 | `SELECT … FROM portal_accounts` |
| `/v1/key` | 用量查询 | `SELECT … FROM portal_spends` |
| `/requests` | 请求记录 | `SELECT … FROM portal_key_links` |
| `/keys` | 密钥列表 | `SELECT …` |
| `/users` | 用户列表 | `SELECT … FROM portal_users` |
| `/settings/panel` | 面板设置读取 | `SELECT … FROM settings` |

**六处全是 `SELECT`。**

它们的「写库调用」是复用同一个数据库执行入口来跑查询 —— 那是接口形态，
不是副作用。**没有一个 GET 在改状态。**

**所以 `Lax` 放行的那条路径上，没有可被利用的状态变更。**

## 七、这份审计**没有**覆盖的

- **Referer 回退路径** —— 只测了 Origin，没测「只有 Referer 没有 Origin」的情形
- **子域是否有独立应用** —— 同源判定用 Host，子域之间的相互信任关系没查
- **WebSocket / SSE 端点的来源校验** —— 它们不走同一套中间件
- **表单提交（`multipart/form-data`）的路径** —— 只测了 JSON

## 八、如果继续

按剩余风险排序：

1. **子域之间的信任关系** —— 同源判定用 Host，如果某个子域上跑着能被他人控制的内容，
   它可以对本域发起「同源」请求
2. **WebSocket / SSE** —— 不走中间件意味着不走这套校验
3. **Referer 回退** —— 现代浏览器都发 Origin，但老客户端可能只发 Referer

---

## 九、D-41 补记（2026-11-28）：锚点不再取 `Host`

第八节第 1 条说的「同源判定用 Host」，在 2026-11-27 的深度审计里被实测成漏洞
（`D-41`，见 `docs/audit/04-security.md` 的 D-4）：DNS rebinding 下浏览器访问
`http://evil.test:8000`，`Host` 与 `Origin` 天然都是 `evil.test:8000`，两者相等，
于是**任意域名**都能通过同源校验：

```
POST /api/login   Host: evil.test:8000   Origin: http://evil.test:8000   -> 200（修前）
```

修完之后同一形态是 `403` + `X-Manager-Csrf-Reason: csrf_origin_unconfigured`。

### 锚点从哪来（越靠前越权威）

1. `WB_PUBLIC_ORIGIN`：部署方声明的公网来源（如
   `https://zhihui.kaogong.art`）。设了它就**只认它**，`Host` 完全不参与。
2. `WB_CORS_ORIGINS`：已有的跨源白名单，外加「不可能被攻击者控制」的同源 Host
   （IP 字面量 / `localhost` / 单标签内网名）。
3. 都没配：只采信**可信反代**（peer 落在 `WB_TRUSTED_PROXY_CIDRS`，与
   `iputil.client_ip` 同一份判据）送来的 `X-Forwarded-Host`；非可信 peer 的
   `X-Forwarded-Host` / `X-Forwarded-Proto` 一律忽略。
4. 最后才是「不可能被 rebinding 的 Host」：IP 字面量、`localhost`、
   单标签内网名。带点的主机名（`evil.test`、`zhihui.kaogong.art`）不能自证身份。

### 代价（故意的）

**用域名直连、不经反代、又没配 `WB_PUBLIC_ORIGIN`** 时，写请求会被拒：
浏览器看到 403，响应头 `X-Manager-Csrf-Reason: csrf_origin_unconfigured`，
服务日志告警一次（附该设哪个变量）。这个形态和攻击形态在服务端**无法区分** ——
能区分的只有「部署方声明的来源」或「可信反代看到的来源」。

不受影响的形态：配了 `WB_PUBLIC_ORIGIN`（推荐）、IP / localhost / 单标签内网名
直连、反代透传 `X-Forwarded-Host`。登录、退出与所有管理面写端点用的是同一套
判定，`routers/portal.py` 的用户端写闸门与 `services/platformauth.py` 的轮询闸门
也已统一到 `security.origin_allowed()`。

### 这次补上的测试

`server/tests/test_d41_csrf_origin_anchor.py`（已登记进
`tools/ci_regression.py` 的 `MODULES`）：修前红、修后绿，并做过变异验证
（把锚点改回「只信 Host」，对应用例必须失败）。`GET` 等安全方法不受影响。
