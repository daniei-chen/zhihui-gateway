# 上游引擎的能力边界（实测）

这份文档记录**实际测出来的**行为，不是协议文档的转述。

测量时间：2026-11-15，直连 `trae` 引擎（`127.0.0.1:8080`，模型 `Doubao-Seed-Code`），
**绕过网关** —— 所以下面每一条都是引擎本身的行为，不是网关的问题。

---

## 一、结论速览

| 参数 | 是否守约 | 实测 |
| --- | --- | --- |
| `stream` + `usage` | **是** | 流末尾带 usage，并有 `[DONE]` |
| `max_tokens` | **否** | 请求 80，实际生成 **560** |
| `max_completion_tokens` | **部分** | 请求 80，两次分别是 **185** 与 **338** |
| `stop` | **否** | 要求「遇到 5 停止」，输出里照常含 5 并数到 20 |
| `temperature` | **否** | 设为 0，两次调用结果不同（68 / 81） |

**一句话**：这个引擎基本忽略采样与控制类参数，只保证「能生成」。

---

## 二、逐项说明

### 2.1 `max_tokens` —— 不生效

```
请求：{"max_tokens": 80}
实际：completion_tokens = 560
```

改成 `max_completion_tokens` 好一些，但仍不准：同一组参数两次分别得到 185 与 338。

**影响**：客户端按「我要一个 80 token 的短回答」估算成本会偏差 3～7 倍，延迟也跟着变。

### 2.2 `stop` —— 不生效

```
请求：{"stop": ["5"]}
要求：从 1 数到 20，遇到 5 就停
实际：1,2,3,4,5,6,...,20        ← 5 照常出现，且没有停
```

`finish_reason` 仍是 `"stop"` —— 也就是说**成功码不能作为「遵守了停止条件」的证据**。

### 2.3 `temperature` —— 不生效

```
请求：{"temperature": 0}   ×2
实际：68 / 81               ← 两次不同
```

`temperature: 0` 通常意味着「尽量确定性」，这里没有体现。

**影响**：任何依赖可复现性的用法（评测、回归比对、测试基线）在这里都不成立。

### 2.4 流式 usage —— 正常

```
事件数：31
含 "usage"：是
含 [DONE]：是
```

这一项是好的：**计费所需的用量能拿到**。

---

## 三、网关做了什么

**网关把这些参数原样透传给引擎** —— 它不做改写，所以引擎不守约时网关也拿不到
「引擎实际用了多少」。

网关侧能做的已经做了：

| 项 | 状态 |
| --- | --- |
| 观测「实际输出超过请求上限」 | 已有计数器 `workbuddy_output_over_limit_total` |
| 超时与预算保护 | 已有（不会因为引擎生成过长而无限等） |
| 计费 | 按**上游回的实际用量**算，不按请求值算 —— 所以不会少算 |

**网关不做的**（以及为什么）：

| 做法 | 为什么不做 |
| --- | --- |
| 网关侧截断流来「实现」`max_tokens` | token 在引擎侧已经生成、已经计费，截断只省客户端等待，却给出残缺回答 |
| 网关侧模拟 `stop` | 同上，且要改写 SSE 流，风险远大于收益 |
| 改写 `temperature` | 无从改起 —— 引擎不读这个字段 |

---

## 四、怎么绕过这些限制

| 想要的效果 | 可行做法 |
| --- | --- |
| 控制回答长度 | 在**提示词**里明确要求字数；`max_tokens` 不可依赖 |
| 遇到特定内容就停 | **客户端侧**检查收到的内容，自己断开 |
| 可复现的输出 | 这里做不到。需要可复现性请换平台 |
| 估算成本 | 按历史平均 token 数估，别按 `max_tokens` 估 |

**推断而不是保证**：上述绕法都是「客户端多做一步」，网关不会替你做。

---

## 五、怎么复核

```bash
KEY=$(cat /opt/workbuddy-manager/engine/secrets/aiwork_api_key)

# max_tokens
curl -s -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"Doubao-Seed-Code","max_tokens":80,
       "messages":[{"role":"user","content":"说一句话"}]}' \
  | grep -o '"completion_tokens":[0-9]*'

# stop
curl -s -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"Doubao-Seed-Code","stop":["5"],
       "messages":[{"role":"user","content":"从1数到20，只输出数字"}]}' \
  | grep -o '"content":"[^"]*"'

# temperature（跑两次对比）
for i in 1 2; do
  curl -s -X POST http://127.0.0.1:8080/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d '{"model":"Doubao-Seed-Code","temperature":0,
         "messages":[{"role":"user","content":"只输出一个 1-100 的随机整数"}]}' \
    | grep -o '"content":"[^"]*"'
done
```

**注意**：这是**引擎侧**的行为。要改这些，得改引擎，不是网关。

---

## 六、维护提示

引擎升级后重跑一遍上面三条 —— 本文的结论是**某一版本的行为**，不是永久事实。

如果哪天 `max_tokens` 开始生效了，记得把 `workbuddy_output_over_limit_total` 的
说明一并更新：那个计数器存在的理由就是「引擎不守约」。
