<div align="center">

<a href="https://zhihui.kaogong.art/">
  <picture>
    <source media="(max-width: 640px)" srcset="docs/images/readme/cover-mobile-v0.0.10.svg" />
    <img src="docs/images/readme/cover-v0.0.10.svg" width="100%" alt="智汇网关 1.2.3 · 让灵感出发，让工作向前。WorkBuddy、Qoder、Trae，独立授权与路由。" />
  </picture>
</a>

<br />

**一个工作台，三条清晰的路径。**

账号、模型、密钥与用量在一起；平台的授权、边界与记录不混在一起。

**中文** · [English](README.en.md)

<a href="https://zhihui.kaogong.art/"><img src="docs/images/readme/button-home.svg" width="144" height="44" alt="访问智汇产品首页" /></a>
<a href="CHANGELOG.md">查看 1.2.3 版本说明</a>
<a href="#开发与部署"><img src="docs/images/readme/button-develop.svg" width="154" height="44" alt="阅读开发与受控部署指南" /></a>

<br /><br />

[本次更新](#123--不停机发布工程化) · [工作台](#看得清也用得顺) · [接入](#统一入口各有其路) · [能力边界](#把支持与不支持都讲清楚) · [开发与部署](#开发与部署)

</div>

<br />

## 1.2.3 · 不停机发布工程化

**本次版本：`v1.2.3`**。这一版把双代服务交接、共享准入、排空、回滚和发布证据收敛为可验证的控制链。

- **不停机交接**：writer guard 使用受控 `server.serve`、75 秒 graceful drain 和 shared admission；候选代不运行后台作业。
- **路由与回滚**：inference-only relay 使用原子单调 route epoch，健康异常自动回退，旧代有在途请求时延后退休。
- **证据与安全**：Ed25519 两阶段签名、恢复证据、快照只读审计、watchdog fail closed 和 host closure manifest 纳入门禁。

**发布边界如实列出。** GitHub Release 需等待 Ubuntu CI、产物摘要和草稿 Release 核对；本次源码版本不能替代国内服务器当前运行版本的独立验收。[完整更新日志](CHANGELOG.md) · [GitHub Release ↗](https://github.com/daniei-chen/zhihui-gateway/releases/tag/v1.2.3)

> [!NOTE]
> 测试数量必须对应具体构建。发布前以 GitHub Ubuntu 门禁、构建产物摘要和服务器运行核验为验收口径；合成夹具不冒充所有客户端版本或供应商推理基准。

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

| Method | Path | 说明 |
| :--- | :--- | :--- |
| GET | `/v1/models` | WorkBuddy 模型目录 |
| POST | `/v1/chat/completions` | WorkBuddy 对话调用 |
| POST | `/v1/responses` | Responses 兼容入口 |
| POST | `/v1/messages` | Anthropic Messages 兼容入口 |
| GET | `/qoder/v1/models` | Qoder 模型目录 |
| POST | `/qoder/v1/chat/completions` | Qoder 对话调用 |
| POST | `/trae/v1/chat/completions` | Trae 对话调用 |

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

[路由与权限](server/routers/gateway.py) · [预算预留](server/budget.py) · [不可变账本](server/ledger.py)

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
| 本地运行、改版或贡献 | [开发指南](docs/DEVELOPMENT.md) · [控制台源码](web/) · [网关服务](server/) |
| 理解版本、签名与回滚 | [产品发布合同](docs/PRODUCT-RELEASE.md) · [1.2.3 变更](CHANGELOG.md) |
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

产品源码采用 [MIT](LICENSE)。组件版权与第三方声明见 [许可证](LICENSE)、[来源声明](UPSTREAM-NOTICES.md)；模型与账号授权遵循各自服务条款，不绕过供应商授权限制。

封面、能力卡片与路由图为可编辑、自包含 SVG，没有外部字体、脚本或远程图片依赖。历史首页扫描素材及衍生海报遵循原 CC BY-SA 4.0，相关许可与原创替换范围见[首页集成说明](site/home-source/INTEGRATION.md)。

<div align="center">
<br />
<a href="https://zhihui.kaogong.art/"><img src="brand/zhihui-z.svg" width="32" height="32" alt="智汇蓝色 Z" /></a>
<p><sub>智汇 ZHIHUI · 少一点喧闹，多一点可控。</sub></p>
</div>

