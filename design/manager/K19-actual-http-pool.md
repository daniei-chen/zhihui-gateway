# 实际 HTTP 连接池修正（本地候选）

范围：K19/K21/K48 的出站连接生命周期，相关 K12 排空。未部署生产，不能据此关闭完整任务。

## 已复现与修正

旧业务路径调用 `config.http_client`，辅助 `pooledhttp` 的测试没有覆盖它。
保留默认24容量、借出25身份时，第一份未归还客户端已被淘汰关闭；同身份客户端共享Cookie。
有 lane 时旧代码还忽略 identity。旧冻结视图的两条独立红测确认 Cookie 与身份边界失败：
`evidence/runs/20261024-030000-dc54decf/stats-manager-red-baseline.json`。
原完整长流红测持续等待超过5分钟，进程、连接与终止依据保留在
`evidence/actual-http-pool-red-terminated.json`，不能把它记成跑完的红测结果。

当前唯一业务实现为 `http_pool.TransportPool`，`config.http_client` 使用它；
`pooledhttp` 只保留转发兼容入口，已删除第二份池实现。

每次调用获得独立 AsyncClient，Cookie、headers、关闭状态互不共享。昂贵的TLS/keepalive
transport在真正发送时按 **事件循环、完整timeout、代理、lane、identity** 复用。
lane与identity同时生效；缓存键内代理/身份摘要化，不输出明文凭据。
返回对象是完整httpx.AsyncClient，build_request/send/post/stream及原调用形式均保留。

## 生命周期和容量

- 引用从传输获取开始，覆盖等待响应头、读取完整正文、流式迭代；在响应关闭/读尽/取消后成对释放。
  客户端租约提前关闭不会关闭另一份租约或正在返回的响应。
- 空闲LRU可以退役，但必须在原循环关闭完成后才能创建替代transport。
  所有组都在使用时按请求pool timeout等待，取消等待不占用槽位，不淘汰活跃流。
- 正常 `close_clients()` 退役当前循环的组，已有响应继续完成；循环结束的生命周期守卫最终关闭其资源。
  跨线程事件循环不复用transport，退役动作回到拥有它的循环；全局容量计数受锁保护。
- 关闭失败的组继续占槽，不通过无限创建替代池掩盖资源泄漏。显式清理可重试；关闭预算为5秒，失败会报错。
- 无运行中循环时保留独立客户端路径，不放入缓存。忽略系统代理，配置代理时保留“内部直连/外部走代理”的双路由。

配置：`WB_HTTP_MAX_POOLS` 默认24；`WB_HTTP_POOL_CONNECTIONS` 默认8；
`WB_HTTP_POOL_KEEPALIVE` 默认4，最大不超过连接数；idle expiry为15秒。
这些是逻辑transport组。一个配置代理的组同时含direct/proxied两条路由，各自受连接限额约束。
因此默认直连路径的池上界是24×8，双路由情形的理论连接上界需按两路计算；
**不能用该限制替代入站并发、正文内存、容器资源和生产容量实测**。
默认连接预算相较旧httpx默认100更严格，发布前须用实际并发分布核验排队与超时。
配置完整schema/非法值拒绝仍归K22，未在本项伪称完成。

## 证据与覆盖映射

`test_actual_http_pool.py` 从真实业务factory发出真实TCP，覆盖状态隔离、Set-Cookie回传隔离、
单连接复用、lane×identity、活跃长流压力、池满超时、等待取消、提前释放租约、正常清理、
多线程/多循环容量、构造失败与关闭失败。`test_http_client_pool.py` 保留代理协议与内部直连测试。

旧 `test_full_upgrade.PoolIsolationTests` 的5个辅助模块用例转为以下实际路径证据：

| 旧性质 | 当前对应 |
|---|---|
| 同身份复用、Cookie隔离 | serial_leases、distinct_leases、upstream_set_cookie |
| 身份/代理隔离 | identity_is_not_overridden_by_lane、different_proxy、internal_target_does_not_touch_proxy |
| 归还租约 | lease_exit_closes_only_the_lease、repeated_lease_close、真实TCP的references归零 |
| 取消释放 | cancelled_waiter、真实TCP中20轮在途取消及关闭后FD回收 |
| 有界淘汰 | idle_eviction、full_active_pool_times_out、cleanup_failure、construction_failure |

Windows：30条连接边界全过；58条预算/并发夹具全过；全量2836中2826通过、0失败、10跳过，
视图前后不变，权威 `evidence/runs/20261024-030723-48db5eee/stats-manager-assets.json`。
Linux同快照全量仍在运行，源包SHA256：
`fe52cfb43020d3e964ba7de1fb0ee0cd16de2ba0c29f0a8cc9045904db0dfa57`。
具体流水线记录包括driver SHA，不用Windows结果覆盖Linux残余。

## Linux旧失败的夹具修正

旧预算测试把spawn/import时间计入SQL竞争8秒，还在Queue超时时跳过进程回收。
现在先等待两个工作进程明确就绪（启动预算60秒），再同时放行；SQL结果仍限8秒，
保留“一胜一拒、一个预留”的断言，所有失败路径finally回收自己的子进程和IPC。

真实TCP测量改为当前factory，等待超额keepalive的EOF处理完成再取稳态样本，
并等服务端处理器与连接关闭后取末样本；FD波动阈值仍4，结束阈值仍起始+2，没有放宽。
这些修正仍须经Linux整套回归，专项通过不等于已证明整套稳定。
