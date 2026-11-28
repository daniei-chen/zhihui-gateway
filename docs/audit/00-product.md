# A. 产品定位（从代码反推，2026-11-28）

> 方法：只读代码 + 启动实测 + 目录/表/配置统计。每条结论带 file:line 或命令。
> 结论先说：这不是"一个网关"，是**四合一单进程应用**：对外 AI 反代 + 管理控制台 + 用户门户（积分/签到/红包）+ 运维发布面。单进程、SQLite、静态前端由 FastAPI 托管。

## A1. 它对外提供什么（入口）

| 面 | 入口 | 证据 |
|---|---|---|
| 推理反代 | `POST /v1/chat/completions` | server/routers/gateway.py:3320；处理器 `_chat` server/routers/gateway.py:2876 |
| 推理反代 | `POST /v1/responses`、`POST /responses` | server/routers/responses.py:2210、2216 |
| 推理反代 | `POST /v1/messages`、`POST /v1/messages/count_tokens` | server/routers/anthropic.py:1254、1641 |
| 管理 API | `/api/*`（28 个 router） | server/main.py:465–713（`include_router` 28 次） |
| 用户门户 | `/api/portal/*`（注册、签到、红包、额度） | server/main.py:537–538；表 portal_*（见 A3） |
| 静态控制台 | `/`、`/_next/*`（Next 静态导出） | server/main.py:1009–1017；STATIC_DIR=web/out server/config.py:118 |
| 运维面 | `/healthz`、`/metrics`、`/api/healthz` | 实测 `/healthz` 200 `{"service":"zhihui-gateway","upstream_ok":false}` |

进程入口：`server/serve.py:98–104`，uvicorn **workers=1**（硬编码），支持 systemd socket activation 传 fd。

## A2. 一个请求从进来到出去

1. 路径改写：`PlatformRoutingMiddleware` 把旧平台前缀（`/trae/v1/*`、`/qoder/v1/*`）重写为 `/v1/*`，再复用同一条链路 —— server/main.py:1177、server/platformpolicy.py:196–217。两平台均 `enabled=False`（server/platformpolicy.py:47–56），仅为存量密钥/调用方保留解析。
2. 排空拦截：`DrainMiddleware` 在 drain 状态下除控制路径外直接 503 —— server/main.py:511、server/drain.py。
3. 审计/体积：`RequestAuditMiddleware`（main.py:1166）、`APIBodyGuard`（main.py:1152，声明长度与实际读取上限同源）。
4. 鉴权：`_authorize`（gateway.py:2899）→ keysvc 校验密钥、IP、模型授权、配额。
5. 预算预授权：`_prepare_forward_async` → `budget.prepare`（gateway.py:2642 附近；web 正文编码 wirebody）。
6. 选上游：`_upstream_for`（gateway.py:2965 附近）按平台/账号池/冷却挑上游与凭据。
7. 转发：httpx 客户端（gateway.py:3005 非流式、3128/3151 流式），deadline/timeout 由 server/deadline.py 统一。
8. 收尾：`_record`（gateway.py:1016 起）回填状态码 → `budget.settle`（gateway.py:1147）→ 不可变账本 `ledger.record`（gateway.py:1155）→ `request_outcomes`（server/db.py:2047）。流式断连走 finally 收尾（DAN-23 契约）。

## A3. 状态存在哪

| 状态 | 位置 | 证据 |
|---|---|---|
| 主库 | SQLite `WB_DATA_DIR/manager.db` | server/config.py:114–117 |
| 表规模 | 95 张表、50 个索引 | `grep -rh 'CREATE TABLE IF NOT EXISTS' server | wc -l` → 95；索引 50 |
| 核心表 | api_keys、budget_reservations、usage_events、request_outcomes、operational_alerts、instance_leases、admission_leases、provider_attempt_events_v2、portal_*（约 20 张） | server/db.py:2047、server/ledger.py:10–90、server/main.py 路由 |
| 不可变账本 | usage_events / adjustment_events / admin_events（UPDATE/DELETE 触发器拒绝） | server/ledger.py:36–92 |
| 用户 | `WB_DATA_DIR/users.json`（首次启动生成签名密钥） | server/config.py:117；server/security.py |
| 上游配置 | `WB_UPSTREAM_CONFIG` 默认 `/opt/workbuddy2api/config.json` | server/config.py:101–102 |
| 备份/发布状态 | `WB_BACKUP_STATUS_FILE` 默认 `/var/lib/zhihui-gateway/backup-status.json`；发布指针在 `deploy/`、`ops/` | server/config.py:115；deploy/systemd |

