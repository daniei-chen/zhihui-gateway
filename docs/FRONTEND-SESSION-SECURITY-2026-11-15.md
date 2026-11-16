# 前端与会话安全审计 — 2026-11-15

覆盖 `web/` 下的 **173 个前端源文件**与会话 cookie 的设置路径。

**结论：没有发现可被利用的问题。** 下面是每一项的检查方式和结果。

---

## 一、XSS：注入点为零

按最常见的三个注入点扫：

| 模式 | 命中 |
| --- | --- |
| `dangerouslySetInnerHTML` | **0** |
| `v-html` | **0** |
| `innerHTML =` | **0** |

**173 个文件全扫，一个都没有。**

这个结论的意义比「没找到 bug」大一些：React 里唯一能把字符串当 HTML 执行的就是
第一个，Vue 里是第二个。**两者都没有出现，意味着「把上游返回的文本渲染成页面」
这条路在代码里根本不存在。**

上游是 AI 模型，它的输出是**完全不可控的文本**。这条检查之所以重要，
是因为这个产品的所有核心界面都在显示上游返回的内容。

## 二、`eval` 与动态代码执行：零

| 模式 | 命中 |
| --- | --- |
| `eval(` | **0** |
| `new Function(` | **0** |

## 三、敏感数据没有落在可被脚本读取的地方

`localStorage` / `sessionStorage` 的全部调用点：

| 位置 | 存的是什么 |
| --- | --- |
| 配置 | 界面配置项 |
| provider | 界面状态 |
| `wb-me` | **用户信息缓存**（用户名、角色） |
| 显示偏好 | 到期日是否按天显示 |

**没有一处存 session token、API key 或密码。**

**用户信息缓存不是凭证** —— 它不含任何能用来发起请求的东西。就算被读走，
攻击者也拿不到会话。

## 四、会话 cookie 的三道保护都在

`routers/auth.py:115` 的 `set_cookie`：

| 属性 | 值 | 作用 |
| --- | --- | --- |
| `httponly` | **True** | 脚本读不到 |
| `samesite` | **`lax`** | 跨站请求不带 |
| `secure` | `cookie_secure(request)` | HTTPS 才发 |

**前端代码里 `document.cookie` 零命中** —— 与 `httponly=True` 一致。
如果前端真在读 cookie，那条属性就白设了；**这里没有这种情况。**

## 五、`secure` 的判据：`auto` 在生产成立

这一项最容易「以为安全其实没有」，所以单独查。

`cookie_secure()` 在 `auto` 模式下读 `X-Forwarded-Proto`（仅当对端是可信代理）。
**如果反向代理没传这个头，后端只看到明文 HTTP，就会静默地不加 `Secure`** ——
而用户访问的确实是 HTTPS。

**生产实际情况：**

| 检查 | 结果 |
| --- | --- |
| `WB_SECURE_COOKIE` | `auto` |
| `zhihui.kaogong.art` 的 nginx 配置 | **`proxy_set_header X-Forwarded-Proto $scheme` 出现 10 次** |
| 后端可信代理判定 | 成立（同机反代） |

**所以 `auto` 会判成 HTTPS，cookie 带 `Secure`。**

顺带说：这个函数旁边有个 `insecure_cookie_warning()`，
**专门在「没带 Secure 但很可能该带」时往日志里点名**。
作者显然踩过这个坑。

## 六、安全响应头：都在，有一处权衡

实测 `https://…/dashboard/` 的响应头：

| 头 | 值 |
| --- | --- |
| `content-security-policy` | `default-src 'self'; … object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'` |
| `strict-transport-security` | `max-age=31536000; includeSubDomains` |
| `x-content-type-options` | `nosniff` |
| `x-frame-options` | `DENY` |

**四个都在，而且 CSP 不是宽松的摆设** —— `object-src 'none'` 关掉了插件、
`frame-ancestors 'none'` 关掉了被嵌入、`base-uri 'self'` 关掉了 base 劫持。

### 唯一的弱点：`script-src` 带 `'unsafe-inline'`

```
script-src 'self' 'unsafe-inline'
```

`'unsafe-inline'` 允许页面里的内联 `<script>` 执行 —— 如果攻击者能往页面注入 HTML，
CSP 就挡不住那段内联脚本。

**但这个风险在本项目里被上一层抵掉了**：XSS 注入点为零（第一节）。
没有注入点，`'unsafe-inline'` 就没有可乘之机。

**而且它大概率是必需的**：Next.js 的水合需要内联脚本。去掉它通常要配套 nonce
（每次响应生成一个随机值，逐个给允许的内联脚本标上），那是**一个功能改动**，
不是改一个头的事。

**所以这里记录为「已知权衡」，不是待修缺陷。**

## 七、这份审计**没有**覆盖的

- **构建产物**（`web/out/`）—— 只看源码，没审编译后的 bundle
- **依赖树**（`node_modules` 下的第三方包）—— 那不是本仓库的代码
- **CSRF 的完整链路** —— `SameSite=Lax` 挡掉了跨站表单提交，
  但没有逐个 POST 端点核对是否有额外的 CSRF token
- **前端路由的权限** —— 界面层面的隐藏不等于后端拒绝（后者在鉴权审计里查过）

## 八、如果要继续深挖，优先级

1. **给 CSP 加 nonce 并去掉 `'unsafe-inline'`** —— 唯一的纵深防御缺口，
   但需要改构建与响应两侧
2. **`SameSite=Lax` 对 POST 的覆盖** —— Lax 允许顶层 GET 导航带 cookie，
   对「用 GET 实现的状态变更」没有保护
3. **依赖树里的已知漏洞** —— 需要跑审计工具，不是文本扫描

