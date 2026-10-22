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

## AI 告警分析（2026-10-22）

CRITICAL / WARN 级别触发时，先让模型给出诊断，再随邮件发出。INFO 不调用（省 token）。

### 降级链

```
第 1 跳  DeepSeek-V4.1    http://127.0.0.1:7864/v1（本机网关）
            ↓ 失败（网关自己挂了 / 超时）
第 2 跳  GLM-5.3-Flash    https://open.bigmodel.cn/api/coding/paas/v4（智谱直连）
            ↓ 也失败
兜底      不发分析，邮件仍按原模板送达
```

**刻意让第一条依赖被监控链路**：网关宕机（R1）时它会连接拒绝，正好验证降级真能生效。

### 提示词要点

- 输出严格四段：`[判断]` `[根因]` `[处置]` `[风险]`
- 要求给可直接粘贴的命令，不要泛泛而谈
- 总长 300 字以内
- 信息不足时明确说"建议先查 X"，禁止编造

### 踩过的坑

| 坑 | 现象 | 处理 |
|---|---|---|
| **max_tokens 太小** | GLM-5.3-Flash 的 reasoning 吃掉 780 tokens，content 返回空，被误判失败 | 提到 **2000** |
| **思考型模型 content 为空** | 部分档位把输出全放 `reasoning_content` | 空 content 时用 `reasoning_content` 兜底 |

### 耗时实测

| 链路 | 耗时 | 输出长度 |
|---|---|---|
| DeepSeek-V4.1（网关） | ~5–6 秒 | 560–660 字 |
| GLM-5.3-Flash（智谱） | ~16–25 秒 | 430–490 字 |

降级到 GLM 时整体延迟约 25 秒（含第一跳失败等待），可接受。

## 鲸鱼娘通知头像（R1–R10 各一张）

邮件里每条规则配一张拟人角色图，替代原来的四色徽章。

| 规则 | 图 | 场景 |
|---|---|---|
| R1 网关宕机 | `whale-r1.png` | 举着 X 木牌，身后是倒塌的门 |
| R2 上游僵死 | `whale-r2.png` | 双手拽紧绷的绳子，拉不动 |
| R3 客户端拒绝 | `whale-r3.png` | 双手交叉，旁边挂禁入牌 |
| R4 上游限流 | `whale-r4.png` | 被蓝色水流冲得东倒西歪 |
| R5 备份过期 | `whale-r5.png` | 抱着一堆标签卷边的罐头 |
| R6 异地未配 | `whale-r6.png` | 身边只有一只孤零零的行李箱 |
| R7 内存吃紧 | `whale-r7.png` | 抱着鼓成球的肚子 |
| R8 僵尸进程 | `whale-r8.png` | 身后跟着三个透明幽灵鱼 |
| R9 内存超限 | `whale-r9.png` | 被透明方块压成一张饼 |
| R10 风控拦截 | `whale-r10.png` | 铁门贴红封条，叉腰不服气 |

**规格**：144×144 圆形裁剪，量化到 128 色，单张 28–38KB。
邮件里显示 104×104（2x 源图保证 retina 清晰）。

**降级顺序**：`whale-rN.png` → 四色徽章 → 不显示图片。
任一环节缺失都不影响邮件送达。

**素材来源**：AI 生成后由 `install.sh` 分发到 `/var/www/mail-assets/`，
外链地址 `https://dsh.kaogong.art/mail-assets/whale-rN.png`。

## 蓝色大肥鱼人设（来自社区原始设定，不是自拟）

网上对 DeepSeek 蓝鲸 logo 的娘化形象有一整套成型设定，直接沿用：

| 设定项 | 内容 |
|---|---|
| 身份 | 「蓝色大肥鱼」，DeepSeek 家的鲸鱼娘 |
| 主食 | 白米饭；人生头等大事是等「token 来！」 |
| 对用户的称呼 | **「鱼片」** |
| 性格 | 聪明但懒、傲娇又黏人、**死也不承认自己胖** |
| 行为 | 平时躺着，到点想下班吃饭；被叫起来会嘟囔但会认真看 |
| 经典台词 | 「我去吃饭了，有事叫我」「本鱼刚扒两口饭就被叫起来了」 |

提示词里写了 5 条示例台词做语气锚点，比抽象描述有效得多。

**底线约束**（防止撒娇过头）：
- 禁止 swap/容器/探活/OOM/RSS/journalctl，非说不可要加括号解释成大白话
- 100 字以内
- 不给命令清单，最多说一个「点一下就成」的动作
- 拿不准就说「本鱼再看看」，禁止硬编原因

### 实际输出样例

```
[要不要管] 先看看，问题不大
[怎么回事] 鱼片，机器内存有点紧，开始拿硬盘当饭碗垫肚子了，还没撑破。
[怎么办] 不用动，我盯着，再往上涨我喊你。
```

```
[要不要管] 要紧，马上处理
[怎么回事] 唔…鱼片，网关彻底歇气了，靠它干活的服务全跟着倒下，这不能算小毛病。
[怎么办] 先把网关那个服务重启一下，点一下就成。
```

```
[要不要管] 先看看，问题不大，但别拖到明天。
[怎么回事] 鱼片，机器倒是没闹脾气，可那份自动备份两天没动静了，
           估计是定时干活的那摊子偷懒了。
[怎么办] 你去瞅一眼那个定时备份的计划还在不在跑，点一下确认就好。
```
