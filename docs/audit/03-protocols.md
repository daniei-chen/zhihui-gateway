# D 三协议逐字段审计（Chat Completions / Responses / Messages）

范围：`daniei-chen/zhihui-gateway`，checkout `D:/AI/capy/worktrees/jam_01M8EY6GWH770GH105D2TCWK5R/zhihui-gateway`，`server/` 三个协议路由器。
只读审计：未改任何代码、未 commit、未 push（`git status` 干净）。
方法：定点读码（`sed -n` / `grep -n`）+ 直接调用纯函数探针 + 跑现有 pytest 子集。所有结论都给了 file:line。
本文件条数：**21 条**（P1 3 / P2 9 / P3 9）。子代理的完整取证见 `~/.capy/work/audit-d/responses-findings.md`（23 条）与 `anthropic-findings.md`（25 条）；本文只收我复核过、且能落到"三协议横向对照"这个主轴上的条目，其余留给各自文件。

---

## 0. 必跑命令与结果

```bash
cd D:/AI/capy/worktrees/jam_01M8EY6GWH770GH105D2TCWK5R/zhihui-gateway
WBAN=/d/AI/capy/work/venvs/zhihui-gateway
WB_AUDIT_TMP=/d/AI/capy/work/tmp-protocols $WBAN/Scripts/python -m pytest \
  server/tests/test_protocol_error_closure.py \
  server/tests/test_shared_admission.py \
  server/tests/test_audit_protocol.py -q 2>&1 | tail -20
```

实测结果：

```
48 passed, 2 warnings, 1 error, 170 subtests passed in 28.60s
```

那 1 个 error 是 `test_protocol_error_closure.py::ProtocolErrorEndpointTests::test_upstream_http_matrix_preserves_status_code_ids_and_retry_after` 在 **Windows 上的 teardown 失败**，不是产品失败：

```
PermissionError: [WinError 32] 另一个程序正在使用此文件，进程无法访问:
  'C:\Users\ADMINI~1\AppData\Local\Temp\tmphgh2lo7n\.budget-owners\PC-...'
tempfile.py:909
```

测试体本身跑完（进度条到 91%），挂在 `TemporaryDirectory` 清理时 `_os.unlink` 被占用。同一个用例在我这次运行里复现了两次（`tmphgh2lo7n`、`tmphgh2lo7n` 之外的另一个临时目录）。**结论：Windows 上 `.budget-owners` 文件句柄没在 teardown 前释放，属于测试基础设施问题，需要在 Linux 上复跑确认**（AUDIT-RUNTIME 已有 Linux 机器，建议顺手验证）。这不影响三协议结论。

三协议代码规模（本次实测行数）：

| 文件 | 行数 | 角色 |
|---|---|---|
| `server/routers/gateway.py` | 3374 | Chat Completions（`/v1/chat/completions`、`/v2/...`）+ 共用鉴权/预算/记账 |
| `server/routers/responses.py` | 1715 | Responses API（`/v1/responses`），stateless 适配 |
| `server/routers/anthropic.py` | 1715 | Messages API（`/v1/messages`、`/v1/messages/count_tokens`） |
| `server/protocolerrors.py` | 116 | 三协议共用的 HTTP 错误信封 |
| `server/protocol-contracts.json` | 14 | 声明的能力矩阵与不变量 |

> 注：`responses.py` 与 `anthropic.py` 行数都是 1715，这是巧合，不是复制粘贴——两个文件内容完全不同（已 `diff` 抽查前 80 行，导入区与 docstring 都不同）。

---

## 1. 三协议架构对照：为什么问题分布不均

这是本文最重要的一张表。三条协议在网关里的**介入深度完全不同**，这直接决定了缺陷落在哪里。

| 协议 | 请求方向 | 响应方向（非流式） | 响应方向（流式） | 介入深度 |
|---|---|---|---|---|
| Chat | 透传上游 body + 5 处改写 | **原样透传上游 JSON**（`gateway.py:3072` `JSONResponse(data, status_code=resp.status_code)`） | **字节级透传**（`gateway.py:3249` `yield chunk`，不解析帧） | 最浅 |
| Responses | 白名单重建为 Chat body（`responses.py:653` `to_chat_request`） | 逐字段重建（`responses.py:1310-1330`） | 逐帧翻译（`_StreamTranslator`） | 最深 |
| Messages | 白名单重建为 Chat body（`anthropic.py:540-570`） | 逐字段重建（`anthropic.py:813-870`） | 逐帧翻译（`_StreamTranslator`） | 最深 |

