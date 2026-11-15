# 故障排查手册 — 基于 2026-11 的真实案例

这份手册只写**真的发生过**的事，每条都带当时的证据和定位路径。
不写"理论上可能"的东西 —— 那种内容查文档比查这里快。

---

## 一、管理员密码对、登录接口 200，但进不去面板

**这是最难自己想到的一条。**

### 症状

```
登录成功: admin（来源 x.x.x.x）
POST /api/login HTTP/1.1" 200 OK
GET /api/system/internal-session-verify HTTP/1.1" 503 Service Unavailable
```

登录**成功**了，但紧接着那一次子请求 503。

### 为什么难发现

| 维度 | 看起来 |
| --- | --- |
| 代码 | 正常 |
| 测试 | 全绿 |
| 配置核对 | 「密钥文件权限都是 `0600`」—— 权限确实对 |
| **实际** | **那条路径的配置项值是空的** |

### 一分钟定位

```bash
# 1. 看有没有 503
journalctl -u workbuddy-web.service --since '-30min' | grep 'internal-session-verify.*503'

# 2. 看启动自检说了什么（1.2.117 起）
journalctl -u workbuddy-web.service --since '-30min' | grep 'identity signing key'

# 3. 直接看那把钥匙
stat -c '%a %U:%G %n' /opt/workbuddy-manager/engine/secrets/identity_signing.key
```

启动日志出现这一行就是正常：

```
INFO server.main: identity signing key ready (/opt/workbuddy-manager/engine/secrets/identity_signing.key)
```

出现 `identity signing key UNREADABLE` 就是它，按提示查四项：**存在、常规文件、`0600`、≤4096 且无空白字符**。

### 不要做的

**别去查密码。** 日志里 `登录成功` 出现就说明密码没问题 —— 这个误导让我在错误方向上多花了一轮。

---

## 二、改完配置不生效，等一会儿又「自己好了」

### 症状

改了环境变量或面板设置，立刻验证没变，过几秒再试又对了。

### 根因

`global_http_proxy()` 有 **2 秒**模块级缓存，命中条件是「generation 没变且未过期」。
**生产上改设置会主动把 generation 加一**，所以真实操作不会遇到；
但**测试里直接改常量**就会撞上 —— 常量变了、generation 没变。

### 在生产上遇到怎么办

不用管。改设置走的是会失效的那条路。

### 在测试里遇到怎么办

```python
config.invalidate_global_proxy()
```

**先写库再改常量的那种用例要再失效一次** —— 写库已经让缓存算过一次（那时常量还是旧值）。

---

## 三、`/metrics` 里某个指标没输出，服务一切正常

### 症状

指标端点返回 200，但翻遍全文找不到某个指标名。

### 根因（已修，但方法通用）

取数源的两个分支**键集不一致**：一个分支有那个键，另一个没有。
`snap['queue_timeouts']` 抛 `KeyError`，被外层宽泛的 `except` 吞掉，只打一行警告。

**结果是指标静默消失。**

### 定位

```bash
# 1. 看有没有降级警告（1.2.115 起带栈）
journalctl -u workbuddy-web.service --since '-1h' | grep -A 20 'Metrics admission snapshot'

# 2. 确认某个指标到底在不在
TOK=$(cat /etc/zhihui/metrics-token)
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:7866/metrics | grep '^workbuddy_gateway_queued'
```

### 教训

**「服务正常 + 指标缺失」是一种独立的失败形态。** 异常被捕获、服务照常，
日志一行警告很容易被扫过去。所以 `1.2.115` 把栈加进了那条警告。

---

## 四、部署时 `prepare` 被拒若干次

### 症状

```
WRITER-MIGRATION-BLOCKED: ... unknown or additional writer blocks first migration
```

### 实测的拒绝次数

| 版本 | 拒绝次数 |
| --- | --- |
| v1.2.102 | 1 |
| v1.2.103 | 2 |
| v1.2.104 | 4（换事务名后 0） |
| v1.2.105 | 4（换事务名后 0） |
| v1.2.109 | 4（换事务名后 0） |

### 已知的**不是**原因

- **不是看门狗**。v1.2.105 那次看门狗状态是 `inactive`，照样被拒 4 次
- **不是事务名**。换名有效只是巧合 —— v1.2.103 换了名字还是被拒

### 现在怎么办

**重试**。`prepare-retry.sh` 会自己重试 4 次；仍然被拒就**换一个事务名再来一轮**。

**根因尚未定位。** 这份记录的价值是让人不要在"看门狗"上浪费时间 —— 那条已经被证据排除了。

