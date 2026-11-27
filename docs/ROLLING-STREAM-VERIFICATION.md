# 跨路由切换的真实 HTTP 流验收

`deploy/verify_rolling_streams.py` 在本机私有临时目录和动态 loopback 端口运行仓库真实 `server.rollingrelay.StreamingRelay`，通过真实 HTTP 客户端观察流量，并调用实际 `deploy.cohort_rollout.atomic_json` 完成 `old → new → old` 两次路由切换。

该工具不接受生产地址、令牌、服务名或现有路由文件，所有请求和上游均为合成数据。它不会连接南京服务器、固定服务端口或真实供应商，也不会执行 `systemctl`、Nginx reload 或发布命令。

## 执行

在安装了项目 Python 依赖的 checkout 中运行：

```bash
python -B -I deploy/verify_rolling_streams.py \
  --hold-seconds 2 \
  --timeout 10 \
  --output /absolute/private/new-stream-proof.json
```

输出路径必须是新文件，不覆盖已有报告；文件权限为 0600。默认两个持续流各发送 8 个严格有序的数据帧及一个结束帧，并在两次切换期间保持打开。`--hold-seconds` 可设为最多 10 秒，超时最多 60 秒；这是有界功能验收，不能代替生产的长时间容量或故障测试。

验证已保存的报告（不启动任何 HTTP 服务）：

```bash
python -B -I deploy/verify_rolling_streams.py \
  --verify-evidence /absolute/private/new-stream-proof.json
```

退出码 0 表示当前合成证据完整；任何失败、缺字段、帧/哈希不一致、时间顺序不成立或超过 1 小时有效期均返回非零。验证不依赖 Python `assert`，`python -O` 也保留检查。

## 判据

报告必须同时满足以下条件：

- 旧实例流的首个数据帧在第一次切换之前抵达，最后数据帧在第二次切换之后抵达；新实例流也必须跨过第二次切换。
- 切换前的新请求到旧实例；第一次切换后的新请求到新实例；第二次切换后的新请求回到旧实例。持有长流期间继续发起短请求，全部 HTTP 200 且内容完整。
- 每个流的请求标识、实例标识、帧序号、总字节数和 SHA256 完全对应期望合成内容，并且实际收到结束帧。仅收到 200 或正常 HTTP EOF 不够。
- 两次切换时真实中继仍持有对应活动流；此时关闭中继会被拒绝。所有流完成后活动计数归零。
- 每个已观测请求在合成上游只有一次 POST 接收记录，没有重放、漏报或中继传输错误。
- 报告记录 HEAD、工作区是否干净，以及 relay、共享异步清理、原子路由和验收脚本的 SHA256。发布验收应从干净的精确候选提交执行；工作区证据只证明所列源字节的行为。

专项测试注入了无结束帧、截断、乱序、停滞和任务取消；还检查报告过期、错误源标识、重复 POST、切换前已结束的流，以及残留活动流都无法通过。取消和失败路径会关闭新建的临时监听和连接。

## 证据范围

成功报告只设置 `synthetic_cutover_verified: true`，`production_zero_downtime_verified` 始终为 false。它证明仓库中继和原子路由在本机受控 HTTP 场景的行为，不证明生产入口的 DNS/TLS/Nginx、实际服务进程退出时序、真实上游或网络质量。报告没有签名，不应把可编辑 JSON 当作受信外部证明。

`rollout-rolling.sh` 原有 `gateway_completed_requests` 统计继续保留其范围。真实生产零中断结论仍需绑定实际发布窗口的入口探针、跨切换长流、服务排空和原始观测证据；本机成功不会修改那项结论。
