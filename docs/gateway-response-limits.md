# 网关响应资源边界

本说明对应 `WB_GATEWAY_MAX_RESPONSE_MB`、`server/upstreambody.py` 和三种对外生成协议。
它约束上游响应的资源消耗，不改变模型输出 token 上限，也不表示一次请求只占用同等大小的进程内存。

## 配置与作用范围

| 项目 | 行为 |
| --- | --- |
| 环境变量 | `WB_GATEWAY_MAX_RESPONSE_MB` |
| 默认值 | 64 MiB |
| 允许范围 | 整数 1～512 MiB；不能用 0 关闭边界 |
| 作用请求 | Chat Completions、Messages、Responses、管理测试台的生成请求 |
| 计数单位 | 每次 HTTP 响应独立计数，包括共享连接池/HTTP2 连接上的并发请求 |
| 计数对象 | 传输正文，以及每一层解压后的正文分别累计 |
| SSE | 整条响应累计，不在事件、输出块、心跳或工具调用之间重置 |
| 其它下载 | 更新包、备份、管理资源下载不自动套用推理响应上限，继续使用各自策略 |

通过正式启动入口加载配置时，schema 会校验配置范围。修改环境变量后需重新启动进程；
`/api/system/config` 用于检查进程实际值与声明值是否一致。`config.http_client()` 只有显式
传入 `bounded_response=True` 才启用响应边界，避免影响合法的大文件管理下载。

超长 SSE 即使每个事件都小于事件上限，只要整条传输或解压正文超过响应上限，也会终止。
默认 64 MiB 因此可能中断原本允许无限累计的超长对话。确有此类负载时，应先测量实际分布，
再在支持范围内调整配置，并同时检查并发量、输出 token 限制与进程内存。

## 解压、背压与关闭

受限请求默认发送 `Accept-Encoding: identity`，避免 HTTPX 在边界检查之前自动将压缩正文展开。
若上游仍返回 `gzip` 或 `deflate`，网关使用有单次输出上限的增量解压，单次最多产出 64 KiB，
并对累计输出进行检查。`deflate` 同时接受 zlib 包装和常见的 raw deflate。

重复的 `Content-Encoding` 字段与逗号分隔的编码按同一序列处理，解码顺序与编码相反，最多支持四层。
未知编码（包括意外的 Brotli、Zstandard）、不完整压缩流、额外尾部字节和串接的多个 gzip member
会作为解码错误拒绝；不会跳过尾部内容后宣称成功。解压后移除原编码/长度头，避免 HTTPX 二次解压。

这是明确的兼容性和性能取舍：默认 identity 放弃 HTTP 传输压缩带来的带宽收益；本机引擎通信通常
更适合此选择，但跨网络的大响应应单独测量。只支持上述编码也意味着忽略 identity 协商的其它编码服务器
会收到错误。不能仅为恢复压缩而移除此层检查，因为事后检查解压块不能防止解压炸弹的大块分配。

读取按下游消费速度推进，未增加后台无界读取队列；解压输出和原始帧都有周期性调度让步，确保超时和取消
有机会运行。成功、超限、格式错误、取消均关闭所属响应，并由原有连接池租约管理释放引用；每个响应的计数
不储存在共享 transport 或连接对象中。

## 对客户端与账本的表现

尚未发送非流式响应时，上游超限/解码失败返回 502，管理测试台同样记录失败。
SSE 已发送响应头后不能改写 HTTP 状态码，因此通过各协议的错误事件结束，并记录失败终态。
Messages 错误流可以保留用于关闭消息的 `message_stop`，但不再追加宣称正常完成的 `end_turn` 或
成功 `message_delta`。Responses 不发送 `response.completed`。

超限、取消或协议失败不证明上游消费为零，也不能仅根据无效或不完整的 usage 字段释放预算。
有限额请求保留未知消费状态，由既有账本/对账流程处理；可用的部分 token 证据仍保留。

## 其它相关边界

单个 SSE 事件/未完成行默认有约 1 MiB 审计缓冲边界，数据行的结构开销也计入，防止大量空 `data:`
行绕过正文长度计数。Responses/从 Chat 转换的 Messages 最多输出 256 个内容项/块，文本、推理和工具
共享此计数；超过时明确报错，不静默丢弃后续工具。

JSON 解析、Python 字符串、翻译器缓存和终态内容拼装会使实际内存大于响应字节数；多请求并发还会累加。
该上限不是 RSS 保证。当前回归验证了 MockTransport 上的并发独立计数、压缩多层、取消、共享连接池回收
以及真实 ASGI 账本结果；没有把本地测试解释为生产 HTTP2 压测、供应商吞吐量或用户端首 token 延迟。

## 可重复验证

使用仓库隔离测试入口执行 `server/tests/test_audit_upstreambody.py`、
`server/tests/test_audit_protocol_limits.py` 和 `server/tests/test_audit_usage_evidence.py`。
需要包含前端契约的组合应使用 `--target manager-assets --require-built-web`。

`tests/benchmarks/gateway_resources.py --baseline <原始检出目录>` 比较纯本地 Unicode、SSE 解析与
ASGI 分片内存开销。输入是合成数据；不打开数据库、账号目录或网络连接，也不启动应用。
