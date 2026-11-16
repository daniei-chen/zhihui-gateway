# 关闭流程的取消韧性 — 2026-11-15

一个**只在该文件组合下出现**的测试失败，追到关闭路径上，得到一个**未验证的推断**。
这份记录的目的是让下一个人不必重新走一遍。

---

## 一、现象

`test_startup_checks_lifetime.py` 里的
`test_cancelled_password_join_still_finishes_other_lifespan_cleanup`：

| 跑法 | 结果 |
| --- | --- |
| 单独跑 | **8 项通过** |
| 与无关文件组合 | 通过 |
| 与 `test_metrics_auth` + `test_preauth_body` 组合 | **失败** |
| **同上，但暂存掉本版改动** | **同样失败** |

最后一行是关键：**这是既有问题，不是任何一次改动引入的。**

## 二、失败的断言

```
Expected close_clients to have been awaited once. Awaited 0 times.
```

关闭流程的最末一步没有被执行。

## 三、关闭路径的实际顺序

```
262  pending_shutdown_cancel = None
264  await weak_admin_check.close()          ← 唯一有 try/except CancelledError 的
     ...
295  await drain.wait_until_drained(...)
300  await instance_runtime.close()
     ...
322  await dailyplan.close_scheduler()
327  await config.close_clients()             ← 没被执行的那一步
356  if pending_shutdown_cancel is not None:
357      raise pending_shutdown_cancel         ← 在 327 之后才抛
```

`pending_shutdown_cancel` 的重新抛出**在 `close_clients` 之后**，所以那条路径本身没问题。

## 四、推断（未验证）

测试连发**两次** `cancel()`。

`264` 行那个 `await` 被 `try/except CancelledError` 包着，**第一次 cancel 被它接住**，
存进 `pending_shutdown_cancel`，流程继续。

**但 300、322、324 这些 await 点都没有保护。**
第二次 cancel 若落在其中之一，`CancelledError` 会**直接穿出 `__aexit__`**，
`close_clients` 就此跳过。

**为什么单独跑能通过**：第 264 行的 `await` 那侧有个最长 3 秒的阻塞等待。
单独跑时两次 cancel 都落在那个窗口里，被同一个 `except` 接住；
组合跑时时序改变，第二次 cancel 落到了后面某个没有保护的点上。

**这一步我没有验证。** 验证需要改核心关闭逻辑或在关闭路径上加插桩 ——
两件事的风险都高于这个测试失败本身。

## 五、这个推断意味着什么

如果它成立，那么在**真实的、被取消两次的关闭**里，
共享 HTTP 客户端（以及中间那些清理）可能不会被关闭。

**这在我这里只是推断，不是结论。** 生产上的关闭通常只取消一次，
而且是由编排层在 drain 完成之后发起的 —— 那种形态下两次 cancel 不会同时出现。

## 六、为什么没有直接改

按推断，"正确"的修法是把关闭流程的每个 await 点都做成取消安全的
（shield 或逐点捕获并记录），或者在最后一步前面把关键资源释放包进 shield。

**但那会让一段已经很复杂的关闭逻辑更复杂，而当前证据只有一条测试断言的缺失。**
用一次未验证的推断去驱动核心 shutdown 的改写，风险大于收益。

**先记录，后面如果出现「关闭时客户端没释放」的真实症状，这份记录能直接接上。**

## 七、还剩什么没做

- 没有验证第二次 cancel 究竟落在哪个 await 点
- 没有确认 `test_metrics_auth` / `test_preauth_body` 具体留下的是什么状态
- 没有确认生产环境的关闭是否可能被取消两次