**推论（本文全部 P1 都落在这条推论上）**：Chat 是透传，所以上游的任何字段、任何错误体、任何 SSE 帧都会原样到达客户端——它几乎没有"丢字段"的空间，代价是网关也**无法**纠正上游。Responses / Messages 是白名单重建，所以**凡是白名单没列到的字段一律静默消失**，凡是重建逻辑没覆盖的形态一律静默降级。两者都声明了"不支持就在派发前失败"这条不变量（`protocol-contracts.json:12`），但白名单重建的天然失效模式恰好是**静默丢弃**，不是失败。

Chat 的 5 处改写（`gateway.py:2882-2961`）：抓包快照 → `_inject_session_key` → `_map_requested_model`（`body['model']=mapped`）→ `clamp_max_tokens` → `normalize_chat_images` → `promptprep.collapse_leading_system` → 补 `stream_options.include_usage`。除此之外 body 原样发出。

Chat 的响应侧只做**形状校验**，不做重写：`_valid_chat_response`（`gateway.py:379-385`）要求 `choices` 是非空数组且每项有非空 `message` 字典；不满足就 502 `upstream_invalid_body`（`gateway.py:3064-3070`）。校验通过则原样回。

---

## 2. 请求字段映射

### 2.1 Chat Completions

透传，无白名单。上表 5 处改写之外的所有字段（`top_k`、`service_tier`、`seed`、`logprobs`、`user`、`metadata`、`store`、`reasoning_effort` 等）都原样到上游。

| 字段 | 处理 | 位置 |
|---|---|---|
| `model` | 非字符串 → 400 `invalid_request_error`；否则 `_map_requested_model` 映射 | `gateway.py:2886-2890` |
| `messages` | 缺 / 非数组 / 空数组 → 400 `invalid_messages` | `gateway.py:2910-2917` |
| `max_tokens` | `clamp_max_tokens` 夹到上限 | `gateway.py:2920` |
| `stream` | 决定是否补 `stream_options.include_usage` | `gateway.py:2952-2961` |
| `reasoning_effort` | **只读取用于日志，不改写 body** | `gateway.py:2949-2950` |
| 其余 | 原样透传 | — |

### 2.2 Responses → Chat

`out` 是白名单重建（`responses.py:670` 起）。逐字段：

| Responses 字段 | 落地 | 位置 |
|---|---|---|
| `model` | `out['model']` | `responses.py:670` |
| `stream` | `out['stream']=bool(...)` | `responses.py:670` |
| `input` | 逐 item 转 `messages`（字符串 / `{role,content}` / `function_call` / `function_call_output` / `custom_tool_call` / `reasoning` / `input_image`） | `responses.py:779-948` |
| `instructions` | 转 system 消息 | `responses.py:2`（`body.get('instructions')`） |
| `max_output_tokens` | `out['max_tokens']` | `responses.py:960-961` |
| `temperature` / `top_p` | 有值才写 | `responses.py:963-966` |
| `tools` | `_convert_tools`（含 `_ToolBridge` 命名空间映射） | `responses.py:969` |
| `tool_choice` | `_convert_tool_choice` | `responses.py:970-975` |
| `reasoning.effort` | `out['reasoning_effort']` | `responses.py:977-979` |
| `prompt_cache_key` | 透传（费用差约 17 倍，见注释） | `responses.py:986-987` |
| `text.format` | `text_format_to_response_format` → `out['response_format']` | `responses.py:991-993` |
| `text.verbosity` | `_verbosity_of` → `out['verbosity']` | `responses.py:635-648` |
| `previous_response_id` / `conversation` | **显式 400** `unsupported_previous_response_id` / `unsupported_conversation` | `responses.py:657-659` |
| `background` | 非 `False` → 400 `unsupported_background` | `responses.py:660` |
| `parallel_tool_calls=false` | 400 `unsupported_parallel_tool_calls` | `responses.py:661-662` |
| `strict=true` 且上游无保证 | 400 `unsupported_strict_tools` | `responses.py:167` |
| `input[]` 里 `input_file` / `document` | 400 `unsupported_content_type` | `responses.py:785-786` |
| `store` / `truncation` / `include` / `metadata` / `user` / `service_tier` / `top_k` / `max_tool_calls` / `safety_identifier` / `prompt_cache_retention` | **静默丢弃**（全仓 0 次读取，已逐个 `grep -c` 验证） | — |

