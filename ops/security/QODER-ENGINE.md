# K20 Qoder 引擎本机鉴权与并发（候选）

## 现状（既有取证 + 审计材料）

- `qoder2-engine.service`：`/usr/bin/python3 /opt/qtg/engines/qoder_proxy.py --port 8799`，绑定 loopback；
- 鉴权：API key 关闭（`API_KEY=None`，`configured_keys()` 为空时只回退到 `API_KEY`）；
- 服务形态：`ThreadingHTTPServer`，`backlog=5`（并发连接排队很短）；
- 只绑 127.0.0.1，**不是公网裸奔**；但同机任何进程都可以直连调用。

## 风险

1. **本机任意进程可调用**：若某个低权限进程被攻破，可直接消费 Qoder 额度；
2. **并发无界**：`ThreadingHTTPServer` 每连接一线程，没有线程/连接预算，
   本机并发暴涨会同时拖垮引擎与 Manager/QTG（共享 CPU/内存）；
3. `backlog=5` 在高并发下会直接拒绝连接，表现为上游 502 而不是排队。

## 候选

### 1. 启用本机 API key（最小改动）

- 在 `/opt/qtg/secrets/qoder_engine_key`（600 root）放一个随机值；
- 引擎启动时读该文件，校验 `Authorization: Bearer`；
- QTG/Manager 调用引擎时带上同一个 key（从各自 secrets 读，或同一个文件只读挂载）；
- 兼容期：先"接受带 key 与不带 key"，观察日志无匿名调用后再切换到"必须带 key"。

### 2. 并发预算

- 给 `ThreadingHTTPServer` 加并发信号量（例如 16），超出返回 429/503；
- `backlog` 调到与并发预算匹配的值（如 32），不要照抄 512；
- 参数由压测决定：用假上游脚本打 N 并发，记录 P95、错误率、RSS。

### 3. 观测

- 在引擎侧暴露 `/healthz`（不外网）与在途计数，供发布排空（K12）与告警（K46）使用。

## 验收

- 匿名调用（不带 key）在启用后被拒绝；
- 并发 32 时 P95 不超过基线的 1.5 倍；
- 引擎重启不影响 Manager/QTG 的其它平台（与 K13 解耦配合）。

## 未验证

- 引擎源码不在执行树内（`/opt/qtg/engines/qoder_proxy.py` 只在服务器），本候选是黑盒合同；
- `backlog=5` 是否为实际生效值需在服务器确认（可能是默认值）；
- 压测必须在隔离环境，不能在生产上打。
