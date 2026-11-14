# 内置模型池：可用模型与能力边界

本文记录**实测**结果，不是对照厂商文档抄的。每一项都给了验证方式和时间。

最后更新：2026-11-14

---

## 一、结论速览

| 能力 | 状态 |
| --- | --- |
| 豆包纯文本（对话） | ✅ 可用 |
| 豆包流式输出 | ✅ 可用 |
| 豆包推理内容（thinking） | ✅ 可用 |
| **豆包图片输入（多模态）** | ❌ **不可用** |
| 其他厂商模型（DeepSeek / GLM / Kimi / Qwen / MiniMax / Step / MiMo） | ✅ 可用 |

---

## 二、豆包模型清单

引擎的模型列表中带以下四个豆包模型，状态均为启用：

| 模型名 | 上下文 | 声明多模态 | 实测纯文本 | 实测图片 |
| --- | --- | --- | --- | --- |
| `Doubao-Seed-2.1-Pro` | 25.6 万 | 是 | ✅ 200 | ❌ 503 |
| `Doubao-Seed-2.1-Turbo` | 25.6 万 | 是 | ✅ 200 | 未单独测 |
| `Doubao-Seed-Code` | 25.6 万 | 是 | ✅ 200（含流式） | ❌ 503 |
| `Doubao-Seed-Evolving` | 25.6 万 | 是 | 未测 | 未测 |

**注意**：模型声明「支持图片」与实际能否传图是两件事。实测带图请求失败，原因码见下节。

---

## 三、怎么调用

### 3.1 模型名要带平台前缀

```
trae/Doubao-Seed-Code
trae/Doubao-Seed-2.1-Turbo
trae/Doubao-Seed-2.1-Pro
```

不带前缀时，网关按「模型目录匹配 → 余额排序 → 单平台兜底」选路，不保证落到能跑豆包的平台。**要稳定调用就带前缀。**

### 3.2 端点与鉴权

```
POST /v1/chat/completions
Authorization: Bearer <密钥>
```

密钥的 `platforms` 字段必须包含 `trae`，否则会被拒（平台无权访问）。

### 3.3 请求示例

```json
{
  "model": "trae/Doubao-Seed-Code",
  "messages": [{"role": "user", "content": "用三句话解释什么是反向代理"}],
  "max_tokens": 256,
  "stream": true
}
```

---

## 四、能力边界（实测）

### 4.1 纯文本 —— 可用

```bash
curl -s -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer <key>" -H 'Content-Type: application/json' \
  -d '{"model":"Doubao-Seed-2.1-Pro","messages":[{"role":"user","content":"say OK"}],"max_tokens":8}'
```

返回 `200`，内容为 `OK`。

### 4.2 流式 —— 可用

同一请求加 `"stream": true`，返回标准 SSE 分片，实测一次调用产生 **47 个事件**。

分片结构与 OpenAI 一致，`delta` 中可同时出现 `reasoning_content`（推理过程）与 `content`（最终回答）。

### 4.3 推理内容 —— 可用

响应里带 `reasoning_content` 字段，内容为模型思考过程。对需要展示推理链的场景可直接使用。

### 4.4 图片输入 —— 不可用

传 `image_url` 类型的内容块会失败，两种不同模型给出两种错误：

| 模型 | 错误 |
| --- | --- |
| `Doubao-Seed-Code` | `503`，`404 page not found` |
| `Doubao-Seed-2.1-Pro` | `503`，`no_healthy_account` |

两种错误含义不同，但都不是「参数写错」：

- `404 page not found` 表示上游那条路径不存在；
- `no_healthy_account` 表示没有能处理该请求的账号。

**结论：目前不要指望豆包做图片理解。** 需要多模态时，用其他声明支持的模型，或先验证再上。

---

## 五、验证方式（可复现）

所有结论都可以用下面两条命令复核。

```bash
# 1. 看引擎侧的模型清单（含每个模型的来源池与启用状态）
K=$(cat /opt/workbuddy-manager/engine/secrets/aiwork_api_key)
curl -s -H "Authorization: Bearer $K" http://127.0.0.1:8080/v1/models

# 2. 直接打一次豆包请求
curl -s -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"model":"Doubao-Seed-Code","messages":[{"role":"user","content":"say OK"}],"max_tokens":8}'
```

经网关的验证记录可在请求日志里按模型名检索到（2026-11-08 有一次成功的
`Doubao-Seed-2.1-Turbo` 调用，平台与实际平台均为 trae，状态 200）。

---

## 六、一个说明：这些模型走哪个池

模型清单里每个模型都带来源池标记。豆包四个模型的来源池都是 `trae`，**不需要单独的豆包凭据**。

引擎内部按 `trae` / `wb` / `doubao` 三个桶统计用量 —— 这是**用量记账的分组**，不是模型归属。不要把它当成「豆包是独立平台」的证据。

---

## 七、维护提示

本文的「不可用」判断基于 2026-11-14 的实测。上游随时可能补齐图片链路。

**重新验证的方法**：按第五节第二条命令，把 `messages` 换成带 `image_url` 的形式，看是否仍返回 503。若返回 200，请更新本文。