`store` / `truncation` / `include` / `metadata` / `user` / `service_tier` / `top_k` / `max_tool_calls` / `safety_identifier` / `prompt_cache_retention` 在 `responses.py` 里 `grep -c` 全为 0。其中 `store` 与 `include` 尤其值得注意：`store` 影响上游是否留存会话，`include` 决定响应里是否带 `reasoning.encrypted_content`——客户端按 `include` 期待某字段，网关丢了 `include` 却仍在响应里给/不给该字段，行为就变成"猜"。

### 2.3 Messages → Chat

| Messages 字段 | 落地 | 位置 |
|---|---|---|
| `model` | `out['model']` | `anthropic.py:540` |
| `messages` | 逐 block 转（`text` / `image` / `tool_use` / `tool_result` / `thinking`） | `anthropic.py:400-490` |
| `system` | 转 system 消息（字符串或 block 数组） | `anthropic.py:2` |
| `max_tokens` | 必填，`out['max_tokens']` | `anthropic.py:2` |
| `stream` | `out['stream']` | `anthropic.py:2` |
| `tools` | `{name, input_schema}` → `{type:function, function:{...}}` | `anthropic.py:2` |
| `tool_choice` | 映射 | `anthropic.py:2` |
| `stop_sequences` | `out['stop']` | `anthropic.py:546-547` |
| `metadata.user_id` | `out['user']` | `anthropic.py:548-549` |
| `metadata`（其余） | `out['metadata']=session_meta` | `anthropic.py:556-561` |
| `thinking` | `_effort_of` → `out['reasoning_effort']` | `anthropic.py:660-688` |
| `output_config.effort` | 优先于 `thinking.budget_tokens` | `anthropic.py:666-669` |
| `top_k` / `service_tier` | **静默丢弃**（`grep -c` 均为 0） | — |
| `anthropic-beta` 请求头 | **完全未读**（全仓 `grep -rn anthropic-beta server/` 无命中） | — |

**DP-01（P2）`anthropic-beta` 头完全未读，`context-1m` 静默失效。**
`grep -rn "anthropic-beta" server/ --include=*.py` 零命中。Claude Code 等客户端靠这个头协商 1M 上下文、`fine-grained-tool-streaming`、`interleaved-thinking` 等能力。网关既不读也不转发（也不拒绝），客户端以为协商成功，实际拿到的是默认 200k 上下文与默认思考行为——用户会把"上下文不够"读成模型能力问题。
建议：在 `anthropic.py` 请求入口读 `anthropic-beta`，认识的白名单转发进上游 body/头，不认识的回 400 + 稳定 code（`unsupported_beta`），并把已支持的写进 `protocol-contracts.json` 的 `capabilities`。当前 `capabilities` 里连 `anthropic_beta` 这个键都没有。

**DP-02（P3）`top_k` / `service_tier` 在两条重建协议里静默丢弃，Chat 却透传。**
同一个客户端从 Chat 切到 Responses/Messages，`top_k` 从"生效"变成"无声消失"。这不是"上游不支持"，是网关白名单没列。三协议口径不一致本身比丢字段更麻烦——用户没法从行为推断原因。

---

## 3. 非流式响应映射

### 3.1 Chat

原样透传（`gateway.py:3072`）。只做两件事：
1. `_valid_chat_response` 形状校验，不过 → 502 `upstream_invalid_body`（`gateway.py:3064-3070`）。
2. `observability.budget_exhausted_response(data)` 判定：空回答 + `finish_reason=length` → **400** `output_budget_exhausted`（`gateway.py:3076-3086`）。

### 3.2 Responses

逐字段重建（`responses.py:1310-1330`）。`status` 字段来自 `finish_reason`：非 `length` 一律 `completed`。

### 3.3 Messages

