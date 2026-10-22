# ops/ —— 网关运维层

这里放「不属于上游项目、但网关运行必需」的本地运维资产。改动后跑 `sudo ops/install.sh` 生效。

## 内容

| 文件 | 作用 | 安装到 |
|---|---|---|
| `wb-gateway-alert.py` | 5 分钟巡检 + 10 条告警规则 + 邮件渲染 | `/usr/local/bin/` |
| `wb-gateway-alert.example.json` | 渠道配置模板（**不含密钥**） | 手工复制为 `/etc/wb-gateway-alert.json` |
| `mail-assets/` | 告警邮件用的图（鲸鱼横幅 + 4 个级别徽章） | `/var/www/mail-assets/` |
| `install.sh` | 一键安装/更新（`--check` 只看差异） | — |

## 告警规则（R1–R10）

| 规则 | 触发条件 | 级别 |
|---|---|---|
| R1 | 服务非 active / 容器非 healthy / 探活非 2xx | CRITICAL |
| R2 | 上游 attempt timeout ≥3 次 / 12 分钟 | WARN |
| R3 | 客户端真实吃到拒绝（并发/配额/容量/池干涸） | WARN |
| R4 | 上游 429 软限流 ≥5 次 | INFO |
| R5 | 每日备份 >26 小时未更新 | WARN |
| R6 | 异地副本未配置（offsite=false） | INFO |
| R7 | swap 使用 ≥60% | WARN |
| R8 | 僵尸进程 ≥3 个 | WARN |
| R9 | 非核心进程单实例内存 >1GB | WARN |
| R10 | WorkBuddy 风控拒绝（11-128）≥5 次 / 10 分钟 | CRITICAL |

## 邮件渲染的三个坑（都已踩过）

1. **`data:` URI 图片被 QQ 邮箱屏蔽** —— base64 内联图当可疑内容过滤。
2. **CID（`multipart/related`）在 QQ 里也不渲染** —— 结构对的但就是不显示。
3. **最终方案：HTTPS 外链** —— 图片放 `/var/www/mail-assets/`，nginx 的
   `/mail-assets/` 路由 `auth_request off`（邮件客户端不带 cookie），
   模板里用绝对 URL。首次打开需点「显示图片」。

## nginx 依赖

```nginx
location ^~ /mail-assets/ {
    auth_request off;
    access_log off;
    root /var/www;
    expires 7d;
    add_header Cache-Control "public, max-age=604800";
    add_header X-Content-Type-Options "nosniff" always;
}
```

## 配置说明

`/etc/wb-gateway-alert.json`（**600，不入库**）：

```json
{
  "wechat":        { "enabled": true,  "api": "http://127.0.0.1:3080/api/dsh-im/delivery/messages" },
  "weixin_direct": { "enabled": true,  "tokenRef": "<从 DSH 凭据库取>" },
  "email":         { "enabled": true,  "smtp_host": "smtp.163.com", "smtp_port": 465,
                     "user": "发信邮箱", "password": "<SMTP 授权码>",
                     "to": "收件1@qq.com, 收件2@qq.com" }
}
```

**收件人支持逗号/分号/空格分隔的多地址。**

## 已知问题

- 微信两条通道当前不可用：`weixin_direct` 返回 `{"ret":-2,"errmsg":"prepare failed"}`，
  `dsh_im` 返回 `401`。**邮件是当前唯一可用的告警通道。**

## 触发方式（两条路径）

| 路径 | 触发源 | 检测延迟 | 常驻开销 |
|---|---|---|---|
| **OnFailure** | `workbuddy-web.service` 的 `OnFailure=` | **秒级** | 0 |
| **定时巡检** | `wb-gateway-alert.timer`（`*:0/1`） | **≤1 分钟** | 0（跑完退出） |

```ini
# workbuddy-web.service
[Unit]
OnFailure=wb-alert-onfailure@%n.service

# wb-alert-onfailure@.service（模板）
[Service]
ExecStart=/opt/workbuddy-manager/venv/bin/python /usr/local/bin/wb-gateway-alert.py --onfailure %i
```

`--onfailure` 模式不跑巡检，直接发一封信，内容含 `ActiveState / SubState / Result / NRestarts`
与最后 12 行 journal —— 崩溃现场第一手证据。

**刻意没做 `journalctl -f` 常驻监听**：需持续 30–50MB 常驻内存，而这台机器
（3.7G 总、DSH 自身占 750MB）余量只有 1.2–1.4G，不值得为「100 毫秒 vs 60 秒」付这个代价。

**资源实测**：单次巡检 254ms / CPU 0.22s / 峰值内存 27MB（跑完释放）。

## 覆盖边界

| 故障 | 能告警 |
|---|---|
| 管理端进程崩溃 | ✅ OnFailure，秒级 |
| 上游容器退出 | ✅ OnFailure（若挂同一钩子） |
| 应用报错 / 资源异常 | ✅ 巡检，≤1 分钟 |
| **整机宕机** | ❌ 脚本同机，需外部拨测 |
| **网络断开** | ❌ 邮件发不出去 |
| **SMTP 故障** | ❌ 落到 `/var/log/wb-gateway-alerts.fired` |
