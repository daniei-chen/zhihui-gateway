<div align="center">

<a href="https://zhihui.kaogong.art/">
  <picture>
    <source media="(max-width: 640px)" srcset="docs/images/readme/cover-mobile-v0.0.10.svg" />
    <img src="docs/images/readme/cover-v0.0.10.svg" width="100%" alt="智汇网关 0.0.10 · 让灵感出发，让工作向前。WorkBuddy、Qoder、Trae，独立授权与路由。" />
  </picture>
</a>

<br />

**一个工作台，三条清晰的路径。**

账号、模型、密钥与用量在一起；平台的授权、边界与记录不混在一起。

<a href="https://zhihui.kaogong.art/"><img src="docs/images/readme/button-home.svg" width="144" height="44" alt="访问智汇产品首页" /></a>
<a href="docs/RELEASE-0.0.10.md"><img src="docs/images/readme/button-release-v0.0.10.svg" width="166" height="44" alt="查看 0.0.10 增补构建、验收与上线状态" /></a>
<a href="#开发与部署"><img src="docs/images/readme/button-develop.svg" width="154" height="44" alt="阅读开发与受控部署指南" /></a>

<br /><br />

[本次更新](#010--受限稳定性补丁二) · [工作台](#看得清也用得顺) · [接入](#统一入口各有其路) · [能力边界](#把支持与不支持都讲清楚) · [开发与部署](#开发与部署)

</div>

<br />

## 0.0.10 · 受限稳定性补丁二

**本次构建：`v0.0.10+stable.20261107.3`**（含 0.0.9 构建 `v0.0.9+stable.20261107.1`）。两版继承 0.0.8 的全部已验收能力（Codex／Claude／DSH 模板兼容、默认单密钥与邀请注册、配对恢复、账本保护），在此之上修复 2026-11-06 生产审计的核心发现：

- **流式与平台**（0.0.9）：SSE 短响应不再被 16 KiB 分块缓冲到 EOF（ZE-01）；流中错误帧强制空行分隔（ZE-02）；`/qoder/v1/*`、`/trae/v1/*` 不再被 workbuddy realm 裁剪（ZG-02）；`count_tokens` 不再抢占生成并发槽。
- **门户与安全**（0.0.9/0.0.10）：登录失败限速并按 来源 IP+用户名 锁定（ZP-001）；账号管理操作补写 `audit_logs`（ZB-02 部分）；Anthropic 工具重复 id/name 去重（ZG-01）。
- **钱包与对账**（0.0.10）：`rejected` 终态的 portal 钱包占用自动释放（`WB_PORTAL_AUTO_CLOSE_REJECTED`，默认关闭）；预算自动对账恢复运行，unknown 不清零。
- **运维配置**：并发/预算闸门 1→8、上游超时 300s、MemoryHigh 1.5 GiB、swappiness 10；`/api/system/changelog` 修复为完整版本列表。

**现网已切换到本次构建。** 2026-11-07 10:26 经受控迁移事务完成切换（`v010-prod-forward: complete`）；三平台数据面、版本接口与更新日志接口均实测通过，审计日志 `release_id` 确认为本构建。源码与现网制品逐文件 sha256 一致（0.0.9：11 个变更文件；0.0.10：8 个变更文件）。本版未执行完整 Linux 门禁（基线口径见 0.0.8），回归测试随版交付，验证口径详见[发布说明](docs/RELEASE-0.0.10.md)。

**WorkBuddy × Codex / Claude / DSH 的特定系统模板兼容已内置。** 针对“普通 API 正常，编码客户端带完整系统上下文却返回 11128”的已复现问题，使用最小措辞或标点调整。11 条规则版本化交付，已有自定义规则保留优先级；用户、助手、工具参数与结果、图像和其他平台原文保留。规则可关闭，真实渠道拒绝仍然返回，不承诺绕过供应商授权或未来风控。[原因、规则、排查与验证 →](docs/WORKBUDDY-CODEX-COMPAT.md)

<a href="docs/RELEASE-0.0.10.md">
  <picture>
    <source media="(max-width: 640px)" srcset="docs/images/readme/release-mobile-v0.0.10.svg" />
    <img src="docs/images/readme/release-v0.0.10.svg" width="100%" alt="0.0.10：流式、门户与钱包修复；来源绑定与账本保护延续。验收以具体构建的签名回执为准。" />
  </picture>
</a>

**未关闭项如实列出。** `session_expired` 独立错误码、数据保留策略、告警通知渠道、SQLite `synchronous` 持久化级别与 nginx keepalive 均未在本轮关闭；长回复 4 MiB 输出保护等上游边界未调整，渠道真实拒绝仍会返回。[0.0.9 说明](docs/RELEASE-0.0.9.md) · [完整更新日志](CHANGELOG.md) · [GitHub Release ↗](https://github.com/daniei-chen/zhihui-gateway/releases/tag/v0.0.10)

> [!NOTE]
> 测试数量必须对应具体构建。本版以受控迁移事务与现网实测为验收口径，完整门禁回执沿用 0.0.8 基线；合成夹具不冒充所有客户端版本或供应商推理基准；真实断电、长期 SLO、国际账号真实推理与完整新主机容灾仍未验证。

<br />

## 看得清，也用得顺

冰蓝、留白、轻量交互。从账号池到调用记录，一处掌握。

<a href="https://zhihui.kaogong.art/">
  <picture>
    <source media="(max-width: 640px)" srcset="site/home/preview/dashboard-mobile.jpg" />
    <img src="site/home/preview/dashboard-desktop.jpg" width="100%" alt="智汇实际控制台构建预览：三平台切换、冰蓝趋势图、数据卡片与浮动 Dock。全部数字为合成数据，不是线上账单。" />
  </picture>
</a>

<p align="center"><sub>实际控制台构建渲染 · 合成数据 · 非当前生产状态截图</sub></p>

- **管理不割裂。** WorkBuddy、Qoder、Trae 共用工作台，保留平台切换、明暗主题、浮动 Dock 与五语界面。
- **授权有边界。** 普通管理员可浏览脱敏信息、添加授权账号；超级管理员维护密钥额度、用户权限与系统设置。
- **用量不猜测。** 查看缓存命中、输出速度和请求记录；缺少可靠用量时显示未知，不包装成零消耗。
- **用户有自己的空间。** 独立工作台管理绑定账号、签到批次、密钥与请求记录；财务策略以已批准配置和可核验证据为准。
- **管家只做该做的事。** 基于有限脱敏证据研判，规则告警与 AI 补充分开；不授予 shell、凭据读取或自动改账权限。

<details>
<summary><b>展开更多工作面板</b> — 账号池、模型、密钥与用量</summary>

<br />

| 账号池 · 身份与状态 | 模型 · 来源与能力 |
| :---: | :---: |
| <img src="site/home/preview/accounts-desktop.jpg" width="100%" alt="账号池实际构建预览，使用合成账号与状态。" /> | <img src="site/home/preview/models-desktop.jpg" width="100%" alt="模型中心实际构建预览，使用合成示例。" /> |
| **密钥 · 按需授权** | **用量 · 核对每次消耗** |
| <img src="site/home/preview/keys-desktop.jpg" width="100%" alt="密钥管理实际构建预览，不含真实凭据。" /> | <img src="site/home/preview/stats-desktop.jpg" width="100%" alt="用量面板实际构建预览，数值不是线上账单。" /> |

这些是历史控制台构建的合成预览，不是另画的概念面板，也不证明供应商可用性。
[预览来源与摘要](site/home/preview/manifest.json) · [README 视觉素材清单](docs/images/readme/manifest.json)

</details>

<br />

<picture>
  <source media="(max-width: 640px)" srcset="docs/images/readme/capabilities-mobile.svg" />
  <img src="docs/images/readme/capabilities.svg" width="100%" alt="接入、授权、核对：三平台独立路由，按需授权，不把缺失用量当作零。" />
</picture>

<br />

## 统一入口，各有其路

切换控制台视图，不会悄悄改变客户端的访问地址。

<picture>
  <source media="(max-width: 640px)" srcset="docs/images/readme/routing-mobile.svg" />
  <img src="docs/images/readme/routing.svg" width="100%" alt="客户端经智汇鉴权、路由与计量后分别访问：WorkBuddy /v1/*、Qoder /qoder/v1/*、Trae /trae/v1/*。" />
</picture>

| 平台 | 客户端 Base URL | 提醒 |
| :--- | :--- | :--- |
| WorkBuddy | `https://你的域名/v1` | Chat、Responses、Messages 适配 |
| Qoder | `https://你的域名/qoder/v1` | 以该引擎能力和账号授权为准 |
| Trae | `https://你的域名/trae/v1` | 以该引擎能力和账号授权为准 |

<details>
<summary><b>一个最小调用示例</b> — 使用自己已授权的模型和网关密钥</summary>

```bash
curl https://你的域名/v1/chat/completions \
  -H "Authorization: Bearer $ZHIHUI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "YOUR_AUTHORIZED_MODEL",
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 128
  }'
```

先在模型页确认实际模型 ID、平台与地区权限。网关密钥不是供应商账号授权；不要将真实密钥写入仓库、截图或工单。

</details>

[路由与权限](manager/server/routers/gateway.py) · [预算预留](manager/server/budget.py) · [不可变账本](manager/server/ledger.py)

<br />

## 把支持与不支持都讲清楚

| Responses 能力 | 当前合同 |
| :--- | :--- |
| 显式完整历史、普通工具回合、SSE、图片 | 支持并回归验证 |
| `previous_response_id` / `conversation` | 不支持服务端历史；转发前明确 400 |
| `background=true` | 不支持后台任务；明确 400 |
| `parallel_tool_calls=false` / 函数 `strict=true` | 上游不能保证；明确 400，不静默忽略 |
| 自定义工具 grammar | 仅描述性提示，不宣称严格执行 |

> [!IMPORTANT]
> **失败不等于零费用，取消不等于供应商停止。** 处理状态不明时保留 unknown；发送与响应头事件不是供应商账单。版本回退只回退代码，不回退刚产生的消费、授权或会话撤销记录。

<details>
<summary><b>展开可靠性与恢复边界</b></summary>

- 主账本采用 WAL/FULL，但物理持久性仍依赖操作系统与存储；进程崩溃恢复测试不是生产断电试验。
- 双账本快照以 main→wallet 顺序短暂获取写锁。完整性与业务一致性检查、隔离恢复在复制后进行。
- 恢复只写新隔离目录，禁用调用、撤销旧会话、封存凭据，不将旧余额直接启用。
- 内部密钥已轮换；代码回滚保留新密钥，不重新启用已经撤销的旧凭据。
- 现网采用排空后受控切换，有短维护窗口，不宣称零停机。历史 unknown、弱口令告警和其余审计项独立跟进。

</details>

<br />

## 开发与部署

**Python 3.12 · Node.js 22+ · Next.js 16 · React 19**

```bash
git clone https://github.com/daniei-chen/zhihui-gateway.git
cd zhihui-gateway
```

| 你的目标 | 从这里开始 |
| :--- | :--- |
| 本地运行、改版或贡献 | [开发指南](docs/DEVELOPMENT.md) · [控制台源码](src/three-platform/web/) · [Manager](manager/server/) |
| 理解版本、签名与回滚 | [产品发布合同](docs/PRODUCT-RELEASE.md) · [0.0.10 验收](docs/RELEASE-0.0.10.md) |
| 查看首页与视觉实现 | [首页源码](site/home/index.html) · [集成与复现](site/home-source/INTEGRATION.md) |
| 追溯实现及剩余工作 | [完整更新日志](CHANGELOG.md) · [历史审计边界](docs/AUDIT-0.0.3.md) |

本地开发不是一键安装完整三平台服务。引擎、账号授权、认证与持久化需要单独配置，静态导出也不包含后端。

```text
固定来源 → 构建与隔离测试 → 哈希、签名与回执 → 受控发布 → 运行核验
```

**不要把 GitHub 自动源码包当作生产安装包。** 现有受控实例仅使用受信发布器，不直接覆盖运行目录，不执行 `git pull && restart`，也不为回退代码恢复旧业务数据库。公开 tag 的提交记录与现网签名清单分别留证，不互相冒充。

<br />

## 致谢与许可

智汇在既有组件上持续演进。感谢 WorkBuddy Manager contributors、workbuddy2api、Qoder / Trae 相关项目与 [linux-do/cdk](https://github.com/linux-do/cdk) 的界面基础。

产品源码采用 [MIT](LICENSE)。组件版权与第三方声明见 [Manager LICENSE](manager/LICENSE)、[来源声明](manager/UPSTREAM-NOTICES.md)；模型与账号授权遵循各自服务条款，不绕过供应商授权限制。

封面、能力卡片与路由图为可编辑、自包含 SVG，没有外部字体、脚本或远程图片依赖。历史首页扫描素材及衍生海报遵循原 CC BY-SA 4.0，相关许可与原创替换范围见[首页集成说明](site/home-source/INTEGRATION.md)。

<div align="center">
<br />
<a href="https://zhihui.kaogong.art/"><img src="brand/zhihui-z.svg" width="32" height="32" alt="智汇蓝色 Z" /></a>
<p><sub>智汇 ZHIHUI · 少一点喧闹，多一点可控。</sub></p>
</div>
