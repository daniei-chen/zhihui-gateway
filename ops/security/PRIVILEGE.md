# K16 权限模型与降权候选（未部署）

**现场事实（2026-10-23 只读）**：
- `qtg.service` / `qoder2-engine.service` / `workbuddy-web.service` 均以 **root** 运行；
- `sub2api-static.service` 的 unit 文件 `777 root:root`（软链指向 `/opt/sub2api-cn/`，目标 644）；
- `agentuser` 具备 `NOPASSWD: ALL`，且与本机自动化/DSH 同身份；
- `/opt/gateway-ops` 属主 agentuser，其中脚本以 root 由 cron 执行（存在"可写代码 + root 执行"的组合）；
- WorkBuddy 容器已是非 root（app/UID 10001）。

**风险**：应用或自动化被利用后，影响面从"单个服务"扩大到"整机"。这不等于"公网匿名已获得 root"（没有入口证据），但必须按实际执行域收紧。

## 目标权限域

| 域 | 属主 | 权限 | 说明 |
|---|---|---|---|
| 代码/发布包 | root | 755/644 | 服务只读，不能改自己跑的代码 |
| 运行状态数据 | 服务用户 | 750 | DB/WAL/日志/锁 |
| 凭据 | root | 600 | 只读一次，运行时不落盘明文 |
| 配置（nginx/systemd） | root | 644 | 变更走发布流程 |
| 自动化脚本 | root 或专用用户 | 750 | **不能**由被调用服务的用户可写 |

## 降权顺序（每步独立可回退）

1. **先做权限清单**：每个 unit 的 `User=`、`ReadWritePaths=`、实际写的目录（strace/审计日志）；不要凭空猜测。
2. **Manager 先降权**（影响面最大、状态目录清晰）：
   - 新建 `wbmanager` 用户；`/opt/workbuddy-manager/data` 与 `releases/*/data` 软链改为该用户可写；
   - unit 加 `User=wbmanager`、`ProtectSystem=strict`、`ReadWritePaths=` 限定；
   - 回退：改回 root + 重启。
3. **QTG 次之**：需要读 `/opt/qoder-hub/secrets/*`（600 root），可改为 group 可读 + QTG 用户入组；不要为此放宽成 644。
4. **引擎**：Qoder 引擎绑定 loopback，降权后需确认它能读账号目录。
5. **agentuser sudoers 收敛**：从 `NOPASSWD: ALL` 改为精确命令白名单（运维需要的 restart/status/journalctl/nginx -t）；保留一个可审计的恢复通道。
6. **gateway-ops**：root cron 执行的脚本目录改为 root 所有、agentuser 只读。

## 验收

- `systemctl show -p User <unit>` 全部为非 root（除必要项）；
- 服务功能回归：三平台推理、登录、备份、调度各跑一遍；
- `sudo -l -U agentuser` 只剩白名单；
- 降权后不能写代码目录（`touch /opt/.../server/x` 应失败）。

## 回退

每个 unit 的 `User=` 改动单独提交；回退只回退该 unit（不整体回退），并在发布记录里写清"为什么当时需要回退"。
