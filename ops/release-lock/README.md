# K28 发布锁与调度/自愈互斥（未部署）

## 接管修正：整个命令生命周期持锁

旧版 `acquire` 是独立进程，退出即释放 flock，不能保护后续命令。此接口已拒绝使用。
当前入口把完整发布控制器作为子进程运行，子进程退出前始终持有内核锁：

```bash
bash ops/release-lock/zh-release-lock.sh run R0-20261024 manager,web -- bash release-controller.sh
```

只有上述控制器及继承锁描述符的后代可以调用 `stage drain/switch/verify/done`。
Python 控制器启动阶段命令时须在 `subprocess` 中传递 `pass_fds=(int(os.environ['ZH_RELEASE_LOCK_FD']),)`。
阶段严格按状态图前进；任意进程不能只凭存在的状态文件修改阶段。
锁路径保持同一个 inode，禁止删除/替换锁文件；状态文件用原子替换单独更新，权限 0600。
父进程转发 SIGTERM/SIGINT 给该次发布的子进程组，退出码原样记录；返回 0 不会自动标为 done。
后台后代若仍持有描述符，实际锁仍有效，即便状态显示命令已退出。控制器应等待所有发布子任务。

`status` 的 `active` 由实际 flock 探测得出，不按文件存在、PID 存在或时间戳推断。
竞争发布返回 75；无有效租约的阶段变更、旧 acquire/release、非法路径或目标返回 64。
状态记录保留作为证据。机器重启后应先核验服务、软链、账本和数据再发起新发布。

## K28 双向发布互斥：DB 侧信号（`settings['publish.state']`）

内核 flock 是单机权威；`settings['publish.state']` 是给调度方（以及同一 DB 文件的
观察者）看的第二信号。两边都在**同一个 SQLite 写事务**里完成「检查对方 + 写自己」，
所以不存在「双方都检查通过」的窗口：

```
发布方: flock ──► BEGIN IMMEDIATE ──► begin_publish()：查活跃 task lease
        │                            （有 → 等待 ≤ ZH_RELEASE_WAIT_LEASE_SECONDS，超时 exit 76）
        │                            （无 → 写 publish.state，带 lease_until）──► COMMIT
        └─ 执行发布 ──► stage 时续期 ──► 结束清除 publish.state（owner 匹配才清）+ 释放 flock
调度方: BEGIN IMMEDIATE ──► 查 publish.state（未过期）──► 探测 flock ──► 写 task lease ──► COMMIT
```

### 部署要求（必读）

**发布 wrapper 必须设置 `ZH_MANAGER_DB`（或 `WB_DB`）才能启用 DB 侧发布信号。**
未设置时 `release_lock.py` **不会猜部署路径**（猜错会写到别的库或污染隔离测试），
此时只有内核 flock 生效：同机互斥仍然成立，但跨副本观察者与「活跃任务租约」检查
看不到发布状态。

```bash
# 推荐：在发布入口显式导出（zh-release-lock.sh 是 exec 薄壳，环境变量天然透传）
export ZH_MANAGER_DB=/opt/workbuddy-manager/data/manager.db
bash ops/release-lock/zh-release-lock.sh run R1-20261025 manager,web -- bash release-controller.sh
```

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `ZH_MANAGER_DB` / `WB_DB` | 未设置（只用 flock） | Manager 主库；显式设置才写/查 `publish.state` |
| `ZH_RELEASE_WAIT_LEASE_SECONDS` | `60` | 有活跃每日任务租约时最多等待多久；`0` = 立即失败 |
| `ZH_PUBLISH_LEASE_TTL` | `900` | `publish.state` 有效期（发布方崩溃的残留会自然过期） |

退出码：`75` = 锁被别的发布/维护命令持有；`76` = 有活跃的每日任务租约（等待超时后
明确失败）。两种都不允许带着冲突继续发布。

调度方（`server/services/dailyplan.py`）在 `tasklease.claim` 的同一写事务内检查
`publish.state` 与 flock，任一为真就不领取任务租约；发布结束后状态清除，下一轮
巡检可以正常 claim。实现与测试见 `manager/server/tasklease.py`、
`manager/server/tests/test_task_leases.py`、`evidence/r1-k28/`。

