# v1.5.80：备份 OOM 的根因与修复（第 42 轮）

## 1. 从「改不了」到「改得了」

上一轮的结论是：`MemoryMax=512M` 这个限制改不了，因为调用方（`systemd-run --scope`）
不在本仓库。

这个结论对，但**问错了问题**。真正该问的是：**一个备份任务为什么会需要 513 MB？**

这一轮去读了 `server/backup_application.py`（367 行，不在受保护清单里），
找到了答案 —— 它不需要那么多内存。**是代码把它用掉了。**

## 2. 根因：收尾阶段两处整文件读入内存

### 2.1 恢复验证

```python
restored = Path(scratch)/'restore.db'
restored.write_bytes(archive.extractfile('manager/data/manager.db').read())
```

备份完成后要把数据库解出来，跑 `PRAGMA quick_check` 和一次行数核对 —— 这是对的做法。
但 `extractfile(...).read()` 把 **750 MB 的 manager.db 整个读进内存**，
`write_bytes` 再写一次。

### 2.2 整包哈希

```python
'sha256': hashlib.sha256(output.read_bytes()).hexdigest()
```

给刚生成的备份包算校验值，用于 `latest.json` 和后续一致性验证。
但 `read_bytes()` 把 **406 MB 的 tar.gz 整个读进内存**。

### 2.3 两处相加

| 位置 | 峰值 |
|---|---|
| 恢复验证（750 MB 库） | ~750 MB |
| 整包哈希（406 MB 包） | ~406 MB |
| **合计** | **~1.1 GB** |

**进程的 cgroup 上限是 512 MB。**

对照内核日志：

```
Nov 25 05:34:56 kernel: Killed process 1058616 (backup_applicat)
         total-vm:801496kB  anon-rss:513072kB
```

**513 MB 正好是撞线的那一刻。**不是「备份需要很多内存」，是「代码在收尾时把不该一次读的东西一次读了」。

## 3. 改法：流式

```python
# 恢复验证
with archive.extractfile('manager/data/manager.db') as src, restored.open('wb') as dst:
    shutil.copyfileobj(src, dst, 1024*1024)

# 整包哈希
digest = hashlib.sha256()
with output.open('rb') as handle:
    for block in iter(lambda: handle.read(1024*1024), b''):
        digest.update(block)
```

**内存占用与文件大小无关，恒定一个 1 MB 缓冲。**

行为完全不变：该解出来的还是解出来、该查的还是查、哈希值一字不差。**只是不再把整份内容同时放在内存里。**

## 4. 为什么这个 bug 能活这么久

因为**它被另一层机制兜住了**。

`evaluate_backup_health` 有四态：`backup_status_unavailable`、`backup_stale`（超 26 小时）、
`backup_publish`、`backup_size`。05:30 那次失败时，04:18 的成功报告还在 26 小时内，
所以全局判为**健康** —— 设计上完全正确，备份只要有一份新鲜的就行。

**代价是：一次真实的失败被合法的兜底掩盖了。**这不是告警设计错了，
而是「兜底成功」这件事本身会让人看不见「为什么会需要兜底」。

如果没有去查 swap 为什么用了 28%，这个 OOM 会一直发生：
每天两次备份，一次成功一次被杀，全局永远健康，直到某天连续两次失败。

## 5. 部署验证

| 项 | 结果 |
|---|---|
| 版本 | **v1.5.80** |
| 构建提交 | `5737592d1fb31abaf5ae7ae3532e29cfbf86ce73` |
| **重启窗口** | **87 个请求，0 个 5xx** |
| 五个页面 | `/` `/dashboard` `/accounts` `/logs` `/settings` 全 **200** |
| 门禁 | 后端 1,117 例 + 前端 245 例 + F821=0 + F811=0 |

## 6. 还没验证的部分（如实写）

**修复是否真的消除 OOM，要等下一次备份跑完才知道。**

备份由定时器触发（`zhihui-recovery.timer` 04:15、另有 05:30 附近的一次）。
也就是说，**这个修复的有效性验证在部署后几小时，不在这轮之内**。

判断依据（下一轮查）：

```bash
# 1. 这次备份有没有 OOM
journalctl --since '-24 hours' | grep -ci 'out of memory.*killed process'

# 2. 备份包是否新鲜（时间应晚于部署时间）
cat /root/backups/workbuddy-daily/latest.json | grep -E 'path|restore_check'

# 3. 巡检是否仍在报 OOM
./deploy/healthcheck.sh | grep '内存不足'
```

**如果下一次备份仍有 OOM，说明还有第三处整文件读取我没找到** —— 那时要重新 grep 全文件。

## 7. 其他两处同类代码：查过，可接受

| 行 | 代码 | 为什么可以留 |
|---|---|---|
| 159 | `sha256(archive.extractfile(member).read())` | 783 个小文件，合计 11 MB |
| 172 | `write_bytes(extractfile(name).read())` | 信任锚、签名、清单三个小文件 |

**不是所有 `.read()` 都要改。**改的依据是「文件可能远大于内存预算」，
不是「出现了 `.read()` 就危险」。

## 8. 结论

- **根因找到并修复**：两处整文件读取，合计 1.1 GB 峰值
- **内存占用现在与文件大小无关**
- **有效性待下一次备份验证**
- **上一轮「改不了」的结论已修正** —— 限制确实改不了，但代码可以改

## 9. 待办

| 项 | 状态 |
|---|---|
| 验证下一次备份不再 OOM | **下一轮必查** |
| 回收 2.2 GB 残留 | 待运维确认 |
| 异地备份 | 需外部存储 |
| P0-1 / P0-2 | 待运维安装 |
| GitHub Release v1.5.80 | CI 构建中 |