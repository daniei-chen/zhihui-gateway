# 05 · 部署 / 发布 / 备份（B 节 + D 节）

**范围**：`zhihui-gateway` 的部署形态、发布与溯源、备份/恢复/更新、以及这些路径上的风险。不含产品定位、预算算法、安全模型本身（见 00/01/02/03/04 节）。
**方法**：只读代码与部署脚本，`grep -n` / `sed -n` 定点取证，未连生产主机、未跑任何部署脚本。结论全部给 `file:line`；凡"生产实际是什么"一律标 **需主机确认**。
**时间**：2026-11-28 00:00–01:55（设备时间）。仓库 `main` @ `ec87e0b`，VERSION 1.6.28。
**取证协作**：两个只读子代理分别核了备份子系统与滚动/writer-guard；本文对全部 19 条做了逐行复核，子代理定位的条目已按仓库实际行号校正（校正记录见下）。

**三句话结论**：

1. 仓库里装着两套部署形态。`deploy/install.sh` 装的是 root + `0.0.0.0:7864` 的裸 uvicorn（形态 A），生产实际跑的是 `setpriv 10001` + `127.0.0.1:7866` + writer-guard 的受管形态（形态 B）。照仓库装出来的主机，端口、用户、完整性契约全和生产对不上。
2. 两个 P0 都在发布链上：中继把网关的容量拒绝当成 drain 重试 6 次（把过载放大成整站 5xx）；蓝绿脚本在 `route_switch old` 后 1 秒就 `systemctl stop` 候选，>90s 的长流必被 SIGKILL，而脚本仍在 `:460` 记"窗口内零 5xx"。
3. 备份与更新器各有硬伤：`manager/data/` 除 5 个硬编码文件外不进备份（静默缺失）；更新器从 `ithtelab/workbuddy-manager` 拉包、只比同源 sha256、无验签，且安装器与更新器都不校验"是否本仓库"。

---

## 一、部署形态（B）

### 1.1 仓库里同时存在两套部署形态，等级不同

| | 形态 A：`install.sh` | 形态 B：受管发布（managed） |
|---|---|---|
| 启动器 | `ExecStart=... uvicorn server.main:app --host 0.0.0.0 --port 7864`（`deploy/workbuddy-web.service:34`，`deploy/install.sh:296` 渲染） | `writer_entry.py` → `launch_guard` → `server.serve`（`ops/writer-guard/launch_guard.py:220-233`） |
| 运行用户 | **root**（单元无 `User=`；`install.sh` 要求 root） | `setpriv --reuid=10001`（`launch_guard.py:223-225`） |
| 端口 | 7864，绑 `0.0.0.0`（`workbuddy-web.service:9`） | 7866 主 / 7865 候选（`launch_guard.py:229-230`） |
| 完整性 | 无（`managed-release.json` 不存在时更新器直接放行） | Ed25519 签名 + `managed-release.json` + `preflight-gate.py` |
| 备份/日志 | 与生产同库 | 与生产同库 |
| 实际生产 | **不是**生产形态 | **是**生产形态（7866/7867 口径见 `deploy/healthcheck.sh:58`、`deploy/rolling-relay.service:15`） |

**关键事实**：`deploy/install.sh` 安装的是形态 A。`grep -c socket deploy/install.sh` = 0；`grep -n "writer_entry\|managed-release" deploy/install.sh` 无命中。也就是说，一个照着仓库文档装出来的主机，拿到的是 root + 7864 + 无签名 + 无 writer-guard 的形态，而生产实际跑的是形态 B。两套形态的端口、用户、完整性契约全部不同，仓库里没有一处说明"install.sh 只用于开发/自建、生产走 managed"。

### 1.2 端口与拓扑（生产口径，来自代码）

```
nginx :443
  └─ map $zhihui_relay_upstream → 7867（中继）或 7866（直连）
       ├─ 中继 zhihui-rolling-relay.service :7867  (rolling-relay.service:19, User=root + setpriv)
       │    ├─ old 后端 127.0.0.1:7866  (relay_app.py:34-41)
       │    └─ new 后端 127.0.0.1:7865  (relay_app.py:34-41)
       ├─ 主实例 :7866  (launch_guard.py:229-230, writer_entry.py main)
       └─ 候选 :7865   (launch_guard.py:216,229-230, WB_BACKGROUND_JOBS=0)
上游 workbuddy2api :7863 (workbuddy-web.service:11)
```
- 中继是独立单元，`Restart=always`、`PrivateTmp=yes`、`ProtectSystem=strict`、`ReadOnlyPaths=/opt/workbuddy-manager/data`（`deploy/rolling-relay.service:19-25`）。
- 主实例与候选共用 `WB_DATA_DIR=/opt/workbuddy-manager/data`（`launch_guard.py:204-210`），即同一个 SQLite 库。
- 切流有两层：nginx `map`（`rollout-rolling.sh:114-146`）与中继路由文件（`rollout-rolling.sh:350-366`）。

### 1.3 后台任务与单写者

- `WB_BACKGROUND_JOBS` 决定是否跑后台循环（`server/main.py:234`）；候选与 helper 被强制置 0（`launch_guard.py:216`、`deploy/workbuddy-web-helper.service:42`）。
- 单写者**没有跨进程锁**：`server/db.py` 只有进程内 `threading.RLock()`（`:23`）+ WAL（`:614`）+ `BEGIN IMMEDIATE`（`:2007`）；`instance_leases`（`server/instancelease.py:73`）是租约表，不是写者互斥。跨进程 `flock` 只出现在 `budgetowner.py:26`、`steward_runtime.py:56` 这些别的子系统。**没有任何启动期检查**阻止 `--workers>1` 或第二个实例同时 serving；`server/main.py` 里没有 worker 数校验，`server/serve.py:95-100` 只是把 `workers=1` 写死。
- 排空计数是**进程内** `dict`（`server/drain.py:18-20`），所以两个进程各自的 `total_in_flight` 互不可见。

### 1.4 滚动发布