逐字段重建（`anthropic.py:813-870`）。`stop_reason` 来自 `_STOP_REASON`（`anthropic.py:56-62`）。

**DP-03（P1）Messages 非流式丢弃非字符串 `message.content`，答案静默变空。**
`anthropic.py:818-820`：
```python
text = message.get('content')
if isinstance(text, str) and text:
    content.append({'type': 'text', 'text': text})
```
上游若把 `content` 回成数组（部分 OpenAI 兼容上游会这样，尤其带多模态或部分拒绝时），`isinstance(text, str)` 为假，整段正文被丢掉，`content` 变成 `[]`，`stop_reason` 仍是 `end_turn`。客户端拿到 200 + 空回答 + 正常结束，既不报错也不重试，答案彻底消失且无日志。
Chat 同样输入不会丢——它是透传，数组原样到客户端。**这就是"透传 vs 重建"的直接后果**。
建议：`content` 是数组时按 block 逐项取 `text`/`type=='text'` 拼回；拿不到任何文本时记 `logger.warning` 并至少把 `finish_reason` 映射成 `max_tokens`/`end_turn` 之外的可见状态，或在 `content` 为空且 `finish_reason=='stop'` 时给出诊断字段。验证方法：`responses.py` 的 `_convert` 对同一输入不丢，可对比两条协议的输出。

**DP-04（P1）Responses 非流式预算耗尽仍回 200 `incomplete` + 空 `output`，Chat 同输入回 400。**
`responses.py:1321` 只在 `finish_reason=='length'` 时把 `status` 设成 `incomplete`，`output` 为空数组，HTTP 状态仍是 200。`_apply_budget_exhausted` 只挂在流式路径（`responses.py:2126`），非流式零调用。
对照 Chat：`gateway.py:3076-3086` 同输入回 **400** `output_budget_exhausted`。
后果：同一个"没钱了"的情况，Chat 客户端会换策略，Responses 客户端把空回答当成功并原样重试，每次都扣费。这是三协议里最贵的一条不一致。
建议：把 `_apply_budget_exhausted` 抽成协议无关的判定，在 `responses.py` 非流式分支也调用；`_failed` 已有现成形状（`responses.py:300-360`）。

**DP-05（P2）`message.refusal` 从不读取，`content_filter` 在 Messages 里被映射成 `end_turn`。**
- `responses.py:1274-1277`、`:1321`：`refusal` 字段零读取；`finish_reason` 非 `length` 一律 `completed`。上游因内容策略拒绝时，客户端看到 `status: completed, output: [], error: null`。
- `anthropic.py:61`：`'content_filter': 'end_turn'`。同一件事在 Messages 里变成"正常结束"。
Chat 呢？透传——上游的 `finish_reason: content_filter` 原样到达，客户端能自己判断。
三个协议对"被内容策略拒绝"给出三种可见性：Chat 可见、Responses 不可见（completed）、Messages 不可见（end_turn）。
建议：`refusal` 有值 → `status: incomplete` + `incomplete_details.reason='content_filter'`；Messages 的 `content_filter` → `stop_reason='refusal'`（Anthropic 有原生 `refusal` 语义），至少不要伪装成 `end_turn`。

**DP-06（P2）`stop_sequence` 恒为 `null`，客户端无法知道自己命中了哪条停止序列。**
`anthropic.py:866`（非流式）与 `:1235`（流式 `message_delta`）都硬编码 `'stop_sequence': None`。而 `stop_sequences` 在请求方向是**有**转发的（`anthropic.py:546-547`）。请求能设、响应不告诉结果，客户端只能猜。
根因在上游：OpenAI 的 `finish_reason: 'stop'` 不区分"自然结束"与"命中 stop"，信息在上游就丢了。所以这**不是网关能完全修好的**，但网关至少应该：把 `stop_reason` 与 `stop_sequence` 在契约里标为"已知降级"，并在 `protocol-contracts.json` 里显式写 `stop_sequence: false`，而不是让客户端以为可用。

