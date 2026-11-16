# 指标清单

> 采集点：`/metrics`（需要 `Authorization: Bearer <token>`，token 在 `/etc/zhihui/metrics-token`）
> 规模：**67 个指标名 / 116 个序列**
> 最后核对：2026-11-15

## 怎么读这份文档

三列最关键的是**「读数为 0 / 负数时怎么理解」** —— 同一个 `0` 在不同指标上的含义完全相反：

- 有的 `0` 是**健康**（没有错误）
- 有的 `0` 是**还没触发**（功能正常，只是没遇到条件）
- 有的 `0` 是**异常**（该有值却没有）

**把这三类混在一起看，就会在正常时报警、在异常时放过。**

---

## 一、存活与自检

| 指标 | 含义 | 读数为 0 时 |
| --- | --- | --- |
| `workbuddy_up` | 服务在跑 | **异常**（值为 1 才正常） |
| `workbuddy_uptime_seconds` | 本次进程运行时长 | 刚重启 |
| `workbuddy_monitor_successes_total` | 监控循环成功次数 | 累计值，重启归零 |
| `workbuddy_monitor_failures_total` | 监控循环失败次数 | **健康** |
| `workbuddy_monitor_last_success_age_seconds` | 距上次监控成功多久 | 超过两个采集周期就该查 |
| `workbuddy_metrics_window_coverage_seconds` | 指标窗口实际覆盖的时长 | 刚重启时偏小，**不是故障** |

## 二、请求量

| 指标 | 含义 |
| --- | --- |
| `workbuddy_requests_total` | 累计请求数 |
| `workbuddy_requests_last_hour` | 最近一小时请求数 |
| `workbuddy_requests_24h_by_model{model=...}` | 按模型的 24 小时请求数 |
| `workbuddy_request_outcomes_total{outcome=...}` | 按终态分类的累计数 |

**用法**：`requests_last_hour` 掉到 0 而服务是 `up`，通常是对外链路或调用方的问题，不是服务本身。

## 三、延迟

| 指标 | 含义 | 注意 |
| --- | --- | --- |
| `workbuddy_latency_ms_p50_1h` / `p95` / `p99` | 端到端延迟分位 | **含上游生成时间** |
| `workbuddy_latency_samples_1h` | 参与计算分位的样本数 | **少于 20 时不要引用分位值** |
| `workbuddy_ttft_ms{quantile=...}` | **首字延迟**分位 | 用户真正「等待」的那段 |
| `workbuddy_ttft_samples` | 首字延迟样本数 | 同上，样本不足时分位值不输出 |
| `workbuddy_first_token_seconds` | 首字延迟（秒，旧口径） | 与新指标并存 |
| `workbuddy_request_duration_seconds` | 单请求耗时 | — |

**关键区别**：端到端延迟会把首字等待藏在生成时间里。
总延迟 10 秒里有 7 秒是等第一个字时，只看 `latency_ms` 会以为是「模型生成慢」。

## 四、上下文长度

| 指标 | 含义 |
| --- | --- |
| `workbuddy_context_tokens{quantile=...}` | 请求的 prompt token 分位 |
| `workbuddy_context_max_tokens` | 最近一小时最大值 |
| `workbuddy_context_samples` | 样本数 |

**没有阈值**：不同模型上限不同且会变，所以只报数不给判断。
**样本少于 20 时不给分位值** —— 几个样本算出的 p99 只是其中最大的那个。

## 五、错误与拒绝

| 指标 | 含义 | 读数为 0 时 |
| --- | --- | --- |
| `workbuddy_errors_total` | 累计错误 | **健康** |
| `workbuddy_errors_last_hour` | 最近一小时错误 | **健康** |
| `workbuddy_errors_5xx_24h` | 24 小时 5xx | **健康** |
| `workbuddy_rejections_429_24h` | 24 小时限流拒绝 | **健康** |
| `workbuddy_rejections_503_24h` | 24 小时 503 | **健康** |
| `zhihui_failure_families_total{family=...}` | 按族分类的失败 | **健康** |
| `workbuddy_retry_after_forwarded` | 转发上游等待时间的次数 | **还没遇到上游限流，不是故障** |
| `workbuddy_output_over_limit_total` | 输出超限次数 | 偶发正常 |
| `workbuddy_body_timeout_total_total` | 请求体读取超时 | 偶发正常 |

## 六、容量与排队

| 指标 | 含义 | 该看什么 |
| --- | --- | --- |
| `workbuddy_gateway_inflight` | 当前在处理的请求 | 相对 `inflight_max` 的比例 |
| `workbuddy_gateway_inflight_max` | 并发上限 | 容量上限 |
| `workbuddy_gateway_inflight_large` | 大请求占用数 | — |
| `workbuddy_gateway_inflight_body_bytes` | 在飞请求体字节 | 接近上限时排队的成因 |
| `workbuddy_gateway_queued` | 排队中的请求数 | 长期大于 0 说明容量不足 |
| `workbuddy_gateway_key_slots` | 密钥并发槽占用 | — |
| `workbuddy_gateway_capacity_rejections_total` | 容量拒绝累计 | **健康**（0 表示没触发保护） |
| `workbuddy_gateway_queue_timeouts_total` | 排队超时累计 | **健康** |
| `workbuddy_gateway_queue_overflows_total` | 排队溢出累计 | **健康** |

