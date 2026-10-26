# K36 · CSP 与安全头全量核对（2026-10-25，lab 实测）

> 环境：`zhihui-local-lab`，真实 Manager（55223）+ 真实 QTG（38195）+ fixture nginx（443）。
> 生产零变更：只读源码与发本地请求。
> 证据：`evidence/k36/enforced-csp/`、`evidence/k36/report-only-csp/`、
> `evidence/browser/k36-csp-*/report.json`。

## 一、结论速览

| 项 | 结论 |
|---|---|
| 三服务是否都设了 5 个必备头 | **是**（Manager / QTG / src-three-platform） |
| CSP 是否有 `unsafe-eval` | **没有**（三处都没有） |
| CSP 是否缺 `object-src` | **修复前缺 → 已修（N35）** |
| QTG 的 401/403 是否带安全头 | **修复前不带 → 已修（N36）** |
| 强制 CSP 下应用能否正常水合 | **能**（`app_hydrated: true`） |
| 强制 CSP 下非法脚本是否被拦 | **能**（eval 拦截、跨源拦截） |
| 边缘当前策略 | **Report-Only**（不拦截，只收集） |

## 二、本轮修掉的两个真实缺陷

### N35 · 两处 CSP 缺 `object-src`

`manager/server/main.py` 与 `src/three-platform/server/main.py` 的 CSP 只有
`default-src 'self'`。按 CSP3，未声明 `object-src` 时回退到 `default-src`，
即**同源的 `<object>`/`<embed>` 仍可加载插件内容**。本应用没有任何插件内容，
收紧到 `none` 零兼容成本。

已修，并加断言：`REQUIRED_CSP_DIRECTIVES` 含 `object-src`。

### N36 · QTG 的 401/403 响应完全不带安全头

这是本轮最有价值的发现。原因在中间件的**包裹顺序**：

```
注册顺序：_observe → _auth_gate → _csrf_gate
实际包裹：_observe 在最内层，_auth_gate / _csrf_gate 在外层
```

`_observe` 在 `call_next` 返回后设头。但 `_auth_gate` 拒绝时是**直接
`return JSONResponse(...401)`**，根本不进入内层中间件，于是：

```
GET /api/v1/meta（无凭据） → 401，X-Frame-Options / CSP 全部缺失
```

等于"认证失败页"成了唯一没有安全头保护的页面 —— 而它恰恰是攻击者最常触达的页面。

**修复**：新增最外层 `_security_headers_outer` 中间件，用 `setdefault` 只补缺失项
（不覆盖内层已设的 CSP 静态跳过、`x-request-id` 等）。QTG 侧 8/8 通过。

**Manager 没有这个问题**：它的设头逻辑在同一中间件内、`call_next` 之后，
401 由依赖抛 `HTTPException` 走异常处理器，不会绕过。已加实测断言锁住这个差异。

## 三、CSP 真实效果实测（浏览器）

### 方法学修正（重要）

第一次用 `page.evaluate` 测 `eval()` 是否被拦，得到"没被拦"的结论 —— **错的**。
`page.evaluate` 走 CDP `Runtime.evaluate`，不受页面 CSP 约束。这与此前
N25（用 `route.abort()` 测取消路径）是同一类错误：**用测试工具的旁路去验证
被测对象的约束**。

改为让**页面自己**加载脚本、在脚本内部触发受约束行为：

- 同源脚本：`route.fulfill` 提供内容（origin 完全相同）→ `'self'` 允许 → 必须能跑
- 跨源脚本：`https://localhost:<port>`（同机不同 host）→ 不在 `'self'` → 必须被拦
- 脚本内部 `eval` / 动态内联 `<script>`：受 CSP 约束

### 结果 A：当前边缘策略 = Report-Only（不拦截）

```
has_csp:                 false      # 只有 Content-Security-Policy-Report-Only
eval_blocked:            false      # 因为没有 enforced CSP
cross_origin_blocked:    false      # 跨源脚本照常执行
inline_blocked:          false
violations:              20 条（上限）
  └─ 15 条 inline script + 5 条 inline style
```

