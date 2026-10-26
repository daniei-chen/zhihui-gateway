# K46 告警覆盖与送达（设计）

**现场事实（2026-10-23 只读）**：
- `notify.enabled=false`（`/opt/notify/config.json`），但另有独立告警链：
  `wb-gateway-alert.timer`（每 1 分钟）、`healthcheck.timer`（每 5 分钟）、`trae-limits.timer`（每小时）；
- 历史记录显示 email 通道曾成功，其他通道失败；
- "全机没有任何告警"是过强结论——准确说法是"没有统一的告警覆盖与送达证明"。

## 覆盖矩阵（信号 → 检测点 → 当前状态 → 待补）

| 信号 | 检测点 | 现状 | 缺口 |
|---|---|---|---|
| 服务存活 | systemd + healthcheck.timer | 有 | 重启事件是否通知 |
| 网关 5xx/延迟 | Manager 指标 / 日志 | 部分（/wb/metrics 曾公开） | 无外部探测与阈值告警 |
| 长流截断 | Manager drain 计数（K12 新增） | 新能力 | 未接告警 |
| 备份年龄 | latest.json mtime | 无 | 超过 36h 未备份应告警 |
| 异地副本失败 | latest.json.offsite=false | 有字段 | 未升级为告警（已持续多天） |
| 证书到期 | certbot.timer | 有续期 | 到期前提醒未知 |
| 磁盘/内存 | 主机指标 | 无统一阈值 | 需阈值与去重 |
| 预算 unknown | budget_reservations | operational.monitor 有 transition | 是否送达未知 |
| 自愈动作 | healthcheck 重启 | 有 | 重启原因/结果无通知 |

## 设计原则

1. **送达要能被证明**：每次告警写一条持久记录（时间/信号/通道/结果），不能只看"函数被调用"；
2. **区分"探测失败"与"业务拒绝"**：额度耗尽/认证失败不应触发重启类自愈；
3. **发布期间静默**：发布锁存在时，告警降级为记录（见 K28）；
4. **不制造告警风暴**：同一信号 5 分钟内只投递一次（去重键 = 信号 + 主机 + 服务）；
5. **敏感信息不进告警正文**：不带 token、cookie、账号凭据。

## 验证方法（需授权）

- 模拟事件：停一个非关键 timer → 观察告警是否 5 分钟内送达；
- 检查 `wb-gateway-alert` 最近一次执行结果与日志；
- 检查 `notify.enabled=false` 的真实原因（是否为临时关闭后忘记恢复）。

## 工具

`ops/alerting/check-silence.sh`：只读检查"是否在静默"——timer 最近执行时间、notify 配置状态、
告警日志最后一条时间；输出 JSON。用于发布前确认观察通道可用（03 方案 §10.1 要求）。
