# 告警该走哪个池子

系统里有**两套并存的告警机制**。这份文档说明它们的区别，以及新加一条告警时该选哪个。

**不写清楚这条线，结果是两个池子各自长出自己的规则，交集靠人记得住。**
2026-11-25 就因为这个，让一条「账号余额快空了」的告警在页面上挂了一个多小时，
而同一套系统里磁盘、证书、备份的告警都会主动发邮件。

## 一、两个池子

| 池子 | 定义在 | 出口 | 谁看得到 |
|---|---|---|---|
| **operational** | `server/operational.py` + `operational_alerts` 表 | 管理页面、`/health/alerts`（仅回环）、`healthcheck.sh` | **主动去看的人** |
| **steward** | `server/steward_rules.py` + `steward_deliveries` 表 | **邮件**（半小时去重 + AI 研判 + 恢复通知） | **会被打扰的人** |

### 1.1 operational 池子的投递出口：默认关闭，但不能静默

`operational` 的告警除了页面和巡检，还有一个**可选**出口：`server/notify.py`。
它默认是 `disabled`（不擅自外发），默认部署下**不会有人被叫醒**。

`disabled` 不等于「什么都没发生」：告警已经被算出来了，只是送不出去。所以丢弃时
必须留下痕迹，三层各一处，任一层都能看见：

| 层 | 位置 |
|---|---|
| 本地 JSONL | `$WB_DATA_DIR/notify/undelivered.jsonl`（`.../data/notify/undelivered.jsonl`） |
| 应用日志 | `workbuddy.notify` 的一条 WARNING，点名告警名 |
| 健康端点 | `/api/health/alerts` 的 `notify` 字段（`configured` / `warning` / `dropped`） |

`dropped.count > 0` 就意味着**这些告警此刻只有这一处能看见**。启动日志也会明确写
「告警未配置，运行告警将被丢弃」。

要真正投递，设置 `WB_NOTIFY_CONFIG`（默认 `/opt/notify/config.json`）指向一个**启用**的配置。
最小可用形态是 `file` 模式 —— 只落本地 JSONL，不联网：

```json
{
  "enabled": true,
  "mode": "file",
  "file": "/opt/notify/alerts.jsonl"
}
```

```bash
sudo install -d -o root -g root -m 0755 /opt/notify
sudo tee /opt/notify/config.json >/dev/null <<'JSON'
{"enabled": true, "mode": "file", "file": "/opt/notify/alerts.jsonl"}
JSON
# 输出文件必须**先建好**并交给服务 UID：容器里以 uid 10001 运行（Dockerfile:155），
# 而 /opt/notify 是 root 的 0755 —— 服务自己建不了文件，dispatch() 会
# PermissionError，每条告警都投递失败，而 status() 仍然报 configured=true，
# 于是「已配置」的假象下反而比未配置更安静。配置本身保持 root 拥有即可。
sudo install -o 10001 -g 10001 -m 0640 /dev/null /opt/notify/alerts.jsonl
# 不需要重启：每次 dispatch 都重新读配置
# 验证：触发一条告警后 tail -f /opt/notify/alerts.jsonl
```

`http` 模式（POST 到 `url`）同样需要 `"enabled": true` 且 `mode` 为 `http`；
`timeout_sec` 会被夹在 0.1–3 秒，投递失败只记 warning，不影响告警状态机。

## 二、怎么选

### 进 steward（发邮件）

**判据：会影响用户，而且现在就该有人动手。**

| 类型 | 现例 |
|---|---|
| 容量将耗尽 | `disk_capacity`、`pool_capacity`、`platform_*_credits` |
| 余额/额度将耗尽 | `platform_workbuddy_credits`、`key_quota_near_limit` |
| 数据保护失效 | `backup_evidence`、`budget_evidence` |
| 证书将过期 | `certificate_expiry` |
| 当时就在失败 | `inference_failure`、`inference_latency` |
| 账本通道异常 | `ledger_channel` |

共同点：**不处理就会变成用户故障，而且处理窗口有限。**

### 进 operational（只进页面/巡检）

**判据：趋势性、可以观察、有充足反应时间，或者本来就是诊断信息。**

| 类型 | 现例 |
|---|---|
| 版本一致性 | 发布指针与 `VERSION` 不符 |
| 发布树残留 | `__pycache__` 会让 `seal` 失败 |
| 配置契约 | 当前配置能否被运行中的版本接受 |
| 单次延迟超标 | 3 次首字节超过 60 秒 |
| 统计类 | 内存 OOM 计数、错误率趋势 |

共同点：**看了会知道，不看也不会立刻出事。**

## 三、三个容易搞错的地方

### 3.1 「探针正常」不等于「余额正常」

`platform_workbuddy` 这条规则看的是**只读探针状态是否为 `up`**。
接口能连上不代表账号里还有积分。

2026-11-25 就是因为把这两件事当成一件事，导致余额告警没有邮件出口。
现在它们是两条独立的规则：`platform_workbuddy` 和 `platform_workbuddy_credits`。

### 3.2 判据只能有一个来源

`key_quota_near_limit` 的阈值和单位换算全在 `credit_watch.quota_alerts()` 里。
`steward_snapshot.quota_fact()` **直接转述它的结论**，不重写 SQL。

重写一份 = 同一判据两个副本。改了一处忘另一处，就会出现
「页面说告警、邮件说没事」—— 比完全不告警更糟，因为运维会以为自己看全了。

### 3.3 「未知」不是「正常」

两个池子都用 `None` / `active=None` 表示**采不到数据**。

报 `False` 等于宣布「一切正常」，而实际上我们不知道。
`steward_snapshot` 顶部那句注释是这条规则的出处：

> A failed collector is unknown, not an empty healthy account pool.

## 四、新加一条告警的检查清单

1. **它会影响用户吗？** 会 → 考虑 steward；不会 → operational
2. **不处理会变成故障吗？** 会 → steward
3. **现在就该有人动手吗？** 是 → steward
4. **这个判据还有别的地方在用吗？** 有 → **转述，不重写**
5. **采不到数据时返回什么？** 必须是「未知」，不是「正常」
6. **如果要发邮件，规则名会不会和现有的撞？** 加后缀区分（`_credits` / `_evidence`）
7. **怎么验证它没坏？** 找一个共用上游步骤的旁证信号 —— 它失败了会一起消失

## 五、现状（2026-11-25）

**steward 的 16 个规则名**（会发邮件）：

```
backup_evidence        budget_evidence          certificate_evidence
certificate_expiry     channel_verification     disk_capacity
inference_failure      inference_latency        key_quota_near_limit
ledger_channel         memory_capacity          memory_evidence
platform_<name>        platform_<name>_credits  pool_capacity
upstream_authorization
```

**这份清单必须靠核对，不能靠印象。**初版写成「14 条」并列了 15 个名字，
漏了 `upstream_authorization` —— 它是 `steward_rules.py` 里那个三元组循环的
第二项，和 `channel_verification`、`pool_capacity` 一起，同一个 `Signal(`
调用点展开成三条规则。数 `Signal(` 的个数会得到 14，数规则名才是 16。

**operational 的 18 项**（只进页面/巡检）：见 `server/operational.py` 里的 `_safe_alerts` 调用链。

**两边交集** = 4 条：余额、配额、磁盘、延迟。

## 六、这份文档为什么存在

第 60、61 两轮都在补同一个结构问题的后果：一条该发邮件的告警落在了不发邮件的池子里。

**问题不在「漏了一条」，而在于没有书面判据。**每次加告警都是凭当时的感觉选池子，
选错的代价要等到真出问题才发现 —— 而那时它已经沉默了足够久。