# 5xx 归因与三天日志考古：查到的缺陷都已修好（第 54 轮）

## 1. 为什么查这个

规范 §3.4 要求「看到 5xx 先分三类」。24 小时里 8 个 5xx，我想确认没有部署窗口责任。

## 2. 归因结果

| 时间 | 状态 | error_code | error_source | 归因 |
|---|---|---|---|---|
| 11-24 19:19:39 | 503 | `gateway.application.error` | gateway | **部署窗口**（已修） |
| 11-24 21:18:30–59 | 503 ×7 | `no_healthy_account` | upstream | **容量拒绝** |

7 条间隔固定（30/35/39/45/49/54/59 秒）来自同一个 key，是客户端重试 —— 上游账号池耗尽，
属运维动作（加账号），非代码缺陷。

**第一类只有 1 条，而且那正是第 48 轮分析过的那个时刻**（19:19:39 停进程 → 19:19:54 才接住）。
中继的重试窗口已从那次的「4 次 / 9 秒」扩到「6 次 / 22.5 秒」，v1.5.82 部署。

## 3. 意外发现：一个 7 天的下降趋势

查 `gateway.application.error` 的历史：

```
11-19   51 次
11-20   12 次
11-21    3 次
11-22    3 次
11-23    1 次
11-24    1 次
11-25    0 次     ← 今天没有
```

从 51 降到 0。但**代码里没有任何一次改动声称修过这个问题** —— 说明它是被一系列
重构顺带治好的，而不是被针对性修掉的。

这既解释了为什么昨天只剩 1 条，也说明「以前很多」这件事已经没人记得了。

## 4. 日志考古：那 71 次到底是什么

`gateway.application.error` 是个笼统的兜底码。翻 `journalctl` 找真正的异常类型：

| 异常 | 次数 | 判断 |
|---|---|---|
| `UpstreamError: 上游控制面不可用：所有上游控制面均不可达` | 24 | 上游问题 |
| `NameError: name 'Path' is not defined` | 16 | **真 bug** |
| `UpstreamError: doubaowork 上游熔断中` | 10 | 上游问题 |
| `ConfigurationError: WB_TRAFFIC_TAP: unknown_variable` | 10 | 配置问题 |
| `NameError: name '_iso_of' is not defined` | 10 | **真 bug** |
| `ValueError: 非法的文件名` | 6 | 输入问题 |
| `NameError: name 'json' is not defined` | 6 | **真 bug** |
| `AttributeError: 'sqlite3.Row' has no attribute 'get'` | 5 | **真 bug** |

**四个是真代码缺陷。**于是逐个确认它们还在不在。

## 5. 四个缺陷都已修好

| 缺陷 | 最后一次出现 | 当前状态 |
|---|---|---|
| `NameError: json` | 11-20 10:12 | 已修 |
| `NameError: Path` | 11-20 07:45 | 已修 |
| `NameError: _iso_of` | 11-21 19:28 | 已修 |
| **`AttributeError: sqlite3.Row.get`** | **11-23 01:00** | 已修 |

最后一个是最近的，也最值得看。它的完整栈是：

```python
# credit_watch.py:265（v1.4.6）
raw = json.loads(row.get('raw') or '{}')
       ^^^^^^^ AttributeError: 'sqlite3.Row' object has no attribute 'get'

↑ 被 operational.py:860 调用
→ ERROR workbuddy.operations: operational health evaluation
```

**`db.query()` 的返回类型是 `list[sqlite3.Row]`**，而 `sqlite3.Row` 没有 `.get()`。
这一行崩溃导致**整个健康评估停摆**。

当前 `credit_watch.py` 已经重构（`_prune`、`latest(with_raw=...)`），那行不在了。

## 6. 顺着查：还有没有同类型的隐患

`budget.py` 里有大量 `row.get(...)`，看起来像同类问题。但查下去是安全的：

```python
# budget.py:unresolved_batches
rows = [dict(r) for r in db.query(...)]      # ← 已转成 dict
yield rows
```

而 `_classify(row, log)` 的第二个参数 `log` 虽然是 `sqlite3.Row`，但全部用下标访问
（`log['usage_source']`、`log['status']`…），**从不调用 `.get()`**。

**同一个文件里两种写法并存，但都是对的** —— 这说明作者清楚区别在哪。

## 7. 还有一层：单点故障不该拖垮全局

那次崩溃真正暴露的问题不是 `sqlite3.Row`，而是**一个子系统的缺陷让整个健康评估停摆**。

查当前实现：

```python
for _name, _active, _detail in _safe_alerts(credit_watch.workbuddy_alerts, 'workbuddy.credits'):
    transition(_name, _active, _detail)
```

**已经修好了。**代码注释写得很清楚：

> 下面每一组告警都用 `_safe_alerts` 单独包起来。原因见该函数的说明：
> 这段求值原本共用一个 try，某组抛异常会让它后面所有告警静默跳过。

## 8. 结论

**这一轮没有改代码，因为查到的四个缺陷全部已经修好。**

这两轮（52、53 之后）的模式很清楚：代码质量已经很高，我查到的问题大多是
**作者踩过、记录过、并已修好的**。规范 §2.1 说这种情况就做诊断，不要硬凑改动。

但这一轮有三件事是实的：

1. **v1.5.83 正式发布**（4 个资产齐了：tar.gz / zip / exe / sha256）
2. **5xx 归因完成**：7 条容量拒绝 + 1 条已修的部署窗口，**本轮无部署责任**
3. **`gateway.application.error` 从 51 降到 0** 这一趋势，第一次被记录下来

第 3 点尤其值得写下来 —— 如果不查，下次看到 71 次只会觉得「一直有这个问题」，
而实际上它已经被治好了，只是没人知道是什么治好的。

## 9. 待办

| 项 | 状态 |
|---|---|
| v1.5.84 Release | CI 构建中（2/4 资产） |
| `gateway.application.error` 为何归零 | 未知具体改动，趋势已记录 |
| `WB_TRAFFIC_TAP: unknown_variable` | 配置项，10 次，需运维确认是否已清理 |
| age 私钥离线保管 | 运维 |