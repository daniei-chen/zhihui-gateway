<div align="center">

<a href="https://zhihui.kaogong.art/">
  <picture>
    <source media="(max-width: 640px)" srcset="docs/images/readme/cover-mobile-v0.0.10.svg" />
    <img src="docs/images/readme/cover-v0.0.10.svg" width="100%" alt="智汇网关 1.2.0 · 让灵感出发，让工作向前。WorkBuddy、Qoder、Trae，独立授权与路由。" />
  </picture>
</a>

<br />

**一个工作台，三条清晰的路径。**

账号、模型、密钥与用量在一起；平台的授权、边界与记录不混在一起。

<a href="https://zhihui.kaogong.art/"><img src="docs/images/readme/button-home.svg" width="144" height="44" alt="访问智汇产品首页" /></a>
<a href="CHANGELOG.md"><img src="docs/images/readme/button-release-v1.2.0.svg" width="156" height="44" alt="查看 1.2.0 版本说明" /></a>
<a href="#开发与部署"><img src="docs/images/readme/button-develop.svg" width="154" height="44" alt="阅读开发与受控部署指南" /></a>

<br /><br />

[本次更新](#120--稳定性大版本) · [工作台](#看得清也用得顺) · [接入](#统一入口各有其路) · [能力边界](#把支持与不支持都讲清楚) · [开发与部署](#开发与部署)

</div>

<br />

## 1.2.0 · 稳定性大版本

**本次构建：`v1.2.0+local.20261109.21`**。这一版不新增功能，把此前二十次修复累积成一个正式里程碑。全部改动经完整隔离门禁（4974 通过 / 0 失败）、Ed25519 签名与可信预检，并已完成生产切换与回滚演练。

- **稳定性**：批量签到、刷新额度、领取补偿、核对令牌四个批量入口，单个账号出错不再拖垮整批；上游输出超限返回明确错误类型，不再混作传输错误；门户账本繁忙时返回可重试提示；客户端中途取消留下的未决冻结随例行对账自动释放，不再依赖服务重启。
- **账务**：回执缺少供应商用量时不再回落成用户扣费金额；用户账本与主库的一致性检查改为独立自检、分钟级执行，不再依附每日备份；新增账目缺失检出。
- **账号与任务**：三平台账号「冷却中」判定改为要求到期时间确实在未来（实测 48 个账号中 28 个曾被永久误标）；随机签到的未确认状态增加超期出口（实测解除一次持续 8.8 天的死锁）；每日任务失败写入审计记录；单账号签到按平台分别声明。
- **可观测性**：非流式请求的首字节耗时也会被记录；账目缺失提示改为只在变化时记录一次；计划设置查询如实返回已保存状态。
- **安全**：复核确认网关进程以非特权账号运行且不持有任何能力位，权限分离由启动链保证；启动链源码纳入版本留存。

**现网已切换到本次构建。** 生产切换后实测：健康检查 200、外网访问正常、零 500 错误；回滚路径已双向演练验证（切至 v0.1.22 再切回，业务请求均正常）。源码与现网制品逐文件 sha256 一致，manifest 共 589 个文件，Ed25519 签名校验通过。

**长回复截断问题已解决。** 输出上限由 4 MiB 提升至 16 MiB，与单次输出的 token 上限自洽；首字节预算显式设为 180 秒（实测 p99 为 52.5 秒，覆盖 99.998% 的请求），客户端不再干等 5 分钟才收到失败。

**未关闭项如实列出。** 前端构建产物未纳入发布门禁覆盖；Git 仓库尚无异地远端；历史遗留的 256 条待核对账目需人工确认；渠道真实拒绝仍会返回，不承诺绕过供应商授权或未来风控。[完整更新日志](CHANGELOG.md) · [GitHub Release ↗](https://github.com/daniei-chen/zhihui-gateway/releases/tag/v1.2.0)

> [!NOTE]
> 测试数量必须对应具体构建。本版以完整隔离门禁（4974 项通过、0 项失败）与现网实测为验收口径；合成夹具不冒充所有客户端版本或供应商推理基准；真实断电、长期 SLO 与完整新主机容灾仍未验证。

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
| 理解版本、签名与回滚 | [产品发布合同](docs/PRODUCT-RELEASE.md) · [1.2.0 变更](CHANGELOG.md) |
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