`deploy/rollout-rolling.sh` 有两条路径：`--blue-green`（默认）与 `--no-blue-green`（`rollout-rolling.sh:13,475-476`）。蓝绿序列（`rollout-rolling.sh:384-465`）：
1. `systemctl start $HELPER`（7865，`WB_BACKGROUND_JOBS=0`）
2. `route_switch new`（中继把新请求给候选）
3. `systemctl stop $GATEWAY` → `systemctl start $GATEWAY`（主实例换成新版本）
4. 轮询 `/api/release` 确认版本
5. `route_switch old` → `sleep 1` → `systemctl stop $HELPER`

第 5 步是 P0-2 的位置（见四）。

### 1.5 systemd socket activation

- 单元 `deploy/systemd/workbuddy-web.socket`：`ListenStream=127.0.0.1:7866`（`:22`）、`Accept=no`（`:25`）、`Backlog=4096`（`:28`）。
- drop-in `deploy/systemd/workbuddy-web.service.d/10-socket-activation.conf`：`Sockets=workbuddy-web.socket`（`:17`）。
- 消费方只有 `server/serve.py:95-100`（`inherited_listener()`，`:48-62` 从 fd 3 取）。
- **安装器不装它**：`deploy/install.sh` 里 `socket` 出现 0 次；`deploy/systemd/INSTALL.md:19-35` 全是手工 `cp` + `systemctl`。
- 形态 A 的 `ExecStart` 是 uvicorn 直启，**不消费** `LISTEN_FDS`。所以形态 A 上装了 socket 单元，只会让 7866 被 systemd 占住而无人 accept，nginx 指向 7866 就 502。

### 1.6 容器形态

`Dockerfile:166` 的 `CMD` 是 `uvicorn server.main:app --host 0.0.0.0 --port 7864`，**绕过 `serve.py`** → 没有 socket activation、没有 `DrainMiddleware` 的 SIGTERM 钩子（`DrainMiddleware` 在 `server/main.py:511`，但 `serve.py:71-73` 的 `DrainApp` 包装才是把 drain 接到 shutdown 的地方）。`docker-compose.yml` 没有 `stop_grace_period`，`docker stop` 默认 10s 后 SIGKILL。

---

## 二、发布与溯源（B'）

### 2.1 版本与 provenance

- `VERSION` = `1.6.28`；`server/release-provenance.json` 里 `version` 同为 1.6.28，但 `candidate_only: true`（`:4`）、`database_rollback: false`（`:5`）、`production_deployed: false`（`:9`）——**这份 provenance 描述的是"未上生产"的候选**。
- `server/release-provenance.json` 的 `source_commit` 指向 `ec87e0b` 之前的提交，`reviewed_hashes` 是 247 个文件的归一化哈希（`tools/check_reviewed_hashes.py` 校验）。
- `server/product-release.json` 是运行时读的版本文件（`server/product.py:22-40`），与 `VERSION` 不同源；两者不一致时 `product.py` 会回退到 `VERSION` 并在日志里告警。

### 2.2 两条签名链（互不覆盖）

| 链 | 覆盖对象 | 信任锚 | 证据 |
|---|---|---|---|
| 公开 Release | 发布包/镜像 | 无签名，只有 sha256（`deploy/verify-release.sh:15-16` 明确"2026-10-14 起不再做离线私钥签名"） | `verify-release.sh:15-16` |
| 受管发布 | `releases/<ver>/` 整棵树 | Ed25519 分离签名 + `managed-release.json` | `deploy/sign-release-ed25519.py:1-40`、`deploy/prepare-managed-stage.sh:21-45`、`deploy/preflight-gate.py:1-40` |

- 受管链的验签方：`ops/writer-guard/writer_entry.py:106-120`（`for name in ('server','deploy')` 校验 host-files 闭包）、`deploy/preflight-gate.py:91`（`manifest_path = root / 'deploy/managed-release.json'`）、`deploy/source_preflight.py:25`（`MANIFEST_FILES`）。
- `deploy/verify_signed_release.py` 是**退役桩**：`main()` 直接 `return 2`（`:12`），留着防止旧自动化误用。
- 公开链的 `verify-release.sh` 只做 `sha256sum -c`（`:30-60`），没有签名步骤。

### 2.3 受保护哈希

`docs/protected-customizations.json` 记录 247 个受保护文件的哈希；`tools/check_reviewed_hashes.py` 复算。`deploy/protected-customizations.py` 是生成/校验工具（`grep -n "def " deploy/protected-customizations.py`）。CI 里 `quality.yml` 会跑（`.github/workflows/quality.yml`）。

### 2.4 必须人工在主机上做的步骤

| 步骤 | 谁做 | 证据 |
|---|---|---|
| `systemctl daemon-reload` + 写单元 | 人 | `install.sh:300`（安装时自动）；`deploy/systemd/INSTALL.md:19-35`（socket 手工 cp + systemctl） |
| `chown -R 10001:10001` 数据目录 | 人 | `install.sh:211`（自动）；managed 路径靠 `launch_guard` 检查 |
| `managed-release.json` 生成 + 签名 | 人 | `deploy/prepare-managed-stage.sh:21-45` |
| `preflight-gate.py` 通过 | 人 | `deploy/preflight-gate.py:1-40` |
| nginx `map` 切换 | 脚本 | `rollout-rolling.sh:114-146` |
| `release.py activate` 切指针 | 人 | `deploy/release.py:554-578`（`cmd_activate`） |
| 备份 cron 安装 | 人 | `deploy/install-recovery-scripts.sh:37-53` |

---

## 三、备份 / 恢复 / 更新（B''）

### 3.1 备份