**含义**：现在的边缘策略只**报告**、不**拦截**。攻击者注入的跨源脚本在真实浏览器里
可以执行。这不是配置错误（Report-Only 是灰度上线的标准做法），但**必须明确
它不提供实际防护**。

### 结果 B：强制 CSP（应用层策略原样）下的行为

```
csp:                  default-src 'self'; img-src 'self' data: blob:;
                      font-src 'self' data:; style-src 'self' 'unsafe-inline';
                      script-src 'self' 'unsafe-inline'; connect-src 'self';
                      object-src 'none'; frame-ancestors 'none';
                      base-uri 'self'; form-action 'self'
has_csp:              true
has_all_headers:      true
same_origin_script_ran: true      # 合法同源脚本能跑
app_hydrated:         true        # Next.js 水合正常
eval_blocked:         true        # eval 被拦
cross_origin_blocked: true        # 跨源脚本被拦
violations:           1 条（跨源探针，预期内）
```

**这是决定性的**：应用层的这套 CSP **可以直接升级为强制策略**，不会瘫痪页面。
它保留 `'unsafe-inline'` 是因为 Next.js 的主题脚本与 RSC 数据是内联的，属于必要放宽；
但没有 `'unsafe-eval'`，且 `base-uri`/`form-action` 都收敛了。

### 如果直接部署 Report-Only 的那条策略会怎样

`default-src 'self'`（无 `style-src`/`script-src` 放宽）在强制模式下会产生
**20+ 条违规**，全部指向 Next.js 的内联脚本和内联样式。**直接升级会导致页面白屏**。
所以 Report-Only 里那条极简策略不能直接转正，必须换成应用层这套。

## 四、修复清单

| 文件 | 改动 |
|---|---|
| `manager/server/main.py` | CSP 加 `object-src 'none'` |
| `src/three-platform/server/main.py` | 同上 |
| `qtg/app/qtg/main.py` | CSP 加 `object-src 'none'` + `form-action 'self'`；加 `Permissions-Policy`；新增最外层 `_security_headers_outer` 修 N36 |
| `manager/server/tests/test_k36_security_headers.py` | 新增 12 项（头成组/指令逐条/无 unsafe-eval/头位置/401 实测） |
| `src/three-platform/server/tests/test_k36_security_headers.py` | 同文件副本（src target 用） |
| `qtg/tests/test_k36_security_headers.py` | 新增 8 项（源码契约 + 真实响应含 401） |
| `tests/browser/k36_csp_probe.mjs` | 浏览器 CSP 实测（含方法学说明） |

测试结果：
- `manager` target：6 passed / 6 skipped（QTG 与仓库根文件在 manager 视图不可见）
- `src` target：5 passed / 7 skipped
- `qtg` target：**8/8 passed**

## 五、未覆盖 / 需生产确认

1. **生产 nginx 是否真的带 `always`**：`recovery/READONLY-EVIDENCE.md` §V6 记录了
   安全头存在，但没记录 `always`。缺 `always` 时 4xx/5xx 不带头 —— 这是 N37 的坑：
   **server 级 `add_header` 会被任何 location 级 `add_header` 完全覆盖**。
   fixture 里就踩到了（`location /` 有 `Cache-Control`，导致 server 级 6 个头全失效，
   实测静态页面 0 个安全头）。**生产必须在 `nginx -T` 输出上逐 location 确认**。
2. **CSP 报告端点 `/__csp`**：Report-Only 指向它，但没有服务接收。
   要么实现收集器，要么删掉 `report-uri` 避免无意义的 404 流量。
3. **报告策略转正**：需用应用层那套（含 `'unsafe-inline'`），不能用边缘极简那套。
   转换前应在生产用 Report-Only 先跑一周，确认违规里没有意外来源。
