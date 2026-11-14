# 数据增长与磁盘预算

测量时间：2026-11-15（版本 `v1.2.70`）。所有数字来自实测，不是估算。

---

## 一、数据库：614 MB，谁占的

| 表 | 占用 | 备注 |
| --- | --- | --- |
| `request_logs` | **105.7 MB** | 最大单表 |
| `forwarding_events` | **89.4 MB** + 索引 21.1 MB | **永不删除** |
| `request_outcomes` | 53.8 MB + 索引 19.9 MB | 90 天滚动 |
| `attempt_events` | 41.2 MB + 索引 14.1 MB | 已停止写入（2026-11-03） |
| `usage_events` | 36.2 MB | 账本事件 |
| `provider_attempt_events_v2` | 28.4 MB | |
| `budget_reservations` | 27.2 MB | |
| `request_performance` | 19.3 MB | |
| `attempt_billing_facts` | 16.2 MB | |

**合计 613.6 MB。**

---

## 二、增长速率

| 表 | 行数 | 跨度 | 每天新增 | 保留策略 |
| --- | --- | --- | --- | --- |
| `forwarding_events` | 383,228 | 40 天 | **9,581 行** | **永不删除** |
| `request_logs` | 332,869 | 62 天 | **5,457 行** | 90 天滚动删除 |

换算成体积（实测每行字节数）：

| 表 | 每行 | 年增 |
| --- | --- | --- |
| `forwarding_events` | 约 244 B | **约 855 MB** |
| `request_logs` | 约 333 B | **趋于稳态**（90 天窗口封顶，约 500 MB） |

**所以数据库的长期增长几乎全部来自 `forwarding_events`** —— 它是唯一一张既
「永不删除」又持续高速写入的表。

---

## 三、真正的问题是备份放大

这是这次审计最该记住的结论：

| 项 | 大小 |
| --- | --- |
| 数据库 | **614 MB** |
| `/root/backups` 总计 | **6.9 GB** |
| **放大倍数** | **约 11 倍** |

放大的来源是**多副本同时存在**：

| 副本 | 份数 | 占用 |
| --- | --- | --- |
| `workbuddy-daily`（每日归档） | 1 | 290 MB |
| `workbuddy-mirror`（本地第二副本） | **7** | **1.8 GB** |
| `zhihui-v2/snapshots`（加密快照） | 2 | 1.6 GB |
| `zhihui-v2/runtime` | 2 | 885 MB |
| `zhihui-v2/transport.git` | — | 470 MB |
| `workbuddy-offsite.git` | — | 509 MB |
| `portal-unfreeze-20261108-111025` | — | 508 MB |

**关键推论**：数据库每增 100 MB，备份侧大约增 1.1 GB。

所以年增不是 855 MB，而是**约 9 GB**。

---

## 四、磁盘还能撑多久

| 项 | 值 |
| --- | --- |
| 磁盘容量 | 59 GB |
| 已用 | 47 GB（83%） |
| **可用** | **9.7 GB** |
| 年增（含备份放大） | 约 9 GB |
| **预计写满** | **约 1 年** |

**这不是紧急故障，但也不是「还有很久」。**

当前的磁盘告警阈值是「可用低于 2 GiB」，按上面的速度，触发时会离写满只有
几周 —— **那时候再处理，可选项会少很多**。

---

## 五、可选的处置方向

**都是策略选择，不是缺陷修复。** 按影响从大到小：

| 方向 | 效果 | 代价 |
| --- | --- | --- |
| `workbuddy-mirror` 从留 7 份改为留 3 份 | 省约 1.2 GB，且把放大系数从 7 降到 3 | 本地副本回溯窗口变短 |
| 清理已停用的 `workbuddy-offsite.git`（509 MB） | 省 509 MB | 异地备份已停用，仓库无用途 |
| 清理 `portal-unfreeze-20261108`（508 MB） | 省 508 MB | 一次性现场备份，已过时效 |
| 给 `forwarding_events` 定保留期（如 180 天） | **治本**：年增从 855 MB 降到接近 0 | 失去更早的转发留痕 |
| 加密快照从留 2 份改为 1 份 | 省约 760 MB | 恢复点变少 |

**唯一治本的是最后倒数第二条** —— 其它都只是把放大系数压小，`forwarding_events`
仍会线性增长。

---

## 六、不建议做的事

| 做法 | 为什么不行 |
| --- | --- |
| 直接删 `forwarding_events` 的行 | 有删除触发器（`forwarding_events_no_delete`），删即缺陷 |
| 手工 `rm` 备份目录 | 保留任务会按水位重建，且可能删掉唯一可恢复的那份 |
| 关掉磁盘告警 | 它是唯一会主动提醒的机制 |

要动 `forwarding_events`，走 `archival.py` 的导出流程（导 JSONL + manifest +
校验，再登记 `archive_runs`），不要直接操作表。

---

## 七、怎么复核本报告

```bash
DB=/opt/workbuddy-manager/data/manager.db

# 表占用
sqlite3 "$DB" "SELECT name, printf('%.1f MB', sum(pgsize)/1048576.0)
               FROM dbstat GROUP BY name ORDER BY sum(pgsize) DESC LIMIT 12;"

# 增长速率
sqlite3 "$DB" "SELECT count(*), datetime(min(ts),'unixepoch','+8 hours'),
               round(1.0*count(*)/max(1,(max(ts)-min(ts))/86400))
               FROM forwarding_events;"

# 备份占用
du -sh /root/backups/*

# 磁盘
df -h /
```

---

## 八、下次复核的时点

**建议每月一次**，重点看两件事：

1. `forwarding_events` 的行数增长是否仍是每天约 9,600；
2. `df -h /` 的可用量是否按预期下降。

如果可用量掉到 **5 GB 以下**，就该做决定了 —— 不要等到 2 GiB 的告警线。
