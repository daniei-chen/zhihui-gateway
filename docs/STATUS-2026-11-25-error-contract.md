# v1.5.81：错误响应契约不一致（第 45 轮）

## 1. 怎么发现的

上一轮结尾定了个方向：查后端 API 的错误契约是否一致 —— 同一个错误在不同端点是否返回同样的结构。

直接 curl 四种错误：

| 状态 | 响应 |
|---|---|
| **404**（不存在的路由） | `{"detail":"not found"}` |
| 405 | `{"detail":"Method Not Allowed","error":{"code":"http_error",...}}` |
| 401 | `{"detail":"未登录","error":{"code":"unauthorized",...}}` |
| 422 | `{"detail":[...],"error":{"code":"validation_error",...}}` |

**404 只有 `detail`，没有 `error`。**

## 2. 先分清「设计」还是「遗漏」

第 30 轮给三个处理器都补了 `error` 对象（`QtgError`、`RequestValidationError`、`StarletteHTTPException`）。
`StarletteHTTPException` 那个覆盖 401/403/404 —— **按理 404 应该有 `error`。**

但实测没有。而且返回的是**小写** `not found`，而 Starlette 默认是 `Not Found`（大写）。
小写说明这是我们自己写的字面量，不是框架默认。

于是 grep：

```
main.py:688   {'detail': 'not found'}      ← 中间件：隐藏内部路径
main.py:999   {"detail": "not found"}      ← 静态文件：路径不存在
main.py:1026  {'error': 'not found'}       ← 静态兜底：连 404.html 都没有
```

**三处手写 404，两种结构。**第 1026 行是第三种：`error` 是**裸字符串**。

## 3. 为什么第 1026 行是真问题

其余所有错误响应的 `error` 都是对象：

```json
{"error": {"code": "unauthorized", "message": "未登录", "type": "api_error"}}
```

而它是：

```json
{"error": "not found"}
```

**客户端按 `error.code` 取值时，走到这条路径拿到 `undefined`**，然后只能笼统当成「未知错误」
—— 明明知道是「地址不存在」，却报不出这个结论。

对比之下，688 和 999 的 `{"detail": ...}` 与 FastAPI 默认形状一致，不是冲突。
**所以只改 1026 一处**，改动面最小。

## 4. 改法

```python
return JSONResponse(
    {
        'detail': 'not found',          # 保持原样：前端与既有断言按它取值
        'error': {                      # 补充：让错误码读取路径也能工作
            'code': 'not_found',
            'message': 'not found',
            'type': 'api_error',
        },
    },
    status_code=404,
)
```

**两条读取路径都能工作** —— 老的按 `detail`、新的按 `error.code`。这是渐进式修正，不是替换。

## 5. 顺带检查：还有没有别处

grep 全仓 `'error': '字符串'`：

| 位置 | 语境 | 是否 HTTP 响应 |
|---|---|---|
| `notify.py:122/134` | 内部函数返回值 | 否 |
| `checkin_scheduler.py:234` | 内部函数返回值 | 否 |
| `runtime.py:137` | 内部状态字典 | 否 |
| `test_*.py` 若干 | 测试数据 | 否 |

**HTTP 层只有改的那一处。**内部函数用裸字符串 `error` 没有契约问题，不动。

## 6. 一个测试细节

`test_static_traversal.py` 里有一个自建的复刻端点，注释写着「与生产实现同源」：

```python
return JSONResponse({'error': 'not found'}, status_code=404)
```

这个测试只断言 404 状态码，不检查 body，**所以改生产代码不会让它失败**。
但「同源」不能只是注释 —— 一起改了，否则下次有人照着测试读契约会读到错的。

## 7. 部署验证

| 项 | 结果 |
|---|---|
| 版本 | **v1.5.81** |
| 构建提交 | `1b338a920d57cddc1669033b042e8cbdc2d80fc6` |
| **重启窗口** | **59 个请求，0 个 5xx** |
| 门禁 | 后端 1,117 例 + 前端 245 例 + F821=0 + F811=0 |

**为什么线上 curl 不到改后的 JSON**：生产有 `STATIC_DIR/404.html`，所以走的是
`FileResponse(404.html)` 分支 —— 用户看到友好的 HTML 页面，这是对的。
**只有静态目录缺 `404.html` 时才会走到我改的 JSON 分支**（测试环境就是这种情况）。

这不是「没生效」，是正确的前置条件分支。代码确认在部署产物里（`grep` 命中 1 处）。

## 8. 这轮的价值

**用户看不到任何变化。**这一版改的是一条几乎不会走到的兜底路径。

但对写程序对接的人来说：**「地址写错了」现在能被准确识别，而不是被归类成「未知错误」。**
这正是规范里「把支持与不支持都讲清楚」的意思 —— 说不清的，就是没说清。

## 9. 待办

| 项 | 状态 |
|---|---|
| 错误契约一致性 | **HTTP 层已统一** |
| 异地备份 | 需外部存储 |
| 回收 2.2 GB 残留 | 待运维确认 |
| P0-1 / P0-2 | 待运维安装 |
| 高负载重启验证 | 需真实高峰 |