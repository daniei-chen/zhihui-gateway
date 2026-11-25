# 2026-11-25 找到一处从未生效的错误翻译（第 28 轮）

## 1. 先纠正上一轮的一个错误结论

第 27 轮我写「`application_error` 发生了却不留痕」。**这个结论是错的。**

那一轮我只查了一个时间点（`2026-11-24 19:19:39`）的日志，看到没有异常就下了结论。
这一轮按「一分钟 20 个」的聚集点（`2026-11-19 20:50`）去查，日志一次给出 1763 行：

```
Nov 19 20:50:00 ... "GET /__qtg/api/stats/credits?days=7&realm=cn&platform=doubaowork" 500 Internal Server Error
Nov 19 20:50:00 ERROR:    Exception in ASGI application
Nov 19 20:50:00   + Exception Group Traceback (most recent call last):
...
```

**日志是有的，而且很完整。**

同一个错误我犯了两次：**用单个样本代表整体**。上上轮是「上游错误码只查了一个」，
上轮是「日志只查了一个时间点」。方法上的教训已经写在第 26 轮文档里，这次仍然踩到。

## 2. 真正的根因：错误翻译器从未被安装

traceback 里的应用代码位置：

```
releases/v1.3.4/server/qtgcore/routers/compat_stats.py", line 340
releases/v1.3.4/server/qtgcore/cache.py", line 91
```

`cache.py:91` 是 `value = await factory()`，异常来自被缓存的加载函数。而 `cache.py:81/93`
明确会抛：

```python
raise UpstreamError('数据已更新，请刷新后重试', code='cache_invalidated')
```

`UpstreamError` 继承自 `QtgError`，而 `errors.py` 里写了完整的翻译器：

```python
def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(QtgError)
    async def _qtg(_: Request, exc: QtgError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())
```

**问题是：`install_handlers` 从来没有被调用过。**

全仓搜索的结果：

```
server/qtgcore/errors.py:7    ← docstring 里提到
server/qtgcore/errors.py:152  ← 定义
```

**两处，没有第三处。**`git log -S install_handlers` 也只有 baseline 同步那一个 commit。

所以：**`QtgError` 及其 5 个子类从未有过异常处理器**，抛出后一路冒到 Starlette，
被兜成 500 并打印 traceback。

## 3. 后果

所有受控错误都失去了语义：

| 异常 | 本该返回 | 实际返回 |
|---|---|---|
| `BadRequest` | 400 | 500 |
| `Unauthorized` | 401 | 500 |
| `Forbidden` | 403 | 500 |
| `NotFound` | 404 | 500 |
| `UpstreamError` | 502 | 500 |

在 `request_outcomes` 里，这些全部落进 `gateway.application.error` ——
**因为它们没有更具体的分类依据**。这正好解释了那一列的画像：

```
http_status 503 × 124
http_status 502 × 9
error_stage (null) × 133      ← 全部没有阶段标记
```

按端点：`/api/stats/daily` 35、`/api/stats/summary` 35、
`/api/system/internal-session-verify` 31、`/api/stats/credits` 10、`/v1/chat/completions` 8。

**也就是说：控制台在缓存失效时会拿到 500，而本该是 502 或 409 之类的可识别响应。**

## 4. 为什么本轮不动手

修法只有一行：

```python
# server/main.py，创建 app 之后
from .qtgcore.errors import install_handlers
install_handlers(app)
```

**但影响面覆盖所有 qtgcore 路由**：

1. 每个 `QtgError` 子类的状态码都会从 500 变成各自的值（400/401/403/404/502）
2. 响应体格式会从 Starlette 的 `Internal Server Error` 文本变成
   `{"error": {"code", "message", "type"}}`
3. 前端若有代码按「500 = 后端崩了」做判断，行为会变

这不是一行改动，是**一次跨路由的行为变更**，需要：

- 确认没有地方依赖 500
- 确认前端能处理新的响应体
- 分别在缓存失效、上游不可用、参数错误三种情况下验证

**本轮剩余预算不够走完这些验证**，而 §1.5 要求不留半成品。

## 5. 下一轮的执行计划

1. 先只做**最小侵入**验证：在本地起服务，人为抛一个 `UpstreamError`，
   确认注册处理器前后 `/api/stats/*` 的响应差异
2. 检查 `web/` 里是否有针对 500 的特判（`grep 'status === 500'` 等）
3. 注册处理器，跑门禁
4. 发版，然后对真实控制台抽查 `stats` 页面

## 6. 顺带说明：`gateway.application.error` 这个分类词本身有误导

它的名字听起来像「应用程序内部错误」，实际含义是「返回了 5xx 但没有任何阶段/终态标记」。
上面查出的 133 条里，**大部分其实是受控错误被兜成 500**，不是程序崩溃。

第 27 轮我把它当成「我们的责任」并去找异常原因，方向对了一半：确实是我们的问题，
但不是「程序出错」，而是「错误翻译没接上」。

**名字会引导判断方向。**这一条值得记下来。

## 7. 本轮产出

- 纠正了上一轮的错误结论（日志是有的）
- 定位到 `install_handlers` 从未调用这一根因
- 给出修复方案与影响面评估
- 没有改代码，没有发版