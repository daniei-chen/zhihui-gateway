# K13 / K08 / K09 运维候选配置（未部署）

本目录是**候选配置**，不是线上现状。当前没有发布授权，任何文件都不得直接覆盖 `/etc`。

| 文件 | 任务 | 作用 |
|---|---|---|
| `systemd/qtg.service.d/10-decouple-engine.conf` | K13 | 说明文件，不安装；ConsistsOf 为自动反向属性，不能配置 |
| `systemd/qoder2-engine.service.d/10-decouple-engine.conf` | K13 | 清空引擎的 `PartOf=qtg.service` |
| `nginx/zhihui-hardening.conf` | K08/K09 | `/wb/metrics` 保护与 `/wb/` 别名收敛（先统计依赖） |

## 现场事实（2026-10-23 只读取证）

- `qtg.service`：`ConsistsOf=qoder2-engine.service`、`User=root`、`MemoryMax=256M`。
- `qoder2-engine.service`：`PartOf=qtg.service`、`TimeoutStopUSec=1min 30s`。
- 两者同时存在时，`systemctl restart qtg` 会传播停止 Qoder 引擎 → 管理面发布连带推理中断（K13）。
- Nginx 生效配置：`/metrics` 已是 404，但 `/wb/` 全站别名仍指向 Manager 根（`location /wb/ { proxy_pass http://127.0.0.1:7864/; }`）。
  **2026-10-25 lab 实测更正**：`/wb/metrics` **不再**匿名可读 —— 返回 401，需要独立指标凭据；
  但 `/wb/api/*` 在持有会话时与 `/api/*` 完全等价（7 个端点实测 200，写操作 `POST /wb/api/logout` 200），
  构成第二套管理面入口，边缘限流/鉴权/CSP 策略不覆盖。详见 `design/K09-edge-location-audit.md`。

## 部署顺序（需授权）

1. **先验证依赖**：统计 `/wb/` 的实际调用（客户端 UA、路径、频率），确认只用 `/wb/v1/*`；
2. 先在 Linux 隔离环境验证引擎 `PartOf=` drop-in 与完整依赖图，再安装引擎侧 drop-in → `systemctl daemon-reload` → **不重启引擎**，只 `systemctl restart qtg` 观察引擎 PID 是否保持；
3. Nginx：`nginx -t` 通过后 `systemctl reload nginx`（graceful，不杀流）；
4. 观察 10 分钟：`/wb/metrics` 404、`/wb/v1` 正常、Qoder 推理正常。

## 回退

- systemd：删除对应 drop-in → `daemon-reload`；不需要重启引擎（若已重启，按原 unit 启动）。
- Nginx：恢复备份的 `sites-enabled/gateway.example.com` → `nginx -t` → `reload`。
- 两者都不得在回退时重开匿名管理面、不得指向死端口（见 03 方案 §10）。
