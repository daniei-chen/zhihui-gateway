# K09 · `/wb/` 全站别名审计（lab VM 实测，2026-10-25）

> 环境：`zhihui-local-lab` QEMU VM，nginx 1.24.0，**按生产现状复刻**边缘配置
> （`/metrics`=404、`location /wb/` 全站别名、平台分流 map、6 项安全头 + `always`）。
> 上游是**真实 Manager（55223）与真实 QTG（38195）**，不是 echo server。
> 生产零变更：只发 HTTP 请求，未改任何生产配置。

## 复刻依据

生产 nginx 原文不在本仓库，`recovery/READONLY-EVIDENCE.md` §V6 记录了 2026-10-23
只读取证时的 `sites-enabled/gateway.example.com` 关键要素：

- `if ($block_sensitive) { return 444; }`
- HSTS / X-Content-Type-Options / X-Frame-Options / Referrer-Policy / CSP-Report-Only / Permissions-Policy
- 平台分流 map、`auth_request /__zh_session`、**`/wb/` 别名**、`/metrics` 404

lab 复刻保留了 `/metrics` 404、`/wb/` 别名、分流 map、六项安全头；
**未复刻** `auth_request /__zh_session`（K04/K05 已在 identity fixture 单独验证，
混进来会让 K09 的变量不唯一）。这一条的差异在下方"未覆盖"里明确列出。

## 结果：30 项探针，27 通过 / 2 确认暴露 / 1 项归属其他卡

证据：`evidence/k09-edge-audit-rows-20261025.txt`（原始行）、
`tests/linux_lab/k09_edge_audit.sh`（可复跑脚本）。

### A. location 优先级与尾斜杠（5/5 通过）

| 探针 | 结果 | 说明 |
|---|---|---|
| `/metrics` | 404 | 精确匹配优先于 `/wb/` 前缀，K08 的修复成立 |
| `/wb`（无斜杠） | 301 | nginx 自动补斜杠，不是"漏网" |
| `/wb/` | 200 | 到达 Manager，返回 Manager 的 JSON 信封 |
| `/wb/metrics` | **401** | 到达 Manager 的 `/metrics`，**需要独立指标凭据** |
| `/wb/api/metrics` | 404 | Manager 无此路由 |

**重要修正**：任务卡与 `ops/README.md` 都写着"`/wb/metrics` 因此可匿名读到
Prometheus 指标"。实测**不成立** —— 返回 401，必须带指标凭据。
这条历史结论应在 `ops/README.md` 中更正。

### B. 未鉴权时 /wb/api/* （7/7 安全）

7 个管理端点在无会话时全部 **401**，与走 `/api/` 前缀的响应码完全一致。
**别名没有绕过 Manager 鉴权**。

### B2. 带会话时（2 项确认暴露 = K09 的真实风险）

用 `gateway-admin` 登录后：

```
INFO /api/keys           =200   /wb/api/keys           =200
INFO /api/me             =200   /wb/api/me             =200
INFO /api/accounts       =200   /wb/api/accounts       =200
INFO /api/logs           =200   /wb/api/logs           =200
INFO /api/tokens         =200   /wb/api/tokens         =200
INFO /api/users          =200   /wb/api/users          =200
INFO /api/settings/panel =200   /wb/api/settings/panel =200
CONFIRMED 写操作 POST /wb/api/logout -> 200
```

**结论：`/wb/` 确实构成第二套管理面入口**。危害不是"绕过鉴权"（鉴权仍然生效），
而是：

1. **边缘策略只覆盖 `/api/`**：限流、`auth_request`、CSP 报告、审计日志的 location
   规则全部作用于 `/api/`，`/wb/api/*` 是同一批处理器的**旁路**；
2. **写操作同样可达**（logout 实证 200），不只是读；
3. 任何针对 `/api/` 的安全加固都会留下一个未被覆盖的等价入口。

这正是 K09 任务卡"边缘策略不一致"的确切含义。

### C. 路径编码 / 大小写 / 规范化（5/5 通过）

| 探针 | 结果 | 结论 |
|---|---|---|
| `/WB/metrics` | 404 | nginx 前缀匹配大小写敏感，大写不命中别名 |
| `/Wb/metrics` | 404 | 同上 |
| `/wb%2fmetrics` | 401 | nginx 解码 `%2f` 后仍命中 `/wb/`，与 `/wb/metrics` **同码** |
| `/wb/%2e%2e/metrics` | 404 | `%2e%2e` 规范化回根，命中精确 `/metrics`=404（安全） |
| `/wb/wb/api/healthz` | 404 | 别名不递归，无二次剥前缀利用 |

**编码不是绕过手段**：`%2f` 只是走到同一个处理器，没有产生新的语义。

### D. 平台分流与凭据隔离（3/3 通过）

| 探针 | 默认 | `X-QTG-Platform: qoder` |
|---|---|---|
| `/api/healthz` | 200 (Manager) | 501 (QTG) |
| 响应头 `x-qtg-version` | 无 | `1.0.0` |

分流 map 生效，且 **qoder/trae 分支清空 `Authorization`**（map 里写死 `""`），
默认分支完整透传。凭据不会跨平台泄漏。

### E. 边缘安全头（6/6 通过，覆盖 200/404/API 三类响应）

`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、
`Permissions-Policy`、`Strict-Transport-Security`、
`Content-Security-Policy-Report-Only` —— 全部带 `always`，404 与 API 响应同样携带。

### F. 边缘与应用的头部叠加（观测）

`X-Content-Type-Options` 与 `X-Frame-Options` 各出现 **2 次**（nginx + 应用各一份），
`Content-Security-Policy` 只出现 1 次（应用层；边缘是 `-Report-Only`，不冲突）。

双层同名头不会出错（浏览器取第一个），但**重复本身是维护信号**：
改边缘头时以应用层为准，避免两处不一致。

## 结论与建议

**K09 状态：verified（问题已确认，方案已验证，未部署）**

| 项 | 结论 |
|---|---|
| 别名是否存在 | **确认存在**，且是完整第二套管理面（7 端点等价 + 写操作可达） |
| 是否绕过鉴权 | **否**，Manager 的 401 仍然生效 |
| 是否匿名泄露指标 | **否**，`/wb/metrics` 返回 401（历史结论需更正） |
| 收敛方案 | `ops/nginx/zhihui-hardening.conf`：保留 `^~ /wb/v1/` 推理路径，其余 `^~ /wb/` 返回 404 |
| 部署前置 | 先跑访问日志统计确认无 `/wb/api`、`/wb/settings` 调用（卡片原文要求） |

### 启用前的依赖统计（卡片原文要求，必须在生产执行）

```bash
awk '{print $7}' /var/log/nginx/access.log \
  | grep '^/wb/' | cut -d/ -f1-3 | sort | uniq -c | sort -rn | head
```

若结果里仍有 `/wb/api/*` 或 `/wb/settings/*`，**必须先做客户端升级**，
不能直接 404。

### 未覆盖

- **生产真实 location 全文**：本仓库无生产 nginx 原文，复刻基于 V6 摘要。
  生产启用前必须在目标机跑 `nginx -T` 全量 diff。
- `auth_request /__zh_session` 与 `/wb/` 的交互：K04/K05 单独验证过鉴权链，
  但"`/wb/` 是否也走 `auth_request`"取决于生产 location 嵌套，复刻里没有。
  这一条**必须在生产 `nginx -T` 输出上确认**。
- `if ($block_sensitive) { return 444; }` 是否覆盖 `/wb/` 路径。
