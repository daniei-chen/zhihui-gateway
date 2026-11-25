# 两套告警系统，一套不发邮件（第 59 轮）

## 1. 一个被我判断错两次的地方

上一轮（58）我查配额告警时看到 `operational_alerts` 表里只有一条记录，
差点下结论「告警只是存着没人看」。这一轮继续查，发现**完全相反** ——
系统里有一套完整的、品牌化的、带去重窗口的告警邮件系统。

**两次都是先看到局部就急着下结论。**这是本章记录的第 12、13 次。

## 2. 邮件系统有多完整

`steward_mail.py` 里连邮件正文都写好了：

- 品牌头图（CID 内嵌）
- 「已核实的信号」区块
- 「管家研判 / AI 辅助，不替代事实」
- 「**同一问题半小时内合并告警，确认恢复后通知**」
- 「不索取密码，不附带密钥」
- 页脚：「少一点打扰，多一分笃定。」

配套数据也都在：

| 表 | 行数 | 作用 |
|---|---|---|
| `steward_deliveries` | **600** | 每次投递 |
| `steward_incidents` | **289** | 事件生命周期 |
| `steward_mail_windows` | **11** | 半小时去重窗口 |
| `steward_analysis` | **574** | AI 研判 |

最近一条是 `inference_latency` 的 firing → recovered，收件人 `2421873411@qq.com`。

**这套东西一直在工作。**

## 3. 但它是另一套

把两张表并列看：

```
operational_alerts（表 + 回环 API，不发邮件）
  workbuddy_low_accounts  since 2026-11-25 15:50:33

steward_deliveries（会发邮件）
  backup_evidence          budget_evidence        certificate_evidence
  channel_verification     disk_capacity          inference_failure
  inference_latency        platform_doubaowork    platform_qoder
  platform_trae            platform_workbuddy     pool_capacity
```

**12 个规则发邮件，`operational` 里的告警不发。**

## 4. 为什么 `platform_workbuddy` 不算覆盖

看起来有「workbuddy」字样，但看它的判断条件：

```python
for platform in snapshot.get('platforms', []):
    output.append(Signal('platform_' + platform['name'], platform['state'] != 'up', ...
```

**它看的是只读探针状态是不是 `up`**，不是账号余额。

而 `workbuddy_low_accounts` 的语义是完全不同的：

```
2 个 WorkBuddy 账号余额低于 1000 分（ì 809、慢点 838）；
每日签到各 +100，若日消耗低于 100 会自行回升
```

**探针还是 `up` 的时候，某个账号的积分可能只剩 809 分**
（`credit_watch.py` 的注释专门解释过这件事：池子总计 15 万分时，
某个号剩 600 分照样会在一次长上下文请求里被打空，然后被 `skip_exhausted` 摘出池子，
而总量看起来还很健康）。

**所以这不是「覆盖面够了」，是「两个不同的信号，一个没有出口」。**

## 5. 这个缺口有多严重

`workbuddy_low_accounts` 从 **15:50:33** 挂到现在（本轮查证时 17:05），
**约 1 小时 15 分钟**，一封邮件都没发。

它要表达的事情是：**账号余额快打空，打空之后推理请求会 503。**

运维只能靠主动打开页面或跑巡检才知道。而 `steward` 那 12 个规则对应的
问题（磁盘、证书、备份、延迟……）都会主动发信。

**同一套系统里的两个告警，一个会打扰你，一个不会。而这个不会的恰好更紧急。**

## 6. 为什么这一轮没有直接改

接进去要动三处：

1. `steward_snapshot.pool_projection()` 加一个「低于阈值的账号数」
   （现在只算 `credits_remain` **总和**，而告警要的是**个别账号**低于 1000）
2. `steward_rules.evaluate()` 加一条 `workbuddy_credits` 规则
3. 测试 + 全门禁（1,135 例）+ 完整部署链

本轮已经发布了 v1.5.86，剩余时间不足以走完整条链。

规范 §1.5 说「一轮结束时要么已发布上线，要么已完整回退，不留半成品」。
**所以我选择不改**，而不是改一半放着。

## 7. 顺带确认的事

查 `credit_watch.py` 时看到这段注释：

> `db.query_one` 返回的是 `sqlite3.Row` —— **它没有 .get()**，只有下标和 .keys()。
> 2026-11-23 首次上线时这里写成了 `row.get('raw')`，采集成功、告警却在装配阶段抛
> AttributeError，把后面所有告警一起跳过。统一转成 dict，后面按下标/键取都安全。

这和我在第 54 轮从生产日志里考古出来的完全一致（`AttributeError: 'sqlite3.Row'
object has no attribute 'get'`，5 次，最后 11-23 01:00）。

**作者踩过、记录过、修好了。**我是从日志倒推回去的 —— 两条路径指向同一个结论。

## 8. 本轮产出

| 项 | 结果 |
|---|---|
| v1.5.86 Release | **已发布，Latest**（4 资产齐） |
| v1.5.87 Release | CI 构建中（0 资产） |
| 线上 | v1.5.87，近 2 小时 0 个 5xx |
| 巡检 | ok 25 / warn 4 / FAIL 0 |

## 9. 下轮要做的

把 `workbuddy_low_accounts`（以及第 58 轮新加的 `key_quota_near_limit`）
接进 `steward` 的邮件通道。接受标准：

1. 两个告警名都出现在 `steward_rules.evaluate()` 的输出里
2. 触发时 `steward_deliveries` 里能看到对应行
3. 恢复时能看到 `recovered` 事件
4. 半小时去重窗口对它们生效

## 10. 一条更普遍的观察

这套系统里已经出现了**两种「告警」概念并存**：`operational`（给页面/巡检看）
和 `steward`（主动打扰人）。这个分工本身没有错 —— 不是所有信号都值得发邮件。

**但没有文档说明哪些该走哪条、判断依据是什么。**
结果是两个池子各自长出了自己的规则，交集靠人记得住。

如果以后还要加告警，第一个问题应该是「它该进哪个池子」，而不是「加在哪」。