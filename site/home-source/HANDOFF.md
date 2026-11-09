# 智汇首页升级交接文档

交接日期：2026-10-30（Asia/Shanghai）。供后续开发者或 AI 接手使用。

## 当前状态与边界

项目基于线上首页的只读快照升级，交付包包含本地可运行页面、静态构建、可编辑源码和公开模型资产。

- 原项目目录：`D:\AI\codex\codex1\zhihui-home-upgrade`。解压后可在其他目录运行，不依赖此绝对路径。
- 本地预览：`http://127.0.0.1:4173/`。
- 原站：`https://zhihui.kaogong.art/`；控制台：`https://zhihui.kaogong.art/dashboard/`。
- **服务器严格只读。另一个 AI 正在升级网关。本次没有上传、部署、重启服务、修改生产配置或业务数据。** 后续上线需要用户另行授权，并与网关工作协调。
- 包内不含登录密码、API Key、真实账号数据或私有工作台截图。不要把历史对话中的凭据写入代码、文档或公开目录。
- 首页不调用业务 API。动画中的任务和性能数字是演示，工作台图片是原首页公开的合成示例素材。

## 用户已经确定的设计

保持原有浅色风格、蓝色 Z 品牌、宽松居中首屏，避免整体深色和大幅改版。首屏不能被压低或缩短；不能为了极简删掉明显的动态效果。

开场顺序：

1. `#top`：原风格首屏与真实控制台构建的预览素材。
2. `#task-scene`：开发任务动画，保留白/灰界面、黑色代码面板、紫色进度及暗色吞吐卡片，不要整体改蓝。
3. `#ride-preview`：鹈鹕骑行（用户也称“鹧鸪”）。整屏、插画底色与天空统一淡蓝 `#edf5ff`；自行车、围巾、河水使用蓝色，保留沙滩和道路层次。
4. `#perspective`：白色《思考者》，必须紧接骑行屏。外部白底，保留白色雕塑和中性展台，不要求所有元素都带蓝色。

随后保留产品说明、四种入口、路由、用量、工作台、模型/用量卡片、接入说明、边界与 FAQ。全页文案围绕 **Codex、WorkBuddy、Qoder、Trae** 四种入口。不要把演示任务窗口当作真实智汇控制台，也不要编造在线性能。

最近的截图反馈指出骑行主体向右裁切，已修正参考站移动端位移与本地定位叠加的问题：前景使用 `left:50%`、`translateX(-50%)`，同时约束宽高，SVG 使用 `xMidYMax meet` 等比容纳并扩大 viewBox 留白。已做尺寸计算，修复后的浏览器截图仍待补充。

## 运行与重建

查看当前版本仅需 Node.js；开发环境使用过 Node.js 24.19.0、Python 3.12.10。

```powershell
node server.mjs
```

打开 `http://127.0.0.1:4173/`。如端口占用，使用 `node server.mjs --port 4175`。服务默认只监听本机 `127.0.0.1`，只提供 `public/` 文件。请通过 HTTP 查看，不要直接双击 HTML。

继续开发：

```powershell
npm ci
python prepare.py
npm run build:viewer
npm run check
npm run build
```

`prepare.py` 不联网，从只读快照、模板和本地参考资源重建页面；`build:viewer` 打包三维查看器；`build` 仅复制 `public/` 到本地 `dist/`，不会发布。

只有修改模型处理流程时才重新生成模型：

```powershell
python -m pip install numpy trimesh Pillow moderngl
npm run build:model
```

交付包包含原始 STL 和未压缩 GLB。`render_model.py` 使用独立 OpenGL 生成四视图与海报，需要可用图形上下文。正常修改页面不需要重新生成模型。

## 文件与修改入口

| 文件或目录 | 用途 |
| --- | --- |
| `prepare.py` | 首页重建、样式/脚本引入、真实预览结构、骑行配色与构图。 |
| `page_content.py` | 四入口文案、导航、旧章节删改与细节卡片插入。 |
| `sections/intro-screens.html` | 第二屏任务和第三屏骑行的结构、文案。 |
| `sections/sculpture.html` | 第四屏雕塑、文案、控制按钮和署名。 |
| `sections/details.html` | 模型与用量的真实预览卡片。 |
| `build_scenes.py` | 隔离任务动画的生成器，保留参考配色。 |
| `public/styles/upgrade.css` | 原风格浅色精修。 |
| `public/styles/motion.css`、`public/scripts/motion.js` | 粒子、轻浮动、信号流和指针倾斜。 |
| `public/styles/chapters.css` | 首屏空间、任务/骑行章节、四入口和响应式规则。 |
| `public/styles/sculpture.css` | 雕塑展区及加载/失败状态。 |
| `public/scripts/upgrade.js` | 平台与代码同步、路由、工作台、菜单、FAQ、复制、全局动效。 |
| `public/scripts/ride.js` | 父页面与两个插画的播放消息桥接。 |
| `public/scripts/sculpture.js` | 雕塑懒加载、按钮、暂停、失败海报。 |
| `src/sculpture-viewer.js` | Three.js 渲染、光照、阴影、镜头和旋转控制。 |
| `build-viewer.mjs` | 打包查看器，复制本地解码器和许可证。 |
| `prepare_model.py`、`compress-model.mjs`、`render_model.py` | 模型标准化、压缩和独立渲染。 |
| `verify.py`、`tests/` | 资产、来源哈希、交互与模型检查。 |
| `source-snapshot/` | 原首页只读快照与校验清单，不应改写。 |
| `reference-motion/`、`reference-models/` | 已采用的公开参考资源、重建资产和来源说明。 |
| `public/preview/` | 16 张公开合成预览图，保留原哈希。 |
| `dist/` | 静态构建输出，不包含登录、网关或后端。 |