- 入口 `server/backup_application.py:374`（`main()`）。流程：`flock` 独占锁（`:377-379`）→ `sqlite3` 在线 backup API（`:418-426`，`pages=256`，`deadline` 900s）→ `PRAGMA quick_check`（`:432`、`:507`）→ 写 `recovery-<ts>.tar.gz`（`:400`）→ `latest.json` 原子替换（`:524-525`）→ `publish_verified_status`（`:526-527`）。
- 目标 `/root/backups/workbuddy-daily/`（`:19`），`latest.json` 0600。
- 内容：`manager.db`（`:440`）+ 5 个硬编码文件（`gateway/config.json`、`gateway/data/state.json`、`gateway/data/session-binds.json`、`manager/data/users.json`、`manager/data/zh-gateway.json`，`:441`）+ `gateway/` 其余文件 + `web/out` 静态资源。**`data/` 目录整体被排除**（`:455` 的 `data` 段），只靠 `add_state_directory`（`:233-239`）加目录条目，不递归。
- 异地：`server/offsitebackup.py`，只写密文到 `workbuddy-recovery-encrypted` 分支（`:18`），`openssl cms` 加密（`:125-127`），回读校验密文 sha256（`:102-110`）。
- 失败语义：`backupstatus.py:72-95` 只在成功时写 `backup-status.json`；失败时**不写**，所以外部只能看到"状态文件变旧"。

### 3.2 恢复

- 演练：`deploy/restore_drill.py`（`main()` 在 `:437`）—— 拼片 → 解密 → 解包 → `quick_check` → 行数复验，全程在私有临时目录，只读快照；可选 `--trusted-checkout` 起隔离 ASGI 验应用。`deploy/recovery-drill.sh` 只是它的 `exec` 包装（`:9`）。
- **真恢复：仓库里没有自动恢复工具。** `docs/DISASTER-RECOVERY.md:146-175` 写的是手工步骤（解到新目录 → 核对 → 停写入 → 切换），`docs/DISASTER-RECOVERY.md:192` 明确"自动恢复：没有"。`restore_drill.py --restore-sidecar` 只把 sidecar 元数据写回快照目录（`:107`、`:499`），不是恢复。
- 异地状态不一致：`docs/DISASTER-RECOVERY.md:192` 记"异地备份 **未启用**（`offsite: false`）"，而 `server/offsitebackup.py` 是完整实现。两者是否一致 **需主机确认**（`backup-status.json` 的 `offsite` 字段）。

### 3.3 更新

- 更新器两份**字节相同**：`server/deploy/update.py` 与 `deploy/update.py`（`diff` 无输出，1941 行）。面板优先用 `server/deploy/update.py`（`server/services/updater.py:139-151`）。
- 目标仓库硬编码 `MANAGER_REPO = os.environ.get('WB_MANAGER_REPO') or 'ithtelab/workbuddy-manager'`（`update.py:75`）——**默认指向 `ithtelab/workbuddy-manager`，不是本仓库 `daniei-chen/zhihui-gateway`**。
- 完整性：`verify_download()`（`update.py:550-600`）只比大小 + `sha256`；`sha256` 来自 GitHub Release API（`:1264-1265`）。**没有签名校验**（`grep -rn "verify_release_signature\|SKIP_SIGNATURE" deploy/update.py` 无命中）。
- 覆盖范围：`_sync_deploy()`（`:957-1003`）默认把新包里的 `deploy/` 整目录覆盖到安装目录，`WB_SYNC_DEPLOY=0` 才跳过。`deploy/` 里有 `update.py` 自己、`verify-release.sh`、`install.sh`。
- 回滚：更新前会把旧代码拷到 `backup-<ts>/`（`update.py:1636-1641`：`server/` + `web/out/`），`_sync_deploy` 再把旧 `deploy/` 拷进 `backup/deploy`（`:993`）。但 `update.py` 里**没有任何 `rollback` 实现**（`grep -cn rollback server/deploy/update.py` = 0），`swap_dir()`（`:1532-1568`）成功后还 `shutil.rmtree(old)`（`:1568`）删掉被替换的旧目录。也就是说"更新后回滚"只能靠人手工从 `backup-<ts>/` 拷回，或走发布器 `deploy/release.py cmd_rollback`（`:580`）切 release 指针。

---

## 四、风险清单（D）

> 严重度：P0 = 生产窗口内必然造成用户可见故障/数据风险；P1 = 明确可复现的故障或安全缺口；P2 = 语义/文档/运维陷阱。每条给"现象 / 为什么 / 怎么验"。
> 计数：**P0×2、P1×11、P2×6，共 19 条**。

### P0-1 中继把"网关容量拒绝"当成"排空"，同一请求重试 6 次，过载被放大
- **file:line**：`server/rollingrelay.py:69-76`（`is_draining_response` = `503` 且带 `Retry-After`）、`:163-198`（重试循环，`STARTUP_RETRIES=6`，退避 `1.5*(n+1)`）；网关准入拒绝的应答在 `server/observability.py:1216-1234`（`503` + `Retry-After: 1`）。
- **现象**：容量拒绝（`gateway_capacity_exceeded`）与 drain 应答的判据完全重合。中继把一条已经明确被拒的 POST 当成"后端正在重启"，重试 6 次、累计约 22.5s，并在这期间占住中继 128 个连接名额之一（`rollingrelay.py:83-113`）。
- **为什么**：过载时最不该做的就是放大请求量。网关已经在 503 上明确说"现在别来"，中继却把同一条请求再送 5 次，还把连接池占满，导致健康请求也开始排队超时——一次容量拒绝会升级成整站 5xx。
- **怎么验**：`sed -n '69,76p;163,198p' server/rollingrelay.py`；`sed -n '1216,1234p' server/observability.py`。单测复现：mock 后端返回 `503 {'Retry-After':'1'}`，断言 `send()` 只调用一次（当前会调用 6 次）。

### P0-2 蓝绿切流后 1 秒就停候选，长流被 SIGKILL
- **file:line**：`deploy/rollout-rolling.sh:422-424`（`route_switch old` → `sleep 1` → `systemctl stop "$HELPER"`）；候选 `TimeoutStopSec=90`、`KillMode=control-group`（`deploy/workbuddy-web-helper.service:58-59`）；graceful 75 + lifespan 75（`ops/writer-guard/launch_guard.py:231`、`server/main.py:331`）。
- **现象**：切回 old 之后 1 秒就 `systemctl stop` 候选，没有任何在途请求判据。候选上正在流式输出的 SSE 请求，SIGTERM 后走 lifespan 的 75s graceful，`TimeoutStopSec=90` 兜底 SIGKILL；超过 90s 的生成必然被截断。
- **为什么**：蓝绿的目的是"零中断"，但这一步把"候选上还有在途请求"这个唯一需要等待的条件忽略了。脚本随后在 `:460` 用 `request_outcomes` 统计"窗口内 5xx"，而流式请求被截断时通常不落 5xx（连接被对端关闭），所以统计会显示"零故障"，故障被静默。
- **怎么验**：`sed -n '415,430p' deploy/rollout-rolling.sh`；`systemctl show workbuddy-web-helper -p TimeoutStopUSec -p KillMode`。演练：起一条 120s 的流式请求，跑一次蓝绿，观察客户端是否在中途收到 EOF。

