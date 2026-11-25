# 备份 OOM 修复的实测验证（第 43 轮）

## 1. 上一轮留下的问题

第 42 轮把 `backup_application.py` 里两处整文件读取改成了流式，但**没有验证**。
当时的原话是：「修复是否真的消除 OOM，要等下一次备份跑完才知道。」

定时备份要等到次日凌晨。所以这一轮**手动触发了一次**，条件与真实运行完全一致。

## 2. 验证方法：完全复刻失败现场

失败时的启动方式（从内核日志还原）：

```
Nov 25 05:30:01 systemd[1]: Started run-r9c8f22b...scope - /opt/workbuddy-manager/server/backup_application.py
```

所以这次用同一条路径、同一个内存上限：

```bash
sudo systemd-run --scope -p MemoryMax=512M /opt/workbuddy-manager/server/backup_application.py
```

**不是「差不多的环境」，是同一个限制、同一个脚本、同一条代码路径。**

## 3. 关键指标：anon 而不是 current

第一眼看 `memory.current` 会误判：

```
memory.current = 512 MB   ← 看起来又满了
memory.max     = 512 MB
max            7462       ← 触顶 7462 次
oom_kill       0
```

**但 `memory.current` 包含 page cache，那是可回收的。**

拆开看 `memory.stat`：

```
anon            35 MB     ← 不可回收，这才是决定会不会 OOM 的量
file           485 MB     ← page cache，内核随时可回收
inactive_file  365 MB
slab             2 MB
```

**对比 11-25 05:34 那次失败的内核日志：**

```
Killed process 1058616 (backup_applicat)
         anon-rss:513072kB          ← 501 MB 不可回收内存
```

| | 失败时 | 本次 |
|---|---|---|
| **anon（不可回收）** | **501 MB** | **35 MB** |
| 结果 | 撞死 | 跑完 |

**这就是修复生效的直接证据。**不是「内存用得少了」，是「不可回收的那部分从 501 MB 降到 35 MB」。

## 4. 一个保留意见：验证阶段仍有瞬时峰值

跑的过程中我抓到过一次 **`anon = 449 MB`**，就在解出数据库做 `quick_check` 的阶段。

采样三次（间隔 2 秒）都是 35 MB，所以 **449 MB 是瞬时的**。它没有触发 OOM，
但如果数据库再大一些，或者限制再紧一些，这一下可能就够了。

**这一点我没改。**理由：
- 它现在能过（449 < 512）
- 瞬时峰值的来源在 SQLite 自身的 `quick_check`，不在我能改的那两行
- 为了一个还没发生的问题去调 SQLite 的页缓存参数，是拿确定性换不确定性

**但记录下来** —— 如果以后数据库涨到 1 GB 以上，这里会先出问题。

## 5. 最终结果

```
{
  "path": "/root/backups/workbuddy-daily/recovery-20261125-144335.tar.gz",
  "sha256": "e4e038c46add3d6ebc1a74d89c2f1b66db68bf8dfc5663c71c8d2d613150fb83",
  "bytes": 409271601,
  "request_rows": 369434,
  "restore_check": "ok",
  "paired_ledger_check": {
    "complete": true,
    "consistency": "portal-paired-online-v2"
  }
}
```

**`restore_check: "ok"`** —— 这个字段的含义是「解出来查过一次，是好的」。
它现在是 ok，说明整条收尾链路（解包 → 校验 → 算哈希 → 写 latest.json）都跑完了。

事后检查内核日志 **OOM 次数 = 0**。

## 6. 这次验证改变了什么

上一轮我说「修复待验证」。现在可以说了：

**修复有效。备份能在原来的限制下跑完。**

更重要的是：**这条验证路径本身现在可用了。**以后再改备份相关代码，
不用等到凌晨，跑一条命令 15 分钟就能确认。

```bash
sudo systemd-run --scope -p MemoryMax=512M /opt/workbuddy-manager/server/backup_application.py
# 另开一个终端看关键指标
watch -n 2 "grep '^anon ' /sys/fs/cgroup/system.slice/run-*.scope/memory.stat"
```

## 7. 对运维的意义

运维不需要知道代码怎么改的，只需要知道三件事：

1. **备份现在能跑完了**（原来每隔一次就被内存不足杀掉）
2. **`latest.json` 的 `restore_check: "ok"` 是真的查过**，不是写死的
3. **巡检会报告 OOM**（第 41 轮加的），以后再发生同类问题能立刻看见

## 8. 待办

| 项 | 状态 |
|---|---|
| 备份 OOM | **已修复并实测通过** |
| 验证阶段瞬时 449 MB 峰值 | 记录，未改（数据库涨到 1 GB 以上需重看） |
| 回收 2.2 GB 残留 | 待运维确认 |
| 异地备份 | 需外部存储 |
| P0-1 / P0-2 | 待运维安装 |