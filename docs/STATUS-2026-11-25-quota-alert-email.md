# 补完最后一条告警出口（第 61 轮）

第 60 轮把账号余额告警接进了邮件，留了一条没做：`key_quota_near_limit`。
这一轮补上。

## 1. 这次的关键决定：不重写判断

两个做法摆在面前：

| 做法 | 代价 |
| --- | --- |
| A. 在 `steward_snapshot` 里写一条 SQL 聚合 `usage_daily` | 判据维护两遍 |
| B. **直接转述 `credit_watch.quota_alerts()` 的结论** | 无 |

选 B。因为 `quota_alerts()` 里已经有三件容易写错的事：

1. **80% 阈值**（`QUOTA_WARN_RATIO`）
2. **单位换算**（`ledger.SCALE = 1_000_000`，配额是积分、用量是百万分之一）
3. **哪些密钥参与**（只有配了日额度且 `enabled=1` 的）

**重写一份 = 同一判据两个副本。**改了一处忘另一处，就会出现
「页面说告警、邮件说没事」—— 这比完全不告警更糟，因为运维会以为自己看全了。

所以新代码只有一句实质逻辑：

```python
def quota_fact():
    try:
        verdict = credit_watch.quota_alerts()
    except Exception:
        return {'active': None, 'detail': '日配额查询失败，本轮不评估'}
    if not verdict:
        return {'active': None, 'detail': '日配额没有返回结论'}
    _name, active, detail = verdict[0]
    return {'active': bool(active), 'detail': detail}
```

## 2. 「未知」和「充足」必须分开

```python
if not verdict:
    return {'active': None, ...}      # ← 不是 False
```

`active=None` 在 `steward_rules` 里的语义是「未知」：既不发告警也不发恢复。
报 `False` 等于宣布「配额充足」，而实际上我们没查到。

这条已经在这个项目里出现过四次（`memory_evidence`、`certificate_evidence`、
`platform_*` 的 `unknown` 状态、以及现在的 `quota_fact`）。
`steward_snapshot` 顶部注释写着一句话：

> A failed collector is unknown, not an empty healthy account pool.

## 3. 怎么验证一条「不该触发」的告警

配额现在只用了 **2.7%**，所以它**不会**发邮件。这就带来一个验证难题：

**「没告警」可能是「不该告警」，也可能是「根本没跑」。**只看前者会把后者当正常。

三条证据分开取：

| 观察 | 说明什么 |
|---|---|
| `inference_latency` 在 17:18:02 有新的 `recovered` | `evaluate()` 在 v1.5.89 部署后确实跑过 |
| steward 相关错误计数 = 0 | `quota_fact()` 没有抛异常 |
| `quota.get('active')` 是 `False` | 2.7% < 80%，不发邮件是对的 |

**第二条是关键**：如果 `quota_fact()` 崩溃，异常会冒到 `local_facts()`，
整份 snapshot 构建失败，`evaluate()` 拿不到 platforms，那 17:18 那条
`inference_latency` recovered 就不会出现。

**换句话说：别人的成功证明了这条没坏。**

这是「怎么发现副作用」这个问题的答案之一 —— 找一个**共用上游步骤**的
旁证信号，它失败了会一起消失。

## 4. 两个池子现在对齐了

| 告警 | 页面/巡检 | 邮件 |
|---|---|---|
| 账号余额偏低 | ✅ `workbuddy_low_accounts` | ✅ 第 60 轮 |
| 密钥日配额接近上限 | ✅ `key_quota_near_limit` | ✅ **本轮** |

## 5. 部署验证

| 项 | 结果 |
|---|---|
| 版本 | **v1.5.89** |
| 构建提交 | `4d56e032d7177a38c921f8f7855416377894676d` |
| **部署窗口** | **86 个请求，0 个 5xx** |
| 门禁 | 1,149 后端 + 245 前端 + F821=0 + F811=0 |

`steward_rules.py` 现在有 14 条规则。

## 6. 累积的告警出口盘点

| 池子 | 项数 | 出口 |
|---|---|---|
| `operational_alerts` | 18 | 页面 + 仅回环的 `/health/alerts` + 巡检 |
| `steward_rules` | 14 | **邮件**（半小时去重 + AI 研判） |

两边的交集 = 4 条（余额、配额、磁盘、延迟类）。其余各自独立。

**这条线仍然没写进任何文档**（第 60 轮已提出）。判断依据其实能总结：

| 该发邮件 | 只进页面/巡检 |
|---|---|
| 会影响用户、现在就该动手 | 趋势性、有充足反应时间 |
| 余额/容量/证书将耗尽 | 版本一致性、发布树残留 |
| 备份/归档失败 | 单次延迟超标 |
| 账本通道异常 | 噪声类统计 |

## 7. 待办

| 项 | 状态 |
|---|---|
| v1.5.87 / v1.5.88 / v1.5.89 Release | CI 构建中 |
| 归档首次真实运行 | 约 2026-12-13 |
| 告警分池判据写进文档 | 待做 |