# v1.5.83：告警去重状态跨重启存活（第 51 轮）

## 1. 问题是怎么被发现的

第 46 轮在查「24 小时内有 35 次服务重启」这条异常线时，顺带看到告警分布：

```
workbuddy_low_accounts   35 次
backup_stale             12 次
model_completion          1 次
```

当时我以为 35 是这个告警**真实触发**了 35 次。这轮回头看才明白：**那 35 次的
分布时刻和重启时刻几乎一一对应。**

## 2. 根因

```python
alerts = {}                     # 模块级内存字典

def transition(name, active, detail):
    old = alerts.get(name)
    if active:
        if old is None:         # ← 判定「状态进入」
            alerts[name] = {...}
            notify.dispatch(name, True, detail)   # ← 投递
```

代码里有一句注释写得很清楚：

> K46：日志之外**再**投递一次（默认 disabled，不擅自外发）。
> 只在状态**进入**时投递，避免 monitor 每轮重复刷。

**这个去重在进程内是有效的。**但 `alerts` 是内存字典 ——
**部署重启后它清空**，monitor 下一轮看到 `old is None`，就判定为「状态进入」，
把同一条**从未恢复**的告警又投递一遍。

**发版越频繁，告警重复得越多。**而这台机器当天有 7 次部署。

## 3. 改法

新增一张表，只存「当前仍处于告警中」的行：

```sql
CREATE TABLE IF NOT EXISTS operational_alerts(
    name TEXT PRIMARY KEY, since INTEGER NOT NULL, detail TEXT NOT NULL);
```

- **进入告警** → 写行
- **告警恢复** → 删行
- **进程启动后第一次调用 `transition()`** → 懒加载回内存

**不存历史**，所以它表达的事实和内存字典完全一致，只是多了跨重启的能力。

### 三个刻意的设计

1. **写库失败只记日志，不抛异常。** 告警状态机的正确性不能依赖磁盘。
2. **`since` 保留最初进入时刻**，不是重启时刻 —— 否则「这个告警已经持续多久了」会失真。
3. **建表语句写在 `db.py` 而不是 `operational.SCHEMA`。** 因为 `operational` 自身
   `import db`，反向 import 会循环。`db.py` 历来是 DDL 的唯一执行点。

## 4. 部署验证

| 项 | 结果 |
|---|---|
| 版本 | **v1.5.83** |
| 构建提交 | `2571ceae2334bd144559b933fde091d8b0ad2c36` |
| **部署窗口** | **106 个请求，0 个 5xx** |
| 表已建立 | 是（`operational_alerts`） |
| 门禁 | 后端 1,117 例 + 前端 245 例 + F821=0 + F811=0 |

## 5. 决定性实证

部署完成后，表里立刻记下了当前告警：

```
name                    since                detail
workbuddy_low_accounts  2026-11-25 15:50:33  2 个 WorkBuddy 账号余额低于 1000 分
```

然后**又重启了一次**服务（15:51:49），观察是否重复投递：

```
15:51:55  INFO  workbuddy.operations: operational alert state restored
          （没有任何新的 ERROR operational alert name=...）
```

**修复前**：每次重启都会再打一条 `ERROR operational alert name=...` 并投递一次。
**修复后**：重启只做一次 `state restored`，不重复。

## 6. 新增测试

`server/tests/test_alert_persistence_across_restart.py`（5 例）：

| 用例 | 验证什么 |
|---|---|
| `test_alert_survives_restart_without_repeating` | 重启后同一条未恢复告警**不再**投递 |
| `test_recovery_removes_row_so_it_does_not_reappear` | 已恢复的告警重启后不「诈尸」 |
| `test_since_timestamp_is_preserved_across_restart` | `since` 是首次进入时刻，不是重启时刻 |
| `test_persist_failure_does_not_break_state_machine` | 写库失败不影响内存状态与投递 |
| `test_load_failure_does_not_block_alerts` | 读库失败不阻断告警判定 |

写这些测试时踩到两个坑，记下来：

1. **`patch.object(db, '_conn', None)` 配合 `patch.stopall` 会互相拆台** ——
   `stopall` 在 cleanup 链里把 `_conn` 复原成那个旧连接，于是测试关掉的是自己的连接、
   留下的却是原库连接，Windows 上就报「文件被占用无法删除」。改成手动保存/恢复。
2. **测试 `_persist_alert` 的容错时不能 patch `_persist_alert` 本身** ——
   那样异常会在 `transition()` 里抛出，测的是「调用方没保护」而不是
   「`_persist_alert` 内部有保护」。要 patch 它内部的 `db.transaction`。

## 7. 这一轮的意义

这是**第一次由我主动发现并修掉的、用户能感知的问题**（以「提醒不再重复」的形式）。

前几轮大多在验证「已经好的东西」或「已经坏但只能记录的东西」。
这一轮是一个完整的闭环：从告警次数异常 → 追到内存字典 → 追到部署重启 →
改代码 → 加测试 → 部署 → 重启验证不再重复。

## 8. 待办

| 项 | 归属 |
|---|---|
| age 私钥离线保管 + 解密验证 | 运维（一次拷贝） |
| 异地备份 | 运维（需外部存储） |
| v1.5.83 Release | 等 CI |