## 七、上游与账号池

| 指标 | 含义 |
| --- | --- |
| `workbuddy_engine_active{platform,engine}` | 各平台实际使用的引擎实现 |
| `workbuddy_account_pool_accounts` | 池内账号数 |
| `workbuddy_account_pool_requests` | 池处理的请求数 |
| `workbuddy_account_pool_top_share` | 最热账号占比（**接近 1 说明分布不均**） |

## 八、存储与进程

| 指标 | 含义 | 注意 |
| --- | --- | --- |
| `workbuddy_db_size_bytes` | 数据库大小 | — |
| `workbuddy_db_wal_size_bytes` | WAL 大小 | 长期偏大说明检查点不及时 |
| `workbuddy_disk_free_bytes` | 磁盘剩余 | **低于阈值该告警** |
| `workbuddy_disk_used_ratio` | 磁盘使用率 | 同上 |
| `workbuddy_process_resident_bytes` | 常驻内存 | 长期上涨要看是否泄漏 |
| `workbuddy_process_virtual_bytes` | 虚拟内存 | — |
| `workbuddy_process_threads` | 线程数 | 持续增长要查 |
| `workbuddy_cgroup_memory_peak_bytes` | cgroup 内存峰值 | 接近容器限额要查 |

## 九、备份与恢复

| 指标 | 含义 | 注意 |
| --- | --- | --- |
| `workbuddy_backup_age_seconds` | 距上次日常备份多久 | 超过一天该查 |
| `workbuddy_snapshot_age_seconds` | 距上次快照多久 | **`-1` 是预期的**：快照根目录对服务用户不可遍历，用下面那条交叉验证 |
| `workbuddy_recovery_chain_age_seconds` | 恢复链的新鲜度 | **这条才适合判断备份是否在跑** |
| `workbuddy_recovery_chain_bytes` | 恢复链总字节 | — |

## 十、审计链

| 指标 | 含义 | 取值 |
| --- | --- | --- |
| `workbuddy_audit_chain_intact` | 哈希链是否完好 | **1 完好 / 0 断裂 / -1 从未检查** |
| `workbuddy_audit_chain_events` | 已校验的事件数 | — |
| `workbuddy_audit_chain_check_failed` | 校验过程本身出错次数 | **健康** |

**`-1` 与 `0` 必须分开看**：`-1` 是「还没查过」，`0` 是「查了，断了」。
把两者合并成一个布尔值，就会把「没检查」当成「有问题」或者反过来。

## 十一、归档

| 指标 | 含义 | 读数为 0 时 |
| --- | --- | --- |
| `workbuddy_archive_enabled` | 归档开关 | 0 表示关闭（配置决定） |
| `workbuddy_archive_worker_alive` | 归档线程存活 | **异常**（1 才正常） |
| `workbuddy_archive_last_cycle_age_seconds` | 距上次归档周期 | 超过周期 2 倍该查 |
| `workbuddy_archive_last_cycle_failed` | 上次周期是否失败 | **健康** |
| `workbuddy_archive_pending_datasets` | 待归档数据集数 | 长期上涨说明归档跟不上 |

## 十二、兼容性与边界

| 指标 | 含义 |
| --- | --- |
| `workbuddy_compatibility_hits_total` | 兼容改写命中次数（按规则） |
| `workbuddy_nginx_fence_active` | 反代围栏状态 |

---

## 建议告警的几条

**只列最该盯的，不是全部：**

| 条件 | 为什么 |
| --- | --- |
| `workbuddy_up == 0` | 服务没在跑 |
| `workbuddy_disk_used_ratio > 0.90` | 磁盘将满 |
| `workbuddy_audit_chain_intact == 0` | 审计链断裂（**不是 -1**） |
| `workbuddy_audit_chain_check_failed > 0` | 校验本身出错，等于没在校验 |
| `workbuddy_archive_worker_alive == 0` 且 `archive_enabled == 1` | 归档停了 |
| `workbuddy_gateway_queued > 0` 持续数分钟 | 容量不足 |
| `workbuddy_monitor_last_success_age_seconds` 超过两个采集周期 | 监控循环卡住，后面的指标都不可信 |
| `workbuddy_recovery_chain_age_seconds` 超过预期周期 | 备份没在跑 |

**反过来，这些不要告警**：`errors_*_24h == 0`、`rejections_*_24h == 0`、
`capacity_rejections_total == 0`、`retry_after_forwarded == 0` —— **它们为 0 是目标状态**。

## 维护说明

指标会随版本增加。**新增指标时请同时更新本文件**，尤其是「读数为 0 时怎么理解」那一列 ——
那是这份文档存在的唯一理由。
