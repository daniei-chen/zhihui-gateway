# 竞品调研与日配额告警（第 58 轮）

规范 §8 要求「审计 GitHub 调研同类竞品，完善我们的智汇网关」。这一轮做这件事，
并且从一个真实对比里找到了缺口。

## 1. 调研了谁

| 项目 | 定位 | 许可 |
|---|---|---|
| **LiteLLM** | 100+ provider 统一网关，Rust 核心 + Python SDK | MIT（高阶能力收费） |
| **Portkey** | 路由 + guardrails + 缓存，配置即代码 | MIT |
| **One API** | 国内 Token 中转站，Go | MIT |
| **New API** | One API 的 fork，补了计费/多语言/支付 | AGPLv3 |

**注意到一点**：New API 是 AGPLv3。规范 §6 明确不做 AGPL/GPL 引入 —— 这一条
在调研里得到印证：One API 是 MIT 而 fork 变成 AGPL，说明这类项目的许可证会收紧。

## 2. 我们已有的（对比后确认）

| 能力 | 我们 | 说明 |
|---|---|---|
| 多密钥管理 | ✅ | `api_keys` 23 列，含 IP 白名单、模型白名单、平台限制、分组 |
| 配额与计费 | ✅ | 积分配额 + Token 配额 + 日配额，`credit_units` 精确到百万分之一 |
| 上游账号池 + 熔断 | ✅ | 池耗尽返回 503 并记 `no_healthy_account` |
| 请求级可观测 | ✅ | `request_outcomes` 13 列 + `usage_events` 16 列 |
| 审计链 | ✅ | `admin_events` 27,087 行哈希链，`broken_links = 0` |
| **零停机发布** | ✅ | 滚动中继 + 22.5 秒重试窗口，26 个真实流量窗口零 5xx |
| 发布签名 | ✅ | Ed25519 + 783 文件逐文件哈希 + 双向闭包 |
| 多语言界面 | ✅ | 五语言 |
| 备份与归档 | ✅ | age 加密 + `quick_check` 验证 + 90 天归档 |

**这些不比竞品弱。**尤其「零停机发布 + 签名发布链」在开源同类里很少见 ——
LiteLLM 和 Portkey 都没把「升级不打断在途请求」当第一类问题。

## 3. 找到了一个真缺口

对比时注意到的：**LiteLLM 有 budget alert、Portkey 有 budget limits、New API 有额度预警。**
三家都把「快用完了」当成独立能力。

查我们：

```
$ grep -rniE 'quota.*(warn|approaching|threshold|80%|near)' server/*.py server/routers/*.py
（无输出）
```

而配额是**强制**的：

```python
# server/budget.py:245-248
if fresh['daily_credit_quota']:
    if int(daily['credit_units']) >= ledger.units(fresh['daily_credit_quota']):
        raise BudgetRejected('今日积分额度不足，次日按服务器时区重置',
                             'daily_credit_quota_exhausted')
```

**用户看到的第一条信息就是「额度不足」，而那时已经调不通了。**

这是规范 §2.1 里的 P1 —— 影响用户，且完全可预防。

## 4. 改法

新增 `credit_watch.quota_alerts()`，接入 `operational.evaluate` 的 `_safe_alerts` 链。

三个设计决定：

**① 单位必须换算**

```python
limit = ledger.units(credit_quota)     # 200.0 积分 -> 200_000_000 units
used  = int(row['used_units'])          # usage_daily.credit_units
```

`ledger.SCALE = 1_000_000`。**直接拿两者比会差一百万倍** —— 要么永远不告警，
要么每秒都告警。测试里专门有一个用例盯这条（80% 触发 / 79.99% 不触发）。

**② 查询失败不抛异常**

```python
except Exception:
    log.warning('日配额告警查询失败', exc_info=True)
    return [('key_quota_near_limit', False, '日配额查询失败，本轮不评估')]
```

2026-11-15 踩过：`credit_watch` 里一个 `KeyError` 让整个健康评估循环每轮都崩。

**③ 阈值 80%**

和同类产品一致，也给运维留出反应时间。

## 5. 部署验证

| 项 | 结果 |
|---|---|
| 版本 | **v1.5.87** |
| 构建提交 | `03d94e6427038fb68d2bb350a057546462929929` |
| **部署窗口** | **101 个请求，0 个 5xx** |
| 门禁 | 1,135 后端 + 245 前端 + F821=0 + F811=0 |

**为什么不告警是对的** —— 用和代码相同的 JOIN 查了一遍：

```
id  name                  cq     tq         used_units  used_tokens  credit_pct
55  智汇管家 · DeepSeek   200.0  20000000   5410000     89733        2.7%
```

积分 2.7%、Token 0.45%，都远低于 80%。

**确认它真的在跑**：`workbuddy_low_accounts` 的 `since = 15:50:33`，
而 v1.5.87 重启于 **16:54:05** —— 早于重启且未变，说明 `evaluate` 循环正常，
告警持久化正常。`_safe_alerts` 的失败日志为 0。

这两个观察合起来才构成证据：**「没告警」可能是「不该告警」，也可能是「根本没跑」。**
只看前者会把后者误当成正常。

## 6. 明确不做的

调研里看到但**不做**的，以及原因：

| 能力 | 为什么不做 |
|---|---|
| 语义缓存 | 审计结论；且我们的上游是单一供应商，缓存收益远小于多 provider 场景 |
| 多 provider 聚合 | 产品定位不同 —— 我们是 WorkBuddy 的网关，不是聚合平台 |
| 插件市场 | 审计结论 |
| guardrails / PII 脱敏 | 不是对外公共服务，成本收益不成立 |
| 兑换码 / 在线支付 | 商业能力，不是工程能力 |

**这份「不做」清单比「要做」清单更重要** —— 否则每次调研都会变成「竞品有什么我们就加什么」。

## 7. 待办

| 项 | 状态 |
|---|---|
| v1.5.86 Release | CI 构建中（2/4） |
| 归档首次真实运行 | 约 2026-12-13 |
| age 私钥离线保管 | 运维 |