本地 Linux 跨进程测试入口：`python3 tests/linux_lab/test_release_lock.py`，覆盖互斥、
阶段权限/顺序、异常退出、SIGTERM、继承锁、锁 inode 与软链拒绝。
K28 的调度领导权与发布握手另有入口：`python3 tests/linux_lab/k28_scheduler_owner.py
--db <临时 manager.db> --json <out.json>`（29 项；Linux 上含真实 flock 与
`release_lock.py` 集成，Windows 上相关两项按平台差异 skip）。

### 关于 k28 脚本第 3 项断言的修正

旧脚本把「两个『实例』对同一份 plan 调用 `is_due()` 都返回 True」记成 FAIL，并据此
判定「没有 leader 选举」。这是**断言错位**：`is_due()` 本来就是纯函数（只看「这一
小时跑过没有」），跨进程唯一执行权不属于它的职责。K28 之后唯一执行权由
`tasklease.claim()` 保证（脚本第 8 项的「第二个 owner 被拒」与第 12 项的「真实双进程
并发只有一个取得租约」）。原项改为「is_due 为纯函数（跨进程去重交给 claim）」并
PASS —— 这是修正错位断言，**不是删除断言**。

**问题**：Manager/QTG 自带调度器（签到、领奖、续期、冷却、备份、pgsync），主机上还有
`healthcheck.timer`（5 分钟）、`wb-gateway-alert.timer`（1 分钟）、`trae-limits.timer`。
发布期间如果自愈脚本重启引擎、或调度器在灰度进程里跑第二份，就会造成重复执行/预算被抢/流被截断。

## 发布锁的语义

一个**跨进程互斥**的锁 + 一份状态记录。锁文件不是"杀进程许可"。

```
/run/zhihui-release.lock          # flock 互斥
/run/zhihui-release.state.json    # 状态（人可读，不含密钥）
```

状态字段：`release_id`、`operator`、`stage`、`status`、`history`、`started_at`、
`targets`、`owner_pid`、`owner_start_ticks`、`lease_sha256`，结束时附 `exit_code/finished_at`。

规则：

1. 部署脚本、在线更新、自愈重启、人工运维入口**先检查锁**；拿不到锁就只读观察，不动手；
2. 自愈在实际锁被持有期间只记录、不重启；发布结束后核验再恢复；
3. 锁过期**不**代表可以杀旧进程或接管预算（owner/fence 仍以实际状态为准）；
4. 锁不携带密钥；状态文件只写版本、阶段、目标与健康结果；
5. 发布期间 `api/readyz` 返回 503 属于"预期"，healthcheck 不得把它判成故障重启。

## 分阶段操作

| 阶段 | 动作 | 自愈/调度 |
|---|---|---|
| prepare | 拿锁、记录基线、安装制品（不切） | 调度正常，自愈观察 |
| drain | Manager 进入 draining；QTG 停止后台任务（环境变量） | **禁止重启** |
| switch | 切软链/重启目标进程 | **禁止重启其他组件** |
| verify | 内容断言 + 业务检查 + 账本对账 | 禁止重启 |
| rollback | 按预检结果回退 | 禁止重启，保留证据 |
| done | 完成记录，控制器退出后释放持锁描述符，再恢复自愈 | — |

## 本地已有能力

- Manager：`server/drain.py`（在途计数、draining 状态、`/api/readyz` 503）；
- QTG：`lifespan` 里启动的调度器（checkin_scheduler 等）——发布模式开关待实现（下一轮）；
- 配置备份：`/opt/gateway-ops/scripts/backup-gateway-config.sh`（每小时 03:17）；
- 自愈：`healthcheck.timer` 依赖主机脚本，需在发布窗口前暂停：
  `systemctl stop healthcheck.timer`（需授权）。

## 风险

- 单机单实例：锁只能防"同机并发操作"，不防另一台机器（当前没有第二台）；
- 锁文件在 `/run`（tmpfs），重启后不会保留锁；发布可能中途被打断，必须先核验，不能推断上一轮成功。
- 任何"为了赶时间跳过锁"的操作，必须在发布记录里显式记为例外并说明理由。