### P1-1 更新器供应链：默认仓库不是本仓库、无签名、摘要与包同源、pip 无 hash
- **file:line**：`update.py:75`（`MANAGER_REPO` 默认 `ithtelab/workbuddy-manager`）、`:550-600`（`verify_download` 只比大小 + sha256）、`:1264-1265`（`pkg_size`/`pkg_digest` 取自 Release API）、`:957-1003`（`_sync_deploy` 覆盖 `deploy/`）、`install_requirements()`（`:1340`，`pip install -r requirements.txt`，无 `--require-hashes`）。
- **现象**：更新器从 `ithtelab/workbuddy-manager` 拉最新 Release，只校验包内 `sha256`（由同一个 API 提供），解包后覆盖 `server/`、`web/out`、`deploy/`，再 `pip install`。整条链路没有签名、没有 pin 到本仓库的 commit、没有 `--require-hashes`。
- **为什么**：`sha256` 与包来自同一个 API，能改包的人也能改摘要；`deploy/` 被覆盖意味着更新器可以替换自己与信任锚；`pip install` 从 PyPI 拉最新依赖，`requirements.txt` 的版本约束若被绕过就是供应链注入点。默认仓库不是本仓库，等于"上游一发布，本仓库的受保护定制就被覆盖"。
- **怎么验**：`grep -n "MANAGER_REPO\|verify_download\|require-hashes" server/deploy/update.py`；`diff deploy/update.py server/deploy/update.py`（应无差异）；`grep -rn "ithtelab" deploy server | head`。

### P1-2 两套部署形态并存，`install.sh` 装出的是 root + 0.0.0.0:7864
- **file:line**：`deploy/workbuddy-web.service:9`（`WB_MANAGER_HOST=0.0.0.0`）、`:34`（uvicorn 7864）、`deploy/install.sh:28`（`MANAGER_PORT=7864`）、`:296-301`（渲染单元 + `systemctl enable --now`）。
- **现象**：`install.sh` 生成的单元没有 `User=`，`ExecStart` 是 uvicorn 直启，端口 7864 绑 `0.0.0.0`。生产口径是 127.0.0.1:7866 + `setpriv` 10001（`launch_guard.py:223-225` 的 setpriv、`:229-230` 的 `127.0.0.1:7866`）。
- **为什么**：仓库里所有运维脚本、健康检查、nginx 片段都按 7866/127.0.0.1 写（`healthcheck.sh:58`、`rolling-relay.service:15`、`rollout-rolling.sh:117-118`）。照 `install.sh` 装出来的主机，脚本全部对不上端口；同时 7864 暴露在 0.0.0.0，任何能访问该网段的人都能直接打管理 API。
- **怎么验**：`grep -n "User=\|ExecStart\|WB_MANAGER_HOST" deploy/workbuddy-web.service`；`grep -n "7866\|7864" deploy/healthcheck.sh deploy/rolling-relay.service deploy/rollout-rolling.sh`。

### P1-3 备份 `data/` 目录被整体排除，只补 5 个硬编码文件
- **file:line**：`server/backup_application.py:455`（`data` 段一律 `continue`）、`:441-449`（`data/` 下只加 5 个文件名）、`:233-239`（`add_state_directory` 只加目录条目）。
- **现象**：除 `manager.db` 与那 5 个硬编码文件（`gateway/config.json`、`gateway/data/state.json`、`gateway/data/session-binds.json`、`manager/data/users.json`、`manager/data/zh-gateway.json`）外，任何新增状态文件都不会进备份；备份仍然报 `ok`，`latest.json` 的 `restore_check` 也是 `ok`。
- **为什么**：静默缺失比失败更危险——恢复时才发现文件不在，而备份报告一直是绿的。`data/` 下已有 `notify/`、`relay-route.json` 等运行时状态，新增状态目录时没人会记得改这份硬编码清单。
- **怎么验**：`sed -n '438,460p' server/backup_application.py`；`tar -tzf /root/backups/workbuddy-daily/recovery-*.tar.gz | grep 'manager/data/'`（需主机）。

### P1-4 每日备份归档没有任何清理者，磁盘单调增长
- **file:line**：`server/backup_application.py:400`（`recovery-<ts>.tar.gz` 写入 `BASE`）；`deploy/recovery_retention.py:15`、`deploy/recovery_housekeeping.py:19` 只处理 `/opt/zhihui-release/...` 与 `zhihui-v2`，**不碰** `/root/backups/workbuddy-daily`。
- **现象**：`grep -rn "workbuddy-daily" server deploy ops` 只命中写入方（`backup_application.py:19`）、健康检查（`healthcheck.sh:539`）、告警（`ops/wb-gateway-alert.py:121`）和 `fork-tools/fork-verify.sh:106`，**没有任何删除/轮转逻辑**。`CHANGELOG.md:2845` 写的保留策略（"最近 3 份 + 7 天内 + 引用的那份"）在仓库里没有实现——和 P1-10 一样，它在主机脚本里。归档含 `manager.db`，单份约 300–450 MB（`CHANGELOG.md:7419` 记 7 份共 2.1 GB）。
- **为什么**：磁盘写满时备份先失败（`MIN_FREE_BYTES` 检查），随后 SQLite 写入也失败，整站开始 5xx。而备份失败不会写 `backup-status.json`，外部只看到"状态变旧"。
- **怎么验**：`grep -rn "workbuddy-daily" . | grep -v '^./docs/' | grep -v test`；`grep -rn "backup-push" . | head`（只在文档/告警文案里，无实现）；主机上 `du -sh /root/backups/workbuddy-daily && ls -1 | wc -l`。