## A4. 配置面

- **环境变量 142 个**（`server/config-schema.json` 的 `settings` 字典，`len==142`），启动时 `configcontract.validate_environment` 校验（server/main.py lifespan）；未登记变量会被拒启动（历史事故 WB_TRAFFIC_TAP）。
- `.env.example` 只列了 **30** 个 `WB_*`（`grep -c '^WB_' .env.example` → 29/30）—— 文档面远小于真实面。
- 配置文件：上游 `config.json`、`users.json`、`backup-status.json`、notify 配置、release/state 文件（deploy/）。
- 数据库即配置：`portal_settings`、`admission_policy`、`display_prefs`（表名见 A3）。

## A5. 部署形态

| 形态 | 证据 |
|---|---|
| Docker 多阶段：node:22 构建静态导出 → python:3.12-alpine 运行，EXPOSE 7864 | Dockerfile:49、92、161–166 |
| docker compose 单服务 `workbuddy-manager`，restart: unless-stopped，前端构建期编译 | docker-compose.yml:15–60 |
| 宿主机 systemd + nginx + socket activation（127.0.0.1:7866） | deploy/systemd/workbuddy-web.socket、deploy/systemd/INSTALL.md |
| Windows 原生启动器（start/stop/update.ps1 + cmd） | windows/README.md、windows/*.cmd |
| 客户端上传脚本 | client/zhihui_upload.py |
| 滚动发布/中继 | server/rollingrelay.py、server/drain.py、deploy/*.sh |

## A6. 文档说的 vs 代码做的（不一致）

1. **Next.js 版本**：docs/DEVELOPMENT.md:85、107 写 "Next.js 15"；README.md:148 与 `web/package.json` 是 Next 16（16.4.3）。文档陈旧。
2. **Python 版本**：docs/DEVELOPMENT.md:86 写 "Python 3.11+"；README/锁文件按 3.12 构建（README.md:148、server/requirements.lock.txt 头）。
3. **Qoder/Trae "已移除"**：README.md:87–89 说端点不再提供服务；代码仍保留 qoder/trae 适配器与中间件（server/platformpolicy.py:43–56、server/main.py:1170 附近），靠 `enabled=False` 下线。行为一致，但文档没写"为存量密钥保留兼容解析"，读文档的人会以为代码已删。
4. **上游拓扑**：README 架构图仍写 `workbuddy2api (Go) :7863`；compose 默认 `WB2API_BASE=http://host.docker.internal:7863`（docker-compose.yml），而 README 的生产描述是容器 `zhihui-workbuddy`（docs/STATUS-2026-11-25-healthcheck-gaps.md）。文档与部署脚本默认值不一致，需按环境确认。
5. **配置面**：README 只强调少数关键变量；真实配置 142 项（A4）。`.env.example` 覆盖 30 项，运维按它配会漏。
6. **"单进程单端口"**：README 的说法与代码一致（serve.py workers=1、静态由 FastAPI 托管），这条可以信。

## A7. 一句话定位

**WorkBuddy 授权/额度的转售与治理网关**：上游账号池 + 按密钥的预算与配额 + 不可变账本 + 管理控制台 + 用户门户（注册/签到/红包/积分），单进程 SQLite，靠 systemd/nginx/Docker 三种方式部署。复杂度不在"转发"，在**钱与状态机**（预算预留/结清/账本/门户积分）和**三套协议适配**。

---

### 附：A 节用到的命令

```bash
grep -c 'include_router' server/main.py                     # 28
grep -rh 'CREATE TABLE IF NOT EXISTS' server --include='*.py' | wc -l   # 95
python -c "import json;s=json.load(open('server/config-schema.json'));print(len(s['settings']))"  # 142
grep -c '^WB_' .env.example                                  # 29
# 启动实测（Windows venv，临时数据目录）
WB_DATA_DIR=... WB_AUTH_DIR=... WB_ADMIN_PASSWORD='SmokeTest-Pass-2026xQ' \
  python -m uvicorn server.main:app --port 7899   # /healthz 200
```
