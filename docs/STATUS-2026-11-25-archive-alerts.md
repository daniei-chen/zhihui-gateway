# v1.5.84：归档失败从此有人知道（第 53 轮）

## 1. 问题

`archiveworker._loop()` 的失败分支原来只做两件事：

```python
except (Exception, SystemExit) as exc:
    _state.update(sampled=True, last_at=int(time.time()), ok=False, error=type(exc).__name__)
    log.error('archive cycle failed: %s; source rows remain protected', type(exc).__name__)
```

**数据是安全的** —— 一行都不删，`source rows remain protected` 这句是准确的。

但**没有人会知道它失败了**，原因有三层：

1. **日志没人盯。**
2. **`status()` 里的 `fresh` 不判断 `ok`。**
   ```python
   fresh=bool(age is not None and age <= max(600, config.ARCHIVE_INTERVAL_SEC * 3))
   ```
   一个每 300 秒都失败的任务，`last_at` 每 5 分钟更新一次，**永远 `fresh=True`**。
3. **healthcheck 里根本没有归档这一项**（`grep archiv deploy/healthcheck.sh` 无输出）。

## 2. 为什么这件事现在重要

上一轮查清楚：归档按 **90 天阈值**触发，而库里最老的数据是 **71 天**。

```
archive_runs    0 行          ← 从来没跑过
request_logs    最老 71 天前
```

**约 2026-12-13 会是它第一次真实运行。**

如果那次失败，或者之后的每一次都失败，按原来的实现：
- 数据不会丢（安全）
- 磁盘会一直涨（`request_logs` 124 MB、`forwarding_events` 105 MB、`request_outcomes` 65 MB…）
- **直到某天磁盘满，才会有人发现「归档一直没工作」**

那时候已经很难定位是哪天开始坏的。

## 3. 改法

`_loop` 的两条路径都接上 `operational.transition`：

```python
try:
    result = run_once()
    ...
    _report_health(True, '归档周期正常')
except (Exception, SystemExit) as exc:
    ...
    _report_health(False, f'归档周期失败：{type(exc).__name__}（源数据未删除）')
```

**detail 里特意写「源数据未删除」。**看告警的人第一反应会是「我的数据呢」，
必须一句话说清楚。

审计链那条同一类问题也一起接上：

```python
def _report_audit_chain(intact, error):
    if intact is False:      # 链真断了 —— 有人改过历史
        operational.transition('audit_chain_broken', True, '管理事件哈希链断裂——历史被改动过')
    elif error is not None:  # 校验本身没跑成 —— 磁盘/权限问题
        operational.transition('audit_chain_broken', True, f'审计链校验未能完成：{error}')
    else:
        operational.transition('audit_chain_broken', False, '审计链完整')
```

**「链断了」和「没查成」用同一个告警名、不同的 detail。**原代码的注释已经强调过
这两件事不能混（混了会让人以为历史被篡改，实际只是没查成），所以 detail 必须区分。

## 4. 两个刻意的保护

1. **告警路径自身失败不能影响归档。** `_report_health` 内部整个包在 try/except 里，
   失败只记一条 warning。告警系统挂了不该让归档停摆。
2. **恢复时主动清除。** 成功路径调 `transition(..., False, ...)`，
   所以一次失败之后不会永远显示失败。

## 5. 部署验证

| 项 | 结果 |
|---|---|
| 版本 | **v1.5.84** |
| 构建提交 | `54a72ad9e9792ed60d4d7c7d183190a2b0c81474` |
| **部署窗口** | **44 个请求，0 个 5xx** |
| 门禁 | 1,117 后端 + 245 前端 + F821=0 + F811=0 |
| `archive_cycle_failed` 告警 | **未出现**（归档正常，符合预期） |

## 6. 一个意外的实证

部署后查 `operational_alerts` 表：

```
name                    since
workbuddy_low_accounts  2026-11-25 15:50:33
```

**`since` 还是 15:50:33** —— 这个时间点跨越了 v1.5.83 和 v1.5.84 两次重启。

第 51 轮做的告警持久化，在这轮的真实部署里**又验证了一次**：重启不重置 `since`，
也不重复投递。

## 7. 新增 5 个测试

`server/tests/test_archive_cycle_alerts.py`

| 用例 | 验证 |
|---|---|
| `failure_raises_alert_and_success_clears_it` | 失败告警 + detail 说明数据未删 + 成功清除 |
| `alert_failure_does_not_propagate` | 告警系统出错不拖垮归档 |
| `broken_chain_is_distinct_from_unchecked` | 「链断了」与「没查成」的说明必须不同 |
| `intact_chain_clears_the_alert` | 链正常时清除告警 |
| `loop_reports_health_on_both_paths` | AST 检查 `_loop` 确实调了 `_report_health` |

最后一条用 AST 而不是运行：因为 `_loop` 是个无限循环，跑不起来。
用 AST 守「调用点存在」是这类问题唯一可靠的测法。

## 8. 为什么这轮值得做

前两轮（51 之后）都在做诊断，三次判断被自己推翻。这轮不一样：

- **问题是真的**（归档失败无声）
- **时机是真的**（19 天后首次运行）
- **改动是小的**（两个函数调用 + 一个辅助函数）
- **验证是确定的**（测试 + 部署窗口 + 表状态）

规范 §2.1 把「日志缺失、看不见的状态」列为 P1。这条正是 ——
不是数据错了，是**该被看见的事没被看见**。

## 9. 待办

| 项 | 状态 |
|---|---|
| v1.5.83 / v1.5.84 Release | CI 构建中 |
| **归档首次真实运行** | 约 2026-12-13，届时需确认 `archive_runs` 出现记录 |
| age 私钥离线保管 | 运维 |
| 异地备份 | 运维 |