# v1.5.79：巡检补上两个盲区（第 41 轮）

## 1. 找盲区的思路

前几轮验证的都是「出事时」的机制（回滚、备份、发布闸门）。这轮换方向：**平时有没有看不见的状态。**

巡检有 13 个部分，看起来齐全。但齐全不等于没有盲区 —— 我列了几个经典盲区逐项查：
**inode、内存、上游容器、数据库写阻塞**。

## 2. 逐项排查结果

| 项 | 当前值 | 结论 |
|---|---|---|
| inode | **26%**（986,184 / 3,932,160） | 安全 |
| 内存 | 253 MB / 2 GB 限制，峰值 262 MB | 安全 |
| swap | 562 / 1987 MB（28%） | 曾有过压力 |
| 上游容器 | `zhihui-workbuddy` **Up 2 days (healthy)** | 健康 |
| 数据库 | `journal_mode=wal`，WAL 已 checkpoint（0 字节） | 正常 |

inode 那一项我多查了一层：`releases/` 下 31 个版本共 58,497 个文件，
单版本 1,839 个（含 `web/out`）。按每年约 30 次部署算，年增约 17%。
**不是紧急风险，但 inode 有一个性质值得记：它不会先变慢，是直接 ENOSPC。**

## 3. 内存那一项挖出了真东西

`swap` 用了 28% 说明曾经有压力。查内核日志：

```
7 天内 OOM：4 次
```

全部指向同一个进程：

```
Nov 25 05:34:56 kernel: backup_applicat invoked oom-killer
Nov 25 05:34:56 kernel: Memory cgroup out of memory: Killed process 1058616 (backup_applicat)
         total-vm:801496kB  anon-rss:513072kB
Nov 25 05:34:56 kernel: oom_memcg=/system.slice/run-r9c8f22b...scope
```

**这是我们上一轮看过的那个 05:31 备份。**

它在 05:34:56 被 OOM 杀掉，用的内存是 **513 MB**，撞上的是 **`MemoryMax=512MB`** 的 cgroup 限制。

**所以 `snapshot-rsa5dz9b/` 那个 2.2 GB 残留目录的成因找到了 —— 不是清理逻辑漏了，是备份进程中途被杀，目录没来得及收尾。**

## 4. 但备份告警是合理的

查到 OOM 后我第一反应是「备份失败没有任何告警，这是缺陷」。继续查发现不是：

`server/operational.py` 的 `evaluate_backup_health` 已经有四个状态：

```
backup_status_unavailable   报告缺失/不可读/无效
backup_stale                报告超过 26 小时
backup_publish              异地发布未验证
backup_size                 密文异常大
```

05:30 那次失败后，04:18 的成功报告仍在 26 小时内，所以全局判为健康 —— **这是对的**。
备份只要有一份新鲜可用就够了，不需要每次成功。

**但这留下一个真实的观测缺口**：如果连续两次失败呢？那时 `backup_stale` 会报，
可运维只能看到「备份旧了」，看不到「为什么旧」。而答案（进程因内存不足被杀）只在 `journalctl -k` 里。

## 5. 改法：补两个观测，不是两个告警

```bash
# inode
inodes=$(df --output=ipcent / | tail -1 | tr -dc '0-9')
if [ "$inodes" -lt 80 ]; then
    pass "inode 已用 ${inodes}%"
else
    note "inode 已用 ${inodes}% —— 磁盘空间可能还够，但建不了新文件；先清理旧版本"
fi

# OOM
oom=$(journalctl --since '-24 hours' --no-pager | grep -ci 'out of memory.*killed process')
if [ "${oom:-0}" -gt 0 ]; then
    note "近 24 小时 ${oom} 次内存不足杀进程（journalctl -k 可见是谁）"
else
    pass '近 24 小时无内存不足杀进程'
fi
```

**为什么 inode 用 80% 而不是更低**：实测 26%，年增 17%，三四年才到 80%。报得太早会变成第 34 轮那种「天天亮着的假警告」。

**为什么 OOM 用 `note` 而不是 `broken`**：OOM 可能是别的进程（这次就是备份而非网关）。
它值得看见，但不构成「整体不可用」。

## 6. 部署验证

| 项 | 改前 | 改后 |
|---|---|---|
| 巡检 | ok 23 / 警告 2 / 问题 0 | **ok 24 / 警告 3 / 问题 0** |
| 新增项 | — | `ok inode 已用 26%` |
| 新增项 | — | `warn 近 24 小时 1 次内存不足杀进程` |
| **重启窗口** | — | **48 个请求，0 个 5xx** |

**警告数从 2 涨到 3，这不是退步。**那个 OOM 事件一直在发生，只是以前看不见。
**看得见的问题比看不见的问题好。**

## 7. 一个改不了的地方

`MemoryMax=512M` 这个限制，我在仓库里搜遍了 `server/`、`deploy/`、`ops/`，
只找到 `rolling-relay.service` 和 `qoder2-engine-rootless.conf` 里有 512M，
都不是备份。

限制来自 `systemd-run --scope -p MemoryMax=512M`，而**调用方不在本仓库**。
所以「512 MB 对备份来说是否太紧」这个问题，我改不了 —— 只能记录。

## 8. 结论

- **inode 不是问题**，但加了观测
- **内存不是问题**，网关只用 253 MB / 2 GB
- **OOM 是问题，但不在网关身上** —— 是备份进程撞限制
- **备份告警设计是对的**，缺的只是「为什么失败」这条线索，现已补上

## 9. 待办

| 项 | 卡在哪 |
|---|---|
| 备份的 512MB 限制是否太紧 | 调用方不在本仓库 |
| 异地备份 | 需外部存储与凭据 |
| 回收 2.2 GB 残留 | 建议运维确认后执行（成因已查明） |
| P0-1 守卫原因码 | 待运维安装 |
| P0-2 socket activation | 待运维安装 unit |
| GitHub Release v1.5.79 | CI 构建中，下一轮发布 |