---

## 五、磁盘在慢慢变满

### 先看构成

```bash
df -m / | tail -1
du -sm /root/backups /opt/zhihui-release /opt/workbuddy-manager 2>/dev/null | sort -rn
```

### 三个已知的大户

| 目录 | 典型占用 | 说明 |
| --- | --- | --- |
| `/opt/workbuddy-manager/releases` | 曾 **4.9 GB** | 每次部署留一个新目录 |
| `/root/backups/workbuddy-daily` | **2.6 GB** | 每次部署一个 ~328 MB 快照 |
| `/opt/zhihui-release/writer-state/transactions` | **5.9 GB** | 迁移事务记录，无清理 |

### 能直接做的

```bash
# 历史版本目录：保留最近 8 个 + current + 在用的（默认 dry-run）
/opt/workbuddy-manager/releases/current/deploy/prune-releases.sh
/opt/workbuddy-manager/releases/current/deploy/prune-releases.sh --keep 8 --apply
```

**实测一次回收 4.6 GB**（146 个目录 → 9 个）。

### 需要决定的

`transactions`、`audit-*-staging`、`/root/backups` 的旧快照 ——
**这些没有自动清理，删之前要确认可回滚范围。**

### 别忘了一件持续发生的事

**每次部署仍会产出一个 ~328 MB 的快照**，而保留策略写的是「等异地备份策略」。
两件事放在一起，磁盘会被填满。**清理只是延后，不是解决。**

---

## 六、某个功能改了不生效 / 改了没反应

### 先确认它在不在跑

**这是 2026-11 反复出现的一类**：改的东西位于**不可达的模块**。

```bash
# 直达内部前缀一律 404（这是刻意的防护）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7866/__qtg/api/settings

# 真正可达的是经过路径重写的那条
curl -s -o /dev/null -w '%{http_code}\n' -H 'x-qtg-platform: qoder' \
  http://127.0.0.1:7866/api/stats/summary
```

### 已知不可达的

| 模块 | 状态 |
| --- | --- |
| `qtgcore/routers/proxy.py` | **路由不可达**（但函数被别处引用） |
| `qtgcore/authz.py` | **完全不可达**（零 import） |

**在不可达模块里改代码，不会有任何效果** —— 而没有报错、没有日志，看起来像"配置没生效"。

---

## 七、一条测试单独跑通过、组合跑失败

### 先分类，别急着改

三种可能，处理方式完全不同：

| 类型 | 怎么确认 |
| --- | --- |
| **产品缺陷** | 单次操作就能复现 |
| **测试写错** | 断言本身不成立 |
| **组合特有** | 单独跑过、组合跑挂 |

### 确认是不是自己改坏的

```bash
git stash push -q -- <你改的文件>
# 跑同样的组合
git stash pop -q
```

**注意**：暂存掉的如果正是被测代码，测试必然失败 —— 那种复核是无效的。
**要确认的是「拿掉改动后失败的仍然是同一批用例」。**

### 已知的两类组合污染

| 组合 | 原因 |
| --- | --- |
| 代理设置那两个文件 | 2 秒缓存没被失效（见第二节） |
| `test_metrics_auth` + `test_preauth_body` + lifespan 测试 | 关闭路径的取消时序（**根因未验证**） |

---

## 八、通用排查顺序

遇到"不对劲"时，按这个顺序看，比凭印象猜快得多：

```bash
# 1. 版本与健康
cat /opt/workbuddy-manager/releases/current/.version
systemctl is-active workbuddy-web.service
curl -s -o /dev/null -w '%{http_code} ' https://你的域名/healthz \
  https://你的域名/readyz https://你的域名/dashboard/

# 2. 最近有没有异常
journalctl -u workbuddy-web.service --since '-30min' | grep -cE 'Traceback|ERROR'

# 3. 按次数排序看告警（比从头读有效）
journalctl -u workbuddy-web.service --since '-24h' -o cat \
  | grep -E 'WARNING|ERROR' \
  | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9:,]+//; s/[0-9]+/N/g' \
  | sort | uniq -c | sort -rn | head -12

# 4. 关键指标
TOK=$(cat /etc/zhihui/metrics-token)
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:7866/metrics \
  | grep -E '^workbuddy_(errors|disk|db_size|latency_ms_p|gateway_inflight |gateway_queued)'
```

**第 3 步是这份手册里最有用的一条** —— 按次数排序会把真正的问题顶到最上面，
而不是让你从头读几千行日志。