### P1-5 异地备份只验密文，不解密验证
- **file:line**：`server/offsitebackup.py:102`（回读后只比 `cipher_sha256`）、`:110`（`'restore_verified': False` 写死）、`:177`（`complete_restore_verified=False`）、`server/backupstatus.py:89-93`（`offsite_restore_verified` 只在有值时为真）。
- **现象**：异地备份的"成功"只证明密文能被读回且哈希一致；没有任何一步用私钥解密并跑 `quick_check`。
- **为什么**：证书/私钥不匹配、`openssl cms` 参数变化、密文被截断但哈希跟着更新——这些都能让"异地备份成功"变成"灾难时解不开"。
- **怎么验**：`sed -n '100,115p;170,180p' server/offsitebackup.py`；`grep -rn "restore_verified" server | head`。

### P1-6 备份与更新/发布没有强制耦合
- **file:line**：`server/deploy/update.py:1571-1720`（`update_manager` 只做代码备份 `backup-<ts>`，不备份数据库）；`deploy/rollout-rolling.sh:384-465`（发布流程无数据库备份调用，只备份 nginx map）；`deploy/install-recovery-scripts.sh:37-53`（cron 安装是独立步骤）。
- **现象**：更新器只备份代码目录（`backup-<ts>`），滚动发布脚本只备份 nginx map，两者都不碰数据库备份；数据库备份靠独立 cron（`install-recovery-scripts.sh`），失败也不阻塞发布。
- **为什么**：`database_rollback: false` 的迁移（1.6.27→1.6.28，`release-provenance.json:5`）一旦需要回滚，没有"发布前快照"就只剩每日备份，最多丢一天。
- **怎么验**：`grep -n "backup" server/deploy/update.py`（只有 `backup-<ts>` 代码备份，无数据库备份）；`grep -n "backup" deploy/rollout-rolling.sh`（无命中）。

### P1-7 单写者契约没有任何启动期强制
- **file:line**：`server/serve.py:95-100`（`workers=1` 写死）、`server/drain.py:18-20`（进程内计数）、`server/db.py:23`（`threading.RLock`，仅进程内）、`server/db.py:614`（WAL）、`server/db.py:2007`（`BEGIN IMMEDIATE`）、`server/instancelease.py:73`（租约表）。
- **现象**：`--workers 4` 或起第二个实例时，两个进程各有自己的 `RLock`，互不可见；SQLite 只有 WAL + `BEGIN IMMEDIATE` 保证单条事务串行，两个写者会撞 `database is locked`（`server/db.py:72` 的 `_record_busy`），而代码明确不做应用层重试（`:29` 注释自述"重试会把尾延迟放大成雪崩"）。`drain` 计数各算各的，`/readyz` 可能两个进程都报健康。
- **为什么**：契约靠"没人这么干"维持。`Dockerfile:166` 的 `CMD` 与 `install.sh` 的 `ExecStart` 都可以被运维改成 `--workers N`，没有任何检查会拒绝。
- **怎么验**：`grep -rn "workers" server/serve.py deploy/workbuddy-web.service Dockerfile`；`grep -rn "workers" server/main.py`（应无）。

### P1-8 `LOCAL-DIVERGENCE.md` 说"验签仍在解压前执行"，但仓库里的更新器已经没有验签代码
- **file:line**：`deploy/LOCAL-DIVERGENCE.md:27`（"经复核**没有安全回退**——验签仍在解压之前执行（`verify_release_signature` → `tarfile.open`），唯一跳过开关仍是原有的 `WB_SKIP_SIGNATURE=1`"）；`deploy/verify-release.sh:15-16`（"2026-10-14 起本项目不再对发布包做离线私钥签名"）；`server/tests/test_release_integrity.py:121-123`（断言 `deploy/update.py` 里**不允许**出现 `def verify_release_signature`、`SKIP_SIGNATURE`、`ssh-keygen` 等符号）。
- **现象**：`grep -rn "verify_release_signature\|WB_SKIP_SIGNATURE" deploy/update.py server/deploy/update.py` 无命中；`verify_download()`（`update.py:550`）只比大小 + sha256。也就是说 `LOCAL-DIVERGENCE.md:27` 描述的是**上游发布包里的** `update.py`（仍带 OpenSSH 验签），而仓库这份 `update.py` 早已把验签整段移除，测试还专门锁住"不许再出现"。
- **为什么**：`LOCAL-DIVERGENCE.md` 是"能不能安全吸收上游包"的复核结论，读者会据此认为"本仓库的更新器在解压前会验签"。实际不会——公开 Release 链只有 sha256，受管链的 Ed25519 只在 `prepare-managed-stage.sh` 那一层。把两条链的结论混在一起，等于给公开链一个它没有的信任属性。
- **怎么验**：`sed -n '20,30p' deploy/LOCAL-DIVERGENCE.md`；`grep -rn "verify_release_signature\|SKIP_SIGNATURE" deploy/update.py server/deploy/update.py`（无命中）；`sed -n '118,128p' server/tests/test_release_integrity.py`。

### P1-9 告警"静默失败"：`notify.dispatch` 在未配置时直接返回，不留痕
- **file:line**：`server/notify.py:71-75`（`_mode()` 返回 `disabled`）、`:108-109`（`if mode == 'disabled': return {'mode': 'disabled', 'delivered': False, 'payload': payload}`）；`ops/wb-gateway-alert.py:1236`（`_onfailure_main` 恒 `return 0`）、`:1247`/`:1331`（`main()` 恒 `return 0`）。
- **现象**：`WB_NOTIFY_*` 未配置时，告警既不发送也不写日志、不写文件，调用方拿到 `{'mode': 'disabled', 'delivered': False, 'payload': ...}` 但没人检查；`wb-gateway-alert.py` 作为 systemd timer 时永远退出 0，`OnFailure=` 永远不会触发。
- **为什么**：告警系统的失败模式是"静默"，而静默在值班视角与"一切正常"不可区分。PR #60 正是修这个（`notify.py` 的 `_mode()` 分支）。
- **怎么验**：`sed -n '66,80p;100,115p' server/notify.py`；`grep -n "return 0" ops/wb-gateway-alert.py`。

