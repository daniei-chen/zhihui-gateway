# K19 大请求与资源预算（候选）

## 现场事实（2026-10-23 只读）

| 项 | 值 | 来源 |
|---|---|---|
| Manager 进程 | `MemoryMax=infinity`、`ProtectSystem=full` | `systemctl show` |
| 并发上限 | 60 普通 / 50 大请求 | 进程环境 `WB_GATEWAY_MAX_CONCURRENCY=60`、`WB_GATEWAY_LARGE_MAX_CONCURRENCY=50` |
| body ceiling | 512 MB（应用层） | `WB_GATEWAY_MAX_BODY_CEILING_MB=512` |
| 公网实际 body 上限 | 64 MB（Nginx `client_max_body_size`） | 生效配置 |
| 主机内存 | 3723 MB total / 1696 MB available | `free -m` |

**矛盾点**：应用层允许 512MB，Nginx 只放 64MB → 公网请求永远到不了应用层上限；
但内网/直连 7864 的调用方可以打到 512MB，而进程内存没有上限（`MemoryMax=infinity`）。

## 候选配置

### 1. 进程内存上限（systemd drop-in）

```ini
# /etc/systemd/system/workbuddy-web.service.d/20-memory.conf
[Service]
# 主机 3.7G；给 Manager 2G 上限、1.6G 软限（超过就回收而不是直接杀）
MemoryHigh=1600M
MemoryMax=2048M
# 不要开 OOMPolicy=continue：内存失控时必须让 systemd 记录并重启，而不是假装没事
```

### 2. body 上限对齐

- 若业务确实需要 >64MB 的请求（大上下文/图片），把 Nginx 提到与之一致，并**同时**评估：
  单请求峰值内存 ≈ body 大小 × 解析倍数；
- 若不需要，把应用层 512MB 降到 ≤ Nginx 值，避免"内网可以传 512MB"的隐性通道。

### 3. 并发预算

- 60 并发 × 单请求最坏内存，应与 `MemoryMax` 一起算；
- 大请求（`declared > 1MB`？实际判断是 `>1024*1024`）单独 50 并发上限，需在文档里写清单位；
- 反压：超出时返回 503 + `Retry-After`（当前实现已有 `gateway_capacity_exceeded`），不要静默排队。

## 验收

1. 用假 provider 做压测：60 并发普通 + 50 并发 1MB body，记录 RSS 峰值；
2. 触发 MemoryHigh 时观察是否出现回收而非 OOM kill；
3. 确认超限请求返回 503 且账本不记成功。

## 风险

- `MemoryMax` 设太小会让正常长上下文请求被杀；必须先用假 provider 测峰值；
- 不能把"内存还有余量"当成"高峰一定够"（报告 §4.5 已明确）。
