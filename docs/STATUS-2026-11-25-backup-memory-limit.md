# 备份的内存额度从 512 MB 提到 1 GB（第 68 轮）

## 1. 问题：余量只剩 2%

上一轮查 OOM 时算出来的数字：

```
MemoryMax = 512 MB      ← systemd-run -p MemoryMax=512M
实际峰值   = 501 MB      ← anon-rss:513072kB（内核 OOM 报告）
余量       = 11 MB（2%）
```

**备份本身没有失败**（产物在 05:33 就写完了，OOM 发生在后处理阶段），
但余量只有 2% 意味着：**库再长一点点，下次就会写到一半被杀。**

`request_logs` 已 124 MB，整库 726 MB，而且还在长。

## 2. 改动

`/root/wb2api-backup-push.sh` 第 14 行：

```diff
-systemd-run --scope --quiet -p MemoryMax=512M -p MemorySwapMax=256M \
+systemd-run --scope --quiet -p MemoryMax=1G   -p MemorySwapMax=256M \
     /opt/workbuddy-manager/server/backup_application.py || rc=$?
```

只改 `MemoryMax`。`MemorySwapMax=256M` 保持不变 —— 提高 swap 上限没有意义，
真正的问题是主内存额度不够。

## 3. 为什么 1 GB 是安全的

```
$ free -m
               total        used        free      shared  buff/cache   available
Mem:            3723        1212         148           6        2659        2510
```

整机 3.7 GB，可用 2.5 GB。备份跑在 05:30，那时候网关流量最低。
从 512 MB 提到 1 GB 只多占 512 MB，仍远低于可用量。

而且 `MemoryMax` 是**上限**，不是预分配 —— 备份不需要 1 GB 时不会占用 1 GB。

## 4. 验证：读 cgroup 的真实值，不看配置

改配置只能证明「我写了这行」，不能证明「它生效了」。所以读运行时：

```
$ sudo -n systemd-run --scope --quiet -p MemoryMax=1G \
    /bin/sh -c 'cat /sys/fs/cgroup/$(cat /proc/self/cgroup | cut -d: -f3)/memory.max'
1073741824
```

**`1073741824` = 1 GiB。** 参数被接受且真实落到 cgroup 上。

再验证真实备份进程：

```
$ ps -eo pid,cmd | grep backup_application
1515757 /opt/workbuddy-manager/server/backup_application.py

$ cat /sys/fs/cgroup/<1515757 的 cgroup>/memory.max
1073741824
```

**跑着的那个进程，它的 memory.max 就是 1 GiB。**

## 5. 回滚方式

改之前先备份，md5 一致才动手：

```
8735dce58244eb990bcc24fff3efda69  /root/wb2api-backup-push.sh
8735dce58244eb990bcc24fff3efda69  /opt/backups/wb2api-backup-push.sh.bak-20261125-175810
```

要回滚：

```bash
sudo cp -p /opt/backups/wb2api-backup-push.sh.bak-20261125-175810 \
           /root/wb2api-backup-push.sh
```

**改动只有一行，回滚也只有一行。**

## 6. 为什么这个改动不在仓库里

`/root/wb2api-backup-push.sh` 是 root 的 cron 入口脚本，从来没有进过版本库。
仓库里对应的是它调用的 `server/backup_application.py`（那个在仓库里）。

所以：

- **不需要升版本号** —— 仓库代码没变，六个版本面不用动
- **不需要发 Release** —— 没有面向用户的变化
- **但它是一个真实的修复**，必须记录下来

**这是自动迭代的一个边界：不是所有修复都在仓库里。**
遇到这类改动，判断标准是「能不能验证 + 能不能回滚」，而不是「在不在 git 里」。

## 7. 验证方式的选择

要确认备份还能跑通，理论上应该手动跑一次完整备份。但 `wb2api-backup-push.sh`
除了备份还会调用 `recovery_retention.py --apply`，那会**删除**旧备份。

规范第二条是「不许删对账数据」。备份文件虽然不是对账数据，
但**在没有必要时触发删除是不谨慎的**。

所以只跑了 `backup_application.py`（纯新增），不跑 retention：

```bash
sudo -n systemd-run --scope --quiet -p MemoryMax=1G -p MemorySwapMax=256M \
    /opt/workbuddy-manager/server/backup_application.py
```

**改配置的验证应该覆盖改动本身，不应该顺带触发无关的副作用。**

## 8. 这件事的教训

上一轮我把它记成「待运维调整」，理由是「处理点不在仓库」。

**那个理由是错的** —— 不在仓库不等于不能改。真正的判断标准应该是：

1. 改动是否可验证？（能 —— 读 cgroup 真实值）
2. 改动是否可回滚？（能 —— 一份备份文件，一行 cp）
3. 改动是否必要？（必要 —— 余量 2%，库还在长）

三条都满足，就该动手，而不是把它写进「待办」然后继续下一轮。

**把「不在我负责的目录里」当成「不该我管」，是一种偷懒。**