# 首页、登录与控制台，是同一个智汇

- `home/index.html`：接入用户提供的新版首页；保留蓝白首屏、悬浮 Z、平台光路、账本与八页工作台预览，并加入独立动效章节和延迟加载的三维展示。
- `home/r-<hash>/`：内容寻址的静态资源。现有 `/home-assets/` 使用一年 immutable 缓存；不能复用同一个 URL 覆盖不同脚本。
- `home-import.json`：当前首页、登录和全部资源的来源、大小与哈希，不代表已部署。
- `home-source/`：可编辑脚本、Three.js 源码、测试与历史快照；复现见其 `INTEGRATION.md`，不包含原始 61 MB 模型中间文件。
- `home/preview/`：由同一份控制台静态构建、隔离合成数据生成的桌面 / 手机预览；`manifest.json` 绑定构建 ID、样式摘要和每张图片哈希。
- `login/index.html`：逐字节保留线上原版三平台登录页。不采用首页包内的三维登录设计。`login/preservation.json` 绑定只读原版哈希，允许文字差异为空；首页导入不得覆盖它。
- `favicon/zhihui-z-20261029/`：同一枚蓝色 Z 的 SVG、PNG、ICO 和 Web Manifest。
- 控制台源码：`src/three-platform/web/`，保留 Dock、动画、平台/语言切换与原有业务页面。

本目录没有生产凭据。首页图示和数字是产品介绍，不是实时生产指标。
README 的首页截图由本目录真实页面本地渲染，控制台截图仅使用合成数据。
生产 Nginx 路径与静态资源别名需要按实际部署配置；不能把整个目录直接覆盖到旧发布树。

## 本地验证

运行 `node site/tools/preview-server.mjs --port 4184`，浏览 `/` 与 `/login`。
本地服务按正式 `/home-assets/` 命名空间、MIME 和窄 CSP 提供资源，不代理业务 API；原版登录预览在浏览器内用模拟响应接管 fetch，CSP 禁止网络连接和表单提交，不改实际登录源码；
控制台链接只到达本地导航验证页，不会登录生产。运行 `node --test site/tests/home-import.test.mjs`
检查清单、资源闭包、路由冲突与本地凭据隔离。真正的登录、账号与密钥验收须在隔离业务环境另做。

首页需要 `frame-src 'self'` 承载 scripts-only 沙箱插画；雕塑 Draco 解码需要
`worker-src 'self' blob:` 与 `script-src 'self' 'wasm-unsafe-eval'`。原版登录保留内联脚本，预览 CSP 使用该脚本的精确 SHA256，不需要通用 `unsafe-eval` 或脚本 `unsafe-inline`，也不能放宽其 `frame-ancestors 'none'`。正式 Nginx 尚未修改。

任务/骑行插画保留来源记录，但尚未确认再分发许可；不要把它们描述为原创 MIT 素材。
雕塑及衍生海报保留 CC BY-SA 4.0，Three.js 和 Draco 保留各自许可。上线前必须处理素材权利边界。

## 历史浏览器回归

旧版布局的回归脚本 `site/tests/home-browser.cjs`、`site/tests/home-mobile.cjs` 正在迁移到新版，
不能把其历史结果当作当前页面通过。它们使用已有 Playwright 安装，或通过
`PLAYWRIGHT_MODULE` 指向安装模块。测试仅启动本地静态服务，拦截外部连接，不读取生产数据。
截图和结果默认进入系统临时目录，也可用 `HOME_QA_OUTPUT` 指定目录。

覆盖首屏入口、原始章节完整性、真实控制台预览图片、平台 / 光路 / 代码联动、账本状态、七个工作台面板、详情抽屉、FAQ、
四种屏宽、光路几何边界、触屏 / 无 JS / 减少动态效果退化。发布时独立保留旧首页目录，精确比对前身后原子切换，
不修改业务数据库。首页、独立登录别名和 CSP 的发布必须使用同一个经过核验的发布清单，不能只切换首页。

另运行 `node site/tests/home-mobile.cjs`：在 Chromium / WebKit 的真实触屏上下文下，
检查 320 / 390 / 430 宽度的首次绘制、返回页面、减少动态效果与无 JS 状态。
同时检查标题和入口的祖先透明度、首屏位置、实际点击命中及导航目的地，
不能以 `isVisible()` 或“布局里有按钮”替代实际可见性。测试中的控制台目的地由本地
fixture 接管，不登录生产或调用业务 API。只暂停装饰动画，不暂停首屏内容的显示。