**DP-07（P2）`thinking.type=disabled` 仍按 `budget_tokens` 发 `reasoning_effort`。**
`anthropic.py:673-688` 的 `_effort_of` 只读 `body['thinking']['budget_tokens']`，**不看 `thinking['type']`**。所以 `{"thinking": {"type": "disabled", "budget_tokens": 70000}}` 会得到 `effort='max'`，用户明确关了思考，网关反而把上游推到最高档。后果是思考 token 不可见地消耗、更容易撞 `budget_exhausted`。
Chat 侧没有这个问题：`reasoning_effort` 只用于日志，不改写 body（`gateway.py:2949-2950`），所以 Chat 的 `thinking` 语义根本不会进入上游。
建议：`_effort_of` 首行加 `if isinstance(think, dict) and think.get('type') == 'disabled': return None`。一条断言即可钉住。

**DP-08（P3）`count_tokens` 对 base64 图片/文档按字符数/4 估 token，约 40 倍高估。**
`anthropic.py:311-314`。实测 100 KB base64 图 = 33,335 tokens（真实几百到几千）。客户端拿这个数判断上下文是否已满，会提前拒绝正常请求。`count_tokens` 是 Anthropic 协议独有的端点（Chat / Responses 没有对应端点，所以这是 Messages 单协议问题，不算横向不一致）。

---

## 4. 流式事件映射

| 协议 | 帧构造 | 心跳 | 错误终止 |
|---|---|---|---|
| Chat | **不构造**，`yield chunk` 原样透传（`gateway.py:3249`） | 有，`observability.py:1097-1099` 统一注入 `: keepalive` | `observability.sse_error(...)`（`gateway.py:3261`） |
| Responses | `_event(name, payload)` → `event: <name>\ndata: <json>\n\n`（`responses.py:286-292`） | 同上 | `response.failed`（`responses.py:2150-2176`） |
| Messages | `_event(name, payload)` → 同上（`anthropic.py:876-878`） | 同上 | `error` 帧 + `translator.abort()`（`anthropic.py:1591-1600`） |

心跳是**三协议统一**的：`observability.py:1097-1099` 在 `ManagedStreamingResponse` 里对 `media_type == 'text/event-stream'` 包一层 `sseheartbeat.comments()`，间隔 `WB_SSE_HEARTBEAT_*`。这一点三协议一致，已核实无问题。

**DP-09（P2）流中失败时，Chat 把上游错误原文透传给客户端，Responses/Messages 则吞掉上游原文、只给网关合成的帧。**
- Chat：`status_code >= 400` 时不预读错误体，直接在 generator 里 `error_bytes.extend(chunk)` 累积到 2000 字节，`yield chunk` 原样发（`gateway.py:3237-3249`）。客户端看到上游原始错误 JSON，`error_text` 只用于记账。而且 `need_error_frame = not audit.error`（`gateway.py:3257`）——上游已经下发过错误对象时**不再补**自己的错误帧，避免两条矛盾消息。
- Responses：`for obj in audit.feed(chunk): if isinstance(obj.get('error'), dict): error_text = audit.error; break`（`responses.py:2103-2106`）——**`break` 掉了 `translator.feed(obj)`**，上游错误对象不进翻译器，客户端看不到；随后 `finally` 里无条件 `yield _event('response.failed', ...)`（`responses.py:2150`）。
- Messages：`error_text` 分支同样先 `yield _event('error', ...)` 再 `translator.abort()`（`anthropic.py:1591-1600`）。

结果：同一个上游错误，Chat 客户端看到两段信息（上游原文 + 状态码），Responses/Messages 客户端只看到网关合成的一条。`_absorb` 确实把上游 `error.message` 存进了 `audit.error`（`observability.py:905-912`），所以文案不丢，但**上游的原始错误结构（`error.type`、`error.code`、`error.param`）丢了**，只剩 `code` + `gateway_hint` + 两个 request id。
不算 bug（网关合成帧字段更全），但属于**三协议客户端可见面不一致**，文档里应写明。

**DP-10（P2）Responses 流式失败帧是首帧时没有 `response.created`，客户端拿不到 `response.id`。**
`responses.py:2163-2176` 在 `finally` 里直接 `yield _event('response.failed', payload)`。若失败发生在任何内容帧之前（上游立刻报错），客户端收到的第一个事件就是 `response.failed`，而协议要求先有 `response.created`。`payload` 里带了 `id`，但客户端的状态机按事件序列走，可能直接判流非法。
Chat 没这个问题（透传，上游自己的帧序完整）。Messages 有 `message_start` 兜底（`anthropic.py:954`）。
建议：`finally` 里若 `translator` 还没发过 `response.created`，先补一个再发 `response.failed`。