### P1-10 每日 05:30 的备份触发入口 `/root/wb2api-backup-push.sh` 不在版本库
- **file:line**：`docs/STATUS-2026-11-25-backup-memory-limit.md:88`（"`/root/wb2api-backup-push.sh` 是 root 的 cron 入口脚本，从来没有进过版本库"）、`docs/OPS-PITFALLS.md:433`（"处理点在 `/root/wb2api-backup-push.sh` —— 那不在仓库里"）；`ops/wb-gateway-alert.py:344` 只把它当字符串写进告警文案。
- **现象**：`grep -rn "backup-push" --include=*.sh --include=*.py deploy server ops` 无命中；仓库里只有 `deploy/install-recovery-scripts.sh`（装 cron 的备份与保留），没有这个推送入口脚本。它的 sha256 只出现在状态文档里（`STATUS-...:73-74`）。
- **为什么**：`server/backup_application.py` 能生成备份，但"每天几点跑、失败重试、日志落哪"全在主机上的一个脚本里。换机、重建、容器化之后备份会静默停摆——而健康检查只能看到 `latest.json` 变旧（26h 提示、50h 才 FAIL，见 P1-11）。
- **怎么验**：`grep -rn "backup-push" . | head`；主机 `crontab -l | grep -i backup`、`sed -n '1,40p' /root/wb2api-backup-push.sh`。

### P1-11 备份失败要 26 小时才提示、50 小时才 FAIL，且失败本身不写状态文件
- **file:line**：`server/backupstatus.py:72-78`（只在 `restore_check == 'ok'` 时写 `backup-status.json`，失败直接 `raise`，不落盘）；`server/operational.py:656`（`backup_stale` 阈值 `age > 26*3600`）；`deploy/healthcheck.sh:547`（`>50` 小时才 `broken`）、`:549`（`>26` 小时只 `note`）。
- **现象**：备份失败时 `latest.json` 保持上一次成功的时间戳，`backup-status.json` 不变。一个 05:30 的失败，最早 26 小时后才变成一条 `note`，50 小时后才变 FAIL。而 `backup_application.py` 里 2026-11-25 的 OOM 事故正是"被前一份报告还没超 26 小时兜住"（`:494-500` 注释自述）。
- **为什么**：失败与"还没到时间"在外部观测上完全同形。巡检口径又刻意把 26h 判成 note（避免维护窗口误报），于是连续失败两天才有一次 FAIL——而备份链的下游（异地加密、恢复演练）都依赖它。
- **怎么验**：`sed -n '72,80p' server/backupstatus.py`；`sed -n '654,658p' server/operational.py`；`sed -n '539,558p' deploy/healthcheck.sh`。

### P2-1 发布 provenance 描述"未上生产"，与 VERSION 1.6.28 的语境不一致
- **file:line**：`server/release-provenance.json:4-9`（`candidate_only: true`、`database_rollback: false`、`production_deployed: false`）。
- **现象**：文件是给"发布前"用的，但仓库里没有"发布后回填"的步骤；`tools/bump_version.py` 也不改这两个字段。
- **为什么**：读 provenance 的人会以为 1.6.28 没上生产，而实际是否上生产只能问主机。审计口径需要它，运维口径会被它误导。
- **怎么验**：`sed -n '1,12p' server/release-provenance.json`；`grep -n "production_deployed" -r server tools deploy`。

### P2-2 两份 1941 行的更新器副本，无一致性测试
- **file:line**：`deploy/update.py` 与 `server/deploy/update.py`（`diff` 无输出）。
- **现象**：两份完全相同，但没有任何测试或 CI 步骤断言它们相等；`server/services/updater.py:139-151` 优先用 `server/deploy/update.py`，`deploy/update.py` 只在直接命令行运行时用。
- **为什么**：改一份忘另一份，就会得到"面板里修好了、命令行没修"的分裂。
- **怎么验**：`diff deploy/update.py server/deploy/update.py && echo SAME`；`grep -rn "server/deploy/update.py" server/tests | head`。

### P2-3 `_sync_deploy` 默认把包内 `deploy/` 覆盖到安装目录（更新器会替换自己）
- **file:line**：`server/deploy/update.py:957-1003`（`_sync_deploy`）、`:948-954`（`WB_SYNC_DEPLOY=0` 才跳过）、`:1693`（调用点）。
- **现象**：更新器把新包的 `deploy/` 覆盖到安装目录，包括 `update.py` 自己、`verify-release.sh`、`install.sh`。docstring 明确这是"刻意的取舍"（"别当成疏漏"），并且只增不删。
- **为什么**：这是设计上接受的代价（让更新器能随包修复），但后果是：一次更新可以改变下一次更新的完整性规则，也会替换 `verify-release.sh` 这类信任锚。于是"更新器的完整性"只能由"上一次的包"保证，无法独立验证。
- **怎么验**：`sed -n '940,1010p' server/deploy/update.py`；`grep -n "WB_SYNC_DEPLOY" server/deploy/update.py`。

### P2-4 容器 `CMD` 绕过 `serve.py`，没有 drain 钩子
- **file:line**：`Dockerfile:166`；`server/serve.py:71-73`（`DrainApp` 包装）；`docker-compose.yml`（无 `stop_grace_period`）。
- **现象**：`docker stop` 发 SIGTERM，uvicorn 直接关闭，`DrainMiddleware` 只在 `serve.py` 的包装里接到 shutdown 信号。
- **为什么**：容器部署的滚动更新会直接截断在途请求，且没有 graceful 窗口配置。
- **怎么验**：`sed -n '160,170p' Dockerfile`；`grep -n "stop_grace_period" docker-compose.yml`（应无）。

