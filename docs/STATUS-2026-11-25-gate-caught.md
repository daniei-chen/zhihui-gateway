# 2026-11-25 门禁拦住了一次破坏契约的修复（第 29 轮）

## 1. 做了什么

第 28 轮定位到 `qtgcore/errors.py` 的 `install_handlers()` 从未被调用，导致
`QtgError` 及五个子类全被兜成 500。本轮按计划修：在 `main.py` 创建 app 之后注册。

改动 3 行 + 注释，跑门禁 —— **红了**。

## 2. 红了什么

```
FAIL: test_bad_tokens_do_not_leak_reason
  File "server/tests/test_api_tokens.py", line 179
    self.assertEqual(r.json().get('detail'), '未登录', '不得泄露失败原因')
  AssertionError: None != '未登录' : 不得泄露失败原因

ERROR: test_session_still_allowed_on_session_only_endpoint
  KeyError: 'detail'

FAIL: test_admin_token_rejected_on_session_only_endpoints
FAIL: test_managed_short_password_and_root_weak_password_are_rejected
  AssertionError: '12' not found in ''

FAILED (failures=3, errors=1, skipped=32)
```

**四个测试，全部因为同一个原因：响应体格式变了。**

## 3. 真正的原因：两套错误格式不兼容

`QtgError.to_body()`（`errors.py:33`）返回：

```python
{"error": {"code": self.code, "message": self.message, "type": self.type}}
```

而 FastAPI／Starlette 的默认错误体是：

```python
{"detail": "..."}
```

全局注册 `install_handlers(app)` 等于**把所有 `QtgError` 的响应体从后一种换成前一种**。

现有代码依赖后一种：

- `test_api_tokens` 断言 `r.json().get('detail') == '未登录'`
- 测试直接 `r.json()['detail']` → `KeyError`
- 其他测试断言状态码内含特定文本

**所以这不是「补一行调用」，是「换一套错误契约」。**

## 4. 第 28 轮的判断哪里不准

我当时写「修法只有一行」，并评估影响面为「状态码从 500 变成 400/401/403/404/502」。

**漏掉了响应体格式这一层。** 状态码变化是`>=500` 区间内的移动，前端区间判断不受影响；
但响应体从 `detail` 换成 `error` 是**结构变化**，任何按字段名取值的地方都会断。

**判断改动影响面时，只看了状态码，没看 body 结构。** 这是这轮真正的教训 ——
比「哪一行没调」更值得记。

## 5. 已回滚

```bash
git reset --hard 23198cf    # 回到上一轮已推送的 commit
```

验证：

```
install_qtg_error_handlers 出现次数 = 0
工作区干净（只剩 6 个未跟踪的 tar 包）
```

**没有推送过任何东西，线上没动过。**门禁在本地就拦住了。

## 6. 如果将来要修，正确顺序是什么

不是注册处理器，而是先统一错误格式。三条路，都要先定契约：

**A. 改 `to_body()` 输出 `detail`**

```python
def to_body(self) -> dict:
    return {"detail": self.message, "code": self.code, "type": self.type}
```

- 优点：向后兼容，`detail` 仍存在
- 缺点：`code`/`type` 平铺在顶层，与 OpenAI 风格的嵌套 `error` 不一致

**B. 保留两套，在中间件里按 Accept 或路径决定**

- 优点：不破坏任何现有调用
- 缺点：两套并存，长期更难维护

**C. 不动 `to_body()`，只把 `UpstreamError` 等少数几个改用 `HTTPException`**

- 优点：最小侵入
- 缺点：治标，`QtgError` 的翻译器仍然闲置

**无论选哪条，都必须先做一件事：把「控制台前端与测试依赖哪些错误字段」列全。**
这轮只看到 `detail` 一处就动手，是方法错误。

## 7. 本轮产出

- 尝试修复并**被门禁拦下**
- 定位到真正障碍：两套错误体结构不兼容
- 完整回滚，线上零影响
- 记下选择路径与前置条件

**这不是失败的一轮。**规范 §2.3 写着「不要用先发后修绕过门禁」——
门禁在这个场景里正好证明了它的价值：如果按「先发后修」推上去，
四个测试覆盖的接口会立刻对控制台返回结构不同的错误体。

## 8. 当前状态

| 项 | 值 |
|---|---|
| 线上版本 | `v1.5.75`（未变） |
| 仓库 HEAD | `23198cf`（已推送） |
| 门禁（回滚后） | 应恢复全绿，下轮确认 |
| 未跟踪文件 | 6 个 tar 包（刻意不提交） |