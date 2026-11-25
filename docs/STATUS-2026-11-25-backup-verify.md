# 备份能否恢复：实测结论（第 39 轮）

## 1. 为什么查这个

上一轮做回滚演练，发现兜底手段从未被验证过，演练当场暴露了缺陷。

顺着这条线，还有一项风险更高的兜底手段从未验证：**备份**。
回滚失败最多是多写一版代码；**备份失败意味着数据真的没了**。

规范 §1.2「不许删对账数据」是原则，备份是把原则变成可行性的那一步。

## 2. 机制比预期完善

`zhihui-recovery.service` 每天运行，产出加密快照；`zhihui-backup-retention.service`
再清理旧快照，只留最新一份。最近一次：

```
04:18:23 启动 → 04:32:34 结束（14 分钟）
status=0
{"local_verified": true,
 "runtime_bytes": 430345627, "snapshot_bytes": 743803102,
 "checks": {"runtime": {"files_verified": 46558},
            "snapshot": {"files_verified": 15799, "sqlite_verified": 7,
                         "release_check": {"release_id":"v1.5.62","files_verified":783,
                                           "check":"ok","signature_verified":true}}}}
```

`latest.json` 里记录的信息远多于「有个文件在那儿」：

```
restore_check: "ok"              ← 做过恢复演练
paired_ledger_check.restore_check: "ok"
request_rows: 367279
release_check.files_verified: 783
state_owner_files_verified: 28
```

## 3. 独立验证：备份真的能读

不看它自己的自述，自己验一遍：

| 检查 | 结果 |
|---|---|
| `sha256sum` 对比 `latest.json` 记录 | **完全一致**（`e77324ee…3a4f`） |
| `tar tzf` 能否完整列出 | **2,284 个条目，无报错** |
| 是否含主库 | **`manager.db` 存在（2 个条目）** |

**备份可用。**这是我能给出的最直接的证据 —— 不是「系统说它 ok」，是我自己算了一遍哈希、把整包列了一遍。

## 4. 找到两个真实问题

### 4.1 没有异地副本

`latest.json` 里明确写着：

```
offsite: false
retention: "retained pending offsite policy"
```

**备份和源数据在同一台机器上，同一块盘（`/` 已用 72%）。**

磁盘故障、误操作、勒索软件 —— 这三种情况下备份会跟着一起没。这不是「备份策略没做好」，
是**备份目前只能防「表被误删」这一类，防不了「机器没了」**。

`retention` 字段自己写着 `pending offsite policy` —— 说明设计时就想到了，只是还没落地。
**这是个已知缺口，不是未知缺陷。**

### 4.2 retention 只清 tar.gz，不清工作目录

`workbuddy-daily/` 下除了保留的那份 tar.gz，还有一个 **2.2 GB 的 `snapshot-rsa5dz9b/`**：

```
2.2G   snapshot-rsa5dz9b/        mtime 11-25 05:34:54
       169 个文件
       内含 manager.db（750 MB）、paired-ledgers/、pair-verification/、quarantine-verification/
```

排查结论：

| 检查 | 结果 |
|---|---|
| `latest.json` 是否引用它 | **否** |
| retention 收据是否引用它 | **否** |
| 还有进程持有它吗 | **否**（`lsof +D` 为空） |
| 谁创建的 | 05:30 那次运行（它产出的 `recovery-20261125-053001.tar.gz` 已被删除） |

**它是 cleansed 掉的 tar.gz 对应的中间目录。**retention 删了压缩包，没删目录。

**我没有删它。**理由：删备份是不可逆动作，而我对「它绝对没用」的把握来自三个间接证据
（未被引用、无人持有、时间吻合），不是来自它的创建者。**在这种事上，保守比对更重要。**

## 5. 这两个问题为什么本轮不修

**4.1 异地备份** —— 需要外部存储（对象存储、另一台机器）。这是基础设施决策，
不是代码改动，而且涉及凭据管理。**不是我能自己决定的。**

**4.2 清理逻辑** —— `backup_retention.py` 位于
`/opt/zhihui-release/reviewed-releases/backup-retention-20261104/`，
**不在本仓库**，属于「已审阅发布件」，改它需要走那条链路的审阅流程。
本仓库里改不到它。

## 6. 能给运维的直接建议

```
# 确认当前磁盘
df -h /

# 这 2.2 GB 是可回收的残留（三个独立证据都指向同一结论，但仍建议先确认再删）
sudo du -sh /root/backups/workbuddy-daily/snapshot-rsa5dz9b

# 若确认无用
sudo rm -rf /root/backups/workbuddy-daily/snapshot-rsa5dz9b
```

**回收后磁盘从 72% 降到约 68%。**这是缓解，不是解决 —— 真正的解决是 4.1 的异地副本。

## 7. 结论

| 项 | 状态 |
|---|---|
| 备份能否恢复 | **能**（sha256 匹配、2284 条目可完整列出、含主库） |
| 恢复演练是否做过 | **做过**（`restore_check: ok`，非我执行） |
| 能否防机器丢失 | **不能**（`offsite: false`） |
| 有无可回收空间 | **有**（2.2 GB 残留，需运维确认） |
| 本轮是否改代码 | **否** |

**备份这一环，比回滚那一环健康。**回滚是「提示会骗人」，备份是「东西是好的，但只有一份且在同一块盘上」。

## 8. 待办

| 项 | 卡在哪 |
|---|---|
| **异地备份** | 需外部存储与凭据，运维决策 |
| retention 清理中间目录 | 脚本在 reviewed-releases 区，不在本仓库 |
| 回收 2.2 GB 残留 | 建议运维确认后执行 |
| P0-1 守卫原因码 | 待运维安装 |
| P0-2 socket activation | 待运维安装 unit |
| CI 漏跑 `.cjs` | 需 `workflow` scope |