### P2-5 `WB_MANAGER_HOST=0.0.0.0` 在单元里，与生产"只绑本机"的契约相反
- **file:line**：`deploy/workbuddy-web.service:9`（`WB_MANAGER_HOST=0.0.0.0`）；对照 `ops/writer-guard/launch_guard.py:229-230`（`--host 127.0.0.1`）与 `deploy/rolling-relay.service:15`（`--host 127.0.0.1`）。
- **现象**：形态 A 的单元把应用绑到所有网卡；形态 B 的 writer-guard 与中继都显式绑 `127.0.0.1`。同一仓库两套绑定口径。
- **为什么**：管理 API 的鉴权是应用层做的，多一层网络暴露就多一层风险；防火墙一旦配置漂移就直达。照 `install.sh` 装出来的主机，7864 对内网完全开放。
- **怎么验**：`grep -n "WB_MANAGER_HOST" deploy/workbuddy-web.service`；`grep -n "127.0.0.1" ops/writer-guard/launch_guard.py deploy/rolling-relay.service`；主机上 `ss -lntp | grep 7864`。

### P2-6 健康检查与发布脚本对端口/单元的假设不一致
- **file:line**：`deploy/healthcheck.sh:58`（7866）、`deploy/rollout-rolling.sh:117-118`（7866/7867）、`deploy/workbuddy-web.service:34`（7864）。
- **现象**：同一仓库里 7864/7866/7867 三个端口都被当作"主实例"。
- **为什么**：脚本在形态 A 上会全部误判（检查 7866 无人监听 → 报"主实例挂了"）。
- **怎么验**：`grep -rn "786[4-7]" deploy | head -40`。

---

## 五、安全升级流程（阶段化 + 回滚边界）

> 目标：把 1.6.27 → 1.6.28 这类"改数据库 + 改协议"的升级做成可回滚的操作。**阶段 1–2 可在测试机做，阶段 3–5 只有生产能做。**

### 阶段 0：升级前（任何环境）
1. `git fetch && git log --oneline -5` 确认目标 commit。
2. `python tools/check_reviewed_hashes.py` 必须全绿（247 个受保护文件）。
3. `python -m pytest server/tests -x -q`（`WB_DATA_DIR` 指向临时目录）。
4. `bash deploy/healthcheck.sh`（形态 B 主机）记录基线。
5. **备份**：`python server/backup_application.py` 成功 + `latest.json` 的 `restore_check == ok`。**这一步现在不是自动的**（P1-6），必须人工执行。

### 阶段 1：构建与完整性（测试机）
6. `python tools/release_policy.py <tag>` 通过。
7. `bash deploy/prepare-managed-stage.sh <release-dir>` 生成 `managed-release.json` + 签名。
8. `python deploy/preflight-gate.py <release-dir>` 必须全绿。
9. `deploy/verify_signed_release.py` **已退役**（`main()` 直接 `return 2`，`:12`），不要拿它当复核手段；独立复核走 `deploy/preflight-gate.py` + `deploy/source_preflight.py`，或手工用 Ed25519 公钥验 `managed-release.sig`。

### 阶段 2：数据库迁移演练（测试机，用生产备份的副本）
10. 用 `recovery-*.tar.gz` 解出的 `manager.db` 副本看迁移计划：`server/migrations.py` **没有 CLI**，用 `plan()` / `status(conn)` 读，执行入口是 `ensure(conn, allow_contract=False)`（`:361`）。
11. 跑 `deploy/migrate_admission_policy.py --database <副本>`（**默认就是预演**，`--apply` 才落库，`:103-104`），确认 `response_budget_protocol` 从 1 到 2 的迁移可逆/不可逆。
12. **回滚边界**：`migrate_admission_policy.py:74-90` 的迁移是**单向**的（把 `admission_policy` 的 `response_budget_protocol` 改成 2）。一旦迁移，旧版本启动会拒绝（`server/sharedadmission.py:112-116` 的 `shared_admission_policy_conflict`、`:137` 的 `shared_admission_policy_changed`）。**所以回滚只能回到"迁移前的数据库备份"，不能回到旧二进制。**

### 阶段 3：生产预检（只有生产能做）
13. `systemctl show workbuddy-web -p ExecStart -p User -p TimeoutStopUSec` 确认形态 B 契约。
14. `ss -lntp | grep -E '786[4-7]'` 确认端口归属。
15. `curl -sf 127.0.0.1:7866/readyz` 与 `curl -sf 127.0.0.1:7867/readyz`。
16. `systemctl show workbuddy-web.socket -p Listen` 确认 socket 单元是否安装（B5：`install.sh` 不装）。

### 阶段 4：切流（只有生产能做）
17. **激活目标版本**：`python deploy/release.py activate <version>`；确认 `releases/current` 已指向该版本。`deploy/rollout-rolling.sh:501` 会校验，未激活会直接退出，不会切流。
18. `bash deploy/rollout-rolling.sh --blue-green <version>`。**先读 P0-2**：当前脚本在 `route_switch old` 后 1 秒就停候选，长流会被截断。若本次升级有长流式请求，建议手工在 `:423` 的 `sleep 1` 后加在途检查（或先 `systemctl stop` 前查 `request_outcomes`）。
19. 观察 `request_outcomes` 的 5xx 率与 `relay` 的 `interrupted` 计数（脚本 `:460` 会统计，但流式截断不一定落 5xx）。
20. `bash deploy/relay-restart-safe.sh` 若需要重启中继。