不要仅修改 `public/index.html`、`public/styles/source.css` 或 `public/animations/*` 完成长久改动：它们会被 `python prepare.py` 覆盖，应修改相应模板或生成器。`public/scripts/sculpture-viewer.js` 也属于打包输出，应修改 `src/sculpture-viewer.js` 后重新打包。

## 动画与模型约定

任务、骑行都是本地自包含 HTML，通过 `sandbox="allow-scripts"` iframe 隔离，没有 `allow-same-origin`。父页面只发送播放信息，消息端校验 `event.source`。不要在插画中加入真实账号数据或业务请求。

全局/局部暂停、系统减少动态效果、后台和离屏状态共同控制自动播放；用户可明确点击播放来覆盖系统减少动效默认值。修改时保留这些能力。

思考者扫描来自 **Scan the World**，保留 837,482 个三角面、418,714 个顶点，只做坐标旋转、平移、等比缩放、平滑法线和白色材质。旋转的是镜头，不单独移动肢体。压缩模型约 10.26 MiB，接近展区才加载；加载失败保留静态海报。Three.js 和 Draco 均为本地资源。

模型和衍生海报遵循 **CC BY-SA 4.0**，保留 `public/models/ATTRIBUTION.txt` 的署名与许可。Three.js MIT、Draco Apache 2.0 许可证在 `public/vendor/`。用户指定迁入的参考站动画保留来源注释，来源记录见 `reference-motion/NOTES.md`。

## 工作台与接入注意事项

真实智汇控制台采用顶部工具栏和底部 Dock。不要恢复早期虚构侧栏。八个工作台预览是仪表盘、账号、密钥、模型、用量、日志、任务、安全，图片采用合成示例数据。

**Codex 真实网关路径尚未核实，不要擅自编造 `/codex/v1`。** 当前显示“按控制台配置”，示例使用 `YOUR_CODEX_BASE_URL` / `YOUR_CODEX_RESPONSES_URL` 占位符。其他入口与示例沿用原页面约定，也不构成当前后端可用性的证明；真正接入或上线前，以网关负责人和控制台配置为准。

控制台链接都指向原站。本地没有伪造登录、账号服务或网关后端。

## 已验证与待验收

最近检查 **14 组通过**，覆盖四入口/三种示例格式同步、工作台、菜单/FAQ/复制、路由与动画生命周期、骑行车轮/脚踏/围巾/风景变化、任务数字变化、模型加载/暂停/失败回退及压缩模型全部索引核对。

静态检查包括 34 个首页资产引用、81 个唯一 ID、29 个快照哈希、16 张合成预览哈希。最后构建中 `public/` 和 `dist/` 的 50 个文件逐一哈希一致。资源经过本地 HTTP 检查，GLB、WebP、WASM MIME 类型正确。

模型独立四角度和上半身细节渲染曾生成并查看。原始网格封闭、绕序一致、一个连通分量、无退化面；压缩后保留面和索引。上述证据不能证明所有扫描瑕疵、自相交都不存在，也不能把独立 OpenGL 模型图称作网页截图。

**网页视觉验收仍未完成。** 支持的浏览器运行时持续返回 `Local browser use is disabled in Work settings`，因此 `design-qa.md` 为 `final result: blocked`。用户后续截图指出骑行裁切，已修复，但修复后仍未截图复核。构建/DOM 检查不能替代视觉验收。

接手后的优先步骤：

1. 浏览器可用后查看 1280×720、窄桌面与 390×844，检查首屏、任务窗口、骑行完整构图、思考者相邻顺序及横向溢出。
2. 观察实际持续动效，验证全局/局部暂停、后台、离屏和减少动态效果。
3. 验证 WebGL 加载、白色雕塑光影、各角度细节、键盘和手机滚动/旋转。
4. 检查浏览器控制台、网络、导航、复制和工作台选择，修复后截图对比并更新 `design-qa.md`。
5. 仅在用户明确授权发布后，协调网关工作，再准备上线；不要直接覆盖线上目录。

## 交付包说明

`README.md` 是运行开发说明；`开始使用.txt` 是快速启动；本文件是接手指南；`design-qa.md` 记录证据与验收限制；`release.json` 列出打包文件 SHA-256。ZIP 旁的 `.sha256` 文件校验整个压缩包。

交付包排除 `node_modules`、私有工作台参考、真实凭据和早期废弃方案。原始公开来源资产供重建使用，不代表需要把它们发布到网站；网站静态根目录为 `dist/`。

## 登录页新增（2026-10-30）

本地新增 `/login`：白瓷智汇之门、蓝色内衬、悬浮 Z 和四路光线，右侧简洁登录表单。预览不发送登录信息。真实 API 约定、启用边界、源码与检查见 `LOGIN-HANDOFF.md`。当前总计 20 组检查通过，静态构建 56 个文件与 public 哈希一致；浏览器视觉验收仍受限。页面与首页同包交付，之前下载的 ZIP 需重新下载才包含此页。
