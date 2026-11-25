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
## 9. 端到端验证：真的跑了一次备份

配置改了、cgroup 读到了，但这还不能证明「备份流程在新额度下能走完」。
所以完整跑了一次（只跑 `backup_application.py`，不跑 retention）。

**结果**：

```json
{"path": "/root/backups/workbuddy-daily/recovery-20261125-175858.tar.gz",
 "sha256": "cfab91d26d1687665c9d677777e86e38337ae5f069f47167f30b811a81ce149a"}
```

```json
{"bytes": 411712838, "created_at": 1795600986, "offsite": false,
 "restore_check": "ok", "schema": 1}
```

**`restore_check: "ok"`** —— 这正是上次被 OOM 杀掉的那一步，这次走完了。

### 过程观测

| 阶段 | 观测 |
|---|---|
| 开始 | 建 `snapshot-2fczq291/`，拷 `manager.db` |
| 1 分 35 秒 | `snapshot` 清理，开始打 tar |
| 2 分 48 秒 | `recovery-...tar.gz.tmp` 增长中 |
| 3 分 58 秒 | `.tmp` 已改名为正式文件（393 MB），RSS 仅 64 MB |
| 4 分 55 秒 | 完成，`restore_check: ok`，`snapshot` 与 `.tmp` 均已清理 |

总用时约 5 分钟，IO 读 2.45 GB / 写 4.24 GB。

### 一个意外收获：残留目录是怎么产生的

备份的步骤是「建 `snapshot-xxxx/` → 拷 DB → 打包 → 清理」。第 2 步打开的文件是：

```
/root/backups/workbuddy-daily/snapshot-2fczq291/manager.db
```

**如果进程在第 3、4 步之间被杀，`snapshot-xxxx/` 就留在盘上了。** 这正好解释了
11-20 之前那 3 次 `FileNotFoundError`（见 `OPS-PITFALLS.md` 第二十条）——
不是文件被删了，是**上一次被杀时留下的临时目录**在下次遍历时找不到预期成员。

所以「OOM」和「残留目录」是同一个原因的两个后果：
**OOM 杀掉进程 → 临时目录留下 → 下次备份遇到残留**。

把内存额度提上去，这两个问题一起消失。

### 验证后的干净度

```
$ ls -d /root/backups/workbuddy-daily/snapshot-*     → 无输出
$ find /root/backups -name '*.tmp'                   → 无输出
$ df -h /                                            → avail=17G pct=71%
$ curl .../api/release                               → v1.5.91 正常
$ healthcheck.sh                                     → ok 26 / 警告 3 / 问题 0
```

多出一次备份后磁盘从 70% 到 71%，生产侧零影响。

## 10. 一句话

**改配置只证明「我写了这行」；读 cgroup 证明「它生效了」；
真跑一次证明「流程能走完」。三件事缺一不可。**