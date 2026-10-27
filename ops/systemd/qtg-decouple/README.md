# K13：解耦 `qoder2-engine.service` 与 `qtg.service`（PartOf）

## 1. 问题

生产基线采集显示：

- `/etc/systemd/system/qtg.service` 是 root 运行的主 unit（**本方案不改它**）。
- `qoder2-engine.service` 仍带 `PartOf=qtg.service`（K13 修复尚未部署）。

`PartOf=qtg.service` 的 systemd 语义：**qtg.service 被 stop/restart 时，qoder2-engine.service 会被连带 stop/restart**。
后果：重启/停用 QTG 入口会把引擎一起打断，正在服务的引擎进程被非必要地重启。K13 目标是去掉这层联动，同时**保持启动顺序**（`After=qtg.service` 保留即可），并且不修改 qtg 主 unit。

## 2. 方案（已验证：备份后直接删除 `PartOf=` 行）

在目标机备份 `qoder2-engine.service` 原文件与当前 PartOf，然后删除该文件中匹配 `PartOf=...qtg...` 的行（保留 `After=qtg.service` 等其余配置），`daemon-reload` 后 `enable --now`，并用 `systemctl show -p PartOf` 断言 PartOf 不再引用 qtg。

> **为什么不首选 drop-in**：本方案最初尝试了 systemd 常用做法——新增
> `/etc/systemd/system/qoder2-engine.service.d/10-qtg-decouple.conf`，内容 `[Unit]` + `PartOf=`（空赋值应清空列表）。
> **隔离 VM（systemd 255.4）实测无效**：`systemctl cat` 确认 drop-in 已被读取，但 `systemctl show -p PartOf` 仍为 `qtg.service`；
> 改成"直接删除 unit 文件中的 PartOf 行"后立即生效（`PartOf=` 为空，qtg restart 不再重启引擎）。
> 该负结果已留证在 `evidence/.../k-supplement/k13/logs/dropin-ineffective-*.log`。
> 因此默认走"备份 + 直接编辑"；`USE_DROPIN=1` 仅保留给已验证该行为的 systemd 版本，不作为默认。

## 3. 安装

```bash
sudo ./install.sh                       # 默认 UNIT_NAME=qoder2-engine.service, QT_UNIT_NAME=qtg.service
sudo UNIT_NAME=qoder2-engine.service QT_UNIT_NAME=qtg.service ./install.sh
```

`install.sh`（幂等）：

1. 校验 root、unit 存在（`systemctl show -p FragmentPath`）。
2. 备份：原 unit 文件 + sha256 + `systemctl show -p PartOf` 输出 → `BACKUP_DIR`（默认 `/var/backups/qtg-decouple/<UTC时间戳>/`）。
3. 若文件中存在匹配 `PartOf=...qtg...` 的行 → 删除；无则视为已解耦（幂等）。
4. `systemctl daemon-reload`。
5. 断言 PartOf **不再包含** qtg（为空或只含其它 unit）。
6. `systemctl enable <unit>` + `restart`，断言 active。
7. 任一步失败 → 自动恢复备份 + `daemon-reload` + `restart`，非 0 退出。

## 4. 验证

```bash
sudo ./verify.sh
# 真实环境必须提供健康检查地址（否则不得声称健康已验证）：
sudo HEALTH_URL_ENGINE=http://127.0.0.1:<port>/healthz \
     HEALTH_URL_QTG=http://127.0.0.1:<port>/healthz ./verify.sh
```

断言：① PartOf 为空或不再含 qtg；② 记录 engine `MainPID` → `restart qtg.service` → engine PID **不变**；③ qtg 自身 PID **变化**；④ 两个 `/healthz` 均 200（`CHECK_HEALTH=0` 仅用于无 HTTP 端点的模拟 unit，会在输出中标注 `health_skipped`）。

## 5. 回退

```bash
sudo BACKUP_DIR=/var/backups/qtg-decouple/<stamp> ./rollback.sh
```

`rollback.sh`：用备份恢复 unit 文件 → `daemon-reload` → `restart qoder2-engine.service` → 断言 PartOf 恢复为备份记录值且服务 active。

## 6. VM 演练证据（2026-10-26，隔离 lab，systemd 255.4）

用 `qtg-sim.service` / `eng-sim.service`（`PartOf=qtg-sim.service`、`After=qtg-sim.service`，`ExecStart=/bin/sleep 3600`）实测：

- 带 PartOf：`restart qtg-sim` → `eng-sim` MainPID 变化（联动重启）✅
- 直接删 PartOf 行后：PartOf 为空；`restart qtg-sim` → `eng-sim` PID **不变**，qtg-sim PID 变化 ✅
- `verify.sh`（`CHECK_HEALTH=0`）在模拟 unit 上通过 ✅
- `rollback.sh` 恢复备份后 PartOf 重现，`restart qtg-sim` 再次联动重启 eng-sim ✅
- 演练后已 `stop/disable + rm` 两个模拟 unit 与 drop-in、`daemon-reload`，确认无残留 unit/进程 ✅

证据：`/home/gateway-lab/lab/evidence/runtime-20261026T012000Z/k-supplement/k13/`（`drill-report.json`、`logs/`、`backup/`）。

## 7. 风险与边界

- 只改 systemd unit 文件与依赖关系；不改引擎/QTG 代码、端口、用户；`qtg.service` 不动。
- 去掉 PartOf 后，stop/restart qtg **不会**再连带引擎；需要同时停引擎时必须显式操作（这正是 K13 目标，但也是运维习惯变化点）。
- `After=qtg.service` 保留启动顺序；`After` 不产生 stop/restart 传播。
- 直接编辑包管理的 unit 文件在升级时可能被覆盖：升级后需重跑 `install.sh`（幂等）；`BACKUP_DIR` 里的原文件可用于比对/回退。
- 回退会 `restart` 引擎（一次真实重启），生产执行前需按发布流程确认窗口；本目录脚本**未获生产授权**，只用于隔离 VM 演练与后续授权窗口。
