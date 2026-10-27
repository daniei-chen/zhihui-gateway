# K49 依赖漏洞可达性复核（2026-10-25）

- 扫描源：`npm audit`（生产树 191 prod / 328 dev / 87 optional，共 555）
- 结果：**5 条告警（1 critical / 4 high）**
- 判定原则：S4 原文——「不把扫描器分数直接等价业务漏洞或'全部安全'」。逐条给**可达性**结论。

## 部署形态（判定的前提，必须先确定）

| 事实 | 证据 |
|---|---|
| 前端 `output: 'export'`（**纯静态导出**） | `next.config.*:20` |
| 发布物是 `out/` 目录（index.html + _next） | `release/release-web-v2.sh:47-48` |
| **无 `next start` / 无 Next 服务端进程** | 全仓无 `next start` 引用；nginx 直接服务静态文件 |
| 因此：Next 的**服务端**代码路径在本部署中不存在 | K14/K33 演练均验证 nginx 静态服务 |

## 逐条判定

| # | 包 | 严重度 | 公告要点 | 可达性判定 | 依据 |
|---|---|---|---|---|---|
| 1 | **next 15.5.25** | **critical** | SSG/ISR 页面缓存投毒（self-hosted） | **不可达** | 该漏洞在 Next 的**服务端渲染/ISR 缓存层**。本项目 `output: 'export'` 生成纯静态 HTML，由 nginx 直接服务，**没有 Next 运行时**可被投毒 |
| 2 | sharp <0.35.5 | high | librsvg 依赖 CVE-2026-96889 | **不可达（构建期）** | `images.unoptimized`（静态导出强制）；且 sharp 只在**构建期**优化图片，不在运行时接收用户输入 |
| 3 | follow-redirects 1.16.0 | high | 数组型 header 绕过跨域凭据剥离 | **不可达** | 前端运行时不用 axios 的 follow-redirects（浏览器 XHR 自带重定向处理）；该包只在 Node 侧使用 |
| 4 | source-map-js ≤1.2.1 | high | 索引 sourcemap 触发事件循环 DoS | **不可达** | 仅在**构建期**解析 sourcemap；生产不加载 .map（静态导出默认不发布 sourcemap） |
| 5 | brace-expansion ≤1.1.20 / 4.0.0–5.0.11 | high | 花括号展开的平方时间 DoS | **不可达** | 属 glob 依赖链，仅构建期使用，不接受外部输入 |

## 结论与建议

- **5 条全部为构建期或不存在运行时路径**，当前**不构成可被外部触发的业务漏洞**。
- 但**不代表可以长期不动**：一旦改为 `next start`（服务端渲染/ISR）或启用图片优化服务，
  #1 与 #2 立刻变为**可达**。这条约束必须写进部署手册。
- 建议（不阻断发布）：
  1. 升级 next 到 ≥15.5.28（消除 critical，即使当前不可达）；
  2. 升级 sharp ≥0.35.5；
  3. 在 CI 里加一条断言：**若 `next.config` 的 `output` 不再是 `'export'`，则 audit 的 high/critical 必须为 0**。

## 证据

- `npm audit --json` 原始输出：`src/three-platform/web/web-audit.json`
- 前端依赖树冻结：`src/three-platform/web/web-deps-frozen.json`（54 直接依赖 / 1603 节点）
- Python 依赖锁：`manager/server/requirements.hashes.txt`（28 条带 sha256）+ `requirements.lock.txt`（27 条）
- 前端锁一致性：`npm ci --dry-run` → `up to date in 571ms`（锁与 node_modules 一致）