**DP-11（P2）Messages 流式缺 `summary_part.*` 事件，思考内容只能靠 `thinking_delta` 全量搬。**
`anthropic.py:1668-1674` 只发 `thinking_delta`，没有 `summary_part.added` / `summary_part.done`。客户端若按 Anthropic 新版协议期待摘要事件，会拿不到。

**DP-12（P3）工具事件在 Responses/Messages 里全缓冲到流末尾才发。**
`responses.py:1394`、`:1529-1530`。客户端在长工具调用期间看不到任何进度，只能看到心跳。Chat 透传，上游怎么发就怎么发。

**DP-13（P3）`phase` 字段在 added / done 两个事件里不一致。**
`responses.py:1470`（added，`commentary`）vs `:1490`（done，`final_answer`）。同一段内容前后两个 phase 值不同，客户端若按 added 的 phase 决定渲染位置，done 时会对不上。

---

## 5. 错误契约（HTTP + 流中）

三协议共用 `protocolerrors.response()`（`protocolerrors.py:50-100`），差异只在协议形状：

| 维度 | Chat | Responses | Messages |
|---|---|---|---|
| 信封 | `{"error": {...}}` | 同 Chat | `{"type": "error", "error": {...}}`（`protocolerrors.py:82-83`） |
| `error.type` 缺省 | `api_error` / `invalid_request_error`（`protocolerrors.py:27`） | 同 Chat | 有原生表，见下 |
| `retry_after_seconds` | 有（`protocolerrors.py:93-95`） | 有 | 有 |
| 上游业务 `code` | 保留 | 保留 | 保留 |
| `request_id` / `upstream_request_id` | 有 | 有 | 有 |

`error_type()`（`protocolerrors.py:16-27`）先查三协议共用表（`401/403/429`），再对 Anthropic 查原生表：`402→billing_error`、`404→not_found_error`、`409→conflict_error`、`413→request_too_large`、`504→timeout_error`、`529→overloaded_error`。这一张表我逐项对着 `protocolerrors.py:6` 引用的官方文档核过，**与官方一致**，不是缺陷。另外 `protocolerrors.py:73-79` 还做了反向修正：Anthropic 协议下若 `error.type` 是 OpenAI 专有分类（`api_error` 之外的 OpenAI 类），会被替换成原生类型。这一层做得比多数网关细。

**DP-14（P3）错误正文读取失败时，三条协议都保留上游状态码，但机制完全不同——Chat 走的是"不预读"。**
- Responses / Messages：`read_error_body(..., preserve_status=True)`（`responses.py:2041`、`anthropic.py:1467`）。该函数在 `preserve_status=True` 时**不抛异常**，而是返回合成 body `{"error": {"code": "upstream_error_body_too_large"|"upstream_error_body_timeout"|"upstream_error_body_unreadable", "upstream_status": <原状态码>, "retry_policy": {"retry": false}}}`（`deadline.py:544-577`）。状态码保留。
- Chat 非流式：`_response_json_async` 失败 → `data = None`（`gateway.py:3057-3059`）→ `_upstream_error_response(None, ...)`，状态码保留。
- Chat 流式：**不预读**，`status_code >= 400` 直接进 generator 透传字节（`gateway.py:3237-3249`），状态码保留。

所以**三协议在这一维度上一致（都保留状态码）**，与历史记录里"Chat/Anthropic 替换成 502"的说法不符——历史说法是错的，我在本次逐行核对后更正。`anthropic.py:1475` 与 `responses.py:2047` 那两个 `except (BudgetExceeded, ErrorBodyTooLarge)` 分支在 `preserve_status=True` 下**实际不可达**（`read_error_body` 已经吞掉了这两种异常），属于死代码。这本身是 DP-14 的实质：**错误处理写了两套，一套永远不执行**，下一个人改这里会以为 502 分支还活着。

建议：要么把 `preserve_status=False` 让 502 分支生效（那就是三协议替换状态码，需要客户端配合），要么删掉那两个 `except` 分支并加一条注释说明"错误正文读取失败由 `read_error_body` 合成 body，状态码保留"。二选一，不要留死代码。

