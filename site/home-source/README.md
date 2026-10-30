# Current Shipping Scope

WorkBuddy / Qoder / Trae only. Original production login is preserved separately.
The supplied four-entry and 3D login notes below are historical, not the release contract.
Original blue-white illustrations live in scenes/; reference-site artwork is excluded.

# 智汇首页 · 本地浅色升级版

从服务器只读快照升级，保留浅色品牌、蓝色 Z 和宽松居中首屏。所有修改仅在本地，没有上传、部署或修改网关与生产配置。

## 查看与运行

当前本地地址：<http://127.0.0.1:4173/>。

登录页：<http://127.0.0.1:4173/login>。本地设计预览不提交账号密码，新增说明见 `LOGIN-HANDOFF.md`。

```powershell
cd D:\AI\codex\codex1\zhihui-home-upgrade
npm ci
npm run dev
```

运行资源全部本地化，没有 CDN 依赖。查看当前版本可直接运行 `node server.mjs`；依赖安装用于重新打包与检查。服务只监听 `127.0.0.1`，只提供 `public/` 文件。

## 当前页面

- 开场依次为宽松首屏与真实控制台预览、原配色开发任务动画、独立的鹈鹕骑行第三屏。
- 全页文案更新为 Codex、WorkBuddy、Qoder、Trae 四种入口。Codex 真实地址未核实，示例使用占位符，以控制台配置为准。
- 任务动画保留白/灰界面、黑色代码面板与紫色进度。吞吐数字明确标记为演示值。
- 骑行屏与插画天空统一淡蓝色，保留道路和沙滩层次，自行车、围巾和河水使用蓝色。车轮、脚踏、围巾、海岸保留原动画。
- 骑行屏正下方加入白色《思考者》三维展区，支持拖动旋转、全貌/细节、暂停、重置和键盘操作。83.7 万三角面未删减；模型约 10.26 MiB，在接近展区时才加载。失败时显示本地静态渲染。
- 模型目录与用量卡片使用真实控制台构建的合成示例截图。八页工作台采用实际顶部栏和底部 Dock 结构。
- 任务、骑行、雕塑与全局动效支持暂停；后台、离屏和系统减少动效会停止自动动画，用户可明确开启。

## 主要文件

- `source-snapshot/site/`：服务器只读快照，29 文件原哈希校验。
- `prepare.py`、`page_content.py`、`sections/`：页面重建、四入口文案与章节源模板。
- `public/styles/`：原版基础样式、浅色精修、动效、章节与雕塑展区样式。
- `public/scripts/upgrade.js`：平台、路由、接入示例、工作台、菜单、FAQ 与动效总控，不调用业务 API。
- `public/animations/task-workspace.*`、`pelican-ride.html`：隔离的本地展示动画。`public/scripts/ride.js` 管理父子页面播放消息。
- `src/sculpture-viewer.js`、`build-viewer.mjs`：Three.js 查看器与打包入口。
- `public/models/`：压缩模型、静态海报、来源清单与 CC BY-SA 4.0 署名。
- `prepare_model.py`、`compress-model.mjs`、`render_model.py`：模型标准化、保留索引的压缩与独立 OpenGL 渲染。
- `evidence/sculpture/`：四角度模型渲染与细节图，是模型证据，不能充当网页截图。
- `public/preview/`：16 张原首页公开的合成示例截图，按原哈希校验。
- `reference-models/`、`reference-motion/`：模型/动画来源与本地中间文件，不进入网站打包。
- `workbench-reference-20261029/`：私有只读参考，不进入网站打包。
- `design-qa.md`：验收状态。

## 检查与打包

```powershell
python prepare.py
npm run build:viewer
npm run check
npm run build
```

`check` 包含脚本语法、资产/锚点/ID、快照与预览哈希、20 组交互和模型检查。压缩模型会重新解码，核对全部面/顶点/索引与位置误差。DOM 测试不渲染网页，不能证明手机排版、真实浏览器剪贴板权限或动画观感。

只有重新生成模型时才需运行 `npm run build:model`，依赖 Python numpy、trimesh、Pillow、moderngl。原始扫描在 `reference-models/`；来源与许可见该目录 README 和 `public/models/ATTRIBUTION.txt`。

`build` 仅复制 `public/` 到本地 `dist/`。控制台链接指向原站，没有本地伪造登录。

## 验收限制

浏览器工具仍返回 `Local browser use is disabled in Work settings`。当前没有网页渲染截图，桌面和手机视觉验收未完成。独立模型四视图已检查，但封闭网格与模型预览不能证明绝对没有自相交或扫描瑕疵。浏览器可用后需检查 1280×720、窄桌面与 390×844 的排版、WebGL、触摸操作及动画。
