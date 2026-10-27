# ADR：凭据轮换与双凭据过渡（R3-L5 / RV04 G4-1、G4-2）

- 状态：**已采纳并实现**（2026-10-25，R3-L5）
- 背景：R0 复核 RV04 指出两类问题——(a) QTG→Manager 服务令牌是单文件、改文件+重启，
  且 k17 演练测的是"脚本自建的 read()"而不是真实消费者；(b) 会话密钥单值，换 key
  立即让全部会话失效、没有过渡窗口。缺口编号：`evidence/r3-inventory/gaps.json`
  G4-1（P1，单文件无 .next）、G4-2（P2，会话 secret 无新旧双验）。
- 证据：`evidence/r3-l5/probe-rotation.json`、`evidence/r3-l5/credential-matrix.md`、
  `evidence/r3-l5/README.md`；测试 `qtg/tests/test_credential_rotation.py`、
  `manager/server/tests/test_session_secret_rotation.py`。

## 决策 1：QTG→Manager 服务令牌用 `<path>` + `<path>.next` 双文件

**读序**：`next` 优先、`current` 兜底；**只在 401/403 时**回退（超时/连接失败/5xx
不换令牌——换也不会变好，只会放大故障）。实现：`manager_api.token_candidates()` /
`_request()`，`x-qtg-token-slot` 头只用于可观测性。

**为什么 next 优先**：轮换流程是「stage 写 `.next` → 验证 → promote 落为 `current`
→ Manager 侧撤销旧值」。`next` 出现即代表**已验证的待生效值**，优先使用它，旧令牌
就能在 promote 后立刻撤销，不必等 QTG 重启或等一个"切换时刻"；若 `.next` 是坏值，
401/403 回退保证稳态不丢请求（代价：一次额外往返 + 一条 warning）。

**轮换四阶段**（`tests/linux_lab/rotate_qtg_token.py`）：

1. `stage --file <path> --new-token-file <f> [--verify-cmd ...]`
   原子写 `<path>.next`（临时文件 + `fsync(file)` + `os.replace` + `fsync(dir)`，0600，
   校验失败自动删掉 `.next` 并返回非零）；
2. QTG **无需重启**：下个请求即命中 `.next`（探针实测 P1 阶段 10/10 走 next、0 失败）；
3. `promote --file <path>`：`<path>` → `<path>.prev`，`<path>.next` → `<path>`，
   读回校验失败自动回滚（用 `.prev` 恢复）；
4. 在 Manager 侧吊销旧令牌（tokensvc 删行）→ 旧值 401（探针 P4 实测），消费者已在
   新值上（P3 10/10 成功）。收口后可删 `.prev`。

**风险**：过渡窗口内 Manager 侧同时接受新旧两把令牌 → 旧令牌泄露的影响延长到撤销
为止，因此「stage→promote→撤销旧值」应在一次维护窗口内完成并记录；`.prev` 含旧令牌，
0600 且 promote 完成后可删。

## 决策 2：Manager 会话密钥用 `users.json` 的 `secret_previous` 双验

- 签发**永远**用 `secret`；验签先 `secret`、失败且存在 `secret_previous` 时再试旧值
  （`manager/server/security.py::_cookie_identity`）。
- 过渡窗口：把旧 secret 写进 `secret_previous`、把 `secret` 换成新值，旧 Cookie 继续
  有效；**删除 `secret_previous` 即收口**，旧会话立即失效。
- 为什么不是"必须强制重登"：账号面板是多标签页/长会话场景，强制重登会产生可避免的
  运维中断；双验的额外暴露面与旧 secret 直接泄露相同（同一 0600 文件），只要窗口
  有界即可接受。
- 证据：`manager/server/tests/test_session_secret_rotation.py`（真实 app + 真实 Cookie
  验签路径：旧 Cookie 在窗口内 200、删 `.previous` 后 401、新会话不受影响）。

## 决策 3：QTG 会话密钥用 `<session_secret>.previous` 双验