### 阶段 5：回滚（只有生产能做）
21. **代码回滚**：`python deploy/release.py rollback`（切 `releases/current` 指针）→ `systemctl restart workbuddy-web`。
22. **数据库回滚**：只能恢复到阶段 0 的备份；`migrate_admission_policy.py` 的迁移不可逆（P1-6/阶段 2）。
23. **不要**在已迁移的库上启动旧版本：`sharedadmission.py:112-116`/`:137` 会拒绝，表现为启动失败而非降级。
24. **中继回滚**：**不要**直接 `systemctl restart zhihui-rolling-relay`——nginx 仍把推理流量指向 7867，重启期间新请求会失败（`deploy/relay-restart-safe.sh` 的注释明确记录了这个中断）。用 `relay-restart-safe.sh`：先把新流量切到主服务（直连），再重启中继。
25. **代码回滚后的静态入口**：`release.py rollback` 只切 `releases/current`，**不会回滚控制台产物**。公网 `/login` 与 `/_next` 由 nginx 从控制台产物和独立静态池提供；`location /` 指向首页目录 `/var/www/zhihui-home`，首页不属于控制台（`deploy/nginx/README.md:57-67` 的白名单；`publish-console.sh` 的 `PAGES` 也不含 `/`）。回滚后必须用旧 `web/out` 重新发布：`sudo deploy/publish-console.sh --web <old-webout.tar.gz> --version <old>`。脚本逐文件校验 `_next/static` 的 size/sha256（`:147-160`），并逐页检查 `/_next/static` 链接与资源 200（`:174-224`）。静态池默认 `/var/cache/zhihui-static-performance-20261104`（`:12`），与 `docs/OPS-RUNBOOK.md:86` 的 nginx `location ^~ /_next/static/` 一致；`deploy/nginx/README.md:77-79` 写的 `/var/www/zhihui-next-pool/` 是旧口径，发布前以生产站点配置的 `root` 为准，不一致就加 `--static-pool`。验收看公网 `https://zhihui.kaogong.art/login` 的 buildId/资源哈希是否与旧版本一致；不要只看 `127.0.0.1:7866`——那是网关自带的 `STATIC_DIR`（`server/main.py:1010,1018`），新旧构建都返回 200。

### 只有生产才能验的清单
- socket activation 是否真的生效（`/proc/<pid>/fd` 里有 `socket:[...]`）。
- 蓝绿切流是否真的零中断（需要真实长流 + 真实 nginx）。
- 备份 cron 是否真的在跑（`crontab -l` + `latest.json` 的 mtime）。
- 磁盘水位与归档增长速度（P1-4）。
- 异地备份能否解密（P1-5，需要私钥）。

---

## 六、复跑命令（只读）

```bash
# 端口/单元契约
grep -rn "786[4-7]" deploy server/relay_app.py | head -40
grep -n "User=\|ExecStart\|WB_MANAGER_HOST" deploy/workbuddy-web.service
grep -c socket deploy/install.sh          # 0

# 更新器
diff deploy/update.py server/deploy/update.py && echo SAME
grep -n "MANAGER_REPO\|verify_download\|require-hashes" server/deploy/update.py

# 备份
sed -n '438,460p' server/backup_application.py
grep -rn "workbuddy-daily" . | grep -v '^./docs/' | grep -v test
grep -rn "backup-push" . | head   # 只在文档/告警文案里，无实现

# 中继/排空
sed -n '60,80p;155,200p' server/rollingrelay.py
sed -n '415,430p' deploy/rollout-rolling.sh

# 告警
sed -n '66,80p;100,115p' server/notify.py
grep -n "return 0" ops/wb-gateway-alert.py
```

---

## 七、对子代理结论的校正（复核时发现的说法与仓库实际不符）

| 子代理/早期稿说法 | 仓库实际 | 证据 |
|---|---|---|
| `MANAGER_REPO` 是 `ithtelworkbuddy/zhihui-gateway` | `ithtelab/workbuddy-manager` | `server/deploy/update.py:75` |
| 完整性校验函数叫 `verify_archive` | 没有这个函数；是 `verify_download` | `server/deploy/update.py:550` |
| 更新器有 `rollback`，`swap_dir` 保留旧代码 | 没有任何 `rollback` 实现（`grep -cn rollback` = 0）；`swap_dir` 成功后 `rmtree` 旧目录 | `server/deploy/update.py:1568` |
| provenance 有 `database_restore_verified` 字段 | 没有；实际是 `candidate_only` / `database_rollback` / `production_deployed` | `server/release-provenance.json:4,5,9` |
| `verify_signed_release.py` 返回 1 | 返回 2 | `deploy/verify_signed_release.py:12` |
| 备份用 `integrity_check` | 用 `quick_check` | `server/backup_application.py:432,507` |
| 备份硬编码清单在 438–444 | 在 `:441`（`state=[...]` 一行） | `server/backup_application.py:441` |
| `install_requirements` 在 1180–1240 | 在 `:1340` | `server/deploy/update.py:1340` |
| `restore_drill.py` 有 `--restore` | 只有 `--restore-sidecar`；仓库没有自动恢复流程 | `deploy/restore_drill.py:437-499`、`docs/DISASTER-RECOVERY.md:146-175` |
| `db.py` 用 `flock` 保证单写者 | 只有进程内 `RLock`；跨进程 `flock` 在 `budgetowner.py`/`steward_runtime.py`，不在 `db.py` | `server/db.py:23`、`server/db.py:614` |
| `launch_guard` 端口在 `:217` | 在 `:229-230`（`--host 127.0.0.1`、`7865 if helper else 7866`） | `ops/writer-guard/launch_guard.py:229-230` |
| `sharedadmission` 冲突在 `:113,131` | 在 `:112-116`（conflict）与 `:137`（changed） | `server/sharedadmission.py:112-137` |
| `LOCAL-DIVERGENCE.md:15` 记 `verify-release.sh` 与发布包「0 行差异」 | 对，但两边都只做 sha256；`LOCAL-DIVERGENCE.md:27` 说的验签在 `update.py` 的 `verify_release_signature`，而仓库这份 `update.py` 已无该函数 | `deploy/LOCAL-DIVERGENCE.md:15,27`、`server/tests/test_release_integrity.py:121-123` |
| socket `Backlog=512` | `Backlog=4096` | `deploy/systemd/workbuddy-web.socket:28` |
| `install.sh` 的 `daemon-reload` 在 `:301` | 在 `:300` | `deploy/install.sh:300` |

**结论仍然成立的部分**：P0-1（`is_draining_response` 的 `:69` docstring 与代码不符）、P0-2（`:422-424` 的 `sleep 1` 后 `systemctl stop`）、P1-3/P1-4/P1-5（备份子系统）、P1-6（无备份耦合）、P1-7（无启动期检查）、P2-2（两份 update.py 字节相同）、P2-4（容器无 `stop_grace_period`）。