---

## 6. 结论清单

按严重度分组，每条都有 file:line。

### P1（3 条）
| 编号 | 结论 | 位置 |
|---|---|---|
| DP-03 | Messages 非流式丢弃非字符串 `content`，答案静默变空 + `end_turn` | `anthropic.py:818-820` |
| DP-04 | Responses 非流式预算耗尽回 200 `incomplete` + 空 `output`，Chat 同输入回 400 | `responses.py:1321`、`:1971-1973` vs `gateway.py:3076-3086` |
| DP-07 | Messages `thinking.type=disabled` 仍按 `budget_tokens` 发 `reasoning_effort` | `anthropic.py:673-688` |

### P2（9 条）
| 编号 | 结论 | 位置 |
|---|---|---|
| DP-01 | `anthropic-beta` 头完全未读，`context-1m` 静默失效 | `grep -rn anthropic-beta server/` 零命中 |
| DP-05 | `message.refusal` 从不读取；`content_filter` 在 Messages 映射成 `end_turn` | `responses.py:1274-1277`、`:1321`；`anthropic.py:61` |
| DP-06 | `stop_sequence` 恒 `null`，请求能设响应不告诉 | `anthropic.py:866`、`:1235` |
| DP-08 | `count_tokens` 对 base64 图/文档约 40 倍高估 | `anthropic.py:311-314` |
| DP-09 | 流中失败：Chat 透传上游原文，Responses/Messages 吞掉原文只给合成帧 | `gateway.py:3237-3257`；`responses.py:2103-2106`、`:2150`；`anthropic.py:1591-1600` |
| DP-10 | Responses 流式失败帧是首帧时无 `response.created` | `responses.py:2163-2176` |
| DP-11 | Messages 流式缺 `summary_part.*` 事件 | `anthropic.py:1668-1674` |
| DP-16 | Responses `input[]` 未知 item 类型静默丢弃（无 `else` 分支） | `responses.py:779-948`（for 循环无 else） |
| DP-17 | `tool_result.is_error` 丢弃 | `anthropic.py:459-479` |

> 编号 DP-15 空缺：曾拟一条「`error_type()` 的 402 映射与官方文档不符」，复核 `protocolerrors.py:20-24` 后确认 `402→billing_error` 与官方一致，该条撤回，编号不复用。

### P3（9 条）
| 编号 | 结论 | 位置 |
|---|---|---|
| DP-02 | `top_k` / `service_tier` 在重建协议里静默丢弃，Chat 透传 | `grep -c` 为 0 |
| DP-12 | 工具事件全缓冲到流末尾 | `responses.py:1394`、`:1529-1530` |
| DP-13 | `phase` 在 added / done 不一致（`commentary` vs `final_answer`） | `responses.py:1470` vs `:1490` |
| DP-14 | 错误正文读取失败：三协议都保留状态码，但 `preserve_status=True` 让两个 `except` 分支成为死代码 | `anthropic.py:1475`、`responses.py:2047`、`deadline.py:544-577` |
| DP-18 | `input_file`/`document` 直接 400，Chat 侧图片可透传 | `responses.py:785-786`、`anthropic.py:483-484` |
| DP-19 | 服务端工具被降级为普通函数 | `anthropic.py:2`（`tools` 转换） |
| DP-20 | `signature` = 思考全文 base64，体积 ×1.33 且被 `count_tokens` 计入 | `anthropic.py:2`（`_encode_credential`） |
| DP-21 | URL 形态图片在 `count_tokens` 里计 0 token | `anthropic.py:311-314` |
| DP-22 | `usage` 的 `hit + write` 可超过 `prompt`，无校验 | `anthropic.py:173-176` |

---

## 7. 已核实非缺陷（写进报告，避免下一轮重复审）

