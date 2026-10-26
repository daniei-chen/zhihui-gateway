# zhihui-gateway · 智惠网关

三平台（WorkBuddy / Qoder / Trae）统一的 LLM 网关：单一 Manager 控制面管理身份、权限、
推理密钥、预算账本与用量计量，数据面兼容 OpenAI Chat Completions / Responses 与
Anthropic Messages 三种协议，前端为统一的 Next.js 控制台。

> 本仓库为**脱敏发布版**：不含生产配置、证据台账、运维交接与任何真实凭据；
> 文档中出现的主机名/IP 均已替换为文档保留值（`gateway.example.com` / `203.0.113.10`）。
> 未附开源许可证前，保留所有权利。

## 组成

| 目录 | 内容 |
|---|---|
| `manager/` | Manager 控制面 + 推理数据面（FastAPI）：登录/RBAC、推理密钥与额度、预算预留与未知消耗保留、请求日志/用量聚合、多协议网关、实例租约 |
| `qtg/` | Qoder / Trae 网关（FastAPI）：平台适配器、能力表与三层成功语义、身份桥、与 Manager 的只读 HTTP 契约 |
| `src/three-platform/web/` | 统一前端（Next.js） |
| `tests/` | 浏览器夹具、Linux lab 演练脚本（回退保护、影子判定等） |
| `tools/` | 路由审计、控制面盘点、签名/验签等工程工具 |
| `ops/` | 部署参考（nginx 策略、容量、安全基线等文档） |
| `design/` | 架构决策与模块设计（ADR、K 系列卡片） |

## 安全与一致性设计（摘要）

- **预算预留与未知消耗保留**：每个请求先做保守预算预留；客户端取消且上游未回
  usage 时，预留进入 `unknown` 状态而不是释放——恢复/回退不丢账。
- **同库无损回退**：schema 迁移采用 expand 优先 + 读者兼容认证（`schema_reader_compatibility`），
  旧版本进程可在新 schema 上继续运行，回退不要求恢复旧快照。
- **原子发布指针 + 启动门禁**：生产布局为 `releases/<version>/` + `current` 符号链接；
  `deploy/source_preflight.py` 在每次启动时校验发布清单的逐文件 sha256/权限位与
  Ed25519 签名，任何未登记的改动都会让服务拒绝启动。
- **密钥权限**：公开推理密钥只在创建响应中返回一次明文，库存哈希；额度/白名单/平台
  绑定逐密钥生效。

## 本地运行测试

```bash
cd manager
python -m venv venv && venv/bin/pip install -r server/requirements.txt
python -B -X utf8 ../tests/zhihui_harness/run_tests.py --target manager-assets --keep
```

> Windows 下先设置 `PYTHONUTF8=1`、`PYTHONDONTWRITEBYTECODE=1`。
> 测试入口是隔离运行器（临时根 + 网络 block + 假 provider），不要直接 `pytest` 全量。

## 单模块快速体验（开发模式）

```bash
cd manager
WB_DATA_DIR=./data venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 7864
# 首次启动会在日志打印生成的初始管理员账号，登录后请立即修改。
```

## 目录契约速览

- `manager/server/migrations.py` — schema 版本化（expand 优先，收缩步骤默认 deferred）
- `manager/server/schema_compat.py` — 旧读者兼容认证（回退安全）
- `manager/server/budget.py` — 预算预留 / 结算 / unknown 语义
- `manager/server/instancelease.py` — 实例租约（单写者交接）
- `qtg/app/qtg/capabilities.py` — 平台能力表与"三层成功"判定
- `tools/audit_control_plane.py` — 控制面路由/权限盘点
- `tools/release_sign.py` — 离线 Ed25519 发布签名（keygen/bundle/verify）

## 状态

主线候选已通过 Windows 3049 项 / Linux 3049 项隔离回归与同库 v7→v9→v7 无损回退演练；
生产发布流程与真实账号验收按部署方节奏单独推进。
