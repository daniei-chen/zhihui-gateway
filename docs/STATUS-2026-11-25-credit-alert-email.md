# 账号余额告警接进邮件通道（第 60 轮）

上轮发现的缺口：同一套系统里，磁盘、证书、备份、延迟都会主动发邮件，
唯独「某个账号快没积分了」不会 —— 而它打空之后推理请求会直接 503。

这一轮把它接上了，并在部署后 1 分钟内验证到了真实投递。

## 1. 关键不在「加一条规则」

如果只是漏了一条规则，加一行就完事。实际不是。

原来唯一的平台规则是：

```python
for platform in snapshot.get('platforms', []):
    output.append(Signal('platform_' + platform['name'],
                         platform['state'] != 'up', ...))
```

**它看的是只读探针状态。** 探针返回 `up` 的含义是「上游接口能连上」，
**不是「账号里还有积分」**。

所以 `platform_workbuddy` 会发邮件，但 `workbuddy_low_accounts` 不会 ——
这不是覆盖面够了，是**两个语义不同的信号，一个没有出口**。

## 2. 判据为什么是「单号余额」而不是「池子总量」

`pool_projection` 原来只算 `credits_remain`，是**总和**。

池子总计 15 万分时，某个号剩 600 分，总量看起来依然健康。而那个号会在
一次长上下文请求里被打空，然后被 `skip_exhausted` 摘出池子。

`credit_watch.workbuddy_alerts` 的注释早就写明这件事：

> 池子总计 15 万分时，某个号剩 600 分照样会在一次长上下文请求里被打空，
> 然后被 `skip_exhausted` 摘出池子，而总量看起来还很健康。

测试里专门造了这组数据（20000 / 5000 / 890 / 809）：

```python
self.assertGreater(pool['credits_remain'], 26000)      # 总量健康
self.assertEqual(pool['low_credit_accounts'], 2)       # 但两个号快空了
```

## 3. 阈值只有一个来源

```python
low_credit_accounts = sum(1 for value in finite
                          if value < credit_watch.WB_LOW_ACCOUNT)
```

**没有在 `steward_snapshot` 里另写一个 1000。**

两处各写一份的话，改了一处忘了另一处，就会出现「页面告警、邮件说没事」——
比完全不告警更糟，因为运维会以为自己看全了。

有一个测试守这条：

```python
self.assertEqual(pool['low_credit_threshold'], credit_watch.WB_LOW_ACCOUNT,
                 '两处阈值必须同源，否则页面告警而邮件不告警')
```

## 4. 采不到数据报「未知」

```python
if low is None or threshold is None:
    output.append(Signal(..., None, 'warning', ...,
                         '未取得可用的余额数据，不能据此判断正常。'))
```

`active=None` 在 `steward_rules` 里的语义是「未知」，既不发「告警」也不发
「恢复」。**报 `False` 等于宣布「没有低分账号」，而实际上我们不知道。**

这条来自 2026-10-20 的教训（失败采集器是未知，不是健康的空账号池），
`steward_snapshot` 顶部注释里也写着同样的话。

## 5. 部署后 1 分钟就验证到了

```
id   event    rule                        t                    status
603  firing   platform_workbuddy_credits  2026-11-25 17:09:02  accepted
```

详情：

```
共 23 个账号，其中 2 个低于 1000 分（已取到 23 个账号的余额）；
余额耗尽的账号会被摘出池子，之后请求可能被拒。
```

**注意那个「2」** —— 和页面里 `workbuddy_low_accounts` 的数字完全一致：

```
2 个 WorkBuddy 账号余额低于 1000 分（ì 809、慢点 838）；每日签到各 +100…
```

两套系统现在说同一件事，而只有一套会主动打扰人。

`status = accepted` 说明邮件真的投出去了，不是只写进队列。

## 6. 部署验证

| 项 | 结果 |
|---|---|
| 版本 | **v1.5.88** |
| 构建提交 | `0112ca5f33991ef4b24795053d1c639119354d55` |
| **部署窗口** | **100 个请求，0 个 5xx** |
| 门禁 | 1,143 后端 + 245 前端 + F821=0 + F811=0 |
| 新规则实测 | `firing` @ 17:09:02，`accepted` |

## 7. 只接了一个

第 58 轮新加的 `key_quota_near_limit` **还在 `operational` 池子里**，
没有邮件出口。

没在这一轮一起做，理由是规范 §2.2「一次只动一件事」。
它的接入路径也不同 —— 需要把 `usage_daily` 的聚合结果放进 snapshot，
比这次多一层。

**它是已知缺口，不是已完成。**

## 8. 一个结构性建议

这一轮验证了上轮的判断：**两个告警池子并存，但没有文档说明哪个信号该进哪个。**

现在 `steward_rules` 有 13 条规则，`operational` 有 18 项告警，
交集靠人记得住。

判断依据其实已经能总结出来了：

| 该发邮件 | 只进页面/巡检 |
| --- | --- |
| 会影响用户、且现在就该动手 | 趋势性、可观察、有充足反应时间 |
| 余额/容量/证书将耗尽 | 版本一致性、发布树残留 |
| 备份/归档失败 | 单次延迟超标 |
| 账本通道异常 | 噪声类统计 |

**这条线没写进任何文档。**下次加告警时，第一个问题应该是「它该进哪个池子」。