同决策 2 的模式（`qtg/app/qtg/security.py`）：`issue_session` 用当前 secret，
`verify_session` 先当前、再 `.previous`；删除 `.previous` 收口。
轮换用 `rotate_qtg_token.py rotate-session --file <session_secret> --new-secret-file <f>`。

## 决策 4：QTG 内部 Bearer 双接受（`<path>` + `<path>.next`）

- `CredentialStore.internal_service_tokens()` 返回两个可接受值（next 在前），
  `IdentityResolver.internal_token_ok()` 用常量时间比较逐个匹配；
- 轮换顺序（**R3-L6 演练修正后**）：写 `.next` → 改 Nginx include 为新值 →
  `nginx -t` → `reload` → **把新值原子写为 current（promote）** → 删除 `.next`。
  QTG 侧在 reload 前就同时接受两值，reload 不会产生 401 空窗。
- **演练发现（必须记住的流程缺口）**：只改 include、不更新接受方 current，就直接删
  `.next`，会出现"nginx 发新值、QTG 只接受旧值"的 401 窗口
  （`evidence/r3-l6/nginx-reload.json` 的 `process_finding` 记录了该现象；修正后
  同一演练失败数为 0、旧值收口后 401、新值 200）。这与 QTG→Manager 令牌的
  "stage→promote→撤销旧值"是同一模式：**切换入口之前/之后都要让接受方完成 promote**。
- **本批未执行** nginx reload 的真实演练（需要在 lab/生产 nginx 上改 include +
  reload，属 R3-L6/发布步骤）；QTG 侧双接受已由测试覆盖（旧/新值都通过，删除 `.next`
  后旧值立即失效）。

## 决策 5：上游平台凭据（workbuddy / qoder / trae / aiwork）**本批不实现 `.next`**

| 凭据 | 真实读取点 | 现行为 | 改文件后是否需要重启 | "改文件+重启" vs "带 `.next` 过渡" |
|---|---|---|---|---|
| `WB2API_KEY` / `wb_config_file.api_key` | QTG `workbuddy._credentials()` 每次调用读 config | 每请求读文件（热） | 否（下一次转发即用新值） | 已有热读，**不需要** `.next`；缺的是"旧/新同时可用"（上游是否允许两 key 并行未知） |
| `qoder_password_file` | `adapters/qoder.py:26`（登录时读） | 适配器缓存 `_authenticated`；重新登录才重读 | 通常否，但仅在 token 过期/401 后生效 | 若要求"立即切"，需重启；`.next` 只能解决读取，解决不了上游会话 |
| `qoder2_panel_password_file` | `adapters/qoder2.py:58` | 登录时读 + 缓存 `_panel_token` | 同上 | 同上 |
| `trae_token_file` | `adapters/trae.py:27` | 登录时读 | 同上 | 同上 |
| `aiwork_admin_token_file` / `aiwork_api_key_file` | `adapters/trae2.py:76`、`:356` | 登录时读 / 每次取 key 读 | 部分热 | 同上 |

**理由**：这些是**上游平台凭据**，真正的过渡能力取决于上游是否允许"旧/新 key 并行"
（外部条件，R3-L6 的 fake 上游矩阵 + 最终真实凭据验收）。在本地把 `.next` 加进 QTG
读取层，只能证明"我们读到新值"，不能证明"上游两把都认"，反而会给出"已支持轮换"的
假象。故本批：**代码不加**，矩阵与代价表列入本 ADR；演进方向为 R3-L6 用 fake 上游
（强制只收新 key / 旧 key 401）先证明本方状态机，再由真实引擎验收。

## 未实现 / 边界（不冒充完成）

1. nginx reload 不中断演练未执行（QTG 双接受已实现+测试；nginx include 切换属 lab/生产）。
2. 上游平台凭据 `.next` 未实现（见决策 5）。
3. 真实令牌签发/吊销经由 Manager tokensvc 已由 Manager 测试覆盖；本批探针用 loopback
   假 Manager 控制"accepted 集合"，E4-1 的真实 Manager 端到端版本可作为 R3-L6 补跑。
4. 凭据明文扫描（日志/header/错误回显）属 G4-5，不在本 ADR。