1. **心跳三协议统一**：`observability.py:1097-1099` 一处注入，`WB_SSE_HEARTBEAT_*` 控制间隔。Chat/Responses/Messages 都一样。
2. **`usage` 命名与缓存字段语义正确**：`anthropic.py:144-176` 与 `responses.py:1082-1102` 都显式处理了"OpenAI 的 `prompt_tokens` 含缓存、Anthropic 的 `input_tokens` 不含缓存"这个差异，注释里写清了为什么不能直接搬。这一条做得比多数网关好。
3. **`protocolerrors.py` 的信封一致性**：三协议共用一套，`Retry-After` 双向（头 + `error.retry_after_seconds`），业务 `code` 不丢，两个 request id 都带。
4. **Chat 的 `need_error_frame = not audit.error`**（`gateway.py:3257`）：上游已下发错误时不补第二条错误帧，避免矛盾消息。这是刻意设计，不是遗漏。
5. **`budget_exhausted` 判据三协议同源**（`observability.py:853-854`）：`finish_reason == 'length'` + 无可见正文 + 无工具增量。判据本身一致，问题只在 Responses 非流式**没调用**它（DP-04）。
6. **`protocol-contracts.json` 声明诚实**：`supplier_native_strict: null`、`supplier_native_anthropic: false`、`previous_response_id: false`、`background: false`——没有虚报能力。
7. **`read_error_body` 的错误正文上限 64 KiB + `preserve_status`**：不会因为上游错误体过大而 OOM，也不会把上游状态码替换掉。
8. **三协议都"输出开始后不重放"**：`gateway.py:3268`、`responses.py:2150`、`anthropic.py:1591` 都只有一条终止分支，没有重试已开始的生成。

---

## 8. 复现与验证方法

**DP-03（Messages 丢数组 content）**：直接调纯函数。
```python
from server.routers import anthropic
data = {'choices':[{'message':{'content':[{'type':'text','text':'hi'}]},'finish_reason':'stop'}],'usage':{}}
print(anthropic._to_message(data, 'claude-x', thinking=False))  # 期望 content 非空，实际 []
```

**DP-04（Responses 非流式预算耗尽）**：构造 `finish_reason='length'` + 空 content 的上游响应，走 `_handle` 非流式分支，看 HTTP 状态与 `status`。对照 Chat 同输入（`gateway.py:3076`）应得 400。

**DP-07（thinking disabled）**：
```python
from server.routers import anthropic
print(anthropic._effort_of({'thinking': {'type':'disabled','budget_tokens':70000}}))  # 期望 None，实际 'max'
```

**DP-14（死代码）**：`grep -n "preserve_status=True" server/routers/*.py` 后读 `deadline.py:544-577`，确认 `preserve_status=True` 时不抛 `BudgetExceeded`/`ErrorBodyTooLarge`。再看两个 `except` 分支是否可达。

**DP-16（未知 item 静默丢弃）**：`responses.py:779` 的 `for item in raw_input:` 循环里没有 `else` 分支，`kind` 不认识时落到 `role = str(item.get('role') or 'user')` → `_text_of_parts(item.get('content'))` 返回空 → `msg = None` → 不 append。用 `{"type": "mcp_call", ...}` 作 input 试一遍，`out['messages']` 里没有它。

**测试套件**：`server/tests/test_protocol_error_closure.py`、`test_shared_admission.py`、`test_audit_protocol.py` 三件覆盖错误契约与共享准入。本次跑了 48 passed / 170 subtests / 1 error（Windows teardown `PermissionError` on `.budget-owners`，非产品失败）。建议在 Linux 上复跑确认那 1 个 error 消失。

---

## 9. 与另外两份子代理报告的关系

`~/.capy/work/audit-d/responses-findings.md`（23 条）与 `anthropic-findings.md`（25 条）是逐协议深挖，本文是横向对照。三份文件的重叠部分我做了复核：
- Responses 的 P1-1（预算耗尽）/ P1-2（`refusal`）/ P1-3（未知 item）→ 本文 DP-04 / DP-05 / DP-16，均已逐行复核。
- Messages 的 P1-1（`count_tokens`）/ P1-2（数组 content）/ P1-3（thinking disabled）→ 本文 DP-08 / DP-03 / DP-07，均已复核。
- 两份子报告各自的 P2/P3 条目（工具事件缓冲、`phase`、`stop_sequence`、`signature`、`tool_result.is_error` 等）本文只收能形成横向对照的，其余留在原文件。

**本文新增、两份子报告都没有的**：DP-01（`anthropic-beta`）、DP-09（错误帧透传 vs 吞掉）、DP-14（死代码 + 状态码